/**
 * Owner view of what a paired installation may do in the Hub, separate from whether it may connect.
 * Rights are explicit projects × capabilities with an expiry; the server computes what is in effect.
 * Task recovery hands a claim stranded on an ended session to a live session of the same
 * installation, owner only. Parsers accept exactly the backend's public fields.
 */
import { HubApiError } from "./api";
import { at, ownerCall, read, write } from "./agentConnectionsApi";
import type { Agent } from "./types";

export const GRANT_STATES = ["absent", "inactive", "revoked", "expired", "active"] as const;
export const GRANT_REASONS = ["grant_absent", "connection_inactive", "owner_revoked", "grant_expired", "no_active_projects", "authorized"] as const;
export type GrantState = typeof GRANT_STATES[number];
export type GrantReason = typeof GRANT_REASONS[number];
/** The backend's bounds: 1 minute to 7 days, and never past the connection's own approval. */
export const GRANT_LIFETIMES = [{ seconds: 3600, label: "1 hour" }, { seconds: 86400, label: "1 day" }, { seconds: 604800, label: "7 days" }] as const;
export const MAX_GRANT_PROJECTS = 32;
export const RECOVERY_REASON_MAX = 500;
export const RECOVERY_HISTORY_MAX = 128;
export const CLAIMS_PAGE = 50;

export interface HubCatalog { capabilities: string[]; taskRecovery: boolean }
export interface Effective { state: GrantState; reason: GrantReason; projects: string[]; capabilities: string[]; expires_at: string | null }
export interface Grant {
  installation_id: string; revision: number; state: GrantState; projects: string[]; capabilities: string[];
  expires_at: string | null; approved_by: string | null; approved_at: string | null; effective: Effective;
}
/** family is the trusted family behind an installation reviewer; legacy reviews have none. */
export interface BridgeReview { actor: string; commit: string; verdict: "approve" | "reject"; family: Agent | null }
export interface BridgeTask {
  id: string; project: string; title: string; status: "assigned" | "claimed" | "in_review" | "verified" | "withdrawn";
  owner: string | null; owner_family: Agent | null; owner_session_id: string | null; revision: number;
  candidate_commit: string | null; candidate_family: Agent | null; review: BridgeReview | null; repair_count: number;
  scope: "staging" | null; proposed_by: string | null; proposed_family: Agent | null; updated_at: string;
  withdrawn_by: string | null; withdrawn_family: Agent | null;
}
export interface Claim {
  /** null with claim_state "unknown": the claim's session can't be identified, so it is shown read-only. */
  task: BridgeTask; claim_session_id: string | null; claim_state: "active" | "revoked" | "expired" | "unknown";
  recovery: { state: "recoverable" | "blocked"; reason: string; target_session_ids: string[] };
}
export interface ClaimsPage { installation_id: string; items: Claim[]; next_after: string | null }
export interface Recovery {
  id: string; task_id: string; installation_id: string; family: Agent; actor: string; at: string; reason: string;
  prior_session_id: string; target_session_id: string; prior_revision: number; resulting_revision: number;
  prior_candidate_commit: string | null; prior_review: BridgeReview | null;
}
export interface RecoveryResult { task: BridgeTask; recovery: Recovery; replayed: boolean }
export interface RecoveryHistory { task: BridgeTask; recoveries: Recovery[] }
export interface RecoveryRequest { expected_revision: number; target_session_id: string; reason: string; client_request_id: string }

export interface HubGrantsClient {
  catalog(signal: AbortSignal): Promise<HubCatalog>;
  grant(installationId: string, signal: AbortSignal): Promise<Grant>;
  saveGrant(installationId: string, body: { expected_revision: number; projects: string[]; capabilities: string[]; lifetime_seconds: number }, csrf: string): Promise<Grant>;
  revokeGrant(installationId: string, expectedRevision: number, csrf: string): Promise<Grant>;
  claims(installationId: string, after: string | null, signal: AbortSignal): Promise<ClaimsPage>;
  recover(taskId: string, body: RecoveryRequest, csrf: string): Promise<RecoveryResult>;
  history(taskId: string, signal: AbortSignal): Promise<RecoveryHistory>;
}

