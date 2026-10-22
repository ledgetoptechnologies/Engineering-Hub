/** Planning records are shared context, never task approval or execution authority. */
import { HubApiError } from "./api";
import { isNoteProject } from "./notesApi";

export const PLANNING_KINDS = ["plan", "feature", "idea", "improvement"] as const;
export const PLANNING_STATUSES = ["proposed", "planned", "in_progress", "done", "declined"] as const;
export type PlanningKind = typeof PLANNING_KINDS[number];
export type PlanningStatus = typeof PLANNING_STATUSES[number];
export type PlanningScope = "topic" | "hub_improvements";
export interface PlanningSelection { scope: PlanningScope; project: string; topic: string | null }
export interface PlanningKey extends PlanningSelection { kind: PlanningKind; item_id: string }
export interface PlanningMeta extends PlanningKey {
  title: string; status: PlanningStatus; path: string; version: number; sha256: string;
  created_by: string; updated_by: string; created_at: string; updated_at: string;
}
export interface PlanningItem extends PlanningMeta { content: string }
export interface PlanningList { items: PlanningMeta[]; truncated: boolean }
export interface PlanningDraft extends PlanningKey {
  title: string; content: string; status: PlanningStatus; expected_version: number;
}
export const PLANNING_LIST_LIMIT = 50;
export const MAX_PLANNING_REQUEST_BYTES = 65536;
export const MAX_PLANNING_CONTENT_BYTES = 48 * 1024;
export const isPlanningTopic = (value: string) => /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.exec(value)?.[0] === value;
export const isPlanningId = (value: string) => /^[a-z0-9][a-z0-9_-]{0,63}$/.exec(value)?.[0] === value;
export function validPlanningSelection(value: PlanningSelection): boolean {
  return isNoteProject(value.project) && (value.scope === "topic" ? typeof value.topic === "string" && isPlanningTopic(value.topic)
    : value.scope === "hub_improvements" && value.project === "engineering-hub" && value.topic === null);
}
export function planningPath(value: PlanningKey): string {
  return value.scope === "topic" ? `planning/topics/${value.topic}/${value.kind}/${value.item_id}.json`
    : `planning/hub-improvements/${value.kind}/${value.item_id}.json`;
}
function body(value: PlanningDraft): string {
  return JSON.stringify({ scope: value.scope, project: value.project, topic: value.topic, kind: value.kind, item_id: value.item_id,
    title: value.title, content: value.content, status: value.status, expected_version: value.expected_version });
}
export function planningRequestBytes(value: PlanningDraft): number { return new TextEncoder().encode(body(value)).length; }
export class PlanningConflictError extends HubApiError {
  constructor(public readonly currentVersion: number) { super(409, "planning_version_conflict"); this.name = "PlanningConflictError"; }
}
function invalid(): never { throw new HubApiError(502, "invalid_planning_response"); }
function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : invalid();
}
function text(value: unknown, max: number): string {
  return typeof value === "string" && value.length <= max && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value) ? value : invalid();
}
function parseMeta(value: unknown, selection: PlanningSelection): PlanningMeta {
  if (!validPlanningSelection(selection)) return invalid();
  const row = object(value);
  if (row.scope !== selection.scope || row.project !== selection.project || row.topic !== selection.topic
      || !PLANNING_KINDS.includes(row.kind as PlanningKind) || typeof row.item_id !== "string" || !isPlanningId(row.item_id)
      || !PLANNING_STATUSES.includes(row.status as PlanningStatus) || !Number.isSafeInteger(row.version) || (row.version as number) < 1
      || typeof row.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(row.sha256)) return invalid();
  const title = text(row.title, 200);
  if (!title.trim()) return invalid();
  const key = { ...selection, kind: row.kind as PlanningKind, item_id: row.item_id };
  if (row.path !== planningPath(key)) return invalid();
  const created = text(row.created_at, 64), updated = text(row.updated_at, 64);
  if (Number.isNaN(Date.parse(created)) || Number.isNaN(Date.parse(updated))) return invalid();
  const createdBy = text(row.created_by, 128), updatedBy = text(row.updated_by, 128);
  if (!createdBy || !updatedBy) return invalid();
  return { ...key, title, status: row.status as PlanningStatus, path: row.path as string, version: row.version as number,
    sha256: row.sha256, created_by: createdBy, updated_by: updatedBy, created_at: created, updated_at: updated };
}
export function parsePlanningList(value: unknown, selection: PlanningSelection, kind?: PlanningKind): PlanningList {
  const row = object(value);
  if (!Array.isArray(row.items) || row.items.length > 99 || typeof row.truncated !== "boolean") return invalid();
  const items = row.items.map(item => parseMeta(item, selection));
  if (items.some(item => kind !== undefined && item.kind !== kind) || new Set(items.map(item => item.path)).size !== items.length) return invalid();
  return { items, truncated: row.truncated };
}
export function parsePlanningItem(value: unknown, key: PlanningKey): PlanningItem {
  const meta = parseMeta(value, key);
  if (meta.kind !== key.kind || meta.item_id !== key.item_id) return invalid();
  const content = text(object(value).content, 65536);
  if (new TextEncoder().encode(content).length > MAX_PLANNING_CONTENT_BYTES) return invalid();
  return { ...meta, content };
}
function params(selection: PlanningSelection): URLSearchParams {
  if (!validPlanningSelection(selection)) throw new HubApiError(422, "invalid_input");
  const result = new URLSearchParams({ scope: selection.scope, project: selection.project });
  if (selection.topic !== null) result.set("topic", selection.topic);
  return result;
}
async function responseJson(response: Response): Promise<unknown> {
  let value: unknown;
  if (response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
    try { value = await response.json(); } catch { /* Never reflect raw proxy responses. */ }
  }
  if (!response.ok) {
    if (response.status === 409) {
      const detail = value && typeof value === "object" ? (value as {detail?: unknown}).detail : null;
      if (detail && typeof detail === "object") {
        const current = (detail as Record<string, unknown>).current_version;
        if ((detail as Record<string, unknown>).code === "planning_version_conflict" && Number.isSafeInteger(current)
            && (current as number) >= 0) throw new PlanningConflictError(current as number);
      }
    }
    const code = response.status === 413 ? "planning_too_large" : response.status === 503 ? "planning_unavailable"
      : response.status === 404 ? "planning_item_not_found" : response.status === 422 ? "invalid_planning_item" : "planning_request_failed";
    throw new HubApiError(response.status, code);
  }
  if (value === undefined) return invalid();
  return value;
}
const readInit = (signal: AbortSignal): RequestInit => ({ method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store",
  headers: { Accept: "application/json" }, signal });
