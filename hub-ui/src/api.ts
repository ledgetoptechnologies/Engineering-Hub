import { createDemoOverview, createDemoTopic, DEMO_SESSION } from "./mocks";
import { canWithdrawDecision, canWithdrawTask, splitWithdrawn } from "./withdrawn";
import { parseAgentObservations, type AgentObservation } from "./agentObservations";
import type {
  Agent,
  Decision,
  EventsStatus,
  Overview,
  ImageVersionsSummary,
  ProjectId,
  Session,
  Task,
  TaskPriority,
  TopicPreflight,
  TopicSnapshot,
} from "./types";

export type HubMode = "live" | "demo";

/**
 * The private gateway answers routes it does not know (a feature this server
 * doesn't have yet) with 403 gateway_route_forbidden or 400 gateway_invalid_*.
 * That is "not available", never a lost sign-in, so callers must not clear the
 * workspace for it.
 */
export async function isMissingRoute(response: Response): Promise<boolean> {
  if (response.status === 404) return true;
  if (![400, 403].includes(response.status)) return false;
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return false;
  try {
    const body: unknown = await response.clone().json();
    const code = (body as { detail?: { code?: unknown } })?.detail?.code;
    return typeof code === "string" && (code === "gateway_route_forbidden" || code.startsWith("gateway_invalid_"));
  } catch {
    return false;
  }
}
export class HubApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
  ) {
    super(code);
    this.name = "HubApiError";
  }
}
// This alphabet is the backend's topic contract. Every character is safe in
// this path segment, including ':'. Encoding it would violate strict ingress.
export function topicPath(topic: string): string {
  if (/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.exec(topic)?.[0] !== topic)
    throw new HubApiError(422, "invalid_topic");
  return `/topics/${topic}`;
}
const knownCodes = new Set([
  "revision_conflict",
  "forbidden",
  "not_found",
  "invalid_input",
  "invalid_project",
  "invalid_answer",
  "idempotency_conflict",
  "human_session_required",
  "executor_not_enabled",
  "native_events_unavailable",
  "paused",
  "already_claimed",
  "invalid_topic",
  "invalid_recipient",
  "topic_read_forbidden",
  "ingress_forbidden",
]);
const fallbackCode = (status: number) =>
  status === 401
    ? "sign_in_required"
    : status === 403
      ? "forbidden"
      : status === 409
        ? "revision_conflict"
        : status === 422
          ? "invalid_input"
          : status === 423
            ? "executor_not_enabled"
            : status === 503
              ? "service_unavailable"
              : "request_failed";

export class HubClient {
  readonly mode: HubMode;
  private readonly fetchImpl: typeof fetch;
  private demo: Overview;
  private demoSession: Session;
  private demoCounter = 0;
  private demoRequests = new Map<
    string,
    { payload: string; task: Task }
  >();
  private demoPriorityRequests = new Map<string, { payload: string; result: Task }>();

  constructor({
    mode,
    fetchImpl = globalThis.fetch,
  }: {
    mode: HubMode;
    fetchImpl?: typeof fetch;
  }) {
    this.mode = mode;
    // Native browser fetch must not receive this HubClient as its receiver.
    this.fetchImpl = fetchImpl.bind(globalThis);
    this.demo = createDemoOverview();
    this.demoSession = { ...DEMO_SESSION };
  }

