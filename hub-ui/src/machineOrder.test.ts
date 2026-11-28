import { describe, expect, it } from "vitest";
import type { MachineGroup } from "./infrastructureMetrics";
import { DEFAULT_MACHINE_ORDER, MACHINE_ORDER_STORAGE_KEY, machineOrderStorageKey, moveMachine, normalizeMachineName, readMachineOrder, sortMachineGroups, writeMachineOrder } from "./machineOrder";

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

  it("stores only normalized names and falls back safely for invalid actor storage", () => {
    const local = storage();
    const actor = { mode: "live" as const, actor: "human:cf:actor-a" };
    writeMachineOrder(["DNS", "main-pc", "DNS", ""], actor, local);
    expect(readMachineOrder(actor, local)).toEqual(["dns", "main pc"]);
    local.setItem(machineOrderStorageKey(actor)!, "not json");
    expect(readMachineOrder(actor, local)).toEqual(DEFAULT_MACHINE_ORDER);
  });

  it("isolates demo and actor preferences from legacy and other actors", () => {
    const local = storage();
    const demo = { mode: "demo" } as const;
    const actorA = { mode: "live" as const, actor: "human:cf:actor-a" };
    const actorB = { mode: "live" as const, actor: "human:cf:actor-b" };
    local.setItem(MACHINE_ORDER_STORAGE_KEY, '["lab-node"]');

    expect(readMachineOrder(demo, local)).toEqual(DEFAULT_MACHINE_ORDER);
    expect(readMachineOrder(actorA, local)).toEqual(DEFAULT_MACHINE_ORDER);
    writeMachineOrder(["lab-node"], demo, local);
    writeMachineOrder(["DNS"], actorA, local);
    writeMachineOrder(["Zeta"], actorB, local);
    expect(readMachineOrder(demo, local)).toEqual(["lab node"]);
    expect(readMachineOrder(actorA, local)).toEqual(["dns"]);
    expect(readMachineOrder(actorB, local)).toEqual(["zeta"]);

    const priorLength = local.length;
    writeMachineOrder(["private machine"], { mode: "live", actor: null }, local);
    expect(local.length).toBe(priorLength);
  });

  it("moving a card saves every shown machine in the new order, and sorting follows it", () => {
    const shown = sortMachineGroups([group("staging"), group("beast"), group("processing"), group("dns")]);
    expect(shown.map(item => item.title)).toEqual(["staging", "dns", "beast", "processing"]);
    const next = moveMachine(shown, "processing", 0);
    expect(next).toEqual(["processing", "staging", "dns", "beast"]);
    expect(sortMachineGroups(shown, next).map(item => item.title)).toEqual(["processing", "staging", "dns", "beast"]);
    expect(moveMachine(shown, "staging", 99)).toEqual(["dns", "beast", "processing", "staging"]);
    expect(moveMachine(shown, "missing", 0)).toEqual(["staging", "dns", "beast", "processing"]);
  });
});
