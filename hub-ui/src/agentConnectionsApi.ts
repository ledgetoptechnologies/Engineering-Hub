/**
 * Owner view of revocable agent connections (staging). Installations pair with a public code and
 * fingerprint, never a secret: this module has no field that could hold an installation or session
 * credential, and parsers reject any response carrying extra fields.
 */
import { HubApiError, isMissingRoute } from "./api";

const base = "/api/hub/v1/agent-connections";
export const FAMILIES = ["codex", "claude", "hermes"] as const;
export const INSTALLATION_STATES = ["pending", "active", "revoked", "expired"] as const;
export const SESSION_STATES = ["active", "revoked", "expired"] as const;
export type Family = typeof FAMILIES[number];
export type InstallationState = typeof INSTALLATION_STATES[number];
export type SessionState = typeof SESSION_STATES[number];
/** Approved installations start with one day; the contract bounds this server side. */
export const APPROVAL_LIFETIME_SECONDS = 86400;
export const NAME_MAX = 100;

export interface Capabilities { enabled: true; environment: "staging"; owner: boolean }
export interface Installation {
  id: string; family: Family; name: string; host_label: string; state: InstallationState; reason: string | null;
  created_at: string; approved_at: string | null; approved_by: string | null; revoked_at: string | null; revoked_by: string | null;
  expires_at: string | null; revision: number; pairing_code: string | null; key_fingerprint: string;
}
export interface Session {
  id: string; installation_id: string; label: string; issued_at: string; expires_at: string;
  last_used_at: string | null; state: SessionState; revision: number;
}
export interface AuditEvent {
  id: number; at: string; actor: string; action: string; installation_id: string | null; session_id: string | null; message_id: string | null;
}
export interface AgentConnectionsClient {
  capabilities(signal: AbortSignal): Promise<Capabilities>;
  installations(signal: AbortSignal): Promise<Installation[]>;
  sessions(installationId: string, signal: AbortSignal): Promise<Session[]>;
  audit(signal: AbortSignal): Promise<AuditEvent[]>;
  approve(id: string, expectedRevision: number, csrf: string): Promise<Installation>;
  rename(id: string, expectedRevision: number, name: string, csrf: string): Promise<Installation>;
  revokeInstallation(id: string, expectedRevision: number, csrf: string): Promise<Installation>;
  revokeSession(id: string, expectedRevision: number, csrf: string): Promise<Session>;
}

function invalid(): never { throw new HubApiError(502, "invalid_agent_connections_response"); }
function record(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  const row = value as Record<string, unknown>;
  if (Object.keys(row).sort().join() !== [...keys].sort().join()) return invalid();
  return row;
}
const idPattern = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const codePattern = /^[a-z][a-z0-9_]{0,63}$/;
function id(value: unknown): string { return typeof value === "string" && idPattern.test(value) ? value : invalid(); }
function nullable<T>(value: unknown, parse: (value: unknown) => T): T | null { return value === null ? null : parse(value); }
/** One line of printable text, no control characters. */
function line(max: number, allowEmpty = false) {
  return (value: unknown): string => typeof value === "string" && value.length <= max && (allowEmpty || value.trim() !== "")
    && !/[\u0000-\u001f\u007f]/.test(value) ? value : invalid();
}
/** Public pairing values: a six-digit code, and the first 16 hex digits of the credential's SHA-256 (not a secret). */
const pairingCode = (value: unknown): string => typeof value === "string" && /^[0-9]{6}$/.test(value) ? value : invalid();
const fingerprint = (value: unknown): string => typeof value === "string" && /^[0-9a-f]{16}$/.test(value) ? value : invalid();
const actionPattern = /^[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){0,2}$/;
function time(value: unknown): string {
  return typeof value === "string" && value.length <= 40 && Number.isFinite(Date.parse(value)) ? value : invalid();
}
function revision(value: unknown): number { return Number.isSafeInteger(value) && (value as number) >= 1 ? value as number : invalid(); }
function oneOf<T extends string>(values: readonly T[]) {
  return (value: unknown): T => values.includes(value as T) ? value as T : invalid();
}
const code = (value: unknown): string => typeof value === "string" && codePattern.test(value) ? value : invalid();