export async function listPlanning(selection: PlanningSelection, signal: AbortSignal, kind?: PlanningKind,
  fetchImpl: typeof fetch = globalThis.fetch): Promise<PlanningList> {
  const query = params(selection);
  query.set("limit", String(PLANNING_LIST_LIMIT));
  if (kind) query.set("kind", kind);
  return parsePlanningList(await responseJson(await fetchImpl.call(globalThis, `/api/hub/v1/planning?${query}`, readInit(signal))), selection, kind);
}
export async function readPlanning(key: PlanningKey, signal: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch): Promise<PlanningItem> {
  const query = params(key);
  if (!PLANNING_KINDS.includes(key.kind) || !isPlanningId(key.item_id)) throw new HubApiError(422, "invalid_input");
  query.set("kind", key.kind); query.set("item_id", key.item_id);
  return parsePlanningItem(await responseJson(await fetchImpl.call(globalThis, `/api/hub/v1/planning/document?${query}`, readInit(signal))), key);
}
export async function savePlanning(draft: PlanningDraft, csrf: string, fetchImpl: typeof fetch = globalThis.fetch): Promise<PlanningItem> {
  if (!csrf) throw new HubApiError(403, "csrf_required");
  if (!validPlanningSelection(draft) || !PLANNING_KINDS.includes(draft.kind) || !isPlanningId(draft.item_id)
      || !PLANNING_STATUSES.includes(draft.status) || !draft.title.trim() || draft.title.length > 200
      || !Number.isSafeInteger(draft.expected_version) || draft.expected_version < 0 || draft.content.includes("\u0000"))
    throw new HubApiError(422, "invalid_input");
  if (new TextEncoder().encode(draft.content).length > MAX_PLANNING_CONTENT_BYTES
      || planningRequestBytes(draft) > MAX_PLANNING_REQUEST_BYTES) throw new HubApiError(413, "planning_too_large");
  const response = await fetchImpl.call(globalThis, "/api/hub/v1/planning", { method: "POST", credentials: "same-origin", redirect: "error", cache: "no-store",
    headers: { Accept: "application/json", "Content-Type": "application/json", "X-Hub-CSRF": csrf }, body: body(draft) });
  const item = parsePlanningItem(await responseJson(response), draft);
  if (item.version !== draft.expected_version + 1) return invalid();
  return item;
}
