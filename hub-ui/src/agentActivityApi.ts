/** Message routing counts for the Agents map. Metadata only: the server never sends message bodies. */
import { HubApiError, isMissingRoute } from "./api";

export const AGENTS = ["hermes", "codex", "claude"] as const;
export type AgentName = typeof AGENTS[number];

export interface AgentActivity { agent: AgentName; pending: number; claimed: number; oldest_pending_at: string | null }
export interface TopicActivity {
  /** null collects messages with no usable topic, which no topic listener can route. */
  topic: string | null;
  agents: AgentName[];
  messages: number;
  pending: number;
  claimed: number;
  pending_by_agent: Partial<Record<AgentName, number>>;
  claimed_by_agent: Partial<Record<AgentName, number>>;
  oldest_pending_at: string | null;
  last_activity_at: string | null;
}
export interface OpenMessage {
  id: string; sender: AgentName; recipient: AgentName; message_type: string;
  priority: "low" | "normal" | "high" | "urgent"; status: "pending" | "claimed"; topic: string | null; created_at: string;
}
export interface ActivitySnapshot {
  observed_at: string; window_hours: number; agents: AgentActivity[]; topics: TopicActivity[]; open: OpenMessage[]; open_total: number;
}

const invalid = (): never => { throw new HubApiError(502, "invalid_agent_activity"); };
const isAgent = (value: unknown): value is AgentName => AGENTS.includes(value as AgentName);
const count = (value: unknown) => Number.isSafeInteger(value) && (value as number) >= 0 ? value as number : invalid();
const text = (value: unknown) => typeof value === "string" && value.length > 0 ? value : invalid();
const orNull = <T,>(value: unknown, parse: (item: unknown) => T) => value === null ? null : parse(value);
const object = (value: unknown) => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : invalid();
const list = (value: unknown) => Array.isArray(value) ? value : invalid();
const agent = (value: unknown) => isAgent(value) ? value : invalid();

function byAgent(value: unknown): Partial<Record<AgentName, number>> {
  const row = object(value);
  return Object.fromEntries(Object.entries(row).map(([key, total]) => [agent(key), count(total)]));
}

export function parseActivity(value: unknown): ActivitySnapshot {
  const root = object(value);
  return {
    observed_at: text(root.observed_at),
    window_hours: count(root.window_hours),
    open_total: count(root.open_total),
    agents: list(root.agents).map(raw => {
      const row = object(raw);
      return { agent: agent(row.agent), pending: count(row.pending), claimed: count(row.claimed),
        oldest_pending_at: orNull(row.oldest_pending_at, text) };
    }),
    topics: list(root.topics).map(raw => {
      const row = object(raw);
      return { topic: orNull(row.topic, text), agents: list(row.agents).map(agent), messages: count(row.messages),
        pending: count(row.pending), claimed: count(row.claimed), pending_by_agent: byAgent(row.pending_by_agent), claimed_by_agent: byAgent(row.claimed_by_agent),
        oldest_pending_at: orNull(row.oldest_pending_at, text), last_activity_at: orNull(row.last_activity_at, text) };
    }),
    open: list(root.open).map(raw => {
      const row = object(raw);
      if (row.status !== "pending" && row.status !== "claimed") return invalid();
      if (!["low", "normal", "high", "urgent"].includes(row.priority as string)) return invalid();
      return { id: text(row.id), sender: agent(row.sender), recipient: agent(row.recipient), message_type: text(row.message_type),
        priority: row.priority as OpenMessage["priority"], status: row.status, topic: orNull(row.topic, text), created_at: text(row.created_at) };
    }),
  };
}

/** null means this Hub is too old to have the route; other failures are thrown. */
export async function readActivity(signal: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch): Promise<ActivitySnapshot | null> {
  const response = await fetchImpl.call(globalThis, "/api/hub/v1/agents/activity", {
    method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store", headers: { Accept: "application/json" }, signal,
  });
  if (await isMissingRoute(response)) return null;
  if (!response.ok) throw new HubApiError(response.status, "agent_activity_failed");
  return parseActivity(await response.json());
}

export function demoActivity(now = Date.now()): ActivitySnapshot {
  const ago = (minutes: number) => new Date(now - minutes * 60_000).toISOString();
  const open: OpenMessage[] = [
    { id: "demo-1", sender: "codex", recipient: "hermes", message_type: "request", priority: "high", status: "pending", topic: "staging-hold", created_at: ago(190) },
    { id: "demo-2", sender: "claude", recipient: "hermes", message_type: "status", priority: "normal", status: "pending", topic: "viewer-heights", created_at: ago(42) },
    { id: "demo-3", sender: "hermes", recipient: "claude", message_type: "request", priority: "normal", status: "claimed", topic: "engineering-hub", created_at: ago(6) },
    { id: "demo-4", sender: "hermes", recipient: "codex", message_type: "note", priority: "low", status: "pending", topic: null, created_at: ago(75) },
  ];
  return {
    observed_at: ago(0), window_hours: 24, open, open_total: open.length,
    agents: [
      { agent: "hermes", pending: 2, claimed: 0, oldest_pending_at: ago(190) },
      { agent: "codex", pending: 1, claimed: 0, oldest_pending_at: ago(75) },
      { agent: "claude", pending: 0, claimed: 1, oldest_pending_at: null },
    ],
    topics: [
      { topic: "staging-hold", agents: ["codex", "hermes"], messages: 9, pending: 1, claimed: 0, pending_by_agent: { hermes: 1 }, claimed_by_agent: {}, oldest_pending_at: ago(190), last_activity_at: ago(190) },
      { topic: "viewer-heights", agents: ["claude", "hermes"], messages: 14, pending: 1, claimed: 0, pending_by_agent: { hermes: 1 }, claimed_by_agent: {}, oldest_pending_at: ago(42), last_activity_at: ago(42) },
      { topic: "engineering-hub", agents: ["claude", "codex", "hermes"], messages: 61, pending: 0, claimed: 1, pending_by_agent: {}, claimed_by_agent: { claude: 1 }, oldest_pending_at: null, last_activity_at: ago(6) },
      { topic: "client-portal", agents: ["claude", "codex"], messages: 5, pending: 0, claimed: 0, pending_by_agent: {}, claimed_by_agent: {}, oldest_pending_at: null, last_activity_at: ago(300) },
      { topic: null, agents: ["codex", "hermes"], messages: 1, pending: 1, claimed: 0, pending_by_agent: { codex: 1 }, claimed_by_agent: {}, oldest_pending_at: ago(75), last_activity_at: ago(75) },
    ],
  };
}
