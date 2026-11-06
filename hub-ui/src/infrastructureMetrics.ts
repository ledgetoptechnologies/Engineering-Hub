/**
 * Machine health read from the Hub's Prometheus: every active target it discovers, grouped by
 * the operator's host label and matched to a recorded device where exactly one fits. The parser
 * is exact-key and bounded like the rest of Infrastructure, and pinned to the contract fixture
 * (fixtures/infrastructure-metrics-v2.json). Schema 3 adds network rates; schemas 1 and 2 are still
 * read during rollout. A missing value stays null and is shown as "not reported", never as zero; an
 * unreachable source is unknown, never down.
 */
import { HubApiError, isMissingRoute } from "./api";

export const EXPORTERS = ["node", "windows", "nvidia"] as const;
export const SCRAPE_STATES = ["fresh", "stale", "unknown"] as const;
export const SOURCE_STATES = ["not_configured", "ok", "partial", "unreachable"] as const;
export const ASSOCIATIONS = ["matched", "unregistered", "ambiguous"] as const;
export const WARNINGS = ["invalid_target", "ambiguous_inventory", "unknown_exporter", "targets_truncated", "series_truncated", "query_failed"] as const;
export type Exporter = typeof EXPORTERS[number];
export type Warning = typeof WARNINGS[number];
export interface Disk { mount: string; size_bytes: number; free_bytes: number | null }
export interface Gpu { index: number; model: string | null; util_ratio: number | null; mem_used_bytes: number | null; mem_total_bytes: number | null; temperature_c: number | null }
export interface ProcessRow { name: string; count: number | null; working_set_bytes: number }
export interface MachineTarget {
  host_key: string; host_label: string | null; association: typeof ASSOCIATIONS[number];
  device_id: string | null; probe_id: string | null; job: string; instance: string; exporter: Exporter | null;
  labels: { host?: string; role?: string; env?: string }; up: boolean | null; last_scrape: string | null;
  state: typeof SCRAPE_STATES[number]; cpu_busy_ratio: number | null; mem_total_bytes: number | null;
  mem_available_bytes: number | null; uptime_seconds: number | null; disks: Disk[]; gpus: Gpu[]; top_processes: ProcessRow[];
  net_rx_bytes_per_second: number | null; net_tx_bytes_per_second: number | null;
}
export interface MetricsSource { status: typeof SOURCE_STATES[number]; truncated: boolean; omitted_count: number | null; warnings: Warning[] }
export interface MachineMetrics { generated_at: string; source: MetricsSource; targets: MachineTarget[] }

const MAX_TARGETS = 200, MAX_DISKS = 16, MAX_GPUS = 8, MAX_PROCESSES = 10;
const LABELS = ["host", "role", "env"] as const;
const slug = /^[a-z0-9][a-z0-9-]{0,63}$/;
const hostKey = /^(host|target):[a-f0-9]{64}$/;

function invalid(): never { throw new HubApiError(502, "invalid_infrastructure_metrics"); }
function record(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  const row = value as Record<string, unknown>;
  if (Object.keys(row).sort().join() !== [...keys].sort().join()) return invalid();
  return row;
}
function list(value: unknown, max: number): unknown[] {
  return Array.isArray(value) && value.length <= max ? value : invalid();
}
/** Length is in Unicode characters, as the backend counts it; a character outside the BMP is one, not two. */
function plain(value: unknown, max: number): string {
  if (typeof value !== "string" || !value || value.trim() !== value || value.length > 2 * max || [...value].length > max) return invalid();
  for (const char of value) { const code = char.codePointAt(0)!; if (code < 32 || code === 127) return invalid(); }
  return value;
}
function oneOf<T extends string>(value: unknown, options: readonly T[]): T {
  return typeof value === "string" && (options as readonly string[]).includes(value) ? value as T : invalid();
}
function time(value: unknown): string {
  return typeof value === "string" && value.length <= 40 && !Number.isNaN(Date.parse(value)) ? value : invalid();
}
function number(value: unknown, { min = 0, max = Number.MAX_SAFE_INTEGER, integer = false, nullable = true } = {}): number | null {
  if (value === null && nullable) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < min || value > max) return invalid();
  if (integer && !Number.isSafeInteger(value)) return invalid();
  return value;
}
const bytes = (value: unknown) => number(value, { integer: true });

