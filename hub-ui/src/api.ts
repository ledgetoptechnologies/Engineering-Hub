import { createDemoOverview, createDemoTopic, DEMO_SESSION } from "./mocks";
import type {
  Agent,
  Decision,
  EventsStatus,
  Overview,
  ProjectId,
  Session,
  Task,
  TopicPreflight,
  TopicSnapshot,
} from "./types";

export type HubMode = "live" | "demo";
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
    { project: ProjectId; title: string; task: Task }
  >();

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
  ): Promise<T> {
    if (body !== undefined && !csrf)
      throw new HubApiError(403, "csrf_required");
    let response: Response;
    try {
      response = await this.fetchImpl(`/api/hub/v1${path}`, {
        method: body === undefined ? "GET" : "POST",
        credentials: "same-origin",
        redirect: "error",
        headers: {
          Accept: "application/json",
          ...(body === undefined
            ? {}
            : { "Content-Type": "application/json", "X-Hub-CSRF": csrf! }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch {
      throw new HubApiError(0, "network_unavailable");
    }
    if (!response.ok) {
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
    return this.mode === "demo"
      ? { ...this.demoSession }
      : this.request("/auth/me");
  }
  async getOverview(): Promise<Overview> {
    if (this.mode === "live") return this.request("/overview");
    this.demoHuman();
    return structuredClone(this.demo);
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
    if (decision.status !== "open" || !decision.options.includes(answer))
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
  async createTask(
    project: ProjectId,
    title: string,
    key: string,
    csrf: string,
  ): Promise<Task> {
    if (this.mode === "live")
      return this.request(
        "/tasks",
        { project, title, client_request_id: key },
        csrf,
      );
    this.demoHuman(csrf);
    const previous = this.demoRequests.get(key);
    if (previous) {
      if (previous.project !== project || previous.title !== title)
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
    };
    this.demo.tasks.push(task);
    this.demoRequests.set(key, { project, title, task });
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
