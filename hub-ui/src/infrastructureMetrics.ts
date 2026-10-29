/**
 * Machine health read from the Hub's configured Prometheus, for owners holding metrics_read.
 * The parser is exact-key and bounded like the rest of Infrastructure, and pinned to the
 * server-generated fixture (fixtures/infrastructure-metrics-v1.json). A missing value stays
 * null and is shown as "not reported", never as zero; an unreachable source is unknown, never down.
 */
import { HubApiError, isMissingRoute } from "./api";

export const EXPORTERS = ["node", "windows", "nvidia"] as const;
export const SCRAPE_STATES = ["fresh", "stale", "unknown"] as const;
export const SOURCE_STATES = ["ok", "partial", "unreachable"] as const;
export type Exporter = typeof EXPORTERS[number];
export interface Disk { mount: string; size_bytes: number; free_bytes: number | null }
export interface Gpu { index: number; model: string | null; util_ratio: number | null; mem_used_bytes: number | null; mem_total_bytes: number | null; temperature_c: number | null }
export interface ProcessRow { name: string; count: number | null; working_set_bytes: number }
export interface MachineTarget {
  device_id: string; probe_id: string; job: string; instance: string; exporter: Exporter | null;
  labels: { host?: string; role?: string; env?: string }; up: boolean | null; last_scrape: string | null;
  state: typeof SCRAPE_STATES[number]; cpu_busy_ratio: number | null; mem_total_bytes: number | null;
  mem_available_bytes: number | null; uptime_seconds: number | null; disks: Disk[]; gpus: Gpu[]; top_processes: ProcessRow[];
}
export interface MachineMetrics { generated_at: string; source: typeof SOURCE_STATES[number]; targets: MachineTarget[] }

const MAX_TARGETS = 200, MAX_DISKS = 16, MAX_GPUS = 8, MAX_PROCESSES = 10;
const LABELS = ["host", "role", "env"] as const;
const slug = /^[a-z0-9][a-z0-9-]{0,63}$/;

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
function plain(value: unknown, max: number): string {
  if (typeof value !== "string" || !value || value.trim() !== value || value.length > max) return invalid();
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
function target(value: unknown): MachineTarget {
  const row = record(value, ["device_id", "probe_id", "job", "instance", "exporter", "labels", "up", "last_scrape", "state",
    "cpu_busy_ratio", "mem_total_bytes", "mem_available_bytes", "uptime_seconds", "disks", "gpus", "top_processes"]);
  if (typeof row.device_id !== "string" || !slug.test(row.device_id) || typeof row.probe_id !== "string" || !slug.test(row.probe_id)) return invalid();
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
  return {
    device_id: row.device_id, probe_id: row.probe_id, job: plain(row.job, 128), instance: plain(row.instance, 64),
    exporter: row.exporter === null ? null : oneOf(row.exporter, EXPORTERS), labels, up: row.up as boolean | null, last_scrape: last, state,
    cpu_busy_ratio: number(row.cpu_busy_ratio, { max: 1 }), mem_total_bytes: bytes(row.mem_total_bytes),
    mem_available_bytes: bytes(row.mem_available_bytes), uptime_seconds: number(row.uptime_seconds, { integer: true }),
    disks: list(row.disks, MAX_DISKS).map(disk), gpus: list(row.gpus, MAX_GPUS).map(gpu),
    top_processes: list(row.top_processes, MAX_PROCESSES).map(processRow),
  };
}
export function parseMetrics(value: unknown): MachineMetrics {
  const row = record(value, ["schema_version", "generated_at", "source", "devices"]);
  if (row.schema_version !== 1) return invalid();
  const source = record(row.source, ["status"]);
  const targets = list(row.devices, MAX_TARGETS).map(target);
  if (new Set(targets.map(item => `${item.job}\n${item.instance}`)).size !== targets.length) return invalid();
  return { generated_at: time(row.generated_at), source: oneOf(source.status, SOURCE_STATES), targets };
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

/** Groups CPU and GPU exporters of one machine together: by the operator's host label, else the device. */
export function groupMachines(targets: MachineTarget[]): { key: string; title: string; labels: MachineTarget["labels"]; targets: MachineTarget[] }[] {
  const groups = new Map<string, MachineTarget[]>();
  for (const item of targets) {
    const key = item.labels.host ? `host:${item.labels.host}` : `device:${item.device_id}`;
    groups.set(key, [...(groups.get(key) ?? []), item]);
  }
  return [...groups.entries()].map(([key, items]) => ({ key, title: items[0].labels.host ?? items[0].device_id,
    labels: { role: items.find(item => item.labels.role)?.labels.role, env: items.find(item => item.labels.env)?.labels.env },
    targets: [...items].sort((a, b) => (a.exporter === "nvidia" ? 1 : 0) - (b.exporter === "nvidia" ? 1 : 0)) }))
    .sort((a, b) => a.title.localeCompare(b.title));
}

export function formatBytes(value: number | null): string {
  if (value === null) return "not reported";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let amount = value, unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit++; }
  return `${amount >= 100 || unit === 0 ? Math.round(amount) : amount.toFixed(1)} ${units[unit]}`;
}
export const percent = (ratio: number | null) => ratio === null ? "not reported" : `${Math.round(ratio * 100)}%`;
export function formatUptime(seconds: number | null): string {
  if (seconds === null) return "not reported";
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), minutes = Math.floor(seconds % 3600 / 60);
  return days ? `${days}d ${hours}h` : hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}
