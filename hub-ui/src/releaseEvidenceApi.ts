/**
 * Read-only release evidence for one container-pilot run, for the Hub owner.
 * Every step result is what the worker reported over its authenticated channel. The Hub checks that
 * the report matches the exact request it sent and its own journal; it does not inspect Docker itself,
 * so nothing here is ever shown as independently verified.
 */
import { HubApiError, isMissingRoute } from "./api";

export const STEP_ACTIONS = ["deploy_container", "smoke_container", "rollback_container", "smoke_container_rollback", "cleanup_candidate", "cleanup_rollback"] as const;
export type StepAction = typeof STEP_ACTIONS[number];
export const STEP_STATES = ["not_started", "unknown", "held", "succeeded", "failed", "ack_pending", "inconsistent"] as const;
export type StepState = typeof STEP_STATES[number];
const HEALTH = ["unknown", "healthy", "unhealthy"] as const;
const WORKER_STATES = ["pending", "running", "succeeded", "failed", "held"] as const;
export const STEP_REASONS = ["inspect_required", "paused", "target_busy", "stale_fence", "identity_conflict",
  "manifest_mismatch", "policy_mismatch", "image_unavailable", "daemon_unavailable", "container_conflict",
  "evidence_mismatch", "health_failed", "effect_failed", "recovery_required"] as const;
export type StepReason = typeof STEP_REASONS[number];
const RUN_STATES = ["pending", "running", "succeeded", "failed", "rolled_back", "held"] as const;
export type RunState = typeof RUN_STATES[number];

export interface StepEvidence {
  action: StepAction;
  command_id: string;
  state: StepState;
  expected_image_config_id: string;
  reported_image_config_id: string | null;
  reported_health: typeof HEALTH[number] | null;
  worker_state: typeof WORKER_STATES[number] | null;
  worker_evidence_verified: boolean | null;
  protocol_verified: boolean;
  journal_verified: boolean;
  reason: StepReason | null;
}
export interface ReleaseEvidence {
  run_id: string;
  task_id: string;
  project: string;
  source_commit: string;
  review: { actor: typeof AGENTS[number]; commit: string; verdict: "approve" };
  run_status: RunState;
  candidate_image_config_id: string;
  pilot_manifest_digest: string;
  rollback_image_config_id: string;
  candidate_deploy: StepEvidence;
  candidate_health_check: StepEvidence;
  rollback_deploy: StepEvidence;
  rollback_health_check: StepEvidence;
  candidate_cleanup: StepEvidence | null;
  rollback_cleanup: StepEvidence | null;
  evidence_basis: "authenticated_worker_report";
  independently_verified: false;
}

const digest = /^sha256:[0-9a-f]{64}$/;
const commit = /^[0-9a-f]{40}$/;
const project = /^[a-z0-9][a-z0-9-]{0,63}$/;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const AGENTS = ["hermes", "codex", "claude"] as const;

function invalid(): never { throw new HubApiError(502, "invalid_release_evidence"); }
const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : invalid();
const keysAre = (row: Record<string, unknown>, keys: string) => { if (Object.keys(row).sort().join() !== keys) invalid(); };
const oneOf = <T extends string>(value: unknown, allowed: readonly T[]): T =>
  typeof value === "string" && (allowed as readonly string[]).includes(value) ? value as T : invalid();
const orNull = <T,>(value: unknown, check: (value: unknown) => T): T | null => value === null ? null : check(value);
const matching = (value: unknown, pattern: RegExp): string => typeof value === "string" && pattern.test(value) ? value : invalid();
const bool = (value: unknown): boolean => typeof value === "boolean" ? value : invalid();

const STEP_KEYS = ["action", "command_id", "expected_image_config_id", "journal_verified", "protocol_verified", "reason",
  "reported_health", "reported_image_config_id", "state", "worker_evidence_verified", "worker_state"].sort().join();
function parseStep(value: unknown, action: StepAction): StepEvidence {
  const row = record(value);
  keysAre(row, STEP_KEYS);
  if (row.action !== action) invalid();
  const step: StepEvidence = {
    action,
    command_id: matching(row.command_id, uuid),
    state: oneOf(row.state, STEP_STATES),
    expected_image_config_id: matching(row.expected_image_config_id, digest),
    reported_image_config_id: orNull(row.reported_image_config_id, value => matching(value, digest)),
    reported_health: orNull(row.reported_health, value => oneOf(value, HEALTH)),
    worker_state: orNull(row.worker_state, value => oneOf(value, WORKER_STATES)),
    worker_evidence_verified: orNull(row.worker_evidence_verified, bool),
    protocol_verified: bool(row.protocol_verified),
    journal_verified: bool(row.journal_verified),
    reason: orNull(row.reason, value => oneOf(value, STEP_REASONS)),
  };
  // A step with no checked receipt must not carry worker claims.
  if (!step.protocol_verified && (step.reported_image_config_id !== null || step.reported_health !== null
      || step.worker_state !== null || step.worker_evidence_verified !== null)) invalid();
  return step;
}

const EVIDENCE_KEYS = ["candidate_cleanup", "rollback_cleanup", "candidate_deploy", "candidate_health_check", "candidate_image_config_id", "evidence_basis",
  "independently_verified", "pilot_manifest_digest", "project", "review", "rollback_deploy", "rollback_health_check",
  "rollback_image_config_id", "run_id", "run_status", "source_commit", "task_id"].sort().join();
