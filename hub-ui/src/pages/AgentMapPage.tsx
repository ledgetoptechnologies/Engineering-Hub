/** Agents map: live sessions, the agents behind them, the topics they talk on, and what nobody has picked up. */
import { useEffect, useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import { demoActivity, readActivity, type ActivitySnapshot, type OpenMessage } from "../agentActivityApi";
import { demoAgentConnections, httpAgentConnections, type Installation, type Session } from "../agentConnectionsApi";
import { COLUMNS, MAP_WIDTH, agentKey, buildAgents, buildSessions, connectedTo, fit, layoutMap, sessionKey, topicKey,
  type AgentLoad, type MapLine, type SessionLoad } from "../agentMap";
import { connectionView } from "../agentStatus";
import { HubApiError } from "../api";
import { relativeAge } from "../dashboardText";
import type { Hub } from "../hub";
import { Badge, PageHeader, PanelTitle, names } from "../ui";

const REFRESH_MS = 15_000;
const NO_TOPIC = "No topic";
const AGENT_LOAD: Record<AgentLoad, { label: string; tone: string }> = {
  working: { label: "Working", tone: "forest" }, waiting: { label: "Not picked up", tone: "amber" },
  idle: { label: "Idle", tone: "neutral" }, offline: { label: "No stream", tone: "neutral" },
};
const SESSION_LOAD: Record<SessionLoad, string> = { working: "Working", active: "Active", quiet: "Quiet" };

interface Directory { installations: Installation[]; sessions: Record<string, Session[]> }

function useActivity(hub: Hub) {
  const [activity, setActivity] = useState<ActivitySnapshot | null>(null);
  const [state, setState] = useState<"loading" | "loaded" | "unsupported" | "failed">("loading");
  useEffect(() => {
    if (hub.demo) { setActivity(demoActivity()); setState("loaded"); return; }
    let controller = new AbortController();
    const load = () => {
      if (document.hidden) return;
      controller.abort();
      controller = new AbortController();
      const { signal } = controller;
      readActivity(signal).then(value => {
        if (signal.aborted) return;
        setActivity(value);
        setState(value ? "loaded" : "unsupported");
      }).catch(error => {
        if (signal.aborted) return;
        if (error instanceof HubApiError && [401, 403].includes(error.status)) hub.clearPrivateWorkspace();
        else setState("failed");
      });
    };
    load();
    const timer = window.setInterval(load, REFRESH_MS);
    document.addEventListener("visibilitychange", load);
    return () => { controller.abort(); window.clearInterval(timer); document.removeEventListener("visibilitychange", load); };
  }, [hub.demo]); // eslint-disable-line react-hooks/exhaustive-deps
  return { activity, state };
}

/** Sessions are owner-only. Anyone else still gets the agents and topics, so a refusal is not an error. */
function useDirectory(hub: Hub) {
  const [directory, setDirectory] = useState<Directory | null>(null);
  useEffect(() => {
    if (hub.demo) { setDirectory(demoAgentConnections()); return; }
    const controller = new AbortController();
    const client = httpAgentConnections();
    const load = async () => {
      const installations = (await client.installations(controller.signal)).filter(item => item.state === "active");
      const lists = await Promise.all(installations.map(item => client.sessions(item.id, controller.signal)));
      if (!controller.signal.aborted)
        setDirectory({ installations, sessions: Object.fromEntries(installations.map((item, index) => [item.id, lists[index]])) });
    };
    const run = () => { if (!document.hidden) load().catch(() => { if (!controller.signal.aborted) setDirectory(null); }); };
    run();
    const timer = window.setInterval(run, REFRESH_MS * 4);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [hub.demo]);
  return directory;
}

function Node({ x, y, width, id, selected, dim, label, onSelect, children }: {
  x: number; y: number; width: number; id: string; selected: boolean; dim: boolean; label: string;
  onSelect: (id: string) => void; children: ReactNode;
}) {
  const key = (event: KeyboardEvent) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    onSelect(id);
  };
  return <g className={`map-node${selected ? " selected" : ""}${dim ? " dim" : ""}`} transform={`translate(${x} ${y})`} role="button" tabIndex={0}
    aria-label={label} aria-pressed={selected} onClick={() => onSelect(id)} onKeyDown={key}>
    <title>{label}</title>
    <rect width={width} height={40} rx={8} />
    {children}
  </g>;
}

function Line({ line, dim }: { line: MapLine; dim: boolean }) {
  const bend = (line.x2 - line.x1) / 2;
  const path = `M${line.x1} ${line.y1} C${line.x1 + bend} ${line.y1} ${line.x2 - bend} ${line.y2} ${line.x2} ${line.y2}`;
  const state = line.waiting > 0 ? " waiting" : line.handling ? " handling" : "";
  return <g className={`map-line${state}${dim ? " dim" : ""}`}>
    <path d={path} />
  </g>;
}

function topicMatches(selected: string | null, message: OpenMessage) {
  if (!selected) return true;
  if (selected.startsWith("topic:")) return selected === topicKey(message.topic);
  return selected === agentKey(message.recipient) || selected === agentKey(message.sender);
}

function OpenMessages({ messages, total, selected, now }: { messages: OpenMessage[]; total: number; selected: string | null; now: number }) {
  const shown = messages.filter(item => topicMatches(selected, item));
  const waiting = shown.filter(item => item.status === "pending").length;
  return <section className="panel" aria-label="Open messages">
    <PanelTitle title="Not picked up" count={selected ? waiting : messages.filter(item => item.status === "pending").length} />
    {shown.length === 0 ? <p className="panel-footnote">Nothing open.</p> : <div className="map-table"><table className="data-table map-messages">
      <thead><tr><th scope="col">Waiting</th><th scope="col">To</th><th scope="col">From</th><th scope="col">Topic</th><th scope="col">Type</th><th scope="col">State</th></tr></thead>
      <tbody>{shown.map(item => <tr key={item.id}>
        <td title={item.created_at}>{relativeAge(item.created_at, now) ?? "Now"}</td>
        <th scope="row">{names[item.recipient]}</th>
        <td>{names[item.sender]}</td>
        <td>{item.topic ? <code>{item.topic}</code> : <span className="muted">{NO_TOPIC}</span>}</td>
        <td>{item.message_type}{item.priority === "high" || item.priority === "urgent" ? <Badge tone="amber">{item.priority}</Badge> : null}</td>
        <td>{item.status === "pending" ? <Badge tone="amber">Not picked up</Badge> : <Badge tone="forest">Being handled</Badge>}</td>
      </tr>)}</tbody>
    </table></div>}
    {total > messages.length && <p className="panel-footnote">Oldest {messages.length} of {total} shown.</p>}
  </section>;
}

export function AgentMapPage({ hub, actions }: { hub: Hub; actions: ReactNode }) {
  const { activity, state } = useActivity(hub);
  const directory = useDirectory(hub);
  const [selected, setSelected] = useState<string | null>(null);
  const [allTopics, setAllTopics] = useState(false);
  const now = activity ? Date.parse(activity.observed_at) : Date.now();
  const tasks = hub.data.tasks;
  const summaries = hub.agents;
  const layout = useMemo(() => {
    if (!activity) return null;
    const connected = (id: string) => {
      const agent = summaries.find(item => item.id === id);
      return !!agent && connectionView(agent, !hub.demo && hub.stale).connected;
    };
    const sessions = directory ? buildSessions(directory.installations, directory.sessions, tasks, now) : [];
    const streams = (id: string) => summaries.find(item => item.id === id)?.connection?.active_streams ?? 0;
    return layoutMap(buildAgents(activity, connected, tasks, streams), sessions, activity.topics, allTopics);
  }, [activity, directory, tasks, summaries, hub.demo, hub.stale, now, allTopics]);
  const lit = layout ? connectedTo(selected, layout.lines) : null;
  const dim = (id: string) => lit !== null && !lit.has(id);
  const select = (id: string) => setSelected(current => current === id ? null : id);
  const working = layout?.agents.filter(({ item }) => item.load === "working").length ?? 0;
  const unclaimed = activity?.agents.reduce((sum, item) => sum + item.pending, 0) ?? 0;

  return <>
    <PageHeader eyebrow="AGENTS" title="Map">{actions}</PageHeader>
    <div className="agents-page">
      {hub.demo && <p className="notice">Demo: these sessions and messages are examples.</p>}
      {state === "loading" && <p role="status">Reading activity…</p>}
      {state === "unsupported" && <p className="notice">This Hub needs an update before it can show the map.</p>}
      {state === "failed" && <p role="alert" className="notice error">{activity ? "Couldn't refresh. Showing the last reading." : "Activity couldn't be read."}</p>}
      {activity && layout && <>
        <dl className="map-stats">
          <div><dt>Working</dt><dd>{working}<small> of {layout.agents.length}</small></dd></div>
          <div className={unclaimed > 0 ? "attention" : undefined}><dt>Not picked up</dt><dd>{unclaimed}</dd></div>
          <div><dt>Live sessions</dt><dd>{directory ? layout.sessions.length : "–"}</dd></div>
          <div><dt>Topics</dt><dd>{activity.topics.filter(item => item.topic !== null).length}</dd></div>
        </dl>
        <section className="panel agent-map" aria-label="Agent map">
          <svg viewBox={`0 0 ${MAP_WIDTH} ${layout.height}`} role="group" aria-label="Sessions, agents and topics">
            {layout.lines.map(line => <Line key={`${line.from}>${line.to}`} line={line} dim={lit !== null && !(lit.has(line.from) && lit.has(line.to))} />)}
            {layout.sessions.map(({ item, y }) => {
              const id = sessionKey(item.id);
              return <Node key={id} id={id} x={COLUMNS.session.x} y={y} width={COLUMNS.session.width} selected={selected === id} dim={dim(id)} onSelect={select}
                label={`${item.label} on ${item.host}, ${SESSION_LOAD[item.load]}${item.task ? `: ${item.task.title}` : ""}`}>
                <circle className={`map-dot ${item.load}`} cx={14} cy={20} r={4.5} />
                <text x={27} y={17} className="map-title">{fit(item.label, 26)}</text>
                <text x={27} y={31} className="map-sub">{fit(item.task?.title ?? `${item.host} · ${relativeAge(item.lastUsedAt, now) ?? "not used yet"}`, 30)}</text>
              </Node>;
            })}
            {layout.agents.map(({ item, y }) => {
              const id = agentKey(item.id);
              const load = AGENT_LOAD[item.load];
              return <Node key={id} id={id} x={COLUMNS.agent.x} y={y} width={COLUMNS.agent.width} selected={selected === id} dim={dim(id)} onSelect={select}
                label={`${names[item.id]}, ${load.label}, ${item.pending} not picked up`}>
                <circle className={`map-dot ${item.load}`} cx={14} cy={20} r={4.5} />
                <text x={27} y={17} className="map-title">{names[item.id]}</text>
                <text x={27} y={31} className="map-sub">{fit([load.label, item.streams > 0 ? `${item.streams} streams` : "",
                  item.pending > 0 && item.load !== "waiting" ? `${item.pending} waiting` : ""].filter(Boolean).join(" · "), 32)}</text>
              </Node>;
            })}
            {layout.topics.map(({ item, y }) => {
              const id = topicKey(item.topic);
              const name = item.topic ?? NO_TOPIC;
              return <Node key={id} id={id} x={COLUMNS.topic.x} y={y} width={COLUMNS.topic.width} selected={selected === id} dim={dim(id)} onSelect={select}
                label={`${name}, ${item.pending} not picked up, ${item.messages} messages`}>
                <text x={12} y={17} className="map-title">{fit(name, 32)}</text>
                <text x={12} y={31} className="map-sub">{item.messages} msg{item.last_activity_at ? ` · ${relativeAge(item.last_activity_at, now) ?? "now"}` : ""}</text>
                {item.pending > 0 && <g className="map-count" transform={`translate(${COLUMNS.topic.width - 22} 20)`}>
                  <circle r={11} /><text textAnchor="middle" dy={4}>{item.pending}</text></g>}
              </Node>;
            })}
          </svg>
          <div className="map-foot">
            <span>Topic lines: last {activity.window_hours} hours of messages. Sessions don't report their topic to the Hub yet.</span>
            {(layout.hiddenTopics > 0 || allTopics) && <button type="button" className="secondary small" onClick={() => setAllTopics(value => !value)}>
              {allTopics ? "Fewer topics" : `${layout.hiddenTopics} more topics`}</button>}
          </div>
        </section>
        <OpenMessages messages={activity.open} total={activity.open_total} selected={selected} now={now} />
      </>}
    </div>
  </>;
}
