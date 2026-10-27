import { describe, expect, it } from "vitest";
import { applyAgentObservations, parseAgentObservations } from "./agentObservations";
import { connectionView } from "./agentStatus";
import type { AgentSummary } from "./types";

const connection = () => ({ state: "connected", source: "embedded_bus", scope: "this_server_process",
  observed_at: new Date().toISOString(), valid_until: new Date(Date.now() + 30_000).toISOString(),
  active_streams: 1, stale_streams: 0, inbox_subscriptions: 1, subscribed: true,
  last_stream_write_at: new Date().toISOString(), native_push_available: true });
describe("independent connection evidence boundary", () => {
  it("projects only known connection fields; missing rows clear connections, retaining base metadata", () => {
    const base: AgentSummary[] = ["codex", "hermes"].map(id => ({ id: id as AgentSummary["id"],
      transport: "unknown", chat_readiness: "unverified", reason: "base", connection: connection() as AgentSummary["connection"] }));
    const observations = parseAgentObservations({ items: [{ id: "codex", connection: { ...connection(), ready: true },
      transport: "online", chat_readiness: "ready", reason: "optimistic" }] });
    const applied = applyAgentObservations(base, observations);
    expect(applied[0]).toMatchObject({ transport: "unknown", chat_readiness: "unverified", reason: "base" });
    expect(applied[0].connection).not.toHaveProperty("ready");
    expect(applied[1].connection).toBeUndefined();
    expect(base[1].connection).toBeDefined();
    expect(connectionView(applied[0], true).connected).toBe(false);
  });
  it("rejects unsafe counts, source and field shapes, while timestamp trust remains bounded", () => {
    for (const bad of [{ active_streams: true }, { active_streams: -1 }, { inbox_subscriptions: 2 ** 53 },
      { source: "other" }, { scope: "all_processes" }, { state: "ready" }, { subscribed: "yes" }, { observed_at: {} }])
      expect(() => parseAgentObservations({ items: [{ id: "codex", connection: { ...connection(), ...bad } }] })).toThrow();
    for (const bad of [{ valid_until: "invalid" }, { valid_until: new Date(Date.now() + 60_000).toISOString() }]) {
      const observation = parseAgentObservations({ items: [{ id: "codex", connection: { ...connection(), ...bad } }] })[0];
      expect(connectionView({ ...observation, transport: "unknown", chat_readiness: "unverified", reason: "fixture" }, false).connected).toBe(false);
    }
  });
});
