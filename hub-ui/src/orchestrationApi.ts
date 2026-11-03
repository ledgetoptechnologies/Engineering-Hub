/**
 * Orchestration view: one request from Beau, split by an orchestrator into one dispatch per role.
 * Read-only. Not shown in the live dashboard yet: the backend is owned by Codex and not built, so
 * the page only appears in demo mode (?demo=1), fed by fixtures/orchestration-wire-v1.json.
 *
 * Everything agent-written here (titles, status lines, checklist items, questions) is plain text.
 * It is rendered as React text only, never as HTML or markdown.
 */
import { HubApiError, isMissingRoute, type HubMode } from "./api";

/** The gate: shown only in demo mode until the backend exists and is reviewed. */
export function orchestrationEnabled(mode: HubMode): boolean {
  return mode === "demo";
}

export const ORCHESTRATION_SCHEMA = "hub-orchestration.v1";
export const DISPATCH_STATES = ["dispatched", "working", "blocked", "asked", "done", "failed", "stale"] as const;
export type DispatchState = typeof DISPATCH_STATES[number];
/** States where work has ended, so no ETA is shown. */
export const FINISHED_STATES: readonly DispatchState[] = ["done", "failed"];

export interface RequestSummary {
  request_id: string; title: string; asked_at: string; updated_at: string;
  dispatch_count: number; done_count: number; waiting_decision_count: number; stale_count: number;
}
export interface RequestList { items: RequestSummary[]; truncated: boolean }
export interface ChecklistItem { text: string; done: boolean }
export interface ProgressTask {
  task_id: string; title: string; project: string; created_by: string; origin_request_id: string | null;
  checklist: ChecklistItem[];
  /** Server-computed from the observed checklist rate; null when there is not enough history. */
  eta_seconds: number | null;
  /** The agent's own estimate. Shown only labelled "agent guess". */
  agent_eta_guess_seconds: number | null;
  status_line: string; blocked_on: string | null; updated_at: string; stale: boolean;
}
export interface Dispatch {
  request_id: string; dispatch_id: string; task_id: string; role: string; state: DispatchState;
  dispatched_at: string; updated_at: string; task: ProgressTask;
}
export interface PendingDecision { decision_id: string; question: string; asked_by_role: string; asked_at: string }
export interface RoleLease { role: string; lease_holder: string | null; lease_expires_at: string | null }
export interface OrchestrationProject { id: string; name: string; created_by: string; origin_request_id: string | null }
export interface RequestDetail {
  request: { request_id: string; title: string; asked_at: string; updated_at: string };
  /** Only decisions still waiting on Beau; pinned at the top of the request. */
  decisions: PendingDecision[];
  dispatches: Dispatch[];
  roles: RoleLease[];
  projects: OrchestrationProject[];
}

export const MAX_REQUESTS = 100;
export const MAX_DISPATCHES = 50;
export const MAX_CHECKLIST = 50;
/** Ten years: anything larger is not an estimate. */
const MAX_ETA_SECONDS = 10 * 365 * 24 * 3600;

const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const ROLE = /^[a-z][a-z0-9-]{0,31}$/;
const PROJECT = /^[a-z0-9][a-z0-9-]{0,63}$/;

function invalid(): never { throw new HubApiError(502, "invalid_orchestration_response"); }
/** An object with exactly these keys; unknown or missing fields are a contract change and are refused. */
function exact(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return invalid();
  const row = value as Record<string, unknown>;
  if (Object.keys(row).sort().join() !== [...keys].sort().join()) return invalid();
  return row;
}
function text(value: unknown, max: number, allowEmpty = false): string {
  if (typeof value !== "string" || value.length > max || (!allowEmpty && !value.trim())
      || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value)) return invalid();
  return value;
}
function id(value: unknown, pattern = ID): string {
  return typeof value === "string" && pattern.exec(value)?.[0] === value ? value : invalid();
}
function time(value: unknown): string {
  const result = text(value, 64);
  return Number.isNaN(Date.parse(result)) ? invalid() : result;
}
function count(value: unknown): number {
  return Number.isSafeInteger(value) && (value as number) >= 0 ? value as number : invalid();
}
function seconds(value: unknown): number | null {
  if (value === null) return null;
  return Number.isSafeInteger(value) && (value as number) >= 0 && (value as number) <= MAX_ETA_SECONDS ? value as number : invalid();
}
function nullable<T>(value: unknown, parse: (value: unknown) => T): T | null {
  return value === null ? null : parse(value);
}
function list(value: unknown, max: number): unknown[] {
  return Array.isArray(value) && value.length <= max ? value : invalid();
}
function unique(values: string[]) {
  if (new Set(values).size !== values.length) invalid();
}

