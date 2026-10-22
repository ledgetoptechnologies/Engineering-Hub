/** Read-only display validation. Neither a current tag nor a report grants deployment authority. */
import type { ImageVersionState, ImageVersionStatus, ImageVersionsSummary } from "./types";

export interface ImageVersionRowView {
  project: string;
  host: string;
  service: string;
  environment: "production" | "staging";
  repository: string;
  reference: string;
  state: ImageVersionState;
  label: string;
  tone: "neutral" | "blue" | "forest";
  reason: string;
  checkedAt: string;
  runningObservedAt: string | null;
  publishedObservedAt: string | null;
  validUntil: string | null;
}
export interface ImageVersionsView {
  state: ImageVersionState;
  label: string;
  message: string;
  reportedBy: string | null;
  reportUpdatedAt: string | null;
  rows: ImageVersionRowView[];
  nextExpiry: number | null;
}

const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const repository = /^ghcr\.io\/[a-z0-9]+(?:[._-][a-z0-9]+)*\/[a-z0-9]+(?:[._/-][a-z0-9]+)*$/;
const reference = /^(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|sha256:[0-9a-f]{64})$/;
const digest = /^sha256:[0-9a-f]{64}$/;
const timestamp = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(?:Z|([+-])(\d{2}):(\d{2}))$/;
const reasons = new Set([
  "same_image", "different_image", "running_not_observed", "published_not_observed",
  "observation_source_mismatch", "reference_mismatch", "repository_mismatch",
  "platform_unverified", "platform_mismatch", "running_observation_in_future",
  "published_observation_in_future", "running_observation_expired",
  "published_observation_expired", "identity_unverified", "identity_conflict",
]);
const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const matches = (value: unknown, pattern: RegExp, max: number): value is string =>
  typeof value === "string" && value.length <= max && pattern.test(value);
const nullableDigest = (value: unknown) => value === null || matches(value, digest, 71);
/** Unlike Date.parse alone, require explicit ISO timezone evidence. */
function time(value: unknown): number | null {
  if (typeof value !== "string" || value.length > 64) return null;
  const match = timestamp.exec(value);
  if (!match) return null;
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const monthDays = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > monthDays[month - 1]
      || hour > 23 || minute > 59 || second > 59
      || (match[7] !== undefined && (Number(match[8]) > 23 || Number(match[9]) > 59))) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}
function nullableTime(value: unknown): boolean { return value === null || time(value) !== null; }

function validItem(value: unknown): value is ImageVersionStatus {
  if (!object(value) || !matches(value.project, identifier, 128) || !matches(value.host, identifier, 128)
      || !matches(value.service, identifier, 128) || typeof value.environment !== "string" || !["production", "staging"].includes(value.environment)
      || !matches(value.repository, repository, 255) || value.repository.length < 12
      || !matches(value.reference, reference, 128) || typeof value.state !== "string" || !["up_to_date", "update_available", "unknown"].includes(value.state)
      || typeof value.reason !== "string" || !reasons.has(value.reason) || time(value.checked_at) === null
      || !nullableTime(value.running_observed_at) || !nullableTime(value.published_observed_at)
      || !nullableTime(value.valid_until) || !nullableDigest(value.current_digest) || !nullableDigest(value.available_digest)
      || !(value.digest_kind === null || (typeof value.digest_kind === "string" && ["config", "manifest", "index"].includes(value.digest_kind)))) return false;
  if (value.state === "unknown") return value.reason !== "same_image" && value.reason !== "different_image"
    && value.valid_until === null && value.digest_kind === null && value.current_digest === null && value.available_digest === null;
  if (value.running_observed_at === null || value.published_observed_at === null || value.valid_until === null
      || value.current_digest === null || value.available_digest === null || value.digest_kind === null) return false;
  const running = time(value.running_observed_at)!, published = time(value.published_observed_at)!, until = time(value.valid_until)!;
  const checked = time(value.checked_at)!;
  if (until <= checked || until <= running || until <= published || until - Math.min(running, published) > 1800_000
      || running > checked + 30_000 || published > checked + 30_000) return false;
  return value.state === "up_to_date"
    ? value.reason === "same_image" && value.current_digest === value.available_digest
    : value.reason === "different_image" && value.current_digest !== value.available_digest && value.digest_kind !== "index";
}
function validSummary(value: unknown): value is ImageVersionsSummary {
  if (!object(value) || typeof value.configured !== "boolean" || value.source !== "agent_observation_report"
      || value.advisory_only !== true || typeof value.reason !== "string" || !["available", "not_configured", "invalid_report", "source_unavailable"].includes(value.reason)
      || !(value.reported_by === null || (typeof value.reported_by === "string" && value.reported_by.length > 0 && value.reported_by.length <= 128))
      || !nullableTime(value.report_updated_at) || !Array.isArray(value.items) || value.items.length > 32 || !value.items.every(validItem)) return false;
  if (value.reason !== "available") return value.items.length === 0;
  if (!value.configured || value.items.length === 0 || value.reported_by === null || value.report_updated_at === null) return false;
  const keys = value.items.map(item => JSON.stringify([item.project, item.host, item.service, item.environment]));
  return new Set(keys).size === keys.length;
}
const labels = { up_to_date: "Up to date", update_available: "Update available", unknown: "Not verified" } as const;
const tones = { up_to_date: "forest", update_available: "blue", unknown: "neutral" } as const;

