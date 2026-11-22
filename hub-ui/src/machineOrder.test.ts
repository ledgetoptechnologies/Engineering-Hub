import { describe, expect, it } from "vitest";
import type { MachineGroup } from "./infrastructureMetrics";
import { DEFAULT_MACHINE_ORDER, normalizeMachineName, readMachineOrder, sortMachineGroups, writeMachineOrder } from "./machineOrder";

const group = (title: string, key = title): MachineGroup => ({
  key, title, labels: {}, targets: [], device_id: null, unregistered: true, ambiguous: false,
});

function storage(): Storage {
  const values = new Map<string, string>();
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, value); },
    removeItem: key => { values.delete(key); },
    clear: () => values.clear(),
    key: index => [...values.keys()][index] ?? null,
    get length() { return values.size; },
  };
}

describe("browser-local machine order", () => {
  it("normalizes exact display aliases without substring matching", () => {
    expect(normalizeMachineName("  Main-PC ")).toBe("main pc");
    const ordered = sortMachineGroups([
      group("Main PC test"), group("DNS"), group("main_pc"), group("Main PC"),
    ], ["main pc"]);
    expect(ordered.map(item => item.title)).toEqual(["Main PC", "main_pc", "DNS", "Main PC test"]);
  });

  it("uses the requested default order and alphabetically follows unlisted machines", () => {
    expect(DEFAULT_MACHINE_ORDER).toEqual(["main pc", "cluster pc", "hermes", "staging", "sync pi", "dns"]);
    const ordered = sortMachineGroups([group("Zeta"), group("DNS"), group("Hermes"), group("Cluster-PC"), group("Main PC")]);
    expect(ordered.map(item => item.title)).toEqual(["Main PC", "Cluster-PC", "Hermes", "DNS", "Zeta"]);
  });

  it("stores only normalized names and falls back safely for invalid browser storage", () => {
    const local = storage();
    writeMachineOrder(["DNS", "main-pc", "DNS", ""], local);
    expect(readMachineOrder(local)).toEqual(["dns", "main pc"]);
    local.setItem("engineering-hub.machine-order.v1", "not json");
    expect(readMachineOrder(local)).toEqual(DEFAULT_MACHINE_ORDER);
  });
});
