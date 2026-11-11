/**
 * Infrastructure devices as the Hub declares and observes them. Declared facts (name, addresses,
 * network) are kept apart from probe observations, and observations age by this browser's clock:
 * an expired or missing check is unknown or out of date, never "up" and never "down".
 */
import { HubApiError } from "./api";

export const PROBE_STATES = ["ok", "failed", "unknown", "stale"] as const;
export type ProbeState = typeof PROBE_STATES[number];
export interface Principal { kind: "human" | "agent"; identity: string }
export interface Observation { probe_id: string; source_label: string | null; state: ProbeState; observed_at: string | null; valid_until: string | null }
export interface Maintenance {
  id: string; revision: number; device_id: string; reason: string; expected_end: string | null; state: "open" | "resolved";
  opened_at: string; opened_by: Principal; resolved_at: string | null; resolved_by: Principal | null; resolution: string | null;
}
export interface Device {
  id: string; name: string; hostname: string | null; addresses: string[]; network: string | null; vlan: number | null;
  /** The exact Prometheus host label, when the machine reports under another name than its hostname. */
  metrics_host: string | null;
  role: string; retired: boolean; revision: number; updated_at: string;
  observations: Observation[]; open_maintenance: Maintenance | null;
}
export interface InfraCapabilities { device_write: boolean; maintenance_write: boolean }
export interface DeviceList { items: Device[]; capabilities: InfraCapabilities }
export interface DeviceDetail { device: Device; maintenance: { items: Maintenance[]; truncated: boolean }; capabilities: InfraCapabilities }

export const DEVICE_MAX = 256;
export const TEXT_MAX = 1000;
const slugPattern = /^[a-z0-9][a-z0-9-]{0,63}$/;
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function invalid(): never { throw new HubApiError(502, "invalid_infrastructure_response"); }
function record(value: unknown, keys: string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  const row = value as Record<string, unknown>;
  if (Object.keys(row).sort().join() !== [...keys].sort().join()) return invalid();
  return row;
}
/** Plain text: no control characters except newlines where allowed. */
function text(value: unknown, max: number, { multiline = false, nullable = false } = {}): string | null {
  if (value === null && nullable) return null;
  if (typeof value !== "string" || !value.trim() || value.length > max) return invalid();
  for (const char of value) {
    const code = char.codePointAt(0)!;
    if ((code < 32 && !(multiline && char === "\n")) || code === 127) return invalid();
  }
  return value;
}
function time(value: unknown, nullable = false): string | null {
  if (value === null && nullable) return null;
  return typeof value === "string" && value.length <= 40 && !Number.isNaN(Date.parse(value)) ? value : invalid();
}
const bool = (value: unknown) => typeof value === "boolean" ? value : invalid();
const revision = (value: unknown) => Number.isSafeInteger(value) && (value as number) >= 0 ? value as number : invalid();

function principal(value: unknown): Principal {
  const row = record(value, ["kind", "identity"]);
  if (row.kind !== "human" && row.kind !== "agent") return invalid();
  return { kind: row.kind, identity: text(row.identity, 128)! };
}
function observation(value: unknown): Observation {
  const row = record(value, ["probe_id", "source_label", "state", "observed_at", "valid_until"]);
  if (typeof row.probe_id !== "string" || !slugPattern.test(row.probe_id)) return invalid();
  if (!(PROBE_STATES as readonly string[]).includes(row.state as string)) return invalid();
  return { probe_id: row.probe_id, source_label: text(row.source_label, 100, { nullable: true }), state: row.state as ProbeState,
    observed_at: time(row.observed_at, true), valid_until: time(row.valid_until, true) };
}
export function parseMaintenance(value: unknown): Maintenance {
  const row = record(value, ["id", "revision", "device_id", "reason", "expected_end", "state", "opened_at", "opened_by",
    "resolved_at", "resolved_by", "resolution"]);
  if (typeof row.id !== "string" || !uuidPattern.test(row.id) || typeof row.device_id !== "string" || !slugPattern.test(row.device_id)) return invalid();
  if (row.state !== "open" && row.state !== "resolved") return invalid();
  const resolved = row.state === "resolved";
  // A resolved entry carries who, when and why; an open one carries none of them.
  if (resolved !== (row.resolved_at !== null) || resolved !== (row.resolved_by !== null) || resolved !== (row.resolution !== null)) return invalid();
  return { id: row.id, revision: revision(row.revision), device_id: row.device_id, reason: text(row.reason, TEXT_MAX, { multiline: true })!,
    expected_end: time(row.expected_end, true), state: row.state, opened_at: time(row.opened_at)!, opened_by: principal(row.opened_by),
    resolved_at: time(row.resolved_at, true), resolved_by: row.resolved_by === null ? null : principal(row.resolved_by),
    resolution: text(row.resolution, TEXT_MAX, { multiline: true, nullable: true }) };
}
const DEVICE_KEYS = ["id", "name", "hostname", "addresses", "network", "vlan", "role", "retired", "revision", "updated_at",
  "observations", "open_maintenance"];
