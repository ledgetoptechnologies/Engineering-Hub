/**
 * HTTP adapter for the capability-gated infrastructure registry. The backend validates the full
 * reference fixture against its response projection (see docs/infrastructure-ui.md).
 */
import { HubApiError, isMissingRoute } from "./api";
import { parseDevice, parseDeviceDetail, parseDeviceList, parseMaintenance, type Device, type DeviceDetail, type DeviceList, type Maintenance } from "./infrastructureModel";

const base = "/api/hub/v1/infrastructure";
export interface DeviceDraft {
  id: string; name: string; hostname: string | null; metrics_host: string | null; addresses: string[]; network: string | null; vlan: number | null; role: string; retired: boolean;
}
export interface InfrastructureClient {
  list(signal: AbortSignal, includeRetired: boolean): Promise<DeviceList>;
  detail(id: string, signal: AbortSignal): Promise<DeviceDetail>;
  saveDevice(draft: DeviceDraft, expectedRevision: number, csrf: string): Promise<Device>;
  openMaintenance(deviceId: string, input: { reason: string; expected_end: string | null; client_request_id: string }, csrf: string): Promise<Maintenance>;
  resolveMaintenance(id: string, input: { expected_revision: number; resolution: string }, csrf: string): Promise<Maintenance>;
}

async function call(path: string, init: RequestInit, fetchImpl: typeof fetch): Promise<unknown> {
  let response: Response;
  try {
    response = await fetchImpl.call(globalThis, base + path, { credentials: "same-origin", redirect: "error", cache: "no-store", ...init });
  } catch (error) {
    if ((init.signal as AbortSignal | undefined)?.aborted) throw error;
    throw new HubApiError(0, "network_unavailable");
  }
  if (!response.ok) {
    if (await isMissingRoute(response)) throw new HubApiError(404, "infrastructure_unavailable");
    let code = response.status === 401 ? "human_session_required" : response.status === 403 ? "infrastructure_forbidden"
      : response.status === 409 ? "conflict" : "infrastructure_request_failed";
    if (response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
      try {
        const detail = (await response.clone().json() as { detail?: { code?: unknown } })?.detail;
        if (typeof detail?.code === "string" && /^[a-z][a-z0-9_.:-]{0,63}$/.test(detail.code)) code = detail.code;
      } catch { /* Never reflect raw server text. */ }
    }
    throw new HubApiError(response.status, code);
  }
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) throw new HubApiError(502, "invalid_infrastructure_response");
  try { return await response.json(); } catch { throw new HubApiError(502, "invalid_infrastructure_response"); }
}
function write(method: string, body: unknown, csrf: string): RequestInit {
  if (!csrf) throw new HubApiError(403, "csrf_required");
  return { method, body: JSON.stringify(body), signal: AbortSignal.timeout(15_000),
    headers: { Accept: "application/json", "Content-Type": "application/json", "X-Hub-CSRF": csrf } };
}
const read = (signal: AbortSignal): RequestInit => ({ method: "GET", headers: { Accept: "application/json" }, signal });

export function httpInfrastructure(fetchImpl: typeof fetch = globalThis.fetch): InfrastructureClient {
  return {
    list: async (signal, includeRetired) => parseDeviceList(await call(`/devices${includeRetired ? "?include_retired=true" : ""}`, read(signal), fetchImpl)),
    detail: async (id, signal) => parseDeviceDetail(await call(`/devices/${encodeURIComponent(id)}`, read(signal), fetchImpl)),
    saveDevice: async (draft, expectedRevision, csrf) => {
      // Pick only declared fields, whatever the caller passed.
      const { id, name, hostname, metrics_host, addresses, network, vlan, role, retired } = draft;
      return parseDevice(await call(`/devices/${encodeURIComponent(id)}`,
        write("PUT", { name, hostname, metrics_host, addresses, network, vlan, role, retired, expected_revision: expectedRevision }, csrf), fetchImpl));
    },
    openMaintenance: async (deviceId, { reason, expected_end, client_request_id }, csrf) => parseMaintenance(await call(
      `/devices/${encodeURIComponent(deviceId)}/maintenance`, write("POST", { reason, expected_end, client_request_id }, csrf), fetchImpl)),
    resolveMaintenance: async (id, { expected_revision, resolution }, csrf) => parseMaintenance(await call(
      `/maintenance/${encodeURIComponent(id)}/resolve`, write("POST", { expected_revision, resolution }, csrf), fetchImpl)),
  };
}

/** Plain wording for a failed write. A failure or no answer never claims the change was or wasn't saved. */
export function writeFailureText(error: unknown): string {
  const code = error instanceof HubApiError ? error.code : "";
  const status = error instanceof HubApiError ? error.status : 0;
  if (status === 0 || error instanceof DOMException) return "No answer from the Hub, so this may or may not have been saved. Reload to check before trying again.";
  if (code === "revision_conflict") return "Someone changed this first. Reload to see the latest; your text is kept here.";
  if (code === "maintenance_already_open") return "This device already has open maintenance. Reload to see it.";
  if (code === "idempotency_conflict") return "This note's request was already used with different details. Start the note again.";
  if (code === "device_retired") return "This device is retired, so maintenance can't be recorded on it.";
  if (code === "device_limit") return "The Hub already holds its limit of 256 devices.";
  if (code.startsWith("invalid_")) return "The Hub didn't accept these details. Check the fields and try again.";
  if (status === 403) return "You aren't allowed to make this change.";
  return "The change couldn't be confirmed. Reload to check before trying again.";
}
