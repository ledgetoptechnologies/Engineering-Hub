import { useEffect, useState } from "react";
import { HubApiError } from "../api";
import { relativeAge } from "../dashboardText";
import { listTopics, rememberedTopics, rememberTopic, type TopicEntry } from "../topicsApi";
import { link } from "../router";
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
          <small>{connection!.active_streams} fresh stream(s) · {connection!.inbox_subscriptions} inbox subscription(s)
            {connection!.stale_streams !== undefined && connection!.stale_streams !== null &&
              ` · ${connection!.stale_streams} stale stream(s)`}</small>
        )}
        {view.connected && (
          <small>{connection!.inbox_subscriptions! > 0 ? "Inbox subscription observed. A server stream write does not prove client receipt or chat readiness."
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
  const [serverTopics, setServerTopics] = useState<TopicEntry[] | null>(null);
  const [remembered, setRemembered] = useState<string[]>(rememberedTopics);
  useEffect(() => {
    if (demo) return;
    const controller = new AbortController();
    listTopics(controller.signal).then((value) => { if (!controller.signal.aborted) setServerTopics(value); })
      .catch((error) => {
        if (controller.signal.aborted) return;
        if (error instanceof HubApiError && [401, 403].includes(error.status)) hub.clearPrivateWorkspace();
        else setServerTopics(null);
      });
    return () => controller.abort();
  }, [demo, hub.data]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setRemembered(rememberTopic(hub.topic)); }, [hub.topic]);
  const topics: TopicEntry[] = serverTopics ?? remembered.map((topic) => ({ topic, last_activity_at: null, peer_count: null, source: "device" }));
  if (!topics.some((entry) => entry.topic === hub.topic))
    topics.unshift({ topic: hub.topic, last_activity_at: null, peer_count: null, source: "device" });
  const connected = hub.data.agents.filter((agent) => connectionView(agent, !demo && stale).connected).length;
  return (
    <>
      <PageHeader eyebrow="AGENTS" title="Agents"
        subtitle="Recent Hub stream observations and reachability on a specific conversation topic. These are checked separately.">
        {actions}
      </PageHeader>
      <div id="agents" className="agents-page">
      <h2 className="agent-section-title">Hub stream observations</h2>
      <section className="agent-cards" aria-label="Hub stream observations">
        {hub.data.agents.map((agent) => <HubConnectionRow key={agent.id} agent={agent} stale={!demo && stale} />)}
      </section>
      <p className="panel-footnote">
        {connected} of {hub.data.agents.length} with a Hub stream observed. Stream observed means the Hub recently wrote to a
        retained stream. An upstream connection may still accept writes after the agent stops listening. This does not prove
        client receipt, listening, or work. Stale means recent stream activity is missing; it doesn't prove the agent is
        offline. Topic readiness is checked separately.
      </p>
      <section className="panel agents-panel" id="topics">
        <PanelTitle title="Topics" count={topics.length} />
        <div className="topics-layout">
          <div className="topic-directory">
            <form className="topic-form" onSubmit={hub.checkTopic}>
              <label htmlFor="topic-root">TOPIC ROOT</label>
              <div>
                <input id="topic-root" value={hub.topicDraft} onChange={(event) => hub.setTopicDraft(event.target.value)}
                  pattern="[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}" maxLength={128} required spellCheck={false} />
                <button className="secondary small" disabled={hub.loading}>Check</button>
              </div>
            </form>
            <ul className="topic-list" aria-label="Topics">
              {topics.map((entry) => (
                <li key={entry.topic}>
                  <button type="button" className={entry.topic === hub.topic ? "topic-item current" : "topic-item"}
                    aria-current={entry.topic === hub.topic ? "true" : undefined} onClick={() => hub.selectTopic(entry.topic)}>
                    <span className="topic-name">{entry.topic}</span>
                    <span className="topic-meta">
                      {entry.peer_count !== null ? `${entry.peer_count} agents` : ""}
                      {entry.last_activity_at ? ` · ${relativeAge(entry.last_activity_at)}` : ""}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            <p className="panel-footnote">
              {serverTopics ? "Topics available to your account."
                : "Topics checked on this device only. The Hub can't list the topics available to your account yet."}
            </p>
          </div>
          <div className="topic-detail" aria-label={`Readiness for ${hub.topic}`}>
            <h3 className="agent-section-title">Readiness on <code>{hub.topic}</code></h3>
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
      </div>
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
            {data.projects.filter((item) => !item.archived).map((item) => (
              <a className="project-row" key={item.id} href={link(`/projects/${item.id}`)}>
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
