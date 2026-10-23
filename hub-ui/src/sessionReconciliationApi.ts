/** Human-only session intent. Evidence and authority are supplied by the server, never this client. */
import { HubApiError, isMissingRoute } from "./api";

const endpoint = "/api/hub/v1/staging/session-reconciliation";
export interface SessionReconciliationSnapshot {
  request_id: string;
  revision: number;
  state: "pending" | "accepted";
  evidence: string;
  report_digest: string;
  expires_in_seconds: number;
  execution_authorization: false;
}
export interface SessionReconciliationDecision {
  request_id: string;
  revision: number;
  state: "accepted" | "declined";
  execution_authorization: false;
}
const validId = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{64}$/.exec(value)?.[0] === value;
const validRevision = (value: unknown): value is number => Number.isInteger(value) && (value as number) >= 1 && (value as number) <= 2 ** 31 - 1;
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new HubApiError(502, "invalid_session_response");
  return value as Record<string, unknown>;
}
export function parseSessionReconciliation(value: unknown): SessionReconciliationSnapshot {
  const row = record(value);
  if (!validId(row.request_id) || !validRevision(row.revision) || !["pending", "accepted"].includes(row.state as string)
      || typeof row.evidence !== "string" || !row.evidence || new TextEncoder().encode(row.evidence).length > 8192 || row.evidence.includes("\0")
      || typeof row.report_digest !== "string" || /^sha256:[0-9a-f]{64}$/.exec(row.report_digest)?.[0] !== row.report_digest
      || !Number.isInteger(row.expires_in_seconds) || (row.expires_in_seconds as number) < 0 || (row.expires_in_seconds as number) > 3600
      || row.execution_authorization !== false) throw new HubApiError(502, "invalid_session_response");
  return { request_id: row.request_id, revision: row.revision, state: row.state as SessionReconciliationSnapshot["state"],
    evidence: row.evidence, report_digest: row.report_digest, expires_in_seconds: row.expires_in_seconds as number, execution_authorization: false };
}
async function request(body: unknown | undefined, csrf: string | undefined, fetchImpl: typeof fetch): Promise<unknown> {
  if (body !== undefined && !csrf) throw new HubApiError(403, "csrf_required");
  let response: Response;
  try {
    response = await fetchImpl.call(globalThis, endpoint, { method: body === undefined ? "GET" : "POST", credentials: "same-origin",
      redirect: "error", cache: "no-store", signal: AbortSignal.timeout(15000), headers: { Accept: "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json", "X-Hub-CSRF": csrf! }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  } catch { throw new HubApiError(0, "network_unavailable"); }
  if (!response.ok && await isMissingRoute(response)) throw new HubApiError(404, "session_not_configured");
  if (!response.ok) throw new HubApiError(response.status, response.status === 403 ? "operator_required"
    : response.status === 404 ? "session_not_configured" : response.status === 410 ? "session_expired"
      : response.status === 409 ? "session_refresh_required" : response.status === 401 ? "sign_in_required" : "session_request_failed");
  try { return await response.json(); } catch { throw new HubApiError(502, "invalid_session_response"); }
}
export async function readSessionReconciliation(fetchImpl: typeof fetch = globalThis.fetch): Promise<SessionReconciliationSnapshot> {
  return parseSessionReconciliation(await request(undefined, undefined, fetchImpl));
}
export async function decideSessionReconciliation(input: { request_id: string; expected_revision: number; action: "accept" | "decline" },
  csrf: string, fetchImpl: typeof fetch = globalThis.fetch): Promise<SessionReconciliationDecision> {
  if (!validId(input.request_id) || !validRevision(input.expected_revision) || !["accept", "decline"].includes(input.action))
    throw new HubApiError(422, "invalid_session_intent");
  // Pick only opaque identity, CAS revision and intent even if a caller passes extra properties.
  const payload = { request_id: input.request_id, expected_revision: input.expected_revision, action: input.action };
  const row = record(await request(payload, csrf, fetchImpl));
  if (row.request_id !== input.request_id || row.revision !== input.expected_revision + 1 || row.execution_authorization !== false
      || row.state !== (input.action === "accept" ? "accepted" : "declined")) throw new HubApiError(502, "invalid_session_response");
  return { request_id: input.request_id, revision: row.revision as number, state: row.state as SessionReconciliationDecision["state"], execution_authorization: false };
}