function invalid(): never { throw new HubApiError(502, "invalid_agent_connections_response"); }
function record(value: unknown, required: readonly string[], optional: readonly string[] = []): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  const row = value as Record<string, unknown>, keys = Object.keys(row);
  if (required.some(key => !(key in row)) || keys.some(key => !required.includes(key) && !optional.includes(key))) return invalid();
  return row;
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const projectId = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const capabilityId = /^[a-z][a-z_]{0,31}\.[a-z][a-z_]{0,31}$/;
const commit = /^[0-9a-f]{40}$/;
const FAMILY = ["codex", "claude", "hermes"];
const actorPattern = /^(?:codex|claude|hermes|installation:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|human:[\x21-\x7e]{1,120})$/;
const str = (pattern: RegExp) => (value: unknown): string => typeof value === "string" && pattern.test(value) ? value : invalid();
const nullable = <T,>(value: unknown, parse: (value: unknown) => T): T | null => value === null ? null : parse(value);
const optional = <T,>(row: Record<string, unknown>, key: string, parse: (value: unknown) => T): T | null => key in row ? nullable(row[key], parse) : null;
const time = (value: unknown): string => typeof value === "string" && value.length <= 40 && Number.isFinite(Date.parse(value)) ? value : invalid();
const count = (min: number) => (value: unknown): number => Number.isSafeInteger(value) && (value as number) >= min ? value as number : invalid();
const family = (value: unknown): Agent => FAMILY.includes(value as string) ? value as Agent : invalid();
const oneOf = <T extends string>(values: readonly T[]) => (value: unknown): T => values.includes(value as T) ? value as T : invalid();
/** One line or paragraph of bounded text, counted in Unicode characters like the backend. */
const text = (max: number) => (value: unknown): string => typeof value === "string" && value.trim() !== "" && value.length <= 2 * max
  && [...value].length <= max && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value) ? value : invalid();
function list<T>(value: unknown, parse: (value: unknown) => T, max: number, unique = true): T[] {
  if (!Array.isArray(value) || value.length > max) return invalid();
  const items = value.map(parse);
  if (unique && new Set(items.map(item => JSON.stringify(item))).size !== items.length) return invalid();
  return items;
}

export function parseCatalog(value: unknown): HubCatalog {
  const row = record(value, ["enabled", "capabilities", "features"]);
  const features = record(row.features, ["task_recovery"]);
  if (row.enabled !== true || typeof features.task_recovery !== "boolean") return invalid();
  return { capabilities: list(row.capabilities, str(capabilityId), 64), taskRecovery: features.task_recovery };
}
function parseEffective(value: unknown): Effective {
  const row = record(value, ["state", "reason", "projects", "capabilities", "expires_at"]);
  const effective = { state: oneOf(GRANT_STATES)(row.state), reason: oneOf(GRANT_REASONS)(row.reason),
    projects: list(row.projects, str(projectId), MAX_GRANT_PROJECTS), capabilities: list(row.capabilities, str(capabilityId), 64),
    expires_at: nullable(row.expires_at, time) };
  // Rights exist only while authorized; anything else carries none.
  if ((effective.reason === "authorized") !== (effective.projects.length > 0) || (!effective.projects.length && effective.capabilities.length)) return invalid();
  return effective;
}
export function parseGrant(value: unknown): Grant {
  const row = record(value, ["installation_id", "revision", "state", "projects", "capabilities", "expires_at", "approved_by", "approved_at", "effective"]);
  return { installation_id: str(uuid)(row.installation_id), revision: count(0)(row.revision), state: oneOf(GRANT_STATES)(row.state),
    projects: list(row.projects, str(projectId), MAX_GRANT_PROJECTS), capabilities: list(row.capabilities, str(capabilityId), 64),
    expires_at: nullable(row.expires_at, time), approved_by: nullable(row.approved_by, str(actorPattern)),
    approved_at: nullable(row.approved_at, time), effective: parseEffective(row.effective) };
}
function parseReview(value: unknown): BridgeReview {
  const row = record(value, ["actor", "commit", "verdict"], ["family"]);
  return { actor: str(actorPattern)(row.actor), commit: str(commit)(row.commit), verdict: oneOf(["approve", "reject"] as const)(row.verdict),
    family: optional(row, "family", family) };
}
/** Always present on a task (TaskDTO's required fields). */
const TASK_KEYS = ["id", "project", "title", "status", "owner", "revision", "candidate_commit", "review", "repair_count", "updated_at"] as const;
/** Present only on some tasks: proposals, installation provenance, context and withdrawal records. */
const TASK_OPTIONAL = ["scope", "proposed_by", "proposed_family", "owner_session_id", "owner_family", "candidate_family", "state_since",
  "details", "links", "withdrawn_reason", "superseded_by", "withdrawn_by", "withdrawn_at", "withdrawn_family", "withdrawn_session_id"] as const;
