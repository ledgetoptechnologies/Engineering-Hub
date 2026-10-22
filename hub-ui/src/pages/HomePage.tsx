import { link } from "../router";
import { connectionView } from "../agentStatus";
import type { Hub } from "../hub";
import { Badge, Icon, PageHeader, names } from "../ui";
import { answeredDecisions, openDecisions, RecentAnswers } from "./decisions";
import { statusLabel, statusTone } from "./tasks";

function greeting(now = new Date()) {
  const hour = now.getHours();
  return hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
}

export function HomePage({ hub, actions }: { hub: Hub; actions: React.ReactNode }) {
  const waiting = openDecisions(hub);
  const tasks = hub.data.tasks;
  const active = tasks.filter((task) => task.status !== "verified");
  const connected = hub.data.agents.filter((agent) => connectionView(agent, !hub.demo && hub.stale).connected);
  const targets = hub.data.projects.filter((item) => item.executor_enabled).length;
  return (
    <>
      <PageHeader eyebrow="DEVELOPMENT / STAGING" title={greeting()}
        subtitle="What needs you, what's moving, and who's around.">{actions}</PageHeader>
      <div className="kpi-grid">
        <a className={`kpi ${waiting.length ? "attention" : ""}`} href={link("/inbox")}>
          <span className="kpi-label"><Icon kind="inbox" size={16} /> Needs you</span>
          <strong>{waiting.length}</strong>
          <span className="kpi-note">{waiting.length === 0 ? "Nothing waiting" : waiting.length === 1 ? "decision" : "decisions"}</span>
        </a>
        <a className="kpi" href={link("/work")}>
          <span className="kpi-label"><Icon kind="work" size={16} /> Work in motion</span>
          <strong>{active.length}</strong>
          <span className="kpi-note">{tasks.length - active.length} review passed</span>
        </a>
        <a className="kpi" href={link("/agents")}>
          <span className="kpi-label"><Icon kind="agents" size={16} /> Agents connected</span>
          <strong>{connected.length}<span className="kpi-of">/{hub.data.agents.length}</span></strong>
          <span className="kpi-note">{hub.stale && !hub.demo ? "Not verified right now" : "Hub connection"}</span>
        </a>
        <a className="kpi" href={link("/staging")}>
          <span className="kpi-label"><Icon kind="staging" size={16} /> Staging</span>
          <strong>{targets}<span className="kpi-of">/{hub.data.projects.length}</span></strong>
          <span className="kpi-note">{hub.data.paused ? "Emergency pause on" : "targets set up"}</span>
        </a>
      </div>
      <div className="home-grid">
        <section className="panel" aria-label="Needs you">
          <div className="panel-title">
            <h2>Needs you</h2>
            <span className="count">{waiting.length}</span>
            <span className="panel-action"><a className="text-link" href={link("/inbox")}>Open inbox →</a></span>
          </div>
          {waiting.length === 0 ? (
            <p className="panel-description">Nothing is waiting on you right now.</p>
          ) : (
            <ul className="preview-list">
              {waiting.slice(0, 6).map((decision) => (
                <li key={decision.id}>
                  <a href={link("/inbox")}>
                    <span className="preview-title">{decision.title}</span>
                    <span className="preview-meta">
                      {hub.projectName(decision.project)}{decision.protected ? " · Protected" : ""}
                    </span>
                  </a>
                </li>
              ))}
              {waiting.length > 6 && <li className="preview-more"><a href={link("/inbox")}>+{waiting.length - 6} more</a></li>}
            </ul>
          )}
          <RecentAnswers hub={hub} decisions={answeredDecisions(hub, undefined, 2)} />
        </section>
        <section className="panel" aria-label="Work in motion summary">
          <div className="panel-title">
            <h2>Work in motion</h2>
            <span className="count">{active.length}</span>
            <span className="panel-action"><a className="text-link" href={link("/work")}>All work →</a></span>
          </div>
          {active.length === 0 ? (
            <p className="panel-description">No open tasks.</p>
          ) : (
            <ul className="preview-list">
              {active.slice(0, 6).map((task) => (
                <li key={task.id}>
                  <a href={link("/work")}>
                    <span className="preview-title">{task.title}</span>
                    <span className="preview-meta">
                      {hub.projectName(task.project)} · {task.owner ? names[task.owner] : "Unclaimed"}
                    </span>
                  </a>
                  <Badge tone={statusTone(task)}>{statusLabel(task)}</Badge>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="panel" aria-label="Agents summary">
          <div className="panel-title">
            <h2>Agents</h2>
            <span className="panel-action"><a className="text-link" href={link("/agents")}>Details →</a></span>
          </div>
          <ul className="agent-mini">
            {hub.data.agents.map((agent) => {
              const view = connectionView(agent, !hub.demo && hub.stale);
              return (
                <li key={agent.id}>
                  <span className={`agent-avatar ${agent.id}`}>{(names[agent.id] ?? "?").charAt(0)}</span>
                  <span>
                    <strong>{names[agent.id] ?? agent.id}</strong>
                    <small>{view.label}</small>
                  </span>
                  <span className={`presence ${view.connected ? "on" : ""}`} aria-hidden="true" />
                </li>
              );
            })}
          </ul>
        </section>
        <section className="panel" aria-label="Projects summary">
          <div className="panel-title">
            <h2>Projects</h2>
            <span className="panel-action"><a className="text-link" href={link("/projects")}>All projects →</a></span>
          </div>
          <ul className="project-mini">
            {hub.data.projects.map((project) => {
              const open = openDecisions(hub, project.id).length;
              const work = hub.data.tasks.filter((task) => task.project === project.id && task.status !== "verified").length;
              return (
                <li key={project.id}>
                  <a href={link(`/projects/${project.id}`)}>
                    <span className="project-square" />
                    <strong>{project.name}</strong>
                    <span className="preview-meta">
                      {open ? `${open} waiting · ` : ""}{work} open {work === 1 ? "task" : "tasks"}
                    </span>
                  </a>
                </li>
              );
            })}
          </ul>
        </section>
      </div>
    </>
  );
}