export function parseCapabilities(value: unknown): Capabilities {
  const row = record(value, ["enabled", "environment", "owner"]);
  // Anything but an enabled staging service reads as unavailable, never as a live one.
  if (row.enabled !== true || row.environment !== "staging" || typeof row.owner !== "boolean") return invalid();
  return { enabled: true, environment: "staging", owner: row.owner };
}
const INSTALLATION_KEYS = ["id", "family", "name", "host_label", "state", "reason", "created_at", "approved_at", "approved_by",
  "revoked_at", "revoked_by", "expires_at", "revision", "pairing_code", "key_fingerprint"] as const;
export function parseInstallation(value: unknown): Installation {
  const row = record(value, INSTALLATION_KEYS);
  const item: Installation = {
    id: id(row.id), family: oneOf(FAMILIES)(row.family), name: line(NAME_MAX)(row.name), host_label: line(NAME_MAX)(row.host_label),
    state: oneOf(INSTALLATION_STATES)(row.state), reason: nullable(row.reason, code), created_at: time(row.created_at),
    approved_at: nullable(row.approved_at, time), approved_by: nullable(row.approved_by, line(128)),
    revoked_at: nullable(row.revoked_at, time), revoked_by: nullable(row.revoked_by, line(128)),
    expires_at: nullable(row.expires_at, time), revision: revision(row.revision),
    pairing_code: nullable(row.pairing_code, pairingCode), key_fingerprint: fingerprint(row.key_fingerprint),
  };
  // A pending request can only be approved against what the installation printed; the code exists only then.
  if ((item.state === "pending") !== (item.pairing_code !== null) || (item.state === "pending" && !item.expires_at)) return invalid();
  if (item.state === "revoked" && !item.revoked_at) return invalid();
  return item;
}
export function parseSession(value: unknown): Session {
  const row = record(value, ["id", "installation_id", "label", "issued_at", "expires_at", "last_used_at", "state", "revision"]);
  return { id: id(row.id), installation_id: id(row.installation_id), label: line(NAME_MAX, true)(row.label),
    issued_at: time(row.issued_at), expires_at: time(row.expires_at), last_used_at: nullable(row.last_used_at, time),
    state: oneOf(SESSION_STATES)(row.state), revision: revision(row.revision) };
}
export function parseAuditEvent(value: unknown): AuditEvent {
  const row = record(value, ["id", "at", "actor", "action", "installation_id", "session_id", "message_id"]);
  if (!Number.isSafeInteger(row.id) || (row.id as number) < 1) return invalid();
  return { id: row.id as number, at: time(row.at), actor: line(128)(row.actor),
    action: typeof row.action === "string" && actionPattern.test(row.action) ? row.action : invalid(),
    installation_id: nullable(row.installation_id, id), session_id: nullable(row.session_id, id), message_id: nullable(row.message_id, id) };
}
function list<T>(value: unknown, key: string, parse: (value: unknown) => T, max: number, unique?: (item: T) => string): T[] {
  const row = record(value, [key]);
  const items = row[key];
  if (!Array.isArray(items) || items.length > max) return invalid();
  const parsed = items.map(parse);
  if (unique && new Set(parsed.map(unique)).size !== parsed.length) return invalid();
  return parsed;
}
export const parseInstallations = (value: unknown) => list(value, "installations", parseInstallation, 500, item => item.id);
export const parseSessions = (value: unknown, installationId: string) => {
  const sessions = list(value, "sessions", parseSession, 64, item => item.id);
  return sessions.some(item => item.installation_id !== installationId) ? invalid() : sessions;
};
export const parseAudit = (value: unknown) => list(value, "events", parseAuditEvent, 100, item => String(item.id));