function parseLinks(value: unknown): { label: string; url: string }[] {
  return list(value, item => { const row = record(item, ["label", "url"]); return { label: text(200)(row.label), url: text(2048)(row.url) }; }, 20, false);
}
export function parseBridgeTask(value: unknown): BridgeTask {
  const row = record(value, TASK_KEYS, TASK_OPTIONAL);
  // Context fields are checked for shape but not kept: this page doesn't show them.
  optional(row, "details", text(20000)); optional(row, "links", parseLinks); optional(row, "state_since", time);
  optional(row, "withdrawn_reason", text(500)); optional(row, "superseded_by", str(uuid)); optional(row, "withdrawn_at", time);
  optional(row, "withdrawn_session_id", str(uuid));
  return { id: str(uuid)(row.id), project: str(projectId)(row.project), title: text(200)(row.title),
    status: oneOf(["assigned", "claimed", "in_review", "verified", "withdrawn"] as const)(row.status),
    owner: nullable(row.owner, str(actorPattern)), owner_family: optional(row, "owner_family", family),
    owner_session_id: optional(row, "owner_session_id", str(uuid)), revision: count(1)(row.revision),
    candidate_commit: nullable(row.candidate_commit, str(commit)), candidate_family: optional(row, "candidate_family", family),
    review: nullable(row.review, parseReview), repair_count: count(0)(row.repair_count),
    scope: optional(row, "scope", oneOf(["staging"] as const)), proposed_by: optional(row, "proposed_by", str(actorPattern)),
    proposed_family: optional(row, "proposed_family", family), updated_at: time(row.updated_at),
    withdrawn_by: optional(row, "withdrawn_by", str(actorPattern)), withdrawn_family: optional(row, "withdrawn_family", family) };
}
function parseClaim(value: unknown): Claim {
  const row = record(value, ["task", "claim_session_id", "claim_state", "recovery"]);
  const recovery = record(row.recovery, ["state", "reason", "target_session_ids"]);
  const claim: Claim = { task: parseBridgeTask(row.task), claim_session_id: nullable(row.claim_session_id, str(uuid)),
    claim_state: oneOf(["active", "revoked", "expired", "unknown"] as const)(row.claim_state),
    recovery: { state: oneOf(["recoverable", "blocked"] as const)(recovery.state), reason: str(/^[a-z][a-z0-9_]{0,63}$/)(recovery.reason),
      target_session_ids: list(recovery.target_session_ids, str(uuid), 16) } };
  // A target is offered only when recovery is allowed, and never the session that holds the claim.
  if ((claim.recovery.state === "recoverable") !== (claim.recovery.target_session_ids.length > 0)
      || (claim.claim_session_id !== null && claim.recovery.target_session_ids.includes(claim.claim_session_id))
      || (claim.recovery.state === "recoverable" && (claim.claim_session_id === null || claim.claim_state === "unknown"))) return invalid();
  return claim;
}
export function parseClaims(value: unknown, installationId: string): ClaimsPage {
  const row = record(value, ["installation_id", "items", "next_after"]);
  const page = { installation_id: str(uuid)(row.installation_id), items: list(row.items, parseClaim, 100, false),
    next_after: nullable(row.next_after, str(uuid)) };
  if (page.installation_id !== installationId || new Set(page.items.map(item => item.task.id)).size !== page.items.length) return invalid();
  return page;
}
function parseRecovery(value: unknown): Recovery {
  const row = record(value, ["id", "task_id", "installation_id", "family", "actor", "at", "reason", "prior_session_id", "target_session_id",
    "prior_revision", "resulting_revision", "prior_candidate_commit", "prior_review"]);
  const item = { id: str(uuid)(row.id), task_id: str(uuid)(row.task_id), installation_id: str(uuid)(row.installation_id),
    family: family(row.family), actor: str(actorPattern)(row.actor), at: time(row.at), reason: text(RECOVERY_REASON_MAX)(row.reason),
    prior_session_id: str(uuid)(row.prior_session_id), target_session_id: str(uuid)(row.target_session_id),
    prior_revision: count(1)(row.prior_revision), resulting_revision: count(2)(row.resulting_revision),
    prior_candidate_commit: nullable(row.prior_candidate_commit, str(commit)), prior_review: nullable(row.prior_review, parseReview) };
  if (item.resulting_revision !== item.prior_revision + 1 || item.prior_session_id === item.target_session_id) return invalid();
  return item;
}
export function parseRecoveryResult(value: unknown, taskId: string): RecoveryResult {
  const row = record(value, ["task", "recovery", "replayed"]);
  if (typeof row.replayed !== "boolean") return invalid();
  const result = { task: parseBridgeTask(row.task), recovery: parseRecovery(row.recovery), replayed: row.replayed };
  if (result.task.id !== taskId || result.recovery.task_id !== taskId) return invalid();
  return result;
}
export function parseHistory(value: unknown, taskId: string): RecoveryHistory {
  const row = record(value, ["task", "recoveries"]);
  const history = { task: parseBridgeTask(row.task), recoveries: list(row.recoveries, parseRecovery, RECOVERY_HISTORY_MAX, false) };
  if (history.task.id !== taskId || history.recoveries.some(item => item.task_id !== taskId)
      || new Set(history.recoveries.map(item => item.id)).size !== history.recoveries.length) return invalid();
  return history;
}

