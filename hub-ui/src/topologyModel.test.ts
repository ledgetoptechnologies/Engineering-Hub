import { describe, expect, it } from "vitest";
import { parseDeviceList, type Device } from "./infrastructureModel";
import { filterTopologyGroups, groupDevicesBySubnet } from "./topologyModel";
import fixture from "./fixtures/infrastructure-v1.json";

const devices = (): Device[] => {
  const parsed = parseDeviceList(structuredClone(fixture.list)).items;
  return [
    { ...parsed[0], network: "198.51.100.0/24", addresses: ["198.51.100.20"], metrics_host: null },
    { ...parsed[1], network: "198.51.100.0/24", retired: true },
    { ...parsed[0], id: "unassigned-host", name: "Unassigned host", network: null, addresses: ["203.0.113.24"],
      hostname: "unassigned.example.test", metrics_host: null },
  ];
};

describe("topology grouping", () => {
  it("groups only by the declared subnet and keeps devices without telemetry or a subnet", () => {
    const groups = groupDevicesBySubnet(devices());

    expect(groups.map(group => group.network)).toEqual(["198.51.100.0/24", null]);
    expect(groups[0].devices.map(device => device.id)).toEqual(["build-pc", "camera-hub"]);
    expect(groups[1].devices.map(device => device.id)).toEqual(["unassigned-host"]);
    expect(groups.flatMap(group => group.devices)).toHaveLength(3);
    expect(groups.flatMap(group => group.devices).every(device => device.metrics_host === null)).toBe(true);
    expect(groups[0].devices[1].retired).toBe(true);
  });

  it("filters by subnet and declared device facts without changing the source groups", () => {
    const groups = groupDevicesBySubnet(devices());
    const filtered = filterTopologyGroups(groups, "unassigned.example.test");

    expect(filtered).toEqual([{ network: null, devices: [groups[1].devices[0]] }]);
    expect(filterTopologyGroups(groups, "198.51.100.0/24")).toEqual([groups[0]]);
    expect(filterTopologyGroups(groups, "staging builds")[0].devices.map(device => device.id)).toEqual(["build-pc"]);
    expect(filterTopologyGroups(groups, "no match")).toEqual([]);
    expect(groups[0].devices).toHaveLength(2);
  });
});
