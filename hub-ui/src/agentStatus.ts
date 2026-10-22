/** Hub-connection evidence for one agent. Pure: same rules the Agents page shows. */
import { relativeAge } from "./dashboardText";
import type { AgentSummary } from "./types";

export function observationTime(value: string | null | undefined): string | null {
  if (!value) return null;
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp) || timestamp > Date.now() + 30_000) return null;
  return `${new Date(timestamp).toLocaleString()} (${relativeAge(value)})`;
}

export interface ConnectionView {
  label: string;
  connected: boolean;
  /** Current, bounded, known-source evidence with valid counts. */
  trusted: boolean;
  observed: string | null;
}

export function connectionView(agent: AgentSummary, stale: boolean): ConnectionView {
  const connection = agent.connection;
  const observed = observationTime(connection?.observed_at);
  const observedAt = Date.parse(connection?.observed_at ?? "");
  const deadline = connection?.valid_until ? Date.parse(connection.valid_until) : NaN;
  const boundedDeadline = Number.isFinite(deadline) && Number.isFinite(observedAt) &&
    deadline > observedAt && deadline <= observedAt + 30_000;
  const fresh = boundedDeadline && deadline > Date.now();
  const expired = observed && boundedDeadline && !fresh;
  const countsValid = !!connection && Number.isInteger(connection.active_streams) &&
    connection.active_streams! >= 0 && Number.isInteger(connection.inbox_subscriptions) &&
    connection.inbox_subscriptions! >= 0 &&
    (connection.stale_streams === undefined ||
      (Number.isInteger(connection.stale_streams) && connection.stale_streams! >= 0));
  const knownSource = connection?.source === "embedded_bus" && connection.scope === "this_server_process";
  const trusted = !stale && !!fresh && !!observed && countsValid && knownSource;
  const connected = trusted && connection?.state === "connected" && connection.active_streams! > 0;
  const notObserved = trusted && connection?.state === "not_observed" && connection.active_streams === 0;
  const heartbeatStale = trusted && connection?.state === "stale" && connection.active_streams === 0;
  const label = stale ? "Hub connection snapshot unverified" : !connection ? "Hub connection unknown"
    : expired ? "Hub connection snapshot expired" : connected ? "Hub stream observed"
    : heartbeatStale ? "Connection stale — heartbeat not recently observed"
    : notObserved ? "No active push stream observed" : "Hub connection unavailable";
  return { label, connected, trusted, observed };
}