const base = (installationId: string) => `/installations/${at(installationId)}`;
export function httpHubGrants(fetchImpl: typeof fetch = globalThis.fetch): HubGrantsClient {
  return {
    catalog: async signal => parseCatalog(await ownerCall("/hub-capabilities", read(signal), fetchImpl)),
    grant: async (installationId, signal) => parseGrant(await ownerCall(`${base(installationId)}/hub-grant`, read(signal), fetchImpl)),
    saveGrant: async (installationId, { expected_revision, projects, capabilities, lifetime_seconds }, csrf) => {
      if (!projects.length || !capabilities.length) throw new HubApiError(422, "invalid_hub_grant");
      return parseGrant(await ownerCall(`${base(installationId)}/hub-grant`,
        write("PUT", { expected_revision, projects, capabilities, lifetime_seconds }, csrf), fetchImpl));
    },
    revokeGrant: async (installationId, expected_revision, csrf) => parseGrant(await ownerCall(`${base(installationId)}/hub-grant/revoke`,
      write("POST", { expected_revision }, csrf), fetchImpl)),
    claims: async (installationId, after, signal) => parseClaims(await ownerCall(`${base(installationId)}/task-claims?limit=${CLAIMS_PAGE}${
      after ? `&after=${encodeURIComponent(str(uuid)(after))}` : ""}`, read(signal), fetchImpl), installationId),
    recover: async (taskId, { expected_revision, target_session_id, reason, client_request_id }, csrf) => {
      if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(client_request_id)) throw new HubApiError(422, "invalid_input");
      return parseRecoveryResult(await ownerCall(`/tasks/${at(taskId)}/recover-claim`,
        write("POST", { expected_revision, target_session_id, reason: reason.trim(), client_request_id }, csrf), fetchImpl), taskId);
    },
    history: async (taskId, signal) => parseHistory(await ownerCall(`/tasks/${at(taskId)}/claim-recoveries`, read(signal), fetchImpl), taskId),
  };
}