function disk(value: unknown): Disk {
  const row = record(value, ["mount", "size_bytes", "free_bytes"]);
  const size = number(row.size_bytes, { integer: true, nullable: false })!, free = bytes(row.free_bytes);
  if (free !== null && free > size) return invalid();
  return { mount: plain(row.mount, 128), size_bytes: size, free_bytes: free };
}
function gpu(value: unknown): Gpu {
  const row = record(value, ["index", "model", "util_ratio", "mem_used_bytes", "mem_total_bytes", "temperature_c"]);
  return { index: number(row.index, { max: 99, integer: true, nullable: false })!, model: row.model === null ? null : plain(row.model, 100),
    util_ratio: number(row.util_ratio, { max: 1 }), mem_used_bytes: bytes(row.mem_used_bytes), mem_total_bytes: bytes(row.mem_total_bytes),
    temperature_c: number(row.temperature_c, { max: 150 }) };
}
function processRow(value: unknown): ProcessRow {
  const row = record(value, ["name", "count", "working_set_bytes"]);
  return { name: plain(row.name, 128), count: row.count === null ? null : number(row.count, { min: 1, integer: true, nullable: false }),
    working_set_bytes: number(row.working_set_bytes, { integer: true, nullable: false })! };
}
const METRIC_KEYS = ["job", "instance", "exporter", "labels", "up", "last_scrape", "state",
  "cpu_busy_ratio", "mem_total_bytes", "mem_available_bytes", "uptime_seconds", "disks", "gpus", "top_processes"];
const isSlug = (value: unknown): value is string => typeof value === "string" && slug.test(value);

/** Schema 1 rows are explicit owner mappings, so each is a matched device of its own. */
function mappedTarget(value: unknown): MachineTarget {
  const row = record(value, ["device_id", "probe_id", ...METRIC_KEYS]);
  if (!isSlug(row.device_id) || !isSlug(row.probe_id)) return invalid();
  const metrics = target(row);
  return { ...metrics, host_key: metrics.labels.host ? `v1-host:${metrics.labels.host}` : `v1-device:${row.device_id}`,
    host_label: metrics.labels.host ?? null, association: "matched", device_id: row.device_id, probe_id: row.probe_id };
}
function discoveredTarget(value: unknown, network: boolean): MachineTarget {
  const row = record(value, ["host_key", "host_label", "association", "device_id", "probe_id", ...METRIC_KEYS,
    ...(network ? ["net_rx_bytes_per_second", "net_tx_bytes_per_second"] : [])]);
  if (typeof row.host_key !== "string" || !hostKey.test(row.host_key)) return invalid();
  const association = oneOf(row.association, ASSOCIATIONS);
  // Only a unique inventory match names a device; unregistered and ambiguous targets never do.
  if (association === "matched" ? !isSlug(row.device_id) : row.device_id !== null) return invalid();
  if (row.probe_id !== null && !isSlug(row.probe_id)) return invalid();
  const label = row.host_label === null ? null : plain(row.host_label, 64);
  if ((label === null) !== row.host_key.startsWith("target:")) return invalid();
  return { ...target(row), host_key: row.host_key, host_label: label, association,
    device_id: row.device_id as string | null, probe_id: row.probe_id as string | null,
    net_rx_bytes_per_second: network ? number(row.net_rx_bytes_per_second) : null,
    net_tx_bytes_per_second: network ? number(row.net_tx_bytes_per_second) : null };
}
function target(row: Record<string, unknown>): MachineTarget {
  if (!row.labels || typeof row.labels !== "object" || Array.isArray(row.labels)) return invalid();
  const raw = row.labels as Record<string, unknown>;
  if (Object.keys(raw).some(key => !(LABELS as readonly string[]).includes(key))) return invalid();
  const labels: MachineTarget["labels"] = {};
  for (const key of LABELS) if (key in raw) labels[key] = plain(raw[key], 64);
  if (row.up !== null && typeof row.up !== "boolean") return invalid();
  const state = oneOf(row.state, SCRAPE_STATES);
  const last = row.last_scrape === null ? null : time(row.last_scrape);
  // Fresh and stale are judged from a real scrape time; unknown may lack one.
  if (state !== "unknown" && last === null) return invalid();
  const total = bytes(row.mem_total_bytes), available = bytes(row.mem_available_bytes);
  if (available !== null && (total === null || available > total)) return invalid();
  return {
    host_key: "", host_label: null, association: "matched", device_id: null, probe_id: null,
    job: plain(row.job, 128), instance: plain(row.instance, 128),
    exporter: row.exporter === null ? null : oneOf(row.exporter, EXPORTERS), labels, up: row.up as boolean | null, last_scrape: last, state,
    cpu_busy_ratio: number(row.cpu_busy_ratio, { max: 1 }), mem_total_bytes: total,
    mem_available_bytes: available, uptime_seconds: number(row.uptime_seconds, { integer: true }),
    disks: list(row.disks, MAX_DISKS).map(disk), gpus: list(row.gpus, MAX_GPUS).map(gpu),
    top_processes: list(row.top_processes, MAX_PROCESSES).map(processRow),
    net_rx_bytes_per_second: null, net_tx_bytes_per_second: null,
  };
}
function discoveredSource(value: unknown): MetricsSource {
  const row = record(value, ["status", "truncated", "omitted_count", "warnings"]);
  if (typeof row.truncated !== "boolean") return invalid();
  const warnings = list(row.warnings, WARNINGS.length).map(item => oneOf(item, WARNINGS));
  if (warnings.some((item, index) => index > 0 && warnings[index - 1] >= item)) return invalid();
  const omitted = number(row.omitted_count, { integer: true });
  if (!row.truncated && omitted !== 0) return invalid();
  return { status: oneOf(row.status, SOURCE_STATES), truncated: row.truncated, omitted_count: omitted, warnings };
}
export function parseMetrics(value: unknown): MachineMetrics {
  const row = record(value, ["schema_version", "generated_at", "source", "devices"]);
  if (row.schema_version !== 1 && row.schema_version !== 2 && row.schema_version !== 3) return invalid();
  const v2 = row.schema_version !== 1, network = row.schema_version === 3;
  const source: MetricsSource = v2 ? discoveredSource(row.source)
    : { status: oneOf(record(row.source, ["status"]).status, ["ok", "partial", "unreachable"] as const), truncated: false, omitted_count: 0, warnings: [] };
  const targets = list(row.devices, MAX_TARGETS).map(item => v2 ? discoveredTarget(item, network) : mappedTarget(item));
  if (new Set(targets.map(item => `${item.job}\n${item.instance}`)).size !== targets.length) return invalid();
  // Not configured means nothing was fetched; unreachable keeps no stale rows.
  if ((source.status === "not_configured" || (v2 && source.status === "unreachable")) && targets.length > 0) return invalid();
  return { generated_at: time(row.generated_at), source, targets };
}

