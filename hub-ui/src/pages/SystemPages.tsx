import { connectionView, observationTime } from "../agentStatus";
import type { Hub } from "../hub";
import type { AgentSummary } from "../types";
import { Badge, Icon, PageHeader, PanelTitle, names, pretty } from "../ui";
import { WorkQueuePanel } from "../WorkQueuePanel";
import { StagingBadge } from "./WorkPages";

function HubConnectionRow({ agent, stale }: { agent: AgentSummary; stale: boolean }) {
  const connection = agent.connection;
  const view = connectionView(agent, stale);
  return (
    <div className="hub-connection-row">
      <span className={`agent-avatar ${agent.id}`}>{(names[agent.id] ?? "?").charAt(0)}</span>
      <div>
        <strong>{names[agent.id] ?? "Unknown agent"}</strong>
        <span>{view.label}</span>
        {view.trusted && connection?.state !== "unavailable" && (
          <small>{connection!.active_streams} active stream(s) · {connection!.inbox_subscriptions} inbox subscription(s)</small>
        )}
        {view.connected && (
          <small>{connection!.inbox_subscriptions! > 0 ? "Inbox subscription observed: new messages can be pushed to this agent."
            : "No active inbox subscription observed. Delivery may still happen another way; this alone does not show the agent is ready."}</small>
        )}
        {view.observed && <small>Snapshot observed: {view.observed}</small>}
        {view.trusted && connection?.state !== "unavailable" && observationTime(connection?.last_stream_write_at) && (
          <small>Last stream write: {observationTime(connection?.last_stream_write_at)}</small>
        )}
      </div>
      <span className={`presence ${view.connected ? "on" : ""}`} aria-hidden="true" />
    </div>
  );
}

export function AgentsPage({ hub, actions }: { hub: Hub; actions: React.ReactNode }) {
  const { peers, demo, stale, topicStale } = hub;
  return (
    <>
      <PageHeader eyebrow="AGENTS" title="Agents"
        subtitle="Whether each agent's app is connected to the Hub, and whether it can be reached on a specific conversation topic. These are checked separately.">
        {actions}
      </PageHeader>
      <section className="panel agents-panel" id="agents">
        <PanelTitle title="Agents" />
        <div className="agents-columns">
          <div>
            <h3 className="agent-section-title">Connected to the Hub</h3>
            <div className="hub-connection-list">
              {hub.data.agents.map((agent) => <HubConnectionRow key={agent.id} agent={agent} stale={!demo && stale} />)}
            </div>
            <p className="panel-footnote">
              Connected means the agent's app is linked to the Hub. It doesn't prove the agent
              is working on anything right now, and not connected doesn't mean it's offline.
            </p>
          </div>
          <div>
            <h3 className="agent-section-title">Topic readiness</h3>
            <form className="topic-form" onSubmit={hub.checkTopic}>
              <label htmlFor="topic-root">TOPIC ROOT</label>
              <div>
                <input id="topic-root" value={hub.topicDraft} onChange={(event) => hub.setTopicDraft(event.target.value)}
                  pattern="[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}" maxLength={128} required spellCheck={false} />
                <button className="secondary small" disabled={hub.loading}>Check</button>
              </div>
            </form>
            {hub.topicError && <p role="alert" className="notice error">{hub.topicError}</p>}
            {peers?.tracking === "not_configured" && !hub.topicError && (
              <p className="topic-tracking">{!demo && (stale || topicStale)
                ? "Topic readiness: Snapshot unverified" : "Topic readiness: Not tracked yet"}</p>
            )}
            <div className="peer-list">
              {peers?.tracking !== "not_configured" && peers?.items.map((peer) => {
                const state = hub.peerState(peer);
                return (
                  <div className="peer-row" key={peer.agent}>
                    <span className={`agent-avatar ${peer.agent}`}>{names[peer.agent].charAt(0)}</span>
                    <div>
                      <strong>{names[peer.agent]}</strong>
                      <small>
                        {state === "unknown_stale" ? "Can't confirm it's reachable right now"
                          : state === "busy_queued" ? "Bound session can queue work"
                            : state === "reachable" ? "Exact-topic route verified" : pretty(state)}
                      </small>
                      <small>
                        {pretty(peer.reason)}
                        {state !== peer.status ? !demo && (stale || topicStale) ? " · Snapshot unverified" : " · Evidence expired" : ""}
                      </small>
                    </div>
                    <Badge tone={state === "reachable" ? "forest" : state === "busy_queued" ? "amber" : "neutral"}>
                      {state === "unknown_stale" ? "Unknown" : state === "linked_unverified" ? "Unverified"
                        : state === "busy_queued" ? "Busy" : state === "reachable" ? "Online · verified" : pretty(state)}
                    </Badge>
                  </div>
                );
              })}
            </div>
            <div className="advisory">
              <span>↳</span>
              <p>
                This is a best guess. Messages are kept even when an agent is offline, and being
                reachable doesn't mean a message was read or acted on.
              </p>
            </div>
            <p className="panel-footnote">
              {demo ? "All agent states above are illustrative fixtures."
                : "An agent shows as unknown until the Hub can confirm its live session for this topic."}
            </p>
          </div>
        </div>
      </section>
    </>
  );
}