  private async request<T>(
    path: string,
    body?: unknown,
    csrf?: string,
    signal?: AbortSignal,
  ): Promise<T> {
    // Bootstrap has no local CSRF secret yet. Its dedicated server boundary
    // requires the browser's exact Origin and a verified Access assertion.
    const bootstrap = path === "/auth/bootstrap";
    if (body !== undefined && !csrf && !bootstrap)
      throw new HubApiError(403, "csrf_required");
    let response: Response;
    try {
      response = await this.fetchImpl(`/api/hub/v1${path}`, {
        method: body === undefined ? "GET" : "POST",
        credentials: "same-origin",
        redirect: "error",
        ...(signal ? { signal } : {}),
        headers: {
          Accept: "application/json",
          ...(body === undefined
            ? {}
            : { "Content-Type": "application/json", ...(csrf ? { "X-Hub-CSRF": csrf } : {}) }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch {
      throw new HubApiError(0, "network_unavailable");
    }
    if (!response.ok) {
      // This optional read may be absent behind an older strict gateway. Keep
      // its route refusal distinct from an authorization/session refusal.
      if (path === "/agents" && await isMissingRoute(response))
        throw new HubApiError(response.status, "agent_observations_unavailable");
      let code = fallbackCode(response.status);
      if (response.headers.get("content-type")?.includes("application/json")) {
        try {
          const parsed: unknown = await response.json();
          if (parsed && typeof parsed === "object" && "detail" in parsed) {
            const detail = parsed.detail;
            if (
              detail &&
              typeof detail === "object" &&
              "code" in detail &&
              typeof detail.code === "string" &&
              knownCodes.has(detail.code)
            )
              code = detail.code;
          }
        } catch {
          /* Never reflect proxy HTML or raw server exceptions. */
        }
      }
      throw new HubApiError(response.status, code);
    }
    try {
      return (await response.json()) as T;
    } catch {
      throw new HubApiError(response.status, "invalid_response");
    }
  }

  private demoHuman(csrf?: string) {
    if (!this.demoSession.authenticated)
      throw new HubApiError(401, "sign_in_required");
    if (csrf !== undefined && csrf !== this.demoSession.csrf_token)
      throw new HubApiError(403, "forbidden");
  }

  async getSession(): Promise<Session> {
    if (this.mode === "demo") return { ...this.demoSession };
    const session = await this.request<Session>("/auth/me");
    if (!session.authenticated && session.auth_provider === "cloudflare_access")
      return this.request("/auth/bootstrap", {});
    return session;
  }
  async getOverview(): Promise<Overview> {
    if (this.mode === "live") return splitWithdrawn(await this.request<Overview>("/overview?include_task_priorities=true"));
    this.demoHuman();
    return splitWithdrawn(structuredClone(this.demo));
  }
  async getImageVersions(): Promise<ImageVersionsSummary | null> {
    if (this.mode === "demo") return null;
    return this.request("/image-versions", undefined, undefined, AbortSignal.timeout(10_000));
  }
  async getAgents(signal?: AbortSignal): Promise<AgentObservation[]> {
    if (this.mode === "demo") return structuredClone(this.demo.agents).map(({ id, connection }) => ({ id, connection }));
    const bounded = AbortSignal.timeout(10_000);
    const body = await this.request<unknown>("/agents", undefined, undefined,
      signal ? AbortSignal.any([signal, bounded]) : bounded);
    try { return parseAgentObservations(body); }
    catch { throw new HubApiError(200, "invalid_response"); }
  }
  async getTopic(topic: string): Promise<TopicSnapshot> {
    const path = topicPath(topic);
    if (this.mode === "live")
      return this.request(`${path}/peers`);
    this.demoHuman();
    return createDemoTopic(topic);
  }
  async getEventsStatus(): Promise<EventsStatus> {
    if (this.mode === "live") return this.request("/events/status");
    this.demoHuman();
    return {
      available: false,
      reason: "demo_no_live_events",
      observer_epoch: "demo-only",
      cursor: 0,
    };
  }
  async answerDecision(
    id: string,
    revision: number,
    answer: string,
    csrf: string,
  ): Promise<Decision> {
    if (this.mode === "live")
      return this.request(
        `/decisions/${encodeURIComponent(id)}/answer`,
        { expected_revision: revision, answer },
        csrf,
      );
    this.demoHuman(csrf);
    const decision = this.demo.decisions.find((item) => item.id === id);
    if (!decision) throw new HubApiError(404, "not_found");
    if (decision.revision !== revision)
      throw new HubApiError(409, "revision_conflict");
    const typed = decision.allow_text === true && !decision.protected
      && answer.trim() !== "" && answer.length <= 4096;
    if (decision.status !== "open" || !(decision.options.includes(answer) || typed))
      throw new HubApiError(409, "invalid_answer");
    Object.assign(decision, {
      answer,
      status: "answered",
      answered_by: this.demoSession.actor,
      revision: decision.revision + 1,
      updated_at: new Date().toISOString(),
    });
    return structuredClone(decision);
  }
  /** Beau clears a task's decision/action item; the server restores the task's earlier state. */
  async resolveWaiting(id: string, revision: number, note: string, csrf: string): Promise<Task> {
    const body = { expected_revision: revision, ...(note.trim() ? { note: note.trim() } : {}) };
    if (this.mode === "live") return this.request(`/tasks/${encodeURIComponent(id)}/resolve-waiting`, body, csrf);
    this.demoHuman(csrf);
    const task = this.demo.tasks.find((item) => item.id === id);
    if (!task) throw new HubApiError(404, "not_found");
    if (task.revision !== revision) throw new HubApiError(409, "revision_conflict");
    if (task.status !== "waiting_on_beau" || !task.waiting_on) throw new HubApiError(409, "not_waiting");
    const { previous_status, type, text } = task.waiting_on;
    const at = new Date().toISOString();
    Object.assign(task, { status: previous_status, waiting_on: undefined, revision: task.revision + 1, updated_at: at,
      waiting_resolved: { type, text, by: this.demoSession.actor, at, ...(body.note ? { note: body.note } : {}) } });
    delete task.waiting_on;
    return structuredClone(task);
  }
  /** Beau undoes an agent's withdrawal of its waiting question; the task waits on him again. */
  async restoreWaiting(id: string, revision: number, csrf: string): Promise<Task> {
    if (this.mode === "live")
      return this.request(`/tasks/${encodeURIComponent(id)}/restore-waiting`, { expected_revision: revision }, csrf);
    this.demoHuman(csrf);
    const task = this.demo.tasks.find((item) => item.id === id);
    if (!task) throw new HubApiError(404, "not_found");
    if (task.revision !== revision) throw new HubApiError(409, "revision_conflict");
    const record = task.waiting_withdrawn;
    if (!record) throw new HubApiError(409, "not_withdrawn");
    if (task.status === "withdrawn" || task.status === "done") throw new HubApiError(409, "task_closed");
    if (task.status === "waiting_on_beau") throw new HubApiError(409, "waiting_on_beau");
    const previous = task.status as typeof record.previous_status;
    const at = new Date().toISOString();
    Object.assign(task, { status: "waiting_on_beau", revision: task.revision + 1, updated_at: at,
      waiting_on: { type: record.type, text: record.text, previous_status: previous, by: record.raised_by,
        at: record.raised_at, restored_by: this.demoSession.actor, restored_at: at } });
    delete task.waiting_withdrawn;
    return structuredClone(task);
  }
  async updateTaskPriority(id: string, revision: number, priorityRevision: number, priority: TaskPriority, key: string, csrf: string): Promise<Task> {
    const body = { expected_revision: revision, expected_priority_revision: priorityRevision, priority, client_request_id: key };
    if (this.mode === "live") return this.request(`/tasks/${encodeURIComponent(id)}/priority`, body, csrf);
    this.demoHuman(csrf);
    const payload = JSON.stringify([id, body]);
    const prior = this.demoPriorityRequests.get(key);
    if (prior) {
      if (prior.payload !== payload) throw new HubApiError(409, "idempotency_conflict");
      return structuredClone(prior.result);
    }
    const task = this.demo.tasks.find((item) => item.id === id);
    if (!task) throw new HubApiError(404, "not_found");
    if (task.revision !== revision || (task.priority_revision ?? 0) !== priorityRevision)
      throw new HubApiError(409, "revision_conflict");
    if (task.status === "done" || task.status === "withdrawn") throw new HubApiError(409, "task_closed");
    Object.assign(task, { priority, priority_revision: priorityRevision + 1 });
    const result = structuredClone(task);
    this.demoPriorityRequests.set(key, { payload, result });
    return structuredClone(result);
  }
  /** Retire a superseded task or question; the server keeps it, with the reason and replacement. */
  async withdraw(
    kind: "task" | "decision",
    id: string,
    revision: number,
    reason: string,
    supersededBy: string | null,
    csrf: string,
  ): Promise<Task | Decision> {
    const body = { expected_revision: revision, reason, ...(supersededBy ? { superseded_by: supersededBy } : {}) };
    if (this.mode === "live")
      return this.request(`/${kind === "task" ? "tasks" : "decisions"}/${encodeURIComponent(id)}/withdraw`, body, csrf);
    this.demoHuman(csrf);
    const item = kind === "task" ? this.demo.tasks.find((task) => task.id === id)
      : this.demo.decisions.find((decision) => decision.id === id);
    if (!item) throw new HubApiError(404, "not_found");
    if (item.revision !== revision) throw new HubApiError(409, "revision_conflict");
    if (kind === "task" ? !canWithdrawTask(item as Task) : !canWithdrawDecision(item as Decision))
      throw new HubApiError(409, "withdraw_not_allowed");
    Object.assign(item, {
      status: "withdrawn", withdrawn_reason: reason.trim(), superseded_by: supersededBy,
      withdrawn_by: this.demoSession.actor, withdrawn_at: new Date().toISOString(),
      revision: item.revision + 1, updated_at: new Date().toISOString(),
    });
    return structuredClone(item);
  }
  async createTask(
    project: ProjectId,
    title: string,
    key: string,
    csrf: string,
    extra: { acceptance?: string[]; reviewer?: Agent | null; priority?: TaskPriority } = {},
  ): Promise<Task> {
    const backlog = {
      ...(extra.acceptance?.length ? { acceptance: extra.acceptance } : {}),
      ...(extra.reviewer ? { assigned_reviewer: extra.reviewer } : {}),
      ...(extra.priority && extra.priority !== "normal" ? { priority: extra.priority } : {}),
    };
    if (this.mode === "live")
      return this.request(
        "/tasks",
        { project, title, client_request_id: key, ...backlog },
        csrf,
      );
    this.demoHuman(csrf);
    const payload = JSON.stringify([project, title, backlog]);
    const previous = this.demoRequests.get(key);
    if (previous) {
      if (previous.payload !== payload)
        throw new HubApiError(409, "idempotency_conflict");
      return structuredClone(previous.task);
    }
    if (
      !this.demo.projects.some((item) => item.id === project) ||
      !title.trim() ||
      !key.trim()
    )
      throw new HubApiError(422, "invalid_input");
    const task: Task = {
      id: `demo-created-${++this.demoCounter}`,
      project,
      title: title.trim(),
      revision: 1,
      updated_at: new Date().toISOString(),
      status: "assigned",
      owner: null,
      candidate_commit: null,
      review: null,
      repair_count: 0,
      ...(backlog.acceptance ? { acceptance: backlog.acceptance } : {}),
      ...(backlog.assigned_reviewer ? { reviewer: backlog.assigned_reviewer } : {}),
      ...(backlog.priority ? { priority: backlog.priority } : {}),
    };
    this.demo.tasks.push(task);
    this.demoRequests.set(key, { payload, task: structuredClone(task) });
    return structuredClone(task);
  }
  async setPause(
    paused: boolean,
    csrf: string,
  ): Promise<{
    paused: boolean;
    revision: number;
    id: string;
    updated_at: string;
  }> {
    if (this.mode === "live") return this.request("/pause", { paused }, csrf);
    this.demoHuman(csrf);
    this.demo.paused = paused;
    return {
      paused,
      revision: ++this.demoCounter,
      id: "pause",
      updated_at: new Date().toISOString(),
    };
  }
  async logout(csrf: string): Promise<{ authenticated: boolean }> {
    if (this.mode === "live") return this.request("/auth/logout", {}, csrf);
    this.demoHuman(csrf);
    this.demoSession = {
      authenticated: false,
      actor: null,
      csrf_token: null,
      github_id: null,
    };
    return { authenticated: false };
  }
  async preflight(topic: string, recipient: Agent): Promise<TopicPreflight> {
    const path = topicPath(topic);
    if (this.mode === "live")
      return this.request(
        `${path}/preflight?recipient=${encodeURIComponent(recipient)}`,
      );
    this.demoHuman();
    const peer = createDemoTopic(topic).items.find(
      (item) => item.agent === recipient,
    );
    if (!peer) throw new HubApiError(422, "invalid_recipient");
    return {
      topic,
      recipient,
      peer,
      advisory: true,
      delivery_guaranteed: false,
    };
  }
}