export function imageVersionsView(input: unknown, stale = false, now = Date.now()): ImageVersionsView {
  const unknown = (message: string): ImageVersionsView => ({ state: "unknown", label: labels.unknown, message,
    reportedBy: null, reportUpdatedAt: null, rows: [], nextExpiry: null });
  if (!Number.isFinite(now)) return unknown("Image observation time is not verified.");
  if (input === undefined || input === null) return unknown("Image monitoring is not set up on this server yet.");
  if (!validSummary(input)) return unknown("Image observations could not be verified.");
  if (input.reason !== "available") return unknown(input.reason === "not_configured"
    ? "No running-image inventory is connected yet." : "Image observations are not available right now.");
  const reportAt = time(input.report_updated_at)!;
  if (reportAt > now + 30_000) return unknown("Image observation time is not verified.");
  const rows: ImageVersionRowView[] = input.items.filter(item => item.environment === "production").map(item => {
    const checked = time(item.checked_at)!;
    const running = time(item.running_observed_at), published = time(item.published_observed_at), until = time(item.valid_until);
    const expired = until !== null && until <= now;
    const future = checked > now + 30_000 || (running !== null && running > now + 30_000) || (published !== null && published > now + 30_000);
    const state = stale || expired || future ? "unknown" : item.state;
    return { project: item.project, host: item.host, service: item.service, environment: item.environment,
      repository: item.repository, reference: item.reference, state, label: labels[state], tone: tones[state],
      reason: stale ? "snapshot_unverified" : expired ? "observation_expired" : future ? "observation_in_future" : item.reason,
      checkedAt: item.checked_at, runningObservedAt: item.running_observed_at,
      publishedObservedAt: item.published_observed_at, validUntil: item.valid_until };
  });
  const state: ImageVersionState = rows.length === 0 || rows.some(row => row.state === "unknown") ? "unknown"
    : rows.some(row => row.state === "update_available") ? "update_available" : "up_to_date";
  const deadlines = rows.filter(row => row.state !== "unknown").map(row => time(row.validUntil)!).filter(until => until > now);
  return { state, label: labels[state], message: rows.length === 0 ? "No production image targets are being observed yet."
    : stale ? "This snapshot is no longer verified. Refresh the workspace for current observations."
      : "Advisory comparison only. Image updates are separate from health and deployment approvals.",
    reportedBy: input.reported_by, reportUpdatedAt: input.report_updated_at,
    rows, nextExpiry: deadlines.length ? Math.min(...deadlines) : null };
}
