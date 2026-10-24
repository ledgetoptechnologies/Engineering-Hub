/**
 * Staging resources: workspaces, builds, test runs and previews that the staging
 * service keeps for EH jobs. EH records them; cleanup is always plan, then apply,
 * and only removes what an EH job created. Contract: docs/modular-staging-v1.md.
 */
import { HubApiError, isMissingRoute } from "./api";

export const RESOURCE_KINDS = ["workspace", "build", "test_run", "preview"] as const;
export type ResourceKind = typeof RESOURCE_KINDS[number];
export const RESOURCE_STATES = ["provisioning", "ready", "failed", "expiring", "deleting", "deleted", "delete_failed"] as const;
export type ResourceState = typeof RESOURCE_STATES[number];
export const PROTECTED_REASONS = ["active_job", "current_release", "rollback_target", "held_campaign", "decision_evidence", "latest_failure"] as const;
export type ProtectedReason = typeof PROTECTED_REASONS[number];

export interface ResourceKeep { mode: "until" | "pinned"; until: string | null; by: string; reason: string; at: string }
export interface StagingResource {
  id: string; kind: ResourceKind; project: string; topic: string | null; job_id: string;
  source_commit: string; artifact_digest: string | null; created_by: string;
  created_at: string; updated_at: string; last_viewed_at: string | null; expires_at: string | null;
  state: ResourceState; size_bytes: number | null; preview_path: string | null;
  keep: ResourceKeep | null; protected: { reason: ProtectedReason; ref: string }[]; revision: number;
}
export interface ResourcePage { items: StagingResource[]; next_after: string | null }
export interface ResourceQuery { project?: string; kind?: ResourceKind; after?: string; limit?: number }
export interface CleanupPlan {
  plan_id: string; digest: string; expires_at: string; total_bytes: number | null;
  remove: { id: string; kind: ResourceKind; size_bytes: number | null; why: "expired" }[];
  /** Why is server wording (pinned, protected, not expired…); unknown reasons are shown, not dropped. */
  kept: { id: string; why: string }[];
}
export interface CleanupResult { removed: string[]; failed: { id: string; code: string }[]; skipped_changed: string[] }
export interface PreviewSession { url: string; expires_at: string }
export const STATUS_STATES = ["not_configured", "invalid", "disabled", "existing_executor_owner", "no_profiles", "unavailable"] as const;
export interface StagingStatus {
  configured: boolean; running: boolean; reachable: boolean;
  /** Recipe names per project this human may use. */
  profiles: Record<string, string[]>;
  state: typeof STATUS_STATES[number] | null;
  /** Absent means the server didn't say, which is not the same as configured. */
  previews_configured: boolean | null;
}
/** Every keep change carries a reason, including letting an item expire again. */
export type KeepRequest = { mode: "until"; until: string; reason: string } | { mode: "pinned" | "none"; reason: string };

export const RESOURCE_PAGE_LIMIT = 50;
export const KEEP_REASON_MAX = 500;
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const projectPattern = /^[a-z0-9][a-z0-9-]{0,63}$/;
const topicPattern = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const actorPattern = /^[^\u0000-\u001f\u007f]{1,128}$/;
const codePattern = /^[a-z][a-z0-9_.:-]{0,63}$/;
const knownCodes = new Set(["staging_resource_conflict", "staging_resource_not_found", "staging_resource_protected",
  "staging_plan_expired", "staging_plan_changed", "staging_unavailable", "staging_busy", "preview_unavailable",
  "preview_not_configured", "keep_reason_required", "human_session_required", "forbidden", "csrf_required"]);
/** The resource store's own names, folded onto the codes the panel words. */
const codeAliases: Record<string, string> = {
  revision_conflict: "staging_resource_conflict", not_found: "staging_resource_not_found", staging_forbidden: "staging_forbidden",
  staging_cleanup_in_progress: "staging_cleanup_in_progress", staging_plan_digest_conflict: "staging_plan_changed",
  staging_preview_not_configured: "preview_not_configured",
};

export function isResourceId(value: string): boolean { return uuid.test(value); }
export function isStagingProject(value: string): boolean { return projectPattern.exec(value)?.[0] === value; }

