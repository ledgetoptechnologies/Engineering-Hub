/** Read-only queue projection. No executor credentials or mutation capability. */
import { HubApiError } from "./api";

const states = ["queued", "claimed", "held", "succeeded", "failed", "cancelled"] as const;
export type WorkState = typeof states[number];
export interface QueueJob {
  id: string; machine_id: string; action_key: string; environment: "production" | "staging";
  state: WorkState; target: { task_id: string; source_commit: string; artifact_digest: string };
  blocked_reason: string | null; requires_reconciliation: boolean;
  eligible: boolean; eligible_requires_worker_admission: boolean;
}
export interface QueueSnapshot {
  paused: boolean; queue_paused: boolean; master_paused: boolean;
  claim_admission_state: "unconfigured" | "trusted_composition" | "worker_policy";
  counts: Partial<Record<WorkState, number>>; jobs: QueueJob[];
}
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const key = /^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$/;
const reasons = new Set(["requires_reconciliation", "paused", "registry_changed", "production_approval_required",
  "admission_not_configured", "target_not_verified", "machine_reserved"]);
function invalid(): never { throw new HubApiError(502, "invalid_queue_snapshot"); }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  return value as Record<string, unknown>;
}
function text(value: unknown, pattern: RegExp): string {
  if (typeof value !== "string" || pattern.exec(value)?.[0] !== value) return invalid();
  return value;
}
function bool(value: unknown): boolean { return typeof value === "boolean" ? value : invalid(); }
export function parseQueueSnapshot(value: unknown): QueueSnapshot {
  const row = object(value);
  const admission = row.claim_admission_state;
  if (admission !== "unconfigured" && admission !== "trusted_composition" && admission !== "worker_policy") return invalid();
  const counts: QueueSnapshot["counts"] = {};
  for (const [state, count] of Object.entries(object(row.counts))) {
    if (!states.includes(state as WorkState) || !Number.isSafeInteger(count) || (count as number) < 0 || (count as number) > 100000) return invalid();
    counts[state as WorkState] = count as number;
  }
  if (!Array.isArray(row.jobs) || row.jobs.length > 50) return invalid();
  const jobs = row.jobs.map((value): QueueJob => {
    const job = object(value), target = object(job.target);
    if (!states.includes(job.state as WorkState) || !["production", "staging"].includes(job.environment as string)
        || !(job.blocked_reason === null || reasons.has(job.blocked_reason as string))) return invalid();
    return { id: text(job.id, uuid), machine_id: text(job.machine_id, key), action_key: text(job.action_key, key),
      environment: job.environment as QueueJob["environment"], state: job.state as WorkState,
      target: { task_id: text(target.task_id, uuid), source_commit: text(target.source_commit, /^[a-f0-9]{40}$/),
        artifact_digest: text(target.artifact_digest, /^sha256:[a-f0-9]{64}$/) },
      blocked_reason: job.blocked_reason as string | null, requires_reconciliation: bool(job.requires_reconciliation),
      eligible: bool(job.eligible), eligible_requires_worker_admission: bool(job.eligible_requires_worker_admission) };
  });
  if (new Set(jobs.map(job => job.id)).size !== jobs.length) return invalid();
  const result: QueueSnapshot = { paused: bool(row.paused), queue_paused: bool(row.queue_paused), master_paused: bool(row.master_paused),
    claim_admission_state: admission, counts, jobs };
  if (result.paused !== (result.queue_paused || result.master_paused)) return invalid();
  return result;
}
export async function readQueueSnapshot(signal: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch): Promise<QueueSnapshot> {
  const response = await fetchImpl.call(globalThis, "/api/hub/v1/work/status?limit=50", {
    method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store", headers: { Accept: "application/json" }, signal,
  });
  if (!response.ok) throw new HubApiError(response.status, response.status === 423 ? "work_queue_not_configured"
    : response.status === 404 ? "work_queue_unavailable" : "queue_read_failed");
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return invalid();
  return parseQueueSnapshot(await response.json());
}
