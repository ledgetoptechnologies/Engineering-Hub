/** Pure model behind the Agents map: claims and credentials, not execution. */
import { AGENTS, type ActivitySnapshot, type AgentName, type TopicActivity } from "./agentActivityApi";
import type { Installation, Session } from "./agentConnectionsApi";
import type { Task } from "./types";

export type AgentLoad = "claimed" | "waiting" | "stream" | "no_stream";
export type SessionLoad = "claimed" | "recent" | "credential";

export const ACTIVE_WINDOW_MS = 10 * 60_000;
export const MAX_TOPICS_SHOWN = 12;
const ROW = 52, NODE = 40, TOP = 6;
export const MAP_WIDTH = 960;
export const COLUMNS = { session: { x: 0, width: 230 }, agent: { x: 365, width: 190 }, topic: { x: 690, width: 270 } };

export interface MapSession {
  id: string; family: AgentName; label: string; host: string; lastUsedAt: string | null; load: SessionLoad; task: Task | null;
}
export interface MapAgent {
  id: AgentName; connected: boolean; streams: number; load: AgentLoad; pending: number; claimed: number; tasks: Task[];
  oldestPendingAt: string | null;
}
export interface Placed<T> { item: T; y: number }
export interface MapLine { from: string; to: string; waiting: number; handling: boolean; y1: number; y2: number; x1: number; x2: number }
export interface MapLayout {
  sessions: Placed<MapSession>[]; agents: Placed<MapAgent>[]; topics: Placed<TopicActivity>[]; lines: MapLine[]; height: number; hiddenTopics: number;
}

export const topicKey = (topic: string | null) => `topic:${topic ?? ""}`;
export const agentKey = (agent: string) => `agent:${agent}`;
export const sessionKey = (session: string) => `session:${session}`;

const familyOf = (task: Task): string | null => task.owner_family ?? task.owner;

export function agentLoad(connected: boolean, pending: number, claimed: number, tasks: number): AgentLoad {
  if (claimed > 0 || tasks > 0) return "claimed";
  if (!connected) return "no_stream";
  return pending > 0 ? "waiting" : "stream";
}

export function buildAgents(activity: ActivitySnapshot, connected: (agent: AgentName) => boolean, tasks: Task[],
  streams: (agent: AgentName) => number = () => 0): MapAgent[] {
  const claimed = tasks.filter(task => task.status === "claimed");
  return AGENTS.map(id => {
    const row = activity.agents.find(item => item.agent === id);
    const own = claimed.filter(task => familyOf(task) === id);
    const live = connected(id);
    return { id, connected: live, streams: streams(id), tasks: own, pending: row?.pending ?? 0, claimed: row?.claimed ?? 0,
      oldestPendingAt: row?.oldest_pending_at ?? null, load: agentLoad(live, row?.pending ?? 0, row?.claimed ?? 0, own.length) };
  });
}

/** Unexpired credentials only. An owner_session_id links a claim, not a live chat. */
export function buildSessions(installations: Installation[], sessions: Record<string, Session[]>, tasks: Task[], now: number): MapSession[] {
  const rows: MapSession[] = [];
  for (const installation of installations) {
    if (installation.state !== "active") continue;
    for (const session of sessions[installation.id] ?? []) {
      if (session.state !== "active" || Date.parse(session.expires_at) <= now) continue;
      const task = tasks.find(item => item.status === "claimed" && item.owner_session_id === session.id) ?? null;
      const used = session.last_used_at ? Date.parse(session.last_used_at) : NaN;
      rows.push({ id: session.id, family: installation.family, label: session.label || installation.name, host: installation.host_label,
        lastUsedAt: session.last_used_at, task, load: task ? "claimed" : now - used < ACTIVE_WINDOW_MS ? "recent" : "credential" });
    }
  }
  const rank = (load: SessionLoad) => ["claimed", "recent", "credential"].indexOf(load);
  return rows.sort((a, b) => AGENTS.indexOf(a.family) - AGENTS.indexOf(b.family) || rank(a.load) - rank(b.load) || a.label.localeCompare(b.label));
}

/** Busy topics first, then the most recent. The caller shows the rest on request. */
export function rankTopics(topics: TopicActivity[]): TopicActivity[] {
  return [...topics].sort((a, b) => b.pending - a.pending || b.claimed - a.claimed
    || (b.last_activity_at ?? "").localeCompare(a.last_activity_at ?? "") || (a.topic ?? "").localeCompare(b.topic ?? ""));
}

const middle = (start: number, rows: number) => TOP + start * ROW + (Math.max(rows, 1) * ROW - ROW) / 2;

export function layoutMap(agents: MapAgent[], sessions: MapSession[], topics: TopicActivity[], showAllTopics = false): MapLayout {
  const ranked = rankTopics(topics);
  const shown = showAllTopics ? ranked : ranked.slice(0, MAX_TOPICS_SHOWN);
  const placedSessions: Placed<MapSession>[] = [];
  const placedAgents: Placed<MapAgent>[] = [];
  let row = 0;
  for (const agent of agents) {
    const own = sessions.filter(session => session.family === agent.id);
    own.forEach((item, index) => placedSessions.push({ item, y: TOP + (row + index) * ROW }));
    placedAgents.push({ item: agent, y: middle(row, own.length) });
    row += Math.max(own.length, 1);
  }
  const rows = Math.max(row, shown.length);
  const offset = (rows - shown.length) / 2;
  const placedTopics = shown.map((item, index) => ({ item, y: TOP + (offset + index) * ROW }));
  const agentY = new Map(placedAgents.map(({ item, y }) => [item.id as string, y]));
  const lines: MapLine[] = [];
  const line = (from: string, to: string, x1: number, y1: number, x2: number, y2: number, waiting = 0, handling = false) =>
    lines.push({ from, to, x1, x2, y1: y1 + NODE / 2, y2: y2 + NODE / 2, waiting, handling });
  for (const { item, y } of placedSessions)
    line(sessionKey(item.id), agentKey(item.family), COLUMNS.session.x + COLUMNS.session.width, y, COLUMNS.agent.x, agentY.get(item.family) ?? y, 0, item.load === "claimed");
  for (const { item, y } of placedTopics) for (const agent of item.agents)
    line(agentKey(agent), topicKey(item.topic), COLUMNS.agent.x + COLUMNS.agent.width, agentY.get(agent) ?? y, COLUMNS.topic.x, y,
      item.pending_by_agent[agent] ?? 0, (item.claimed_by_agent[agent] ?? 0) > 0);
  return { sessions: placedSessions, agents: placedAgents, topics: placedTopics, lines, height: TOP * 2 + rows * ROW,
    hiddenTopics: ranked.length - shown.length };
}

/** The nodes a selection touches, so everything else can dim. */
export function connectedTo(selected: string | null, lines: MapLine[]): Set<string> | null {
  if (!selected) return null;
  // Every selection resolves to its agents. Those agents' sessions and topics light up, and nothing past them.
  const agents = new Set(selected.startsWith("agent:") ? [selected]
    : lines.filter(item => item.from === selected || item.to === selected).map(item => item.from === selected ? item.to : item.from));
  const seen = new Set([selected, ...agents]);
  for (const item of lines) {
    if (agents.has(item.from) && item.to.startsWith("topic:") && !selected.startsWith("topic:")) seen.add(item.to);
    if (agents.has(item.to) && item.from.startsWith("session:") && !selected.startsWith("session:")) seen.add(item.from);
  }
  return seen;
}

export const fit = (value: string, max: number) => [...value].length <= max ? value : `${[...value].slice(0, max - 1).join("")}…`;