export function parseReleaseEvidence(value: unknown): ReleaseEvidence {
  const row = record(value);
  keysAre(row, EVIDENCE_KEYS);
  // The only basis this view can show. Anything claiming more is a contract change and is refused.
  if (row.evidence_basis !== "authenticated_worker_report" || row.independently_verified !== false) invalid();
  const review = record(row.review);
  keysAre(review, "actor,commit,verdict");
  return {
    run_id: matching(row.run_id, uuid),
    task_id: matching(row.task_id, uuid),
    project: matching(row.project, project),
    source_commit: matching(row.source_commit, commit),
    review: { actor: oneOf(review.actor, AGENTS), commit: matching(review.commit, commit), verdict: oneOf(review.verdict, ["approve"] as const) },
    run_status: oneOf(row.run_status, RUN_STATES),
    candidate_image_config_id: matching(row.candidate_image_config_id, digest),
    pilot_manifest_digest: matching(row.pilot_manifest_digest, digest),
    rollback_image_config_id: matching(row.rollback_image_config_id, digest),
    candidate_deploy: parseStep(row.candidate_deploy, "deploy_container"),
    candidate_health_check: parseStep(row.candidate_health_check, "smoke_container"),
    rollback_deploy: parseStep(row.rollback_deploy, "rollback_container"),
    rollback_health_check: parseStep(row.rollback_health_check, "smoke_container_rollback"),
    candidate_cleanup: orNull(row.candidate_cleanup, value => parseStep(value, "cleanup_candidate")),
    rollback_cleanup: orNull(row.rollback_cleanup, value => parseStep(value, "cleanup_rollback")),
    evidence_basis: "authenticated_worker_report",
    independently_verified: false,
  };
}

export function releaseEvidencePath(runId: string): string {
  if (!uuid.test(runId)) throw new HubApiError(422, "invalid_run_id");
  return `/api/hub/v1/container-pilot/runs/${runId}/release-evidence`;
}

async function failure(response: Response): Promise<never> {
  if (await isMissingRoute(response)) throw new HubApiError(404, "release_evidence_unsupported");
  let code = response.status === 401 ? "human_session_required" : response.status === 403 ? "release_evidence_forbidden"
    : response.status === 404 ? "not_found" : response.status === 423 ? "container_pilot_not_enabled"
    : "release_evidence_request_failed";
  if (response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
    try {
      const detail = (await response.clone().json() as { detail?: { code?: unknown } })?.detail;
      if (typeof detail?.code === "string" && /^[a-z][a-z0-9_.:-]{0,63}$/.test(detail.code)) code = detail.code;
    } catch { /* Never reflect proxy HTML or raw server text. */ }
  }
  throw new HubApiError(response.status, code);
}

export async function readReleaseEvidence(runId: string, signal: AbortSignal,
  fetchImpl: typeof fetch = globalThis.fetch): Promise<ReleaseEvidence> {
  const path = releaseEvidencePath(runId);
  let response: Response;
  try {
    response = await fetchImpl.call(globalThis, path, { method: "GET", credentials: "same-origin", redirect: "error",
      cache: "no-store", headers: { Accept: "application/json" }, signal });
  } catch (error) {
    if (signal.aborted && (error as Error)?.name === "AbortError") throw error;
    throw new HubApiError(0, "network_unavailable");
  }
  if (!response.ok) return failure(response);
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return invalid();
  let body: unknown;
  try { body = await response.json(); } catch { return invalid(); }
  return parseReleaseEvidence(body);
}

/** Plain wording per step state. "Succeeded" is always the worker's report, never the Hub's own check. */
export const stepStateText: Record<StepState, { title: string; tone: "forest" | "amber" | "neutral" }> = {
  not_started: { title: "Not started", tone: "neutral" },
  unknown: { title: "Outcome unknown", tone: "amber" },
  held: { title: "On hold", tone: "amber" },
  ack_pending: { title: "Worker response retained; acknowledgment pending", tone: "amber" },
  succeeded: { title: "Worker reports success", tone: "forest" },
  failed: { title: "Worker reports failure", tone: "amber" },
  inconsistent: { title: "Doesn't match the Hub's records", tone: "amber" },
};

export const reasonText: Record<StepReason, string> = {
  inspect_required: "The worker needs an inspection before it can say what happened.",
  paused: "The pilot was paused.",
  target_busy: "The staging target was busy with other work.",
  stale_fence: "The request was superseded by newer work.",
  identity_conflict: "The worker found a container that isn't the one the Hub expected.",
  manifest_mismatch: "The deployment manifest didn't match the approved one.",
  policy_mismatch: "The container settings didn't match the approved policy.",
  image_unavailable: "The image wasn't available on the staging machine.",
  daemon_unavailable: "The container engine wasn't reachable.",
  container_conflict: "Another container was in the way.",
  evidence_mismatch: "The worker's evidence didn't match what it ran.",
  health_failed: "The health check failed.",
  effect_failed: "The step didn't take effect.",
  recovery_required: "Earlier work needs recovery before anything else runs.",
};

export const runStatusText: Record<RunState, string> = {
  pending: "Waiting to start", running: "Running", succeeded: "Finished", failed: "Failed",
  rolled_back: "Rolled back", held: "On hold",
};