export function StagingPage({ hub, actions }: { hub: Hub; actions: React.ReactNode }) {
  const { data } = hub;
  return (
    <>
      <PageHeader eyebrow="STAGING" title="Staging"
        subtitle="Staging targets, the emergency pause, execution evidence and the validation queue. Software only.">
        {actions}
      </PageHeader>
      <div className="staging-layout">
        <section className="panel" id="staging">
          <PanelTitle title="Staging overview"><Badge tone="neutral">SOFTWARE ONLY</Badge></PanelTitle>
          <p className="panel-description">Independent projects. Explicit activation. No production shortcuts.</p>
          <div className="staging-control">
            <div>
              <span className={`status-orb ${data.paused ? "paused" : ""}`} />
              <span>
                <strong>{data.paused ? "Emergency pause on: new staging work is blocked" : "Emergency pause off"}</strong>
                <small>
                  {!data.autonomy.configured ? "Automatic runner not set up. Agents only act on the tasks shown here."
                    : `Automatic runner: ${pretty(data.autonomy.state)}`}
                </small>
              </span>
            </div>
            <button className="secondary small" disabled={!hub.writable} onClick={() => hub.requestPause(!data.paused)}>
              <Icon kind="pause" size={14} /> {data.paused ? "Resume" : "Pause new work"}
            </button>
          </div>
          <div className="project-list">
            {data.projects.map((item) => (
              <a className="project-row" key={item.id} href={`#/projects/${item.id}`}>
                <span className="project-icon"><Icon kind="folder" /></span>
                <div>
                  <strong>{item.name}</strong>
                  <small>
                    {item.executor_enabled ? "Staging target enabled"
                      : "No staging target yet. Setting one up needs an inventory of the target and your acceptance."}
                  </small>
                </div>
                <StagingBadge hub={hub} id={item.id} />
              </a>
            ))}
          </div>
          {data.runs.length > 0 && (
            <div className="run-list">
              <h3>Execution evidence</h3>
              {data.runs.map((run) => (
                <details key={run.id}>
                  <summary>
                    {hub.projectName(run.project)}{" "}
                    <Badge tone={run.status === "held" || run.status === "failed" ? "amber" : "neutral"}>{pretty(run.status)}</Badge>
                  </summary>
                  <p className="small-text">Target: {run.target}</p>
                  {run.status === "held" && run.events.at(-1)?.reason && (
                    <p className="small-text">Needs attention: {pretty(run.events.at(-1)!.reason!)}</p>
                  )}
                  <code>{run.artifact_digest}</code>
                  <p className="small-text">Fence {run.fencing_generation} · {run.events.length} recorded events</p>
                </details>
              ))}
            </div>
          )}
          <p className="panel-footnote">Production, GitHub merge and real processing capabilities are not enabled.</p>
        </section>
        <WorkQueuePanel mode={hub.demo ? "demo" : "live"} refreshToken={data}
          stale={hub.stale} onUnauthorized={hub.clearPrivateWorkspace} />
      </div>
    </>
  );
}