async function call(path: string, init: RequestInit, fetchImpl: typeof fetch): Promise<unknown> {
  let response: Response;
  try {
    response = await fetchImpl.call(globalThis, base + path, { credentials: "same-origin", redirect: "error", cache: "no-store", ...init });
  } catch (error) {
    if ((init.signal as AbortSignal | undefined)?.aborted) throw error;
    throw new HubApiError(0, "network_unavailable");
  }
  if (!response.ok) {
    if (await isMissingRoute(response)) throw new HubApiError(404, "agent_connections_unavailable");
    let failure = response.status === 401 ? "human_session_required" : response.status === 403 ? "agent_connections_forbidden"
      : response.status === 409 ? "revision_conflict" : "agent_connections_request_failed";
    if (response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
      try {
        const detail = (await response.clone().json() as { detail?: { code?: unknown } })?.detail;
        if (typeof detail?.code === "string" && codePattern.test(detail.code)) failure = detail.code;
      } catch { /* Never reflect raw server text. */ }
    }
    throw new HubApiError(response.status, failure);
  }
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return invalid();
  try { return await response.json(); } catch { return invalid(); }
}
function write(method: string, body: unknown, csrf: string): RequestInit {
  if (!csrf) throw new HubApiError(403, "csrf_required");
  return { method, body: JSON.stringify(body), signal: AbortSignal.timeout(15_000),
    headers: { Accept: "application/json", "Content-Type": "application/json", "X-Hub-CSRF": csrf } };
}
const read = (signal: AbortSignal): RequestInit => ({ method: "GET", headers: { Accept: "application/json" }, signal });
const at = (value: string) => encodeURIComponent(id(value));

export function httpAgentConnections(fetchImpl: typeof fetch = globalThis.fetch): AgentConnectionsClient {
  return {
    capabilities: async signal => parseCapabilities(await call("/capabilities", read(signal), fetchImpl)),
    installations: async signal => parseInstallations(await call("/installations", read(signal), fetchImpl)),
    sessions: async (installationId, signal) => parseSessions(await call(`/installations/${at(installationId)}/sessions`, read(signal), fetchImpl), installationId),
    audit: async signal => parseAudit(await call("/audit", read(signal), fetchImpl)),
    approve: async (installationId, expected_revision, csrf) => parseInstallation(await call(`/installations/${at(installationId)}/approve`,
      write("POST", { expected_revision, lifetime_seconds: APPROVAL_LIFETIME_SECONDS }, csrf), fetchImpl)),
    rename: async (installationId, expected_revision, name, csrf) => parseInstallation(await call(`/installations/${at(installationId)}`,
      write("PATCH", { expected_revision, name: line(NAME_MAX)(name.trim()) }, csrf), fetchImpl)),
    revokeInstallation: async (installationId, expected_revision, csrf) => parseInstallation(await call(`/installations/${at(installationId)}/revoke`,
      write("POST", { expected_revision }, csrf), fetchImpl)),
    revokeSession: async (sessionId, expected_revision, csrf) => parseSession(await call(`/sessions/${at(sessionId)}/revoke`,
      write("POST", { expected_revision }, csrf), fetchImpl)),
  };
}

export const FAMILY_NAME: Record<Family, string> = { codex: "Codex", claude: "Claude", hermes: "Hermes" };
export const STATE_LABEL: Record<InstallationState, string> = {
  pending: "Waiting for approval", active: "Active", revoked: "Revoked", expired: "Expired" };
export const STATE_TONE: Record<InstallationState, string> = { pending: "amber", active: "forest", revoked: "neutral", expired: "neutral" };
const REASON_TEXT: Record<string, string> = {
  // The badge already says a pending request is waiting, so its reason adds nothing.
  awaiting_owner_approval: "", owner_revoked: "Revoked by the owner.", pairing_expired: "The pairing request ran out before approval.",
  installation_expired: "Its approval ran out.",
};
/** Fixed codes only; an unknown code is shown as the code, never as server text. */
export const reasonText = (reason: string | null) => reason === null ? "" : REASON_TEXT[reason] ?? `Reason: ${reason.replaceAll("_", " ")}.`;
const ACTION_TEXT: Record<string, string> = {
  "enrollment.requested": "asked to pair", "installation.approved": "approved the installation",
  "installation.renamed": "renamed the installation", "installation.revoked": "revoked the installation",
  "session.issued": "started a session", "session.revoked": "revoked a session", "message.sent": "sent a message",
  "message.claimed": "claimed a message", "message.claim_renewed": "renewed a message claim", "message.acknowledged": "acknowledged a message",
};
export const actionText = (action: string) => ACTION_TEXT[action] ?? action.replace(/[._]/g, " ");

