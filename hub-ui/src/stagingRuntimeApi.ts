/**
 * What is running on the staging PC: a read-only observation for the Hub owner.
 * The worker source is the fixed rootless engine EH jobs use; the host source is an optional
 * root-owned snapshot of everything else (PA, Viewer…). Nothing here starts, stops or removes
 * anything, and an observation is never a readiness claim.
 */
import { HubApiError, isMissingRoute } from "./api";

export const SOURCE_IDS = ["worker", "host"] as const;
export type SourceId = typeof SOURCE_IDS[number];
export const SOURCE_STATES = ["observed", "unavailable", "stale", "not_configured"] as const;
export type SourceState = typeof SOURCE_STATES[number];
export const RUNTIME_STATES = ["ready", "not_configured", "unavailable"] as const;
/** Engine states we word; anything else the engine reports is shown as "unknown", never as running. */
export const CONTAINER_STATES = ["running", "restarting", "paused", "created", "exited", "stopped", "dead", "removing"] as const;
export type ContainerState = typeof CONTAINER_STATES[number] | "unknown";

export interface RuntimeSourceReport { id: SourceId; state: SourceState; observed_at: string | null }
export interface RuntimeContainer { id: string; name: string; image: string; state: ContainerState; scope: SourceId; managed: false }
export interface RuntimeService { name: string; state: "running"; scope: "host" }
export interface RuntimeSnapshot {
  schema: "hub-staging-runtime.v1"; observed_at: string;
  sources: RuntimeSourceReport[]; containers: RuntimeContainer[]; services: RuntimeService[];
}
export interface StagingRuntime { configured: boolean; state: typeof RUNTIME_STATES[number]; snapshot: RuntimeSnapshot | null }

export const RUNTIME_ITEM_MAX = 500;
const labelPattern = /^[^\u0000-\u001f\u007f]{1,128}$/u;
const imagePattern = /^[^\u0000-\u001f\u007f\s]{1,512}$/u;

function invalid(): never { throw new HubApiError(502, "invalid_staging_runtime"); }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  return value as Record<string, unknown>;
}
function oneOf<T extends string>(value: unknown, options: readonly T[]): T {
  return typeof value === "string" && (options as readonly string[]).includes(value) ? value as T : invalid();
}
function matching(value: unknown, pattern: RegExp): string {
  return typeof value === "string" && pattern.exec(value)?.[0] === value ? value : invalid();
}
function timestamp(value: unknown): string {
  return typeof value === "string" && value.length <= 64 && !Number.isNaN(Date.parse(value)) ? value : invalid();
}
function list(value: unknown): unknown[] {
  return Array.isArray(value) && value.length <= RUNTIME_ITEM_MAX ? value : invalid();
}

function parseSource(value: unknown): RuntimeSourceReport {
  const row = object(value);
  return { id: oneOf(row.id, SOURCE_IDS), state: oneOf(row.state, SOURCE_STATES),
    observed_at: row.observed_at === null ? null : timestamp(row.observed_at) };
}
function parseContainer(value: unknown): RuntimeContainer {
  const row = object(value);
  if (row.managed !== false) return invalid();
  const state = typeof row.state === "string" && (CONTAINER_STATES as readonly string[]).includes(row.state.toLowerCase())
    ? row.state.toLowerCase() as ContainerState : typeof row.state === "string" ? "unknown" : invalid();
  return { id: matching(row.id, /^[a-f0-9]{12,64}$/), name: matching(row.name, labelPattern), image: matching(row.image, imagePattern),
    state, scope: oneOf(row.scope, SOURCE_IDS), managed: false };
}
function parseService(value: unknown): RuntimeService {
  const row = object(value);
  return { name: matching(row.name, labelPattern), state: oneOf(row.state, ["running"] as const), scope: oneOf(row.scope, ["host"] as const) };
}
export function parseStagingRuntime(value: unknown): StagingRuntime {
  const row = object(value);
  if (typeof row.configured !== "boolean") return invalid();
  const state = oneOf(row.state, RUNTIME_STATES);
  if (row.snapshot === null) return { configured: row.configured, state, snapshot: null };
  const snap = object(row.snapshot);
  if (snap.schema !== "hub-staging-runtime.v1") return invalid();
  const sources = list(snap.sources).map(parseSource);
  // One report per source: two disagreeing reports for the same engine would make the page contradict itself.
  if (new Set(sources.map(source => source.id)).size !== sources.length) return invalid();
  return { configured: row.configured, state, snapshot: {
    schema: "hub-staging-runtime.v1", observed_at: timestamp(snap.observed_at), sources,
    containers: list(snap.containers).map(parseContainer), services: list(snap.services).map(parseService),
  } };
}