function parseSummary(value: unknown): RequestSummary {
  const row = exact(value, ["request_id", "title", "asked_at", "updated_at", "dispatch_count", "done_count",
    "waiting_decision_count", "stale_count"]);
  const result = { request_id: id(row.request_id), title: text(row.title, 200), asked_at: time(row.asked_at),
    updated_at: time(row.updated_at), dispatch_count: count(row.dispatch_count), done_count: count(row.done_count),
    waiting_decision_count: count(row.waiting_decision_count), stale_count: count(row.stale_count) };
  if (result.done_count > result.dispatch_count || result.stale_count > result.dispatch_count) return invalid();
  return result;
}

export function parseRequestList(value: unknown): RequestList {
  const row = exact(value, ["schema", "items", "truncated"]);
  if (row.schema !== ORCHESTRATION_SCHEMA || typeof row.truncated !== "boolean") return invalid();
  const items = list(row.items, MAX_REQUESTS).map(parseSummary);
  unique(items.map(item => item.request_id));
  return { items, truncated: row.truncated };
}

function parseTask(value: unknown): ProgressTask {
  const row = exact(value, ["task_id", "title", "project", "created_by", "origin_request_id", "checklist", "eta_seconds",
    "agent_eta_guess_seconds", "status_line", "blocked_on", "updated_at", "stale"]);
  if (typeof row.stale !== "boolean") return invalid();
  const checklist = list(row.checklist, MAX_CHECKLIST).map(item => {
    const entry = exact(item, ["text", "done"]);
    if (typeof entry.done !== "boolean") return invalid();
    return { text: text(entry.text, 300), done: entry.done };
  });
  return { task_id: id(row.task_id), title: text(row.title, 200), project: id(row.project, PROJECT),
    created_by: text(row.created_by, 128), origin_request_id: nullable(row.origin_request_id, v => id(v)), checklist,
    eta_seconds: seconds(row.eta_seconds), agent_eta_guess_seconds: seconds(row.agent_eta_guess_seconds),
    status_line: text(row.status_line, 500, true), blocked_on: nullable(row.blocked_on, v => text(v, 300)),
    updated_at: time(row.updated_at), stale: row.stale };
}

function parseDispatch(value: unknown, requestId: string): Dispatch {
  const row = exact(value, ["request_id", "dispatch_id", "task_id", "role", "state", "dispatched_at", "updated_at", "task"]);
  if (row.request_id !== requestId || !DISPATCH_STATES.includes(row.state as DispatchState)) return invalid();
  const task = parseTask(row.task);
  if (row.task_id !== task.task_id) return invalid();
  return { request_id: requestId, dispatch_id: id(row.dispatch_id), task_id: task.task_id, role: id(row.role, ROLE),
    state: row.state as DispatchState, dispatched_at: time(row.dispatched_at), updated_at: time(row.updated_at), task };
}

/** `expectedId` is the id that was asked for; a different request in the answer is refused. */
export function parseRequestDetail(value: unknown, expectedId: string): RequestDetail {
  const row = exact(value, ["schema", "request", "decisions", "dispatches", "roles", "projects"]);
  if (row.schema !== ORCHESTRATION_SCHEMA) return invalid();
  const head = exact(row.request, ["request_id", "title", "asked_at", "updated_at"]);
  if (head.request_id !== expectedId) return invalid();
  const request = { request_id: id(head.request_id), title: text(head.title, 200), asked_at: time(head.asked_at),
    updated_at: time(head.updated_at) };
  const decisions = list(row.decisions, MAX_DISPATCHES).map(item => {
    const entry = exact(item, ["decision_id", "question", "asked_by_role", "asked_at"]);
    return { decision_id: id(entry.decision_id), question: text(entry.question, 500),
      asked_by_role: id(entry.asked_by_role, ROLE), asked_at: time(entry.asked_at) };
  });
  const dispatches = list(row.dispatches, MAX_DISPATCHES).map(item => parseDispatch(item, request.request_id));
  const roles = list(row.roles, MAX_DISPATCHES).map(item => {
    const entry = exact(item, ["role", "lease_holder", "lease_expires_at"]);
    const lease = { role: id(entry.role, ROLE), lease_holder: nullable(entry.lease_holder, v => id(v)),
      lease_expires_at: nullable(entry.lease_expires_at, time) };
    // A holder without an expiry (or the reverse) is not a lease the page can describe honestly.
    if ((lease.lease_holder === null) !== (lease.lease_expires_at === null)) return invalid();
    return lease;
  });
  const projects = list(row.projects, MAX_DISPATCHES).map(item => {
    const entry = exact(item, ["id", "name", "created_by", "origin_request_id"]);
    return { id: id(entry.id, PROJECT), name: text(entry.name, 120), created_by: text(entry.created_by, 128),
      origin_request_id: nullable(entry.origin_request_id, v => id(v)) };
  });
  unique(decisions.map(item => item.decision_id));
  unique(dispatches.map(item => item.dispatch_id));
  unique(roles.map(item => item.role));
  unique(projects.map(item => item.id));
  const roleNames = new Set(roles.map(item => item.role));
  const projectIds = new Set(projects.map(item => item.id));
  // Every dispatch's role and project must be described, so "unstaffed" is never a guess.
  if (dispatches.some(item => !roleNames.has(item.role) || !projectIds.has(item.task.project))
      || decisions.some(item => !roleNames.has(item.asked_by_role))) return invalid();
  return { request, decisions, dispatches, roles, projects };
}

