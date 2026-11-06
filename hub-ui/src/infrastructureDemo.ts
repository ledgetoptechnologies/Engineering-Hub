/**
 * Illustrative devices for demo mode. Names are placeholders and addresses come from the
 * documentation ranges (192.0.2.0/24, 198.51.100.0/24, 2001:db8::/32); nothing is read from a real network.
 */
import { HubApiError } from "./api";
import type { InfrastructureClient } from "./infrastructureApi";
import type { HistoryRange, HistorySeries, MachineHistoryData, MachineMetrics, MachineTarget } from "./infrastructureMetrics";
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

const GB = 1024 ** 3;
const demoKey = (char: string) => `host:${char.repeat(64)}`;

export function demoMachineMetrics(now = new Date()): MachineMetrics {
  const machine = (over: Partial<MachineTarget> & Pick<MachineTarget, "host_key" | "instance">): MachineTarget => ({
    host_label: null, association: "matched", device_id: null, probe_id: null, job: "node", exporter: "node", labels: {}, up: true,
    last_scrape: new Date(now.getTime() - 10_000).toISOString(), state: "fresh", cpu_busy_ratio: null, mem_total_bytes: null,
    mem_available_bytes: null, uptime_seconds: 864_000, disks: [], gpus: [], top_processes: [],
    net_rx_bytes_per_second: null, net_tx_bytes_per_second: null, ...over });
  return { generated_at: now.toISOString(), source: { status: "ok", truncated: false, omitted_count: 0, warnings: [] }, targets: [
    machine({ host_key: demoKey("a"), host_label: "storage", device_id: "storage-server", instance: "192.0.2.10:9100", cpu_busy_ratio: 0.18,
      mem_total_bytes: 64 * GB, mem_available_bytes: 41 * GB, net_rx_bytes_per_second: 42_000_000, net_tx_bytes_per_second: 7_500_000,
      disks: [{ mount: "/", size_bytes: 200 * GB, free_bytes: 150 * GB }, { mount: "/mnt/data", size_bytes: 40_000 * GB, free_bytes: 6_000 * GB }] }),
    machine({ host_key: demoKey("b"), host_label: "build", device_id: "build-pc", instance: "198.51.100.20:9182", job: "windows", exporter: "windows",
      cpu_busy_ratio: 0.92, mem_total_bytes: 128 * GB, mem_available_bytes: 30 * GB, net_rx_bytes_per_second: 1_200_000, net_tx_bytes_per_second: 310_000,
      disks: [{ mount: "C:", size_bytes: 2_000 * GB, free_bytes: 900 * GB }] }),
    machine({ host_key: demoKey("c"), host_label: "lab-node", association: "unregistered", instance: "192.0.2.77:9100", cpu_busy_ratio: 0.04,
      mem_total_bytes: 16 * GB, mem_available_bytes: 12 * GB, net_rx_bytes_per_second: 9_000, net_tx_bytes_per_second: 4_000,
      disks: [{ mount: "/", size_bytes: 500 * GB, free_bytes: 410 * GB }] }),
  ] };
}

export function demoMachineHistory(hostKey: string, range: HistoryRange, now = Date.now()): MachineHistoryData {
  const [seconds, step] = { "1h": [3600, 30], "6h": [21600, 180], "24h": [86400, 720], "7d": [604800, 3600] }[range];
  const end = Math.floor(now / 1000 / step) * step, seed = hostKey.charCodeAt(5);
  const wave = (base: number, swing: number, phase: number, top: number) => Array.from({ length: seconds / step + 1 }, (_, index): [number, number | null] => {
    const moment = end - seconds + index * step;
    if (index > seconds / step * 0.62 && index < seconds / step * 0.66) return [moment, null];
    const value = base + swing * Math.sin(moment / 5400 + phase + seed) + swing / 3 * Math.sin(moment / 700 + phase);
    return [moment, Math.min(top, Math.max(0, value))];
  });
  const line = (key: HistorySeries["key"], label: string | null, unit: HistorySeries["unit"], points: [number, number | null][]): HistorySeries => ({ key, label, unit, points });
  return { range, source: "ok", series: [
    line("cpu", null, "ratio", wave(0.35, 0.25, 0, 1)), line("memory", null, "ratio", wave(0.55, 0.06, 1, 1)),
    line("disk", "/", "ratio", wave(0.25, 0.005, 2, 1)), line("disk", "/mnt/data", "ratio", wave(0.85, 0.01, 3, 1)),
    line("network_rx", null, "bytes_per_second", wave(30_000_000, 22_000_000, 4, 1e12)),
    line("network_tx", null, "bytes_per_second", wave(8_000_000, 6_000_000, 5, 1e12)),
  ] };
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
