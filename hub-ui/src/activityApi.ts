/** Project activity from the Hub's durable audit log (GET /api/hub/v1/activity). Data only. */
import { HubApiError, isMissingRoute } from "./api";

export interface ActivityItem {
  sequence: number;
  kind: "task" | "decision" | "run";
  verb: string;
  entityId: string;
  revision: number;
  at: string;
  actor: string;
  /** The entity's title now, not necessarily when this happened. */
  title: string | null;
}
export interface ActivityPage { items: ActivityItem[]; nextBefore: number | null }

const KINDS = new Set(["task", "decision", "run"]);
const ACTION = /^(task|decision|run)\.([a-z_]{1,40})$/;

function invalid(): never { throw new HubApiError(502, "invalid_activity"); }

export function parseActivity(value: unknown, project: string): ActivityPage {
  if (!value || typeof value !== "object") return invalid();
  const body = value as Record<string, unknown>;
  if (body.project !== project || !Array.isArray(body.items) || body.items.length > 100) return invalid();
  const items: ActivityItem[] = [];
  for (const raw of body.items) {
    const row = raw as Record<string, unknown> | null;
    const action = typeof row?.action === "string" ? ACTION.exec(row.action) : null;
    if (!row || !action || !KINDS.has(String(row.kind)) || action[1] !== row.kind) continue;
    if (!Number.isSafeInteger(row.sequence) || (row.sequence as number) < 1) continue;
    if (!Number.isSafeInteger(row.revision) || typeof row.entity_id !== "string" || row.entity_id.length > 64) continue;
    if (typeof row.created_at !== "string" || !Number.isFinite(Date.parse(row.created_at))) continue;
    items.push({
      sequence: row.sequence as number, kind: row.kind as ActivityItem["kind"], verb: action[2],
      entityId: row.entity_id, revision: row.revision as number, at: row.created_at,
      actor: typeof row.actor === "string" && row.actor.length <= 200 ? row.actor : "unknown",
      title: typeof row.current_title === "string" ? row.current_title.slice(0, 200) : null,
    });
  }
  const next = body.next_before;
  return { items, nextBefore: Number.isSafeInteger(next) && (next as number) > 0 ? next as number : null };
}

/** null means this server has no activity route yet. */
export async function readActivity(project: string, before: number | null, signal: AbortSignal,
  fetchImpl: typeof fetch = globalThis.fetch): Promise<ActivityPage | null> {
  const params = new URLSearchParams({ project, limit: "50" });
  if (before !== null) params.set("before", String(before));
  const response = await fetchImpl.call(globalThis, `/api/hub/v1/activity?${params}`, {
    method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store",
    headers: { Accept: "application/json" }, signal,
  });
  if (await isMissingRoute(response)) return null;
  if (!response.ok) throw new HubApiError(response.status, "activity_failed");
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return invalid();
  return parseActivity(await response.json(), project);
}

const VERBS: Record<string, string> = {
  created: "created", proposed: "proposed", claimed: "claimed", candidate: "submitted a candidate for",
  reviewed: "reviewed", answered: "answered", context_invalidated: "changed the context of",
  run_created: "started a run for", run_failed: "recorded a failed run for", succeeded: "finished",
  rolled_back: "rolled back", recovery_held: "held for recovery",
};
export const describeVerb = (verb: string) => VERBS[verb] ?? verb.replaceAll("_", " ");