export const METRICS_HOST_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
export function parseDevice(value: unknown): Device {
  // metrics_host is newer; a Hub that predates it simply has none.
  const has = Boolean(value && typeof value === "object" && "metrics_host" in value);
  const row = record(value, has ? [...DEVICE_KEYS, "metrics_host"] : DEVICE_KEYS);
  if (has && row.metrics_host !== null && (typeof row.metrics_host !== "string" || !METRICS_HOST_PATTERN.test(row.metrics_host))) return invalid();
  if (typeof row.id !== "string" || !slugPattern.test(row.id)) return invalid();
  if (!Array.isArray(row.addresses) || row.addresses.length > 16 || row.addresses.some(item => typeof item !== "string" || !isIpAddress(item))) return invalid();
  if (row.vlan !== null && (!Number.isInteger(row.vlan) || (row.vlan as number) < 1 || (row.vlan as number) > 4094)) return invalid();
  if (!Array.isArray(row.observations) || row.observations.length > 16) return invalid();
  const observations = row.observations.map(observation);
  if (new Set(observations.map(item => item.probe_id)).size !== observations.length) return invalid();
  const open = row.open_maintenance === null ? null : parseMaintenance(row.open_maintenance);
  if (open && (open.state !== "open" || open.device_id !== row.id)) return invalid();
  return { id: row.id, name: text(row.name, 100)!, hostname: row.hostname === null ? null : hostnameValue(row.hostname),
    metrics_host: has ? row.metrics_host as string | null : null,
    addresses: row.addresses as string[], network: row.network === null ? null : networkValue(row.network), vlan: row.vlan as number | null,
    role: text(row.role, 100)!, retired: bool(row.retired), revision: revision(row.revision), updated_at: time(row.updated_at)!,
    observations, open_maintenance: open };
}
function hostnameValue(value: unknown): string {
  return typeof value === "string" && isHostname(value) ? value : invalid();
}
function networkValue(value: unknown): string {
  return typeof value === "string" && isNetwork(value) ? value : invalid();
}
function capabilities(value: unknown): InfraCapabilities {
  const row = record(value, ["device_write", "maintenance_write"]);
  return { device_write: bool(row.device_write), maintenance_write: bool(row.maintenance_write) };
}
export function parseDeviceList(value: unknown): DeviceList {
  const row = record(value, ["items", "capabilities"]);
  if (!Array.isArray(row.items) || row.items.length > DEVICE_MAX) return invalid();
  const items = row.items.map(parseDevice);
  if (new Set(items.map(item => item.id)).size !== items.length) return invalid();
  return { items, capabilities: capabilities(row.capabilities) };
}
export function parseDeviceDetail(value: unknown): DeviceDetail {
  const row = record(value, ["device", "maintenance", "capabilities"]);
  const device = parseDevice(row.device);
  const history = record(row.maintenance, ["items", "truncated"]);
  if (!Array.isArray(history.items) || history.items.length > 50) return invalid();
  const items = history.items.map(parseMaintenance);
  if (items.some(item => item.device_id !== device.id)) return invalid();
  return { device, maintenance: { items, truncated: bool(history.truncated) }, capabilities: capabilities(row.capabilities) };
}

