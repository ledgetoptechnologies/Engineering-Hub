import { AgentConnectionsPanel } from "../AgentConnectionsPanel";
import { useEffect, useState } from "react";
import { HubApiError } from "../api";
import { relativeAge } from "../dashboardText";
import { listTopics, rememberedTopics, rememberTopic, visibleTopics, type TopicEntry } from "../topicsApi";
import { link } from "../router";
import { connectionView, observationTime } from "../agentStatus";
import type { Hub } from "../hub";
import type { AgentSummary } from "../types";
import { Badge, Icon, PageHeader, PanelTitle, names, pretty } from "../ui";
import { WorkQueuePanel } from "../WorkQueuePanel";
import { StagingRuntimePanel } from "../StagingRuntimePanel";
import { RunReleaseEvidence } from "../ReleaseEvidencePanel";
import { StagingResourcesPanel } from "../StagingResourcesPanel";
import { SessionReconciliationPanel } from "../SessionReconciliationPanel";
import { SharedStagingPanel } from "../SharedStagingPanel";
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

/** Agent installations on their own page: pairing, sessions and Hub access, apart from the status overview. */
function AgentInstallationsPage({ hub, actions }: { hub: Hub; actions: React.ReactNode }) {
  return (
    <>
      <PageHeader eyebrow="AGENTS / INSTALLATIONS" title="Installations">
        {actions}
      </PageHeader>
      <div id="agents" className="agents-page">
        {/* Keyed on the signed-in actor: a different person gets a fresh panel, with nothing of the previous owner's kept or late-filled. */}
        <AgentConnectionsPanel key={hub.session?.actor ?? "signed-out"} hub={hub} />
        <p className="panel-footnote">
          Installations are listed only when this Hub runs agent sessions. Stream observations and topic readiness
          are on the <a className="text-link" href={link("/agents")}>Agents overview</a>.
        </p>
      </div>
    </>
  );
}

export function AgentsPage({ hub, actions, view = "overview" }: { hub: Hub; actions: React.ReactNode; view?: "overview" | "installations" }) {
  return view === "installations" ? <AgentInstallationsPage hub={hub} actions={actions} /> : <AgentsOverview hub={hub} actions={actions} />;
}

export function TopicList({ topics, current, onSelect, hasServer }: {
  topics: TopicEntry[]; current: string; onSelect: (topic: string) => void; hasServer: boolean;
}) {
  return <>
    <ul className="topic-list" aria-label="Topics">
      {topics.map((entry) => (
        <li key={entry.topic}>
          <button type="button" className={entry.topic === current ? "topic-item current" : "topic-item"}
            aria-current={entry.topic === current ? "true" : undefined} onClick={() => onSelect(entry.topic)}>
            <span className="topic-name">{entry.topic}</span>
            <span className="topic-meta">
              {entry.source === "device" ? "This device" : "Hub directory"}
              {entry.peer_count !== null ? ` · ${entry.peer_count} agents` : ""}
              {entry.last_activity_at ? ` · ${relativeAge(entry.last_activity_at)}` : ""}
            </span>
          </button>
        </li>
      ))}
    </ul>
    <p className="panel-footnote">
      {hasServer ? "Hub directory entries are available to your account. This device entries are local selections."
        : "Topics saved or selected on this device. The Hub can't list the topics available to your account yet."}
    </p>
  </>;
}

function AgentsOverview({ hub, actions }: { hub: Hub; actions: React.ReactNode }) {
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
  const topics = visibleTopics(serverTopics, remembered, hub.topic);
  return (
    <>
      <PageHeader eyebrow="AGENTS" title="Agents">
        {actions}
      </PageHeader>
      <div id="agents" className="agents-page">
      <h2 className="agent-section-title">Hub stream observations</h2>
      <section className="agent-cards" aria-label="Hub stream observations">
        {hub.agents.map((agent) => <HubConnectionRow key={agent.id} agent={agent} stale={!demo && stale} />)}
      </section>
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
            <TopicList topics={topics} current={hub.topic} onSelect={hub.selectTopic} hasServer={serverTopics !== null} />
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
            {demo && <p className="panel-footnote">All agent states above are illustrative fixtures.</p>}
          </div>
        </div>
      </section>
      </div>
    </>
  );
}

/** One line saying how many projects have a staging target configured (configuration only, not verified readiness), so a row of "no target" badges doesn't read as a fault. */
export function stagingSummary(projects: { executor_enabled: boolean }[]): string {
  const ready = projects.filter((item) => item.executor_enabled).length;
  if (ready === 0) return "No projects are connected to the staging PC yet. Connecting one needs an inventory of the target and your acceptance.";
  return `${ready} of ${projects.length} project${projects.length === 1 ? " has" : "s have"} a staging target enabled.`;
}

