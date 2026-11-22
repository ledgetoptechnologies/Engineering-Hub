import type { Device } from "./infrastructureModel";

export interface TopologyGroup {
  network: string | null;
  devices: Device[];
}

/** Group by the explicitly declared CIDR only; an address never implies a subnet. */
export function groupDevicesBySubnet(devices: readonly Device[]): TopologyGroup[] {
  const groups = new Map<string | null, Device[]>();
  for (const device of devices) {
    const current = groups.get(device.network) ?? [];
    current.push(device);
    groups.set(device.network, current);
  }
  return [...groups.entries()]
    .map(([network, items]) => ({ network, devices: [...items].sort((a, b) => a.name.localeCompare(b.name)) }))
    .sort((a, b) => a.network === null ? (b.network === null ? 0 : 1)
      : b.network === null ? -1 : a.network.localeCompare(b.network));
}

/** Match declared fields and subnet labels; empty groups disappear and inputs are left untouched. */
export function filterTopologyGroups(groups: readonly TopologyGroup[], query: string): TopologyGroup[] {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return [...groups];
  return groups.flatMap(group => {
    const devices = group.devices.filter(device => [
      device.name, device.hostname, device.role, device.network, String(device.vlan ?? ""), ...device.addresses,
    ].some(value => value?.toLocaleLowerCase().includes(needle)));
    return devices.length ? [{ network: group.network, devices }] : [];
  });
}
