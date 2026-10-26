/**
 * Illustrative devices for demo mode. Names are placeholders and addresses come from the
 * documentation ranges (192.0.2.0/24, 198.51.100.0/24, 2001:db8::/32); nothing is read from a real network.
 */
import { HubApiError } from "./api";
import type { InfrastructureClient } from "./infrastructureApi";
import type { Device, DeviceDetail, Maintenance } from "./infrastructureModel";

export function demoDevices(now = new Date()): { devices: Device[]; history: Record<string, Maintenance[]> } {
  const at = (minutes: number) => new Date(now.getTime() - minutes * 60_000).toISOString();
  const probe = (id: string, label: string, state: "ok" | "failed", seen: number, valid = 5) =>
    ({ probe_id: id, source_label: label, state, observed_at: at(seen), valid_until: at(seen - valid) });
  const device = (over: Partial<Device> & Pick<Device, "id" | "name" | "role">): Device => ({
    hostname: null, addresses: [], network: null, vlan: null, retired: false, revision: 1, updated_at: at(60 * 24),
    observations: [], open_maintenance: null, ...over });
  const maintenance: Maintenance = { id: "6f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b", revision: 1, device_id: "build-pc",
    reason: "Planned shutdown to replace the disk.\nWill power back on after the new disk is in.", expected_end: at(20), state: "open",
    opened_at: at(95), opened_by: { kind: "agent", identity: "hermes" }, resolved_at: null, resolved_by: null, resolution: null };
  const resolved: Maintenance = { ...maintenance, id: "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", revision: 2,
    reason: "Firmware update.", expected_end: null, state: "resolved", opened_at: at(60 * 24 * 6), resolved_at: at(60 * 24 * 6 - 40),
    resolved_by: { kind: "agent", identity: "hermes" }, resolution: "Updated and checked; back in service." };
  const devices = [
    device({ id: "storage-server", name: "Storage server", hostname: "storage.example.test", addresses: ["192.0.2.10"],
      network: "192.0.2.0/24", vlan: 10, role: "File storage and the Hub", observations: [probe("exporter", "Exporter scrape", "ok", 1)] }),
    device({ id: "build-pc", name: "Build PC", hostname: "build.example.test", addresses: ["198.51.100.20", "2001:db8::20"],
      network: null, vlan: 20, role: "Staging builds", open_maintenance: maintenance,
      observations: [probe("exporter", "Exporter scrape", "ok", 100)] }),
    device({ id: "office-printer", name: "Office printer", addresses: ["192.0.2.40"], network: "192.0.2.0/24", vlan: 10, role: "Printing",
      observations: [probe("ping", "Ping check", "failed", 2)] }),
    device({ id: "camera-hub", name: "Camera hub", role: "Security cameras" }),
  ];
  return { devices, history: { "build-pc": [maintenance, resolved] } };
}

export function demoInfrastructure(): InfrastructureClient {
  const refuse = () => Promise.reject(new HubApiError(403, "demo_read_only"));
  const readOnly = { device_write: false, maintenance_write: false };
  return {
    list: async () => ({ items: demoDevices().devices, capabilities: readOnly }),
    detail: async (id) => {
      const { devices, history } = demoDevices();
      const device = devices.find(item => item.id === id);
      if (!device) throw new HubApiError(404, "device_not_found");
      return { device, maintenance: { items: history[id] ?? [], truncated: false }, capabilities: readOnly } satisfies DeviceDetail;
    },
    saveDevice: refuse, openMaintenance: refuse, resolveMaintenance: refuse,
  };
}
