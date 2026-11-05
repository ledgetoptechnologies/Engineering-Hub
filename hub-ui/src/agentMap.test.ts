import { describe, expect, it } from "vitest";
import { demoActivity, parseActivity, type TopicActivity } from "./agentActivityApi";
import type { Installation, Session } from "./agentConnectionsApi";
import { MAX_TOPICS_SHOWN, agentKey, agentLoad, buildAgents, buildSessions, connectedTo, fit, layoutMap, rankTopics, sessionKey, topicKey } from "./agentMap";
import type { Task } from "./types";

const NOW = Date.parse("2026-11-05T12:00:00Z");
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();
const task = (values: Partial<Task>) => ({ id: "t", title: "Fix it", status: "claimed", owner: "codex", ...values }) as Task;
const installation = (values: Partial<Installation> = {}) => ({ id: "i1", family: "claude", name: "Desk", host_label: "beast", state: "active", ...values }) as Installation;
const session = (values: Partial<Session> = {}) => ({ id: "s1", installation_id: "i1", label: "hub", state: "active",
  expires_at: ago(-30), last_used_at: ago(1), ...values }) as Session;
const topic = (name: string | null, values: Partial<TopicActivity> = {}): TopicActivity => ({ topic: name, agents: ["claude"], messages: 1, pending: 0, claimed: 0,
  pending_by_agent: {}, claimed_by_agent: {}, oldest_pending_at: null, last_activity_at: ago(5), ...values });

describe("agent load", () => {
  it("counts a claimed message or task as working, even with no stream", () => {
    expect(agentLoad(false, 0, 1, 0)).toBe("working");
    expect(agentLoad(true, 3, 0, 1)).toBe("working");
  });
  it("separates unread work from idle and missing streams", () => {
    expect(agentLoad(true, 2, 0, 0)).toBe("waiting");
    expect(agentLoad(true, 0, 0, 0)).toBe("idle");
    expect(agentLoad(false, 2, 0, 0)).toBe("offline");
  });
  it("only counts claimed tasks owned by that family", () => {
    const agents = buildAgents(demoActivity(NOW), () => true, [task({ status: "in_review" }), task({ owner: "installation:x", owner_family: "hermes" })]);
    expect(agents.find(item => item.id === "codex")?.tasks).toHaveLength(0);
    expect(agents.find(item => item.id === "hermes")?.tasks).toHaveLength(1);
  });
});

describe("sessions", () => {
  it("leaves out ended sessions and inactive installations", () => {
    const rows = buildSessions([installation(), installation({ id: "i2", state: "revoked" })], {
      i1: [session(), session({ id: "s2", state: "revoked" }), session({ id: "s3", expires_at: ago(1) })],
      i2: [session({ id: "s4", installation_id: "i2" })] }, [], NOW);
    expect(rows.map(item => item.id)).toEqual(["s1"]);
  });
  it("ties a task to its exact session and ranks working first", () => {
    const rows = buildSessions([installation()], { i1: [session({ id: "s1", label: "a", last_used_at: ago(60) }), session({ id: "s2", label: "b" })] },
      [task({ owner_session_id: "s1", owner_family: "claude" })], NOW);
    expect(rows.map(item => [item.id, item.load])).toEqual([["s1", "working"], ["s2", "active"]]);
  });
  it("calls an unused session quiet", () => {
    expect(buildSessions([installation()], { i1: [session({ last_used_at: null })] }, [], NOW)[0].load).toBe("quiet");
  });
});