async function failure(response: Response): Promise<never> {
  if (await isMissingRoute(response)) throw new HubApiError(404, "staging_runtime_unsupported");
  let code = response.status === 503 ? "staging_unavailable" : response.status === 401 ? "human_session_required"
    : response.status === 403 ? "staging_forbidden" : "staging_request_failed";
  if (response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
    try {
      const detail = object(object(await response.clone().json()).detail);
      if (typeof detail.code === "string" && /^[a-z][a-z0-9_.:-]{0,63}$/.test(detail.code)) code = detail.code;
    } catch { /* Never reflect proxy HTML or raw server exceptions. */ }
  }
  throw new HubApiError(response.status, code);
}
export async function readStagingRuntime(signal: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch): Promise<StagingRuntime> {
  const response = await fetchImpl.call(globalThis, "/api/hub/v1/staging/runtime", {
    method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store", headers: { Accept: "application/json" }, signal,
  });
  if (!response.ok) return failure(response);
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return invalid();
  let body: unknown;
  try { body = await response.json(); } catch { return invalid(); }
  return parseStagingRuntime(body);
}

/** The image's own name and tag, without the registry address or digest, so no internal host is shown. */
export function imageLabel(image: string): string {
  const name = image.split("@")[0].split("/").at(-1) ?? "";
  return name || "image";
}

const containerStateNames: Record<ContainerState, string> = {
  running: "Running", restarting: "Restarting", paused: "Paused", created: "Created, not started", exited: "Stopped",
  stopped: "Stopped", dead: "Failed", removing: "Being removed", unknown: "State unknown",
};
export const containerStateLabel = (state: ContainerState) => containerStateNames[state];
export const containerTone = (state: ContainerState): "forest" | "amber" | "neutral" =>
  state === "running" ? "forest" : state === "restarting" || state === "dead" || state === "unknown" ? "amber" : "neutral";

export function ago(iso: string, now: Date): string {
  const minutes = Math.round((now.getTime() - Date.parse(iso)) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

export interface SourceView {
  id: SourceId; title: string; description: string;
  /** Whether the list below can be read as what's there. Stale lists are shown, but marked. */
  listed: boolean; tone: "forest" | "amber" | "neutral"; line: string;
  containers: RuntimeContainer[]; services: RuntimeService[];
}
const sourceText: Record<SourceId, { title: string; description: string; notConfigured: string }> = {
  worker: { title: "Staging engine", description: "The rootless engine EH builds and tests run in.",
    notConfigured: "The staging engine view isn't set up, so what runs in it isn't shown." },
  host: { title: "Other programs on the PC", description: "Seen on the PC, but not run or controlled by EH (for example PA or the Viewer).",
    notConfigured: "The host view isn't set up, so other programs on the PC aren't shown." },
};
/** One section per source, worded from that source's own report; a missing report is unknown, never "nothing running". */
export function sourceViews(snapshot: RuntimeSnapshot, now: Date): SourceView[] {
  return SOURCE_IDS.map(id => {
    const report = snapshot.sources.find(source => source.id === id);
    const text = sourceText[id];
    const containers = snapshot.containers.filter(item => item.scope === id);
    const services = id === "host" ? snapshot.services : [];
    const base = { id, title: text.title, description: text.description, containers, services };
    if (!report) return { ...base, listed: false, tone: "amber", line: "Not reported, so what's running here is unknown." };
    const seen = report.observed_at ? ago(report.observed_at, now) : null;
    switch (report.state) {
      case "observed": return { ...base, listed: true, tone: "forest", line: `Seen ${seen ?? "at an unreported time"}.` };
      case "stale": return { ...base, listed: true, tone: "amber",
        line: `Out of date: last seen ${seen ?? "at an unknown time"}. It may have changed since.` };
      case "unavailable": return { ...base, listed: false, tone: "amber",
        line: `Couldn't be read${seen ? ` (last seen ${seen})` : ""}, so what's running here is unknown.` };
      case "not_configured": return { ...base, listed: false, tone: "neutral", line: text.notConfigured };
    }
  });
}
export function countLine(containers: RuntimeContainer[], services: RuntimeService[]): string {
  const running = containers.filter(item => item.state === "running").length;
  const parts = [`${running} running`];
  if (containers.length > running) parts.push(`${containers.length - running} not running`);
  if (services.length) parts.push(`${services.length} ${services.length === 1 ? "service" : "services"}`);
  return parts.join(" · ");
}