export class ResourceConflictError extends HubApiError {
  constructor(public readonly currentRevision: number) { super(409, "staging_resource_conflict"); this.name = "ResourceConflictError"; }
}
function invalid(): never { throw new HubApiError(502, "invalid_staging_response"); }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  return value as Record<string, unknown>;
}
function matching(value: unknown, pattern: RegExp): string {
  return typeof value === "string" && pattern.exec(value)?.[0] === value ? value : invalid();
}
function nullable<T>(value: unknown, parse: (value: unknown) => T): T | null { return value === null ? null : parse(value); }
function oneOf<T extends string>(value: unknown, options: readonly T[]): T {
  return typeof value === "string" && (options as readonly string[]).includes(value) ? value as T : invalid();
}
function timestamp(value: unknown): string {
  return typeof value === "string" && value.length <= 64 && !Number.isNaN(Date.parse(value)) ? value : invalid();
}
function bytes(value: unknown): number | null {
  if (value === null) return null;
  return Number.isSafeInteger(value) && (value as number) >= 0 ? value as number : invalid();
}
function boundedText(value: unknown, max: number): string {
  return typeof value === "string" && value.trim() && value.length <= max && !value.includes("\u0000") ? value : invalid();
}
function idList(value: unknown, max = 500): string[] {
  if (!Array.isArray(value) || value.length > max) return invalid();
  return value.map(id => matching(id, uuid));
}

function parseKeep(value: unknown): ResourceKeep {
  const row = object(value);
  const mode = oneOf(row.mode, ["until", "pinned"] as const);
  const until = mode === "until" ? timestamp(row.until) : row.until === undefined || row.until === null ? null : invalid();
  return { mode, until, by: matching(row.by, actorPattern), reason: boundedText(row.reason, KEEP_REASON_MAX), at: timestamp(row.at) };
}
export function parseResource(value: unknown): StagingResource {
  const row = object(value);
  if (!Array.isArray(row.protected) || row.protected.length > 10) return invalid();
  const revision = row.revision;
  if (!Number.isSafeInteger(revision) || (revision as number) < 1) return invalid();
  return {
    id: matching(row.id, uuid), kind: oneOf(row.kind, RESOURCE_KINDS), project: matching(row.project, projectPattern),
    topic: nullable(row.topic, topic => matching(topic, topicPattern)), job_id: matching(row.job_id, uuid),
    // Declared provenance, not verified: shown as text, never used to fetch anything.
    source_commit: matching(row.source_commit, /^[a-f0-9]{7,64}$/),
    artifact_digest: nullable(row.artifact_digest, digest => matching(digest, /^sha256:[a-f0-9]{64}$/)),
    created_by: matching(row.created_by, actorPattern), created_at: timestamp(row.created_at), updated_at: timestamp(row.updated_at),
    last_viewed_at: nullable(row.last_viewed_at, timestamp), expires_at: nullable(row.expires_at, timestamp),
    state: oneOf(row.state, RESOURCE_STATES), size_bytes: bytes(row.size_bytes),
    // Never navigated to: previews open only through a preview session on the separate preview origin.
    preview_path: nullable(row.preview_path, path => boundedText(path, 1024)),
    keep: nullable(row.keep, parseKeep),
    protected: row.protected.map(entry => {
      const item = object(entry);
      return { reason: oneOf(item.reason, PROTECTED_REASONS), ref: boundedText(item.ref, 200) };
    }),
    revision: revision as number,
  };
}
export function parseResourcePage(value: unknown, query: ResourceQuery): ResourcePage {
  const row = object(value);
  if (!Array.isArray(row.items) || row.items.length > 100) return invalid();
  const items = row.items.map(parseResource);
  // A page for another filter would show the wrong project's resources after a race.
  if (items.some(item => (query.project && item.project !== query.project) || (query.kind && item.kind !== query.kind))) return invalid();
  if (new Set(items.map(item => item.id)).size !== items.length) return invalid();
  return { items, next_after: nullable(row.next_after, after => matching(after, uuid)) };
}
export function parseCleanupPlan(value: unknown): CleanupPlan {
  const row = object(value);
  if (!Array.isArray(row.remove) || !Array.isArray(row.kept) || row.remove.length > 500 || row.kept.length > 500) return invalid();
  const remove = row.remove.map(entry => {
    const item = object(entry);
    return { id: matching(item.id, uuid), kind: oneOf(item.kind, RESOURCE_KINDS), size_bytes: bytes(item.size_bytes),
      why: oneOf(item.why, ["expired"] as const) };
  });
  const kept = row.kept.map(entry => { const item = object(entry); return { id: matching(item.id, uuid), why: matching(item.why, codePattern) }; });
  const ids = [...remove, ...kept].map(item => item.id);
  if (new Set(ids).size !== ids.length) return invalid();
  return { plan_id: matching(row.plan_id, uuid), digest: matching(row.digest, /^[a-f0-9]{64}$/),
    expires_at: timestamp(row.expires_at), total_bytes: bytes(row.total_bytes), remove, kept };
}
export function parseCleanupResult(value: unknown, plan: CleanupPlan): CleanupResult {
  const row = object(value);
  if (!Array.isArray(row.failed) || row.failed.length > 500) return invalid();
  const removed = idList(row.removed), skipped = idList(row.skipped_changed);
  const failed = row.failed.map(entry => { const item = object(entry); return { id: matching(item.id, uuid), code: matching(item.code, codePattern) }; });
  // Every outcome must be for an item the plan said it would remove, and each item has exactly one outcome.
  const planned = new Set(plan.remove.map(item => item.id));
  const outcomes = [...removed, ...failed.map(item => item.id), ...skipped];
  if (outcomes.some(id => !planned.has(id)) || new Set(outcomes).size !== outcomes.length) return invalid();
  return { removed, failed, skipped_changed: skipped };
}
/** A preview may only open on a different origin from the dashboard, never inside EH's own origin. */
export function parsePreviewSession(value: unknown, dashboardOrigin: string): PreviewSession {
  const row = object(value);
  const text = boundedText(row.url, 2048);
  let url: URL;
  try { url = new URL(text); } catch { return invalid(); }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (!(url.protocol === "https:" || (url.protocol === "http:" && local)) || url.username || url.password) return invalid();
  if (!dashboardOrigin || url.origin === dashboardOrigin) return invalid();
  return { url: url.href, expires_at: timestamp(row.expires_at) };
}

