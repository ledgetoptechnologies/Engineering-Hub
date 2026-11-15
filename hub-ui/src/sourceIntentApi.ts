import { HubApiError, isMissingRoute } from "./api";

const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const PROJECT = /^[a-z0-9][a-z0-9-]{0,63}$/;
const RECIPE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const COMMIT = /^[a-f0-9]{40}$/;
const DIGEST = /^[a-f0-9]{64}$/;
const PROFILE = /^sha256:[a-f0-9]{64}$/;
type JsonObject = Record<string, unknown>;
export type SourceIntent = { submissionId: string; state: "unreadable" } | {
  submissionId: string; state: "deposit_unknown"; project: string; recipe: string;
  actor: string; requestId: string; sourceId: string; commandId: string; taskId: string; taskRevision: number;
  sourceCommit: string; sourceDigest: string; treeOid: string; commitObjectDigest: string;
  expandedBytes: number; entries: number; profileDigest: string; profileGeneration: number;
};
export interface SourceIntentPage { items: SourceIntent[]; nextCursor: string | null }
function invalid(): never { throw new HubApiError(502, "invalid_source_intent_handoff"); }
function obj(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : invalid();
}
function match(value: unknown, pattern: RegExp): string {
  return typeof value === "string" && pattern.test(value) ? value : invalid();
}
function positive(value: unknown): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 && value < 2 ** 31 ? value : invalid();
}
export function parseSourceIntentPage(value: unknown): SourceIntentPage {
  const page = obj(value);
  if (page.scope !== "original_intent_only" || page.worker_authority_verified !== false || !Array.isArray(page.items) || page.items.length > 50) return invalid();
  const nextCursor = page.next_cursor === null ? null : match(page.next_cursor, UUID);
  const items = page.items.map((value): SourceIntent => {
    const row = obj(value); const submissionId = match(row.submission_id, UUID);
    if (row.state === "unreadable") return { submissionId, state: "unreadable" };
    if (row.state !== "deposit_unknown" || row.source_identity_assurance !== "journal_only"
      || row.worker_ledger_authority !== "unverified" || row.source_post_outcome !== "unknown"
      || row.review_status !== "historical_not_current" || row.execution_authorization !== false
      || row.automatic_retry_or_release !== false) return invalid();
    const profile = obj(row.reviewed_profile);
    const binding = obj(row.source_binding);
    const project = match(row.project, PROJECT), recipe = match(row.recipe, RECIPE);
    if (profile.project !== project || profile.recipe !== recipe) return invalid();
    const sourceCommit = match(row.source_commit, COMMIT), sourceDigest = match(row.source_digest, DIGEST);
    if (binding.scheme !== "git-sha1-tree-v1" || binding.commit_oid !== sourceCommit || binding.archive_sha256 !== sourceDigest
      || typeof binding.expanded_bytes !== "number" || !Number.isSafeInteger(binding.expanded_bytes)
      || binding.expanded_bytes < 0 || binding.expanded_bytes > 64 * 1024 ** 2
      || typeof binding.entries !== "number" || !Number.isSafeInteger(binding.entries) || binding.entries < 1 || binding.entries > 5000) return invalid();
    return { submissionId, state: "deposit_unknown", project, recipe,
      actor: match(row.actor, /^(codex|claude|hermes)$/), requestId: match(row.request_id, UUID),
      sourceId: match(row.source_id, UUID), commandId: match(row.reserved_command_id, UUID),
      taskId: match(row.review_task_id, UUID), taskRevision: positive(row.review_task_revision),
      sourceCommit, sourceDigest, treeOid: match(binding.tree_oid, COMMIT),
      commitObjectDigest: match(binding.commit_object_sha256, DIGEST),
      expandedBytes: binding.expanded_bytes, entries: binding.entries,
      profileDigest: match(profile.digest, PROFILE), profileGeneration: positive(profile.generation) };
  });
  return { items, nextCursor };
}

export async function readSourceIntentPage(after: string | null, signal: AbortSignal,
  fetchImpl: typeof fetch = globalThis.fetch): Promise<SourceIntentPage> {
  if (after !== null) match(after, UUID);
  const query = new URLSearchParams({ limit: "50" });
  if (after !== null) query.set("after", after);
  const response = await fetchImpl.call(globalThis, `/api/hub/v1/staging/source-intent-handoffs?${query}`, {
    method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store",
    headers: { Accept: "application/json" }, signal,
  });
  if (!response.ok) {
    if (await isMissingRoute(response)) throw new HubApiError(404, "source_handoff_unsupported");
    throw new HubApiError(response.status, response.status === 401 ? "human_session_required" : "source_handoff_unavailable");
  }
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return invalid();
  let body: unknown; try { body = await response.json(); } catch { return invalid(); }
  return parseSourceIntentPage(body);
}