/** Whole percent of checklist items done; null for an empty checklist (progress unknown, not zero). */
export function checklistPercent(checklist: readonly ChecklistItem[]): number | null {
  if (!checklist.length) return null;
  return Math.floor((checklist.filter(item => item.done).length / checklist.length) * 100);
}

/** Human ETA wording: "under a minute", "about 12 min", "about 2 h 5 min", "about 3 days". */
export function formatEta(value: number | null): string {
  if (value === null) return "unknown";
  if (value < 60) return "under a minute";
  const minutes = Math.round(value / 60);
  if (minutes < 60) return `about ${minutes} min`;
  if (value < 48 * 3600) {
    const hours = Math.floor(minutes / 60), rest = minutes % 60;
    return rest ? `about ${hours} h ${rest} min` : `about ${hours} h`;
  }
  return `about ${Math.round(value / 86400)} days`;
}

/** Human actors are recorded as "human:..."; anything else (an agent or the orchestrator) is agent-created. */
export function isAgentCreated(createdBy: string): boolean {
  return !createdBy.startsWith("human:");
}

/** True when the dispatch is stale by state or its task's progress is flagged stale. */
export function isStale(dispatch: Dispatch): boolean {
  return dispatch.state === "stale" || dispatch.task.stale;
}

/*
 * PROVISIONAL endpoints. Codex owns the backend and has not built or reviewed these paths, so the
 * shape and prefix may change (other Hub routes live under /api/hub/v1). Nothing calls these while
 * orchestrationEnabled() is false for live mode.
 */
export const PROVISIONAL_LIST_PATH = "/api/orchestration/requests";
export const provisionalDetailPath = (requestId: string) => `/api/orchestration/requests/${id(requestId)}`;

async function readJson(path: string, signal: AbortSignal, fetchImpl: typeof fetch): Promise<unknown> {
  const response = await fetchImpl.call(globalThis, path, { method: "GET", credentials: "same-origin", redirect: "error",
    cache: "no-store", headers: { Accept: "application/json" }, signal });
  if (!response.ok) {
    if (await isMissingRoute(response)) throw new HubApiError(404, "orchestration_unsupported");
    throw new HubApiError(response.status, response.status === 401 ? "human_session_required"
      : response.status === 403 ? "orchestration_forbidden" : "orchestration_request_failed");
  }
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return invalid();
  try { return await response.json(); } catch { return invalid(); }
}

export async function listOrchestrationRequests(signal: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch): Promise<RequestList> {
  return parseRequestList(await readJson(PROVISIONAL_LIST_PATH, signal, fetchImpl));
}

export async function readOrchestrationRequest(requestId: string, signal: AbortSignal,
  fetchImpl: typeof fetch = globalThis.fetch): Promise<RequestDetail> {
  if (ID.exec(requestId)?.[0] !== requestId) throw new HubApiError(422, "invalid_input");
  return parseRequestDetail(await readJson(provisionalDetailPath(requestId), signal, fetchImpl), requestId);
}

export interface OrchestrationReader {
  list: (signal: AbortSignal) => Promise<RequestList>;
  read: (requestId: string, signal: AbortSignal) => Promise<RequestDetail>;
}
export const liveOrchestrationReader: OrchestrationReader = {
  list: signal => listOrchestrationRequests(signal),
  read: (requestId, signal) => readOrchestrationRequest(requestId, signal),
};