export function StagingPage({ hub, actions, view = "overview" }: {
  hub: Hub; actions: React.ReactNode; view?: "overview" | "queues" | "resources";
}) {
  const { data } = hub;
  if (view === "queues") return (
    <>
      <PageHeader eyebrow="STAGING / QUEUES & SESSIONS" title="Queues & sessions">
        {actions}
      </PageHeader>
      <div className="staging-layout single">
        {(hub.demo || hub.session?.authenticated) && (
          <SharedStagingPanel mode={hub.demo ? "demo" : "live"} csrf={hub.session?.csrf_token ?? null}
            writable={hub.writable} stale={hub.stale} onUnauthorized={hub.clearPrivateWorkspace} />
        )}
        {!hub.demo && data.autonomy.configured && hub.session?.authenticated && (
          <SessionReconciliationPanel csrf={hub.session.csrf_token}
            enabled={true} writable={hub.writable} stale={hub.stale}
            onAuthError={hub.clearPrivateWorkspace} />
        )}
        <WorkQueuePanel mode={hub.demo ? "demo" : "live"} refreshToken={data}
          stale={hub.stale} onUnauthorized={hub.clearPrivateWorkspace} />
      </div>
    </>
  );
  if (view === "resources") return (
    <>
      <PageHeader eyebrow="STAGING / KEPT RESOURCES" title="Kept resources">
        {actions}
      </PageHeader>
      <div className="staging-layout single">
        <StagingResourcesPanel mode={hub.demo ? "demo" : "live"} refreshToken={data}
          csrf={hub.session?.csrf_token ?? null} writable={hub.writable} stale={hub.stale}
          projects={data.projects.filter((item) => !item.archived).map((item) => ({ id: item.id, name: item.name }))}
          viewerActor={hub.session?.actor ?? null} onUnauthorized={hub.clearPrivateWorkspace} />
      </div>
    </>
  );
  return (
    <>
      <PageHeader eyebrow="STAGING" title="Staging">
        {actions}
      </PageHeader>
      <div className="staging-layout">
        <section className="panel" id="staging">
          <PanelTitle title="Staging overview"><Badge tone="neutral">SOFTWARE ONLY</Badge></PanelTitle>
          <div className="staging-control">
            <div>
              <span className={`status-orb ${data.paused ? "paused" : ""}`} />
              <span>
                <strong>{data.paused ? "Emergency pause on: new staging work is blocked" : "Emergency pause off"}</strong>
                <small>
                  {!data.autonomy.configured ? "No automatic runner is set up, so nothing runs on its own. Agents only act on the tasks shown here."
                    : `Automatic runner: ${pretty(data.autonomy.state)}`}
                </small>
                <small>The pause is an emergency stop: it blocks new automated staging work until you resume.</small>
              </span>
            </div>
            <button className="secondary small" disabled={!hub.writable} onClick={() => hub.requestPause(!data.paused)}>
              <Icon kind="pause" size={14} /> {data.paused ? "Resume" : "Pause new work"}
            </button>
          </div>
          <p className="staging-summary" role="status">{stagingSummary(data.projects.filter((item) => !item.archived))}</p>
          <div className="project-list">
            {data.projects.filter((item) => !item.archived).map((item) => (
              <a className="project-row" key={item.id} href={link(`/projects/${item.id}`)}>
                <span className="project-icon"><Icon kind="folder" /></span>
                <div>
                  <strong>{item.name}</strong>
                  <small>
                    {item.executor_enabled ? "Staging target enabled" : "Not connected to the staging PC yet"}
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
                  {!hub.demo && hub.session?.authenticated && (
                    <RunReleaseEvidence key={`${run.id}-${hub.session?.actor ?? "signed-out"}`} runId={run.id} onUnauthorized={hub.clearPrivateWorkspace} />
                  )}
                </details>
              ))}
            </div>
          )}
          <p className="panel-footnote">Production, GitHub merge and real processing capabilities are not enabled.</p>
        </section>
        {(hub.demo || hub.session?.authenticated) && (
          <StagingRuntimePanel mode={hub.demo ? "demo" : "live"} onUnauthorized={hub.clearPrivateWorkspace} />
        )}
      </div>
      <p className="panel-footnote">
        Queues and session review are under <a className="text-link" href={link("/staging/queues")}>Queues &amp; sessions</a>;
        kept items and clean-up are under <a className="text-link" href={link("/staging/resources")}>Kept resources</a>.
      </p>
    </>
  );
}