// Form-side checks mirror the server's so mistakes show before saving. The server stays the authority.
export function isSlug(value: string): boolean { return slugPattern.test(value); }
export function isHostname(value: string): boolean {
  return value.length >= 1 && value.length <= 253
    && value.split(".").every(part => /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(part));
}
function ipv4(value: string): number[] | null {
  const parts = value.split(".");
  if (parts.length !== 4 || parts.some(part => !/^(0|[1-9][0-9]{0,2})$/.test(part) || Number(part) > 255)) return null;
  return parts.map(Number);
}
function isIpv6(value: string): boolean {
  if (value.includes("%") || !/^[0-9A-Fa-f:.]+$/.test(value) || !value.includes(":")) return false;
  try { return new URL(`http://[${value}]/`).hostname.length > 2; } catch { return false; }
}
export function isIpAddress(value: string): boolean { return ipv4(value) !== null || isIpv6(value); }
/** 4 or 6 for an address or a CIDR network's base address, or null when it's neither. */
export function ipFamily(value: string): 4 | 6 | null {
  const address = value.split("/")[0];
  return ipv4(address) ? 4 : isIpv6(address) ? 6 : null;
}
/** A network in CIDR form. For IPv4 the host bits must be zero, as the server requires. */
export function isNetwork(value: string): boolean {
  const [address, bits, extra] = value.split("/");
  if (extra !== undefined || bits === undefined || !/^\d{1,3}$/.test(bits)) return false;
  const prefix = Number(bits), v4 = ipv4(address);
  if (v4) {
    if (prefix > 32) return false;
    const number = v4.reduce((total, part) => total * 256 + part, 0);
    return prefix === 32 || number % 2 ** (32 - prefix) === 0;
  }
  return isIpv6(address) && prefix <= 128;
}
/**
 * Whether an address falls inside a network. An address of the other IP family is never inside it,
 * as the server requires; IPv6 inside IPv6 is left to the server.
 */
export function inNetwork(address: string, network: string): boolean | null {
  const [base, bits] = network.split("/");
  const a = ipv4(address), b = ipv4(base);
  const family = ipFamily(address), networkFamily = ipFamily(base);
  if (family && networkFamily && family !== networkFamily) return false;
  if (!a || !b) return null;
  const size = 2 ** (32 - Number(bits));
  const value = (parts: number[]) => parts.reduce((total, part) => total * 256 + part, 0);
  return Math.floor(value(a) / size) === Math.floor(value(b) / size);
}

export function ago(iso: string, now: Date): string {
  const minutes = Math.floor((now.getTime() - Date.parse(iso)) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.floor(hours / 24)} days ago`;
}
function span(milliseconds: number): string {
  const minutes = Math.max(1, Math.floor(milliseconds / 60_000));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} h` : `${Math.floor(hours / 24)} days`;
}

export interface ObservationView { label: string; line: string; tone: "forest" | "amber" | "neutral"; attention: boolean }
/**
 * One probe, worded from its own report. A passed check is not power, health or agent readiness;
 * a failed one is not "offline"; an expired one is out of date, whatever state it last had.
 */
export function observationView(item: Observation, now: Date): ObservationView {
  const label = item.source_label ?? item.probe_id;
  const seen = item.observed_at ? ago(item.observed_at, now) : null;
  const expired = item.state === "stale" || (item.valid_until !== null && now.getTime() >= Date.parse(item.valid_until));
  if (item.state === "unknown" || !item.observed_at || (!item.valid_until && item.state !== "stale"))
    return { label, line: "Unknown: no current check", tone: "neutral", attention: false };
  if (expired) return { label, line: `Out of date · last seen ${seen}`, tone: "amber", attention: true };
  if (item.state === "ok") return { label, line: `Check passed · ${seen}`, tone: "forest", attention: false };
  return { label, line: `Last check failed · ${seen}`, tone: "amber", attention: true };
}

/** Agents by name; the signed-in person as "You"; another person without exposing their identity key. */
export function actorLabel(value: Principal, self: string | null = null): string {
  if (value.kind === "agent") return value.identity.charAt(0).toUpperCase() + value.identity.slice(1);
  return self !== null && value.identity === self ? "You" : "Another person";
}
export interface MaintenanceView { line: string; overdue: boolean }
/** Passing the expected end makes a note overdue; it never closes it or implies the device is back. */
export function maintenanceView(item: Maintenance, now: Date, self: string | null = null): MaintenanceView {
  const who = actorLabel(item.opened_by, self);
  const reason = item.reason.split("\n")[0];
  if (item.expected_end && now.getTime() > Date.parse(item.expected_end))
    return { line: `${who}: ${reason} · overdue by ${span(now.getTime() - Date.parse(item.expected_end))}`, overdue: true };
  return { line: `${who}: ${reason}${item.expected_end ? ` · expected to end in ${span(Date.parse(item.expected_end) - now.getTime())}` : ""}`, overdue: false };
}
export function needsAttention(device: Device, now: Date): boolean {
  return device.observations.some(item => observationView(item, now).attention)
    || (device.open_maintenance !== null && maintenanceView(device.open_maintenance, now).overdue);
}
