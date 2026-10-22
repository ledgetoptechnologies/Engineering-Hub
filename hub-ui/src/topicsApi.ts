/** Topic directory: the server's list when it has one, plus topics checked on this device. */
import { HubApiError, isMissingRoute } from "./api";

export interface TopicEntry { topic: string; last_activity_at: string | null; peer_count: number | null; source: "server" | "device" }
const TOPIC = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const KEY = "hub.recentTopics";

export const isTopic = (value: unknown): value is string => typeof value === "string" && TOPIC.test(value);

export function parseTopicList(value: unknown): TopicEntry[] {
  const bad = () => { throw new HubApiError(502, "invalid_topic_list"); };
  if (!value || typeof value !== "object" || !Array.isArray((value as { items?: unknown }).items)) return bad();
  const items = (value as { items: unknown[] }).items;
  if (items.length > 500) return bad();
  const seen = new Set<string>();
  return items.map((raw) => {
    const row = raw as Record<string, unknown>;
    if (!row || typeof row !== "object" || !isTopic(row.topic) || seen.has(row.topic)) return bad();
    seen.add(row.topic);
    const at = typeof row.last_activity_at === "string" && Number.isFinite(Date.parse(row.last_activity_at)) ? row.last_activity_at : null;
    const peers = Number.isSafeInteger(row.peer_count) && (row.peer_count as number) >= 0 ? row.peer_count as number : null;
    return { topic: row.topic, last_activity_at: at, peer_count: peers, source: "server" as const };
  });
}

async function unavailableCatalog(response: Response): Promise<boolean> {
  try {
    const body: unknown = await response.clone().json();
    return (body as { detail?: { code?: unknown } })?.detail?.code === "topic_catalog_unavailable";
  } catch { return false; }
}

/** null means the server has no topic list yet (404); errors other than that are thrown. */
export async function listTopics(signal: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch): Promise<TopicEntry[] | null> {
  const response = await fetchImpl.call(globalThis, "/api/hub/v1/topics", {
    method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store", headers: { Accept: "application/json" }, signal,
  });
  if (await isMissingRoute(response)) return null;
  // No trusted resolver for this account: fall back explicitly, never an empty "authoritative" list.
  if (response.status === 503 && await unavailableCatalog(response)) return null;
  if (!response.ok) throw new HubApiError(response.status, "topic_list_failed");
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) throw new HubApiError(502, "invalid_topic_list");
  return parseTopicList(await response.json());
}

export function rememberedTopics(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(KEY) ?? "[]");
    return Array.isArray(value) ? value.filter(isTopic).slice(0, 12) : [];
  } catch {
    return [];
  }
}

export function rememberTopic(topic: string): string[] {
  if (!isTopic(topic)) return rememberedTopics();
  const next = [topic, ...rememberedTopics().filter((item) => item !== topic)].slice(0, 12);
  try { localStorage.setItem(KEY, JSON.stringify(next)); } catch { /* storage may be unavailable */ }
  return next;
}
