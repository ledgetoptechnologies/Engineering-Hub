import type { MachineGroup } from "./infrastructureMetrics";

export const MACHINE_ORDER_STORAGE_KEY = "engineering-hub.machine-order.v1";
export const MACHINE_ORDER_DEMO_STORAGE_KEY = `${MACHINE_ORDER_STORAGE_KEY}.demo`;
export type MachineOrderScope = { mode: "demo" } | { mode: "live"; actor: string | null };
export const DEFAULT_MACHINE_ORDER = ["main pc", "cluster pc", "hermes", "staging", "sync pi", "dns"] as const;
const MAX_NAMES = 50;
const MAX_NAME_LENGTH = 100;

/** Exact display-name match after case, whitespace and common punctuation normalization. */
export function normalizeMachineName(value: string): string {
  return value.normalize("NFKC").toLocaleLowerCase("en-US").replace(/[_-]+/g, " ")
    .replace(/[^\p{L}\p{N}]+/gu, " ").trim().replace(/\s+/g, " ");
}

function normalizedNames(values: string[]): string[] {
  const output: string[] = [];
  const seen = new Set<string>();
  for (const value of values.slice(0, MAX_NAMES)) {
    if (typeof value !== "string" || value.length > MAX_NAME_LENGTH) continue;
    const name = normalizeMachineName(value);
    if (name && !seen.has(name)) { seen.add(name); output.push(name); }
  }
  return output;
}

export function normalizeMachineOrder(values: string[]): string[] { return normalizedNames(values); }

export function sortMachineGroups(groups: MachineGroup[], order: readonly string[] = DEFAULT_MACHINE_ORDER): MachineGroup[] {
  const rank = new Map(normalizedNames([...order]).map((name, index) => [name, index]));
  const getRank = (group: MachineGroup) => rank.get(normalizeMachineName(group.title)) ?? Number.MAX_SAFE_INTEGER;
  return [...groups].sort((a, b) => getRank(a) - getRank(b)
    || a.title.localeCompare(b.title, undefined, { sensitivity: "base" })
    || a.key.localeCompare(b.key));
}

export function machineOrderStorageKey(scope: MachineOrderScope): string | null {
  if (scope.mode === "demo") return MACHINE_ORDER_DEMO_STORAGE_KEY;
  return scope.actor ? `${MACHINE_ORDER_STORAGE_KEY}.live.${encodeURIComponent(scope.actor)}` : null;
}

export function readMachineOrder(scope: MachineOrderScope, storage?: Pick<Storage, "getItem">): string[] {
  try {
    const key = machineOrderStorageKey(scope);
    if (!key) return [...DEFAULT_MACHINE_ORDER];
    const local = storage ?? (typeof window === "undefined" ? undefined : window.localStorage);
    const raw = local?.getItem(key);
    if (raw === undefined || raw === null) return [...DEFAULT_MACHINE_ORDER];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.length > MAX_NAMES || parsed.some(value => typeof value !== "string" || value.length > MAX_NAME_LENGTH))
      return [...DEFAULT_MACHINE_ORDER];
    return normalizedNames(parsed);
  } catch { return [...DEFAULT_MACHINE_ORDER]; }
}

export function writeMachineOrder(order: string[], scope: MachineOrderScope, storage?: Pick<Storage, "setItem">): void {
  try {
    const key = machineOrderStorageKey(scope);
    if (!key) return;
    const local = storage ?? (typeof window === "undefined" ? undefined : window.localStorage);
    local?.setItem(key, JSON.stringify(normalizedNames(order)));
  } catch { /* Browser storage can be disabled; ordering still works for this page session. */ }
}
