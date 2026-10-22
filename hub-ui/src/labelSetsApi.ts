/** Agent-defined labelling sets: images plus human choices. Data only, no agent code. */
import { HubApiError } from "./api";

export interface LabelChoice { key: string; label: string }
export interface ItemLabel { choice: string; by: string; at: string }
export interface LabelItem { id: string; meta: Record<string, string | number>; label: ItemLabel | null; revision: number }
export interface LabelSetSummary {
  id: string; project: string; title: string; item_count: number; labeled_count: number; updated_at: string;
}
export interface LabelSet {
  id: string; project: string; title: string; instructions: string;
  choices: LabelChoice[]; items: LabelItem[]; updated_at: string;
}

const slug = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
function invalid(): never { throw new HubApiError(502, "invalid_label_set"); }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  return value as Record<string, unknown>;
}
function text(value: unknown, max: number, allowEmpty = false): string {
  return typeof value === "string" && value.length <= max && (allowEmpty || value.trim() !== "") ? value : invalid();
}
const id = (value: unknown) => { const v = text(value, 128); return slug.test(v) ? v : invalid(); };
function count(value: unknown): number {
  return Number.isSafeInteger(value) && (value as number) >= 0 ? value as number : invalid();
}
function time(value: unknown): string { const v = text(value, 64); return Number.isNaN(Date.parse(v)) ? invalid() : v; }

function parseChoice(value: unknown): LabelChoice {
  const row = object(value);
  return { key: id(row.key), label: text(row.label, 80) };
}
function parseMeta(value: unknown): Record<string, string | number> {
  const row = object(value);
  const entries = Object.entries(row);
  if (entries.length > 8) return invalid();
  const meta: Record<string, string | number> = {};
  for (const [key, item] of entries) {
    if (!/^[A-Za-z][A-Za-z0-9_]{0,31}$/.test(key)) return invalid();
    if (typeof item === "number" && Number.isFinite(item)) meta[key] = item;
    else meta[key] = text(item, 120, true);
  }
  return meta;
}
function parseItem(value: unknown, choices: Set<string>): LabelItem {
  const row = object(value);
  let label: ItemLabel | null = null;
  if (row.label !== null) {
    const raw = object(row.label);
    const choice = id(raw.choice);
    if (!choices.has(choice)) return invalid();
    label = { choice, by: text(raw.by, 128), at: time(raw.at) };
  }
  const revision = count(row.revision);
  return { id: id(row.id), meta: parseMeta(row.meta ?? {}), label, revision };
}

export function parseLabelSet(value: unknown, expectedId?: string): LabelSet {
  const row = object(value);
  if (!Array.isArray(row.choices) || row.choices.length < 2 || row.choices.length > 6) return invalid();
  const choices = row.choices.map(parseChoice);
  const keys = new Set(choices.map((choice) => choice.key));
  if (keys.size !== choices.length) return invalid();
  if (!Array.isArray(row.items) || row.items.length > 1000) return invalid();
  const items = row.items.map((item) => parseItem(item, keys));
  if (new Set(items.map((item) => item.id)).size !== items.length) return invalid();
  const set: LabelSet = {
    id: id(row.id), project: id(row.project), title: text(row.title, 200),
    instructions: text(row.instructions ?? "", 4096, true), choices, items, updated_at: time(row.updated_at),
  };
  if (expectedId !== undefined && set.id !== expectedId) return invalid();
  return set;
}

export function parseLabelSetList(value: unknown, project: string): LabelSetSummary[] {
  const row = object(value);
  if (!Array.isArray(row.items) || row.items.length > 200) return invalid();
  return row.items.map((raw) => {
    const item = object(raw);
    const summary = {
      id: id(item.id), project: id(item.project), title: text(item.title, 200),
      item_count: count(item.item_count), labeled_count: count(item.labeled_count), updated_at: time(item.updated_at),
    };
    // A list for another project would show the wrong project's work after a race.
    if (summary.project !== project || summary.labeled_count > summary.item_count) return invalid();
    return summary;
  });
}

const getInit = (signal: AbortSignal): RequestInit => ({
  method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store",
  headers: { Accept: "application/json" }, signal,
});
async function json(response: Response): Promise<unknown> {
  if (!response.ok) {
    let code = response.status === 404 ? "label_sets_unavailable" : response.status === 409 ? "label_conflict" : "label_request_failed";
    if (response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
      try {
        const detail = object(object(await response.json()).detail);
        if (detail.code === "label_conflict" || detail.code === "label_set_not_found") code = detail.code;
      } catch { /* Never reflect server bodies. */ }
    }
    throw new HubApiError(response.status, code);
  }
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return invalid();
  try { return await response.json(); } catch { return invalid(); }
}

export const imageUrl = (setId: string, itemId: string) =>
  `/api/hub/v1/label-sets/${encodeURIComponent(id(setId))}/items/${encodeURIComponent(id(itemId))}/image`;

export async function listLabelSets(project: string, signal: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch) {
  const params = new URLSearchParams({ project: id(project) });
  const response = await fetchImpl.call(globalThis, `/api/hub/v1/label-sets?${params}`, getInit(signal));
  return parseLabelSetList(await json(response), project);
}

export async function readLabelSet(setId: string, signal: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch) {
  const response = await fetchImpl.call(globalThis, `/api/hub/v1/label-sets/${encodeURIComponent(id(setId))}`, getInit(signal));
  return parseLabelSet(await json(response), setId);
}

export async function saveLabel(setId: string, item: LabelItem, choice: string | null, csrf: string,
  choices: LabelChoice[], fetchImpl: typeof fetch = globalThis.fetch): Promise<LabelItem> {
  if (!csrf) throw new HubApiError(403, "csrf_required");
  if (choice !== null && !choices.some((entry) => entry.key === choice)) throw new HubApiError(422, "invalid_choice");
  const response = await fetchImpl.call(globalThis,
    `/api/hub/v1/label-sets/${encodeURIComponent(id(setId))}/items/${encodeURIComponent(id(item.id))}/label`, {
      method: "PUT", credentials: "same-origin", redirect: "error", cache: "no-store",
      headers: { Accept: "application/json", "Content-Type": "application/json", "X-Hub-CSRF": csrf },
      body: JSON.stringify({ choice, expected_revision: item.revision }),
    });
  const saved = parseItem(await json(response), new Set(choices.map((entry) => entry.key)));
  if (saved.id !== item.id || saved.revision !== item.revision + 1
      || (saved.label?.choice ?? null) !== choice) return invalid();
  return saved;
}