describe("layout", () => {
  const agents = buildAgents(demoActivity(NOW), () => true, []);
  it("puts busy topics first and caps the default view", () => {
    const many = Array.from({ length: MAX_TOPICS_SHOWN + 3 }, (_, index) => topic(`quiet-${index}`));
    const layout = layoutMap(agents, [], [...many, topic("busy", { pending: 1 })]);
    expect(layout.topics).toHaveLength(MAX_TOPICS_SHOWN);
    expect(layout.topics[0].item.topic).toBe("busy");
    expect(layout.hiddenTopics).toBe(4);
    expect(layoutMap(agents, [], many, true).hiddenTopics).toBe(0);
  });
  it("ranks recent activity when nothing is waiting", () => {
    expect(rankTopics([topic("old", { last_activity_at: ago(90) }), topic("new")]).map(item => item.topic)).toEqual(["new", "old"]);
  });
  it("marks a line only for the agent the message is waiting on", () => {
    const layout = layoutMap(agents, [], [topic("t", { agents: ["claude", "hermes"], pending: 2, pending_by_agent: { hermes: 2 } })]);
    const waiting = (agent: string) => layout.lines.find(line => line.from === agentKey(agent))?.waiting;
    expect(waiting("hermes")).toBe(2);
    expect(waiting("claude")).toBe(0);
  });
  it("marks only the agent holding a claimed message as handling it", () => {
    const layout = layoutMap(agents, [], [topic("t", { agents: ["claude", "hermes"], claimed: 1, claimed_by_agent: { claude: 1 } })]);
    const handling = (agent: string) => layout.lines.find(line => line.from === agentKey(agent))?.handling;
    expect(handling("claude")).toBe(true);
    expect(handling("hermes")).toBe(false);
  });
  it("gives every row its own space", () => {
    const sessions = buildSessions([installation()], { i1: [session({ id: "a" }), session({ id: "b" }), session({ id: "c" })] }, [], NOW);
    const layout = layoutMap(agents, sessions, [topic("t")]);
    const ys = layout.sessions.map(item => item.y);
    expect(new Set(ys).size).toBe(3);
    expect(Math.max(...layout.agents.map(item => item.y)) + 40).toBeLessThanOrEqual(layout.height);
  });
  it("lights a topic's agents and their sessions, but not other topics", () => {
    const sessions = buildSessions([installation()], { i1: [session()] }, [], NOW);
    const layout = layoutMap(agents, sessions, [topic("a"), topic("b", { agents: ["hermes"] })]);
    const lit = connectedTo(topicKey("a"), layout.lines)!;
    expect(lit.has(agentKey("claude"))).toBe(true);
    expect(lit.has(sessionKey("s1"))).toBe(true);
    expect(lit.has(topicKey("b"))).toBe(false);
    expect(connectedTo(null, layout.lines)).toBeNull();
  });
  it("doesn't leak a selection through a shared agent", () => {
    const sessions = buildSessions([installation()], { i1: [session(), session({ id: "s2" })] }, [], NOW);
    const layout = layoutMap(agents, sessions, [topic("a", { agents: ["claude", "hermes"] }), topic("b"), topic("c", { agents: ["codex"] })]);
    const fromTopic = connectedTo(topicKey("a"), layout.lines)!;
    expect(fromTopic.has(topicKey("b"))).toBe(false);
    expect(fromTopic.has(agentKey("codex"))).toBe(false);
    const fromAgent = connectedTo(agentKey("claude"), layout.lines)!;
    expect([topicKey("a"), topicKey("b"), sessionKey("s1"), sessionKey("s2")].every(key => fromAgent.has(key))).toBe(true);
    expect(fromAgent.has(topicKey("c"))).toBe(false);
    const fromSession = connectedTo(sessionKey("s1"), layout.lines)!;
    expect(fromSession.has(topicKey("a"))).toBe(true);
    expect(fromSession.has(sessionKey("s2"))).toBe(false);
  });
});

describe("activity parsing", () => {
  it("accepts the demo shape", () => {
    const snapshot = demoActivity(NOW);
    expect(parseActivity(JSON.parse(JSON.stringify(snapshot)))).toEqual(snapshot);
  });
  it("refuses an unknown agent or a bad count", () => {
    const snapshot = JSON.parse(JSON.stringify(demoActivity(NOW)));
    expect(() => parseActivity({ ...snapshot, open_total: -1 })).toThrow();
    expect(() => parseActivity({ ...snapshot, agents: [{ ...snapshot.agents[0], agent: "other" }] })).toThrow();
  });
});

it("shortens long names on a character boundary", () => {
  expect(fit("short", 10)).toBe("short");
  expect(fit("a-very-long-topic-name", 8)).toBe("a-very-…");
});