export const CAPABILITY_LABEL: Record<string, string> = {
  "project.read": "Read project details", "component.read": "Read components", "component.write": "Edit components",
  "task.read": "Read tasks", "task.propose": "Propose tasks", "task.claim": "Claim tasks", "task.candidate": "Submit work for review",
  "task.review": "Review others' work", "planning.read": "Read plans and ideas", "planning.write": "Write plans and ideas",
  "decision.request": "Ask you questions", "entity.withdraw": "Withdraw its own tasks and questions",
};
export const capabilityLabel = (id: string) => CAPABILITY_LABEL[id] ?? id;
export const EFFECTIVE_TEXT: Record<GrantReason, string> = {
  grant_absent: "No Hub access. Approving a connection doesn't grant any.",
  connection_inactive: "No Hub access while this connection isn't active.",
  owner_revoked: "No Hub access: you removed it. The earlier choices are shown for reference.",
  grant_expired: "No Hub access: it ran out.",
  no_active_projects: "No Hub access: none of its projects are active any more.",
  authorized: "Hub access in effect.",
};
const RECOVERY_BLOCK: Record<string, string> = {
  claim_session_active: "The session holding it is still live, so it can't be handed over.",
  task_execution_present: "Work has already been started for this task, so it can't be handed over here.",
  no_live_session: "This installation has no live session to hand it to.",
  hub_grant_required: "This installation needs Hub access to claim tasks in this project first.",
  hub_capability_forbidden: "This installation needs the Claim tasks right for this project first.",
  invalid_claim_provenance: "Who holds this claim can't be confirmed, so it can't be handed over here.",
  task_state_ineligible: "This task isn't in a state that can be handed over.",
  no_authorized_target: "No live session of this installation has Hub access to claim tasks in this project.",
  recovery_history_full: "This task has reached its limit of recorded hand-overs.",
};
export const recoveryBlockText = (reason: string) => RECOVERY_BLOCK[reason] ?? `Can't be handed over right now (${reason.replaceAll("_", " ")}).`;

/** Plain wording for a failed grant or recovery change. No answer never claims it happened or didn't. */
export function grantFailureText(error: unknown): string {
  const code = error instanceof HubApiError ? error.code : "";
  const status = error instanceof HubApiError ? error.status : 0;
  if (status === 0 || error instanceof DOMException) return "No answer from the Hub, so this may or may not have happened. Refresh to check before trying again.";
  const known: Record<string, string> = {
    grant_revision_conflict: "Hub access changed since you loaded it. Refresh to see the latest, then try again.",
    revision_conflict: "This task changed since you loaded it. Refresh to see the latest, then try again.",
    invalid_hub_grant: "The Hub didn't accept these choices. Pick at least one project and one right.",
    project_archived: "One of those projects is archived, so it can't be granted.",
    installation_inactive: "This connection isn't active, so its Hub access can't change.",
    invalid_session: "That session has ended. Refresh and choose another.",
    invalid_recovery_target: "That session can't take this task. Refresh and choose one of the sessions offered.",
    claim_session_active: "The session holding this task is still live, so it can't be handed over.",
    task_execution_present: "Work has already been started for this task, so it can't be handed over here.",
    recovery_history_full: "This task has reached its limit of recorded hand-overs.",
    idempotency_conflict: "This hand-over request was already used with different details. Close the dialog and start again.",
    hub_grant_required: "This installation needs Hub access for this project first.",
    hub_capability_forbidden: "This installation needs the Claim tasks right for this project first.",
  };
  if (known[code]) return known[code];
  if (status === 403) return "Only the staging owner can change this.";
  if (status === 404) return "The Hub no longer has this record. Refresh to see the current state.";
  return "The change couldn't be confirmed. Refresh to check before trying again.";
}
