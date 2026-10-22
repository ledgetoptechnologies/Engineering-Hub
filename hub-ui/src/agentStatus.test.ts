import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { connectionView } from "./agentStatus";
import type { AgentSummary } from "./types";

const now = Date.parse("2026-10-21T12:00:00Z");
function agent(overrides: Partial<NonNullable<AgentSummary["connection"]>> = {}): AgentSummary {
  return {
    id: "hermes", transport: "unknown", chat_readiness: "unverified", reason: "fixture",
    connection: {
      state: "connected", source: "embedded_bus", scope: "this_server_process",
      observed_at: new Date(Date.now()).toISOString(),
      valid_until: new Date(Date.now() + 30_000).toISOString(),
      active_streams: 1, stale_streams: 0, inbox_subscriptions: 1, subscribed: true,
      last_stream_write_at: new Date(now).toISOString(), native_push_available: true,
      ...overrides,
    },
  };
}

describe("connection heartbeat status", () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(now); });
  afterEach(() => vi.useRealTimers());

  it("does not turn stale retained streams green when snapshots refresh", () => {
    for (let refresh = 0; refresh < 4; refresh++) {
      vi.setSystemTime(now + refresh * 60_000);
      const view = connectionView(agent({ state: "stale", active_streams: 0,
        stale_streams: 2, inbox_subscriptions: 0, subscribed: false }), false);
      expect(view.trusted).toBe(true);
      expect(view.connected).toBe(false);
      expect(view.label).toBe("Connection stale — heartbeat not recently observed");
    }
  });

  it("distinguishes stale heartbeat evidence from expired or unverified snapshots", () => {
    const item = agent({ state: "stale", active_streams: 0, stale_streams: 1 });
    expect(connectionView(item, false).label).toContain("Connection stale");
    vi.setSystemTime(now + 30_000);
    expect(connectionView(item, false).label).toBe("Hub connection snapshot expired");
    expect(connectionView(item, true).label).toBe("Hub connection snapshot unverified");
  });

  it("honors a short evidence TTL without inferring chat readiness", () => {
    const item = agent({ valid_until: new Date(now + 2_000).toISOString() });
    expect(connectionView(item, false).connected).toBe(true);
    expect(connectionView(item, false).label).toBe("Hub stream observed");
    expect(item.chat_readiness).toBe("unverified");
    vi.setSystemTime(now + 2_000);
    expect(connectionView(item, false).connected).toBe(false);
    expect(connectionView(item, false).label).toBe("Hub connection snapshot expired");
  });

  it("keeps zero fresh streams neutral and accepts older snapshots without stale counts", () => {
    expect(connectionView(agent({ state: "not_observed", active_streams: 0,
      inbox_subscriptions: 0, subscribed: false, stale_streams: undefined }), false))
      .toMatchObject({ connected: false, label: "No active push stream observed" });
    expect(connectionView(agent({ stale_streams: undefined }), false).connected).toBe(true);
    expect(connectionView(agent({ stale_streams: -1 }), false).trusted).toBe(false);
    expect(connectionView(agent({ stale_streams: Number.NaN }), false).connected).toBe(false);
  });
});