async function failure(response: Response): Promise<never> {
  let code = response.status === 503 ? "staging_unavailable" : response.status === 404 ? "staging_resource_not_found"
    : response.status === 409 ? "staging_resource_conflict" : "staging_request_failed";
  let current: number | null = null, named = false;
  if (response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
    try {
      const detail = object(object(await response.clone().json()).detail);
      if (typeof detail.code === "string" && knownCodes.has(detail.code)) { code = detail.code; named = true; }
      else if (typeof detail.code === "string" && Object.hasOwn(codeAliases, detail.code)) { code = codeAliases[detail.code]; named = true; }
      if (Number.isSafeInteger(detail.current_revision) && (detail.current_revision as number) >= 1) current = detail.current_revision as number;
    } catch { /* Never reflect proxy HTML or raw server exceptions. */ }
  }
  // A missing item names itself; only an unnamed 404 or the gateway's refusal means this Hub lacks the feature.
  if (!named && await isMissingRoute(response)) throw new HubApiError(404, "staging_resources_unsupported");
  if (response.status === 409 && code === "staging_resource_conflict" && current !== null) throw new ResourceConflictError(current);
  throw new HubApiError(response.status, code);
}
async function json(response: Response): Promise<unknown> {
  if (!response.ok) return failure(response);
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return invalid();
  try { return await response.json(); } catch { return invalid(); }
}
const getInit = (signal: AbortSignal): RequestInit => ({
  method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store", headers: { Accept: "application/json" }, signal,
});
const postInit = (csrf: string, body: unknown, signal?: AbortSignal): RequestInit => {
  if (!csrf) throw new HubApiError(403, "csrf_required");
  return { method: "POST", credentials: "same-origin", redirect: "error", cache: "no-store", signal,
    headers: { Accept: "application/json", "Content-Type": "application/json", "X-Hub-CSRF": csrf }, body: JSON.stringify(body) };
};