/** null means this Hub or this person has no metrics: the panel is hidden, as for other optional routes. */
export async function readMetrics(signal: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch): Promise<MachineMetrics | null> {
  let response: Response;
  try {
    response = await fetchImpl.call(globalThis, "/api/hub/v1/infrastructure/metrics",
      { method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store", headers: { Accept: "application/json" }, signal });
  } catch (error) {
    if (signal.aborted) throw error;
    throw new HubApiError(0, "network_unavailable");
  }
  if (response.status === 401) throw new HubApiError(401, "human_session_required");
  if ([403, 404, 503].includes(response.status) || await isMissingRoute(response)) return null;
  if (!response.ok) throw new HubApiError(response.status, "infrastructure_metrics_failed");
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return invalid();
  let body: unknown;
  try { body = await response.json(); } catch { return invalid(); }
  return parseMetrics(body);
}

export const HISTORY_RANGES = ["1h", "6h", "24h", "7d"] as const;
export type HistoryRange = typeof HISTORY_RANGES[number];
export type HistoryPoint = [number, number | null];
const HISTORY_KEYS = ["cpu", "memory", "disk", "network_rx", "network_tx"] as const;
export interface HistorySeries { key: typeof HISTORY_KEYS[number]; label: string | null; unit: "ratio" | "bytes_per_second"; points: HistoryPoint[] }
export interface MachineHistoryData { range: HistoryRange; source: typeof SOURCE_STATES[number]; series: HistorySeries[] }
const MAX_HISTORY_SERIES = 80, MAX_HISTORY_POINTS = 250;

function historySeries(value: unknown): HistorySeries {
  const row = record(value, ["key", "label", "unit", "points"]);
  const unit = oneOf(row.unit, ["ratio", "bytes_per_second"] as const);
  const points = list(row.points, MAX_HISTORY_POINTS).map((item): HistoryPoint => {
    if (!Array.isArray(item) || item.length !== 2) return invalid();
    return [number(item[0], { integer: true, nullable: false })!, number(item[1], unit === "ratio" ? { max: 1 } : {})];
  });
  if (!points.length) return invalid();
  return { key: oneOf(row.key, HISTORY_KEYS), label: row.label === null ? null : plain(row.label, 128), unit, points };
}
export function parseHistory(value: unknown): MachineHistoryData {
  const row = record(value, ["schema_version", "generated_at", "host_key", "range", "step_seconds", "source", "series"]);
  if (row.schema_version !== 1) return invalid();
  return { range: oneOf(row.range, HISTORY_RANGES), source: oneOf(record(row.source, ["status"]).status, SOURCE_STATES),
    series: list(row.series, MAX_HISTORY_SERIES).map(historySeries) };
}
/** null means this person or Hub has no history for this machine: the charts are hidden. */
export async function readHistory(hostKey: string, range: HistoryRange, signal: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch): Promise<MachineHistoryData | null> {
  let response: Response;
  const query = new URLSearchParams({ host: hostKey, range });
  try {
    response = await fetchImpl.call(globalThis, `/api/hub/v1/infrastructure/metrics/history?${query}`,
      { method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store", headers: { Accept: "application/json" }, signal });
  } catch (error) {
    if (signal.aborted) throw error;
    throw new HubApiError(0, "network_unavailable");
  }
  if (response.status === 401) throw new HubApiError(401, "human_session_required");
  if ([403, 404, 503].includes(response.status) || await isMissingRoute(response)) return null;
  if (!response.ok) throw new HubApiError(response.status, "infrastructure_history_failed");
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return invalid();
  let body: unknown;
  try { body = await response.json(); } catch { return invalid(); }
  return parseHistory(body);
}

export interface MachineGroup {
  key: string; title: string; labels: MachineTarget["labels"]; targets: MachineTarget[];
  /** The one recorded device every target here matched, if they agree. */
  device_id: string | null; unregistered: boolean; ambiguous: boolean;
}
/**
 * Groups a machine's CPU and GPU exporters by the server's host key: the operator's host label,
 * else the single target. A shared label is display grouping, not proof of one physical machine.
 */
export function groupMachines(targets: MachineTarget[]): MachineGroup[] {
  const groups = new Map<string, MachineTarget[]>();
  for (const item of targets) groups.set(item.host_key, [...(groups.get(item.host_key) ?? []), item]);
  return [...groups.entries()].map(([key, items]) => {
    const devices = new Set(items.map(item => item.device_id));
    return { key, title: items[0].host_label ?? items[0].device_id ?? items[0].instance,
      labels: { role: items.find(item => item.labels.role)?.labels.role, env: items.find(item => item.labels.env)?.labels.env },
      targets: [...items].sort((a, b) => (a.exporter === "nvidia" ? 1 : 0) - (b.exporter === "nvidia" ? 1 : 0)),
      device_id: devices.size === 1 ? items[0].device_id : null,
      unregistered: items.every(item => item.association === "unregistered"),
      ambiguous: items.some(item => item.association === "ambiguous") };
  }).sort((a, b) => a.title.localeCompare(b.title));
}

export interface QuickStats {
  cpu: number | null; memory: number | null; memoryText: string; disk: Disk | null; diskRatio: number | null;
  down: number | null; up: number | null; reporting: boolean;
}
/** The few numbers shown under a device. Storage is the fullest disk, since that's the one to watch. */
export function quickStats(group: MachineGroup): QuickStats {
  const items = group.targets.filter(item => item.exporter !== "nvidia");
  const main = items.find(item => item.cpu_busy_ratio !== null || item.mem_total_bytes !== null) ?? items[0];
  const total = main?.mem_total_bytes ?? null, free = main?.mem_available_bytes ?? null;
  const used = total !== null && free !== null ? total - free : null;
  const disks = items.flatMap(item => item.disks).filter(item => item.free_bytes !== null);
  const usedShare = (item: Disk) => (item.size_bytes - item.free_bytes!) / item.size_bytes;
  const disk = disks.length ? disks.reduce((a, b) => usedShare(b) > usedShare(a) ? b : a) : null;
  return { cpu: main?.cpu_busy_ratio ?? null, memory: used !== null && total ? used / total : null,
    memoryText: used !== null ? `${formatBytes(used)} of ${formatBytes(total)}` : "not reported",
    disk, diskRatio: disk ? usedShare(disk) : null, down: main?.net_rx_bytes_per_second ?? null, up: main?.net_tx_bytes_per_second ?? null,
    reporting: group.targets.some(item => item.up === true && item.state === "fresh") };
}

export function formatBytes(value: number | null): string {
  if (value === null) return "not reported";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let amount = value, unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit++; }
  return `${amount >= 100 || unit === 0 ? Math.round(amount) : amount.toFixed(1)} ${units[unit]}`;
}
export const formatRate = (value: number | null) => value === null ? "not reported" : `${formatBytes(Math.round(value))}/s`;
export const percent = (ratio: number | null) => ratio === null ? "not reported" : `${Math.round(ratio * 100)}%`;
export function formatUptime(seconds: number | null): string {
  if (seconds === null) return "not reported";
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), minutes = Math.floor(seconds % 3600 / 60);
  return days ? `${days}d ${hours}h` : hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}