/** Plain wording for a failed change. No answer never claims the change did or didn't happen. */
export function writeFailureText(error: unknown): string {
  const failure = error instanceof HubApiError ? error.code : "";
  const status = error instanceof HubApiError ? error.status : 0;
  if (status === 0 || error instanceof DOMException) return "No answer from the Hub, so this may or may not have happened. Refresh to check before trying again.";
  // A specific code wins over its status: an expired pairing may also arrive as 409.
  if (failure === "pairing_expired" || status === 410) return "This pairing request ran out. Start pairing again on that computer.";
  if (failure === "enrollment_not_pending") return "This request is no longer waiting for approval. Refresh to see what happened to it.";
  if (status === 429) return "The Hub's limit for this was reached. Revoke something unused, or wait, then try again.";
  if (failure === "revision_conflict" || status === 409) return "This changed since you loaded it. Refresh to see the latest, then try again.";
  if (status === 404) return "The Hub no longer has this record. Refresh to see the current list.";
  if (failure.startsWith("invalid_") || status === 422) return "The Hub didn't accept this. Check the details and try again.";
  if (status === 403) return "Only the staging owner can change agent connections.";
  return "The change couldn't be confirmed. Refresh to check before trying again.";
}

/** Clearly fake demo records: documentation-style ids, no real host or credential shapes. */
export function demoAgentConnections(now = Date.now()): { installations: Installation[]; sessions: Record<string, Session[]>; audit: AuditEvent[] } {
  const iso = (offset: number) => new Date(now + offset).toISOString();
  const installation = (fields: Partial<Installation> & Pick<Installation, "id" | "family" | "name" | "host_label" | "state">): Installation => ({
    reason: null, created_at: iso(-3_600_000), approved_at: null, approved_by: null, revoked_at: null, revoked_by: null,
    expires_at: null, revision: 1, pairing_code: null, key_fingerprint: "0000000000000000", ...fields });
  return {
    installations: [
      installation({ id: "demo-claude-laptop", family: "claude", name: "Claude on the laptop", host_label: "demo-laptop", state: "active",
        approved_at: iso(-3_000_000), approved_by: "Demo owner", expires_at: iso(80_000_000), revision: 2 }),
      installation({ id: "demo-claude-desk", family: "claude", name: "Claude on the desk PC", host_label: "demo-desk", state: "pending",
        created_at: iso(-60_000), reason: "awaiting_owner_approval", expires_at: iso(540_000), pairing_code: "000000", key_fingerprint: "00000000deadbeef" }),
      installation({ id: "demo-codex-old", family: "codex", name: "Codex test box", host_label: "demo-old", state: "revoked",
        reason: "owner_revoked", approved_at: iso(-90_000_000), approved_by: "Demo owner", revoked_at: iso(-7_200_000), revoked_by: "Demo owner", revision: 3 }),
    ],
    sessions: { "demo-claude-laptop": [
      { id: "demo-session-1", installation_id: "demo-claude-laptop", label: "main chat", issued_at: iso(-300_000), expires_at: iso(600_000),
        last_used_at: iso(-20_000), state: "active", revision: 1 },
      { id: "demo-session-0", installation_id: "demo-claude-laptop", label: "", issued_at: iso(-2_000_000), expires_at: iso(-1_100_000),
        last_used_at: iso(-1_200_000), state: "expired", revision: 1 },
    ] },
    audit: [
      { id: 2, at: iso(-60_000), actor: "claude", action: "enrollment.requested", installation_id: "demo-claude-desk", session_id: null, message_id: null },
      { id: 1, at: iso(-300_000), actor: "claude", action: "session.issued", installation_id: "demo-claude-laptop", session_id: "demo-session-1", message_id: null },
    ],
  };
}