export async function listResources(query: ResourceQuery, signal: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch): Promise<ResourcePage> {
  if ((query.project && !isStagingProject(query.project)) || (query.after && !isResourceId(query.after))) throw new HubApiError(422, "invalid_input");
  const params = new URLSearchParams({ limit: String(query.limit ?? RESOURCE_PAGE_LIMIT) });
  if (query.project) params.set("project", query.project);
  if (query.kind) params.set("kind", query.kind);
  if (query.after) params.set("after", query.after);
  const response = await fetchImpl.call(globalThis, `/api/hub/v1/staging/resources?${params}`, getInit(signal));
  return parseResourcePage(await json(response), query);
}
export async function keepResource(resource: StagingResource, request: KeepRequest, csrf: string,
  fetchImpl: typeof fetch = globalThis.fetch): Promise<StagingResource> {
  if (!isResourceId(resource.id)) throw new HubApiError(422, "invalid_input");
  if (!request.reason.trim() || request.reason.length > KEEP_REASON_MAX) throw new HubApiError(422, "keep_reason_required");
  const body = { ...request, reason: request.reason.trim(), expected_revision: resource.revision };
  const response = await fetchImpl.call(globalThis, `/api/hub/v1/staging/resources/${resource.id}/keep`, postInit(csrf, body));
  const saved = parseResource(await json(response));
  if (saved.id !== resource.id || saved.revision <= resource.revision) return invalid();
  return saved;
}
export async function planCleanup(project: string | null, csrf: string, fetchImpl: typeof fetch = globalThis.fetch): Promise<CleanupPlan> {
  if (project !== null && !isStagingProject(project)) throw new HubApiError(422, "invalid_input");
  const response = await fetchImpl.call(globalThis, "/api/hub/v1/staging/cleanup/plan", postInit(csrf, project ? { project } : {}));
  return parseCleanupPlan(await json(response));
}
export async function applyCleanup(plan: CleanupPlan, csrf: string, fetchImpl: typeof fetch = globalThis.fetch): Promise<CleanupResult> {
  const response = await fetchImpl.call(globalThis, "/api/hub/v1/staging/cleanup/apply",
    postInit(csrf, { plan_id: plan.plan_id, digest: plan.digest }));
  return parseCleanupResult(await json(response), plan);
}
export async function startPreview(resource: StagingResource, csrf: string, dashboardOrigin: string = globalThis.location?.origin ?? "",
  fetchImpl: typeof fetch = globalThis.fetch): Promise<PreviewSession> {
  if (!isResourceId(resource.id) || resource.kind !== "preview") throw new HubApiError(422, "invalid_input");
  const response = await fetchImpl.call(globalThis, `/api/hub/v1/staging/resources/${resource.id}/preview-session`, postInit(csrf, {}));
  return parsePreviewSession(await json(response), dashboardOrigin);
}

export function parseStagingStatus(value: unknown): StagingStatus {
  const row = object(value);
  const bool = (field: unknown) => typeof field === "boolean" ? field : invalid();
  const profiles: Record<string, string[]> = {};
  const entries = Object.entries(object(row.profiles));
  if (entries.length > 200) return invalid();
  for (const [project, recipes] of entries) {
    if (!isStagingProject(project) || !Array.isArray(recipes) || recipes.length > 50) return invalid();
    profiles[project] = recipes.map(recipe => matching(recipe, /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/));
  }
  const status: StagingStatus = { configured: bool(row.configured), running: bool(row.running), reachable: bool(row.reachable), profiles,
    state: row.state === undefined || row.state === null ? null : oneOf(row.state, STATUS_STATES),
    previews_configured: row.previews_configured === undefined || row.previews_configured === null ? null : bool(row.previews_configured) };
  // Unconfigured can't also claim to be running or reachable.
  if (!status.configured && (status.running || status.reachable)) return invalid();
  return status;
}
export async function readStagingStatus(signal: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch): Promise<StagingStatus> {
  const response = await fetchImpl.call(globalThis, "/api/hub/v1/staging/status", getInit(signal));
  return parseStagingStatus(await json(response));
}
const statusStates: Record<typeof STATUS_STATES[number], string> = {
  not_configured: "The staging service isn't set up yet.",
  invalid: "The staging service settings aren't valid, so it isn't being used.",
  disabled: "The staging service is turned off.",
  existing_executor_owner: "The staging PC is still owned by the earlier executor, so the new service isn't in use.",
  no_profiles: "The staging service has no build or test recipes yet.",
  unavailable: "The staging service isn't available right now.",
};
/** One honest line: never "ready" unless the server says configured, running and reachable. */
export function statusLine(status: StagingStatus): { text: string; tone: "neutral" | "amber" | "forest" } {
  if (status.state) return { text: statusStates[status.state], tone: "amber" };
  if (!status.configured) return { text: statusStates.not_configured, tone: "amber" };
  if (!status.running) return { text: "The staging queue controller isn't running, so no new builds or tests will start.", tone: "amber" };
  if (!status.reachable) return { text: "The staging PC isn't answering, so new builds or tests can't start.", tone: "amber" };
  const projects = Object.keys(status.profiles).length;
  return { text: `Staging PC reachable. ${projects === 0 ? "No projects have" : projects === 1 ? "1 project has" : `${projects} projects have`
    } build or test recipes you can use.`, tone: "forest" };
}

// Wording shared by the panel, the cleanup dialog and tests.
const kindNames: Record<ResourceKind, [string, string]> = {
  workspace: ["workspace", "workspaces"], build: ["build", "builds"], test_run: ["test run", "test runs"], preview: ["preview", "previews"],
};
const protectedNames: Record<ProtectedReason, string> = {
  rollback_target: "rollback copy", current_release: "current release", active_job: "in use by a running job",
  held_campaign: "held for review", decision_evidence: "evidence for a decision", latest_failure: "latest failure, kept for diagnosis",
};
export const kindLabel = (kind: ResourceKind, count = 1) => kindNames[kind][count === 1 ? 0 : 1];
export const protectedLabel = (reason: ProtectedReason) => protectedNames[reason];

