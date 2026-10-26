/**
 * Whether the shared staging queue (deployments plus staging builds) is open, for the Hub owner.
 * Status is read on load and on Refresh only. Recovery is retried only when the owner asks, and it
 * only rechecks the original work; a failed or interrupted request never proves anything ran or failed.
 */
import { HubApiError, isMissingRoute } from "./api";

export const SHARED_STATES = ["not_started", "pending", "recovering", "recovery_required", "active", "stopping", "stopped", "shutdown_failed"] as const;
export type SharedState = typeof SHARED_STATES[number];
/** Each state carries exactly this reason; anything else is a contract change and is refused. */
const REASONS: Record<SharedState, string | null> = {
  not_started: null, pending: "operator_acceptance_required", recovering: "original_operations_pending",
  recovery_required: "original_operations_unverified", active: null, stopping: null, stopped: null,
  shutdown_failed: "owned_drain_failed",
};
export interface SharedStagingStatus { state: SharedState; reason: string | null }

const statusPath = "/api/hub/v1/staging/shared/status";
const recoverPath = "/api/hub/v1/staging/shared/recover";
/** Recovery can take a long time; past this the browser stops waiting, but the Hub carries on. */
export const RECOVER_WAIT_MS = 120_000;

function invalid(): never { throw new HubApiError(502, "invalid_shared_staging_status"); }
export function parseSharedStagingStatus(value: unknown): SharedStagingStatus {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  const row = value as Record<string, unknown>;
  if (Object.keys(row).sort().join() !== "reason,state") return invalid();
  if (typeof row.state !== "string" || !(SHARED_STATES as readonly string[]).includes(row.state)) return invalid();
  const state = row.state as SharedState;
  if (row.reason !== REASONS[state]) return invalid();
  return { state, reason: REASONS[state] };
}

async function failure(response: Response): Promise<never> {
  if (await isMissingRoute(response)) throw new HubApiError(404, "shared_staging_unsupported");
  let code = response.status === 401 ? "human_session_required" : response.status === 403 ? "staging_forbidden"
    : response.status === 503 ? "shared_staging_unavailable" : "shared_staging_request_failed";
  if (response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
    try {
      const detail = (await response.clone().json() as { detail?: { code?: unknown } })?.detail;
      if (typeof detail?.code === "string" && /^[a-z][a-z0-9_.:-]{0,63}$/.test(detail.code)) code = detail.code;
    } catch { /* Never reflect proxy HTML or raw server text. */ }
  }
  throw new HubApiError(response.status, code);
}

async function send(path: string, init: RequestInit, fetchImpl: typeof fetch): Promise<SharedStagingStatus> {
  let response: Response;
  try {
    response = await fetchImpl.call(globalThis, path, { credentials: "same-origin", redirect: "error", cache: "no-store", ...init });
  } catch (error) {
    // A caller's own abort stays an abort; anything else is "no answer", not a failure of the work.
    if (init.signal && (init.signal as AbortSignal).aborted && (error as Error)?.name === "AbortError") throw error;
    throw new HubApiError(0, "network_unavailable");
  }
  if (!response.ok) return failure(response);
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return invalid();
  let body: unknown;
  try { body = await response.json(); } catch { return invalid(); }
  return parseSharedStagingStatus(body);
}

export function readSharedStaging(signal: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch): Promise<SharedStagingStatus> {
  return send(statusPath, { method: "GET", headers: { Accept: "application/json" }, signal }, fetchImpl);
}

export function recoverSharedStaging(csrf: string, fetchImpl: typeof fetch = globalThis.fetch,
  signal: AbortSignal = AbortSignal.timeout(RECOVER_WAIT_MS)): Promise<SharedStagingStatus> {
  if (!csrf) return Promise.reject(new HubApiError(403, "csrf_required"));
  return send(recoverPath, { method: "POST", signal, body: "{}",
    headers: { Accept: "application/json", "Content-Type": "application/json", "X-Hub-CSRF": csrf } }, fetchImpl);
}

/** Plain wording per state. "Open" means jobs may be taken, never that anything is deployed, healthy or approved. */
export const stateText: Record<SharedState, { title: string; line: string; tone: "forest" | "amber" | "neutral" }> = {
  not_started: { title: "Not started", tone: "neutral",
    line: "The shared staging queue hasn't started yet, so no staging jobs or deployments are being taken." },
  pending: { title: "Waiting for the session check", tone: "amber",
    line: "Nothing is taken until you accept the staging session check. Accepting it then rechecks any work that was under way before the Hub restarted." },
  recovering: { title: "Checking earlier work", tone: "amber",
    line: "Checking the work that was under way before the Hub restarted. New jobs wait until that's done." },
  recovery_required: { title: "Earlier work not confirmed", tone: "amber",
    line: "Some work from before the restart couldn't be confirmed, so new jobs stay stopped. Retrying checks only that same work again; it never starts it again." },
  active: { title: "Open", tone: "forest",
    line: "The queue is taking staging jobs and deployments, one at a time on the staging PC. This doesn't mean any app is deployed or healthy." },
  stopping: { title: "Stopping", tone: "amber",
    line: "No new jobs are taken. Work already under way is being allowed to finish." },
  stopped: { title: "Stopped", tone: "neutral", line: "The shared staging queue has stopped. No jobs are being taken." },
  shutdown_failed: { title: "Didn't stop cleanly", tone: "amber",
    line: "The queue stopped taking jobs, but work that was under way couldn't be confirmed as finished. Check it before the next start." },
};

/** Wording for a recovery request that didn't return a status. Never claims the work failed or ran. */
export function recoverFailureText(error: unknown): string {
  const code = error instanceof HubApiError ? error.code : "";
  const status = error instanceof HubApiError ? error.status : 0;
  if (status === 0 || error instanceof DOMException)
    return "No answer yet. Recovery may still be running on the Hub. Use Refresh to see where it got to; don't retry until then.";
  if (code === "shared_staging_recovery_required")
    return "Recovery didn't confirm all the earlier work. Use Refresh to see the current state.";
  if (code === "shared_staging_recovery_already_attempted")
    return "Recovery has already run for this start. Use Refresh to see the current state.";
  if (code === "shared_staging_lifecycle_changed")
    return "The Hub's staging setup changed or is stopping, so nothing was retried. Use Refresh to see the current state.";
  return "The retry couldn't be confirmed. Use Refresh to see the current state before trying again.";
}
