import type { Agent, AgentSummary } from "./types";

/** Only connection evidence crosses the periodic-read boundary. */
export type AgentObservation = Pick<AgentSummary, "id" | "connection">;
const record = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

export function parseAgentObservations(value: unknown): AgentObservation[] {
  if (!record(value) || !Array.isArray(value.items) || value.items.length > 3)
    throw new Error("invalid_agent_observations");
  const ids = new Set<string>();
  return value.items.map((item: unknown) => {
    if (!record(item) || !["codex", "claude", "hermes"].includes(String(item.id)) ||
        typeof item.id !== "string" || ids.has(item.id)) throw new Error("invalid_agent_observations");
    ids.add(item.id);
    const result: AgentObservation = { id: item.id as Agent };
    const c = item.connection;
    if (c == null) return result;
    if (!record(c) || !["connected", "stale", "not_observed", "unavailable"].includes(String(c.state)) ||
        c.source !== "embedded_bus" || c.scope !== "this_server_process") throw new Error("invalid_agent_observations");
    for (const key of ["observed_at", "valid_until", "last_stream_write_at"])
      if (c[key] !== null && (typeof c[key] !== "string" || (c[key] as string).length > 100))
        throw new Error("invalid_agent_observations");
    for (const key of ["active_streams", "inbox_subscriptions", "stale_streams"])
      if (!(key === "stale_streams" && c[key] === undefined) && c[key] !== null &&
          (!Number.isSafeInteger(c[key]) || (c[key] as number) < 0)) throw new Error("invalid_agent_observations");
    for (const key of ["subscribed", "native_push_available"])
      if (c[key] !== null && typeof c[key] !== "boolean") throw new Error("invalid_agent_observations");
    result.connection = {
      state: c.state as NonNullable<AgentSummary["connection"]>["state"],
      source: "embedded_bus", scope: "this_server_process",
      observed_at: c.observed_at as string | null, valid_until: c.valid_until as string | null,
      last_stream_write_at: c.last_stream_write_at as string | null,
      active_streams: c.active_streams as number | null, inbox_subscriptions: c.inbox_subscriptions as number | null,
      ...(c.stale_streams === undefined ? {} : { stale_streams: c.stale_streams as number | null }),
      subscribed: c.subscribed as boolean | null, native_push_available: c.native_push_available as boolean | null,
    };
    return result;
  });
}

export function applyAgentObservations(base: AgentSummary[], observations: AgentObservation[]): AgentSummary[] {
  return base.map(agent => ({ ...agent, connection: observations.find(item => item.id === agent.id)?.connection }));
}