export function formatBytes(value: number | null): string {
  if (value === null) return "size unknown";
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let size = value, unit = 0;
  while (size >= 1000 && unit < units.length - 1) { size /= 1000; unit++; }
  return unit === 0 ? `${value} bytes` : `${size.toFixed(size >= 10 ? 0 : 1)} ${units[unit]}`;
}
function relative(ms: number): string {
  const hours = Math.round(ms / 3_600_000);
  if (hours < 1) return "less than an hour";
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return `${Math.round(hours / 24)} days`;
}
const day = (iso: string) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
/** The one line that says how long a resource will be kept, and why. Protection outranks pins, pins outrank dates. */
export function expiryLabel(resource: StagingResource, now: Date): { text: string; tone: "neutral" | "amber" | "forest" } {
  if (resource.state === "deleted") return { text: "Removed", tone: "neutral" };
  if (resource.protected.length) return { text: `Protected: ${resource.protected.map(item => protectedLabel(item.reason)).join(", ")}`, tone: "forest" };
  if (resource.keep?.mode === "pinned") return { text: `Pinned: ${resource.keep.reason}`, tone: "forest" };
  const until = resource.keep?.mode === "until" ? resource.keep.until : resource.expires_at;
  if (!until) return { text: "No expiry set", tone: "neutral" };
  const left = Date.parse(until) - now.getTime();
  if (left <= 0) return { text: "Expired, removed at the next cleanup", tone: "amber" };
  if (resource.keep?.mode === "until") return { text: `Kept until ${day(until)}`, tone: "neutral" };
  return { text: `Expires in ${relative(left)}`, tone: left < 24 * 3_600_000 ? "amber" : "neutral" };
}
/** "Keep 7 more days" counts from whichever is later: now, or the date it would currently go. */
export function extendedUntil(resource: StagingResource, now: Date, days = 7): string {
  const current = resource.keep?.mode === "until" ? resource.keep.until : resource.expires_at;
  const base = Math.max(now.getTime(), current ? Date.parse(current) : 0);
  return new Date(base + days * 86_400_000).toISOString();
}
export const canChangeKeep = (resource: StagingResource) =>
  resource.protected.length === 0 && !["deleting", "deleted"].includes(resource.state);

function countPhrase<T>(items: T[], key: (item: T) => string, label: (key: string, count: number) => string): string {
  const counts = new Map<string, number>();
  for (const item of items) counts.set(key(item), (counts.get(key(item)) ?? 0) + 1);
  const parts = [...counts].map(([name, count]) => `${count} ${label(name, count)}`);
  return parts.length <= 1 ? parts.join("") : `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}`;
}
const keptNames: Record<string, string> = {
  pinned: "pinned", keep_until: "kept by request", kept: "kept by request", protected: "protected",
  not_expired: "not expired yet", not_owned: "not created by an EH job",
};
export function keptLabel(why: string): string {
  const reason = why.startsWith("protected:") ? why.slice(10) : why;
  if ((PROTECTED_REASONS as readonly string[]).includes(reason)) return protectedLabel(reason as ProtectedReason);
  return keptNames[why] ?? why.replace(/[_:.-]+/g, " ");
}
export function planSummary(plan: CleanupPlan): { remove: string; keep: string } {
  const unknown = plan.remove.some(item => item.size_bytes === null);
  const size = plan.total_bytes === null ? "size unknown" : `${formatBytes(plan.total_bytes)}${unknown ? ", some sizes unknown" : ""}`;
  return {
    remove: plan.remove.length === 0 ? "Nothing to remove."
      : `Will remove ${countPhrase(plan.remove, item => item.kind, (kind, count) => kindLabel(kind as ResourceKind, count))} (${size}).`,
    keep: plan.kept.length === 0 ? "" : `Will keep ${countPhrase(plan.kept, item => item.why, why => keptLabel(why))}.`,
  };
}
export function resultSummary(result: CleanupResult): string {
  const parts = [`${result.removed.length} removed`];
  if (result.skipped_changed.length) parts.push(`${result.skipped_changed.length} skipped because ${
    result.skipped_changed.length === 1 ? "it" : "they"} changed since the plan`);
  parts.push(`${result.failed.length} failed`);
  return parts.join(", ") + ".";
}
