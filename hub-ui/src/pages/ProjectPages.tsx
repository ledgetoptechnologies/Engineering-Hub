import { useState } from "react";
import { relativeAge, waitingOn } from "../dashboardText";
import type { Hub } from "../hub";
import { NotesPanel } from "../NotesPanel";
import { link, type ProjectTab } from "../router";
import type { Decision, Task } from "../types";
import { Badge, EmptyState, Icon, names, pretty } from "../ui";
import { answeredDecisions, DecisionList, openDecisions, RecentAnswers } from "./decisions";
import { LabelSetsPanel } from "./LabelSets";
import { stateAge, statusLabel, statusTone, TaskTable } from "./tasks";

const STATUSES: Task["status"][] = ["assigned", "claimed", "in_review", "verified"];
const columnName: Record<Task["status"], string> = {
  assigned: "Assigned", claimed: "In progress", in_review: "In review", verified: "Review passed",
};

export interface ProjectStats {
  waiting: Decision[]; tasks: Task[]; byStatus: Record<Task["status"], number>;
  lastActivity: string | null; agents: string[]; active: number;
}
export function projectStats(hub: Hub, id: string): ProjectStats {
  const waiting = openDecisions(hub, id);
  const tasks = hub.data.tasks.filter((task) => task.project === id);
  const byStatus = Object.fromEntries(STATUSES.map((status) => [status, tasks.filter((task) => task.status === status).length])) as ProjectStats["byStatus"];
  const times = [...tasks.map((task) => task.updated_at), ...hub.data.decisions.filter((d) => d.project === id).map((d) => d.updated_at)]
    .filter((value) => Number.isFinite(Date.parse(value)));
  const lastActivity = times.length ? times.reduce((a, b) => (Date.parse(a) > Date.parse(b) ? a : b)) : null;
  const agents = [...new Set(tasks.flatMap((task) => [task.owner, task.proposed_by, task.review?.actor]).filter((a): a is NonNullable<typeof a> => !!a))];
  return { waiting, tasks, byStatus, lastActivity, agents, active: tasks.length - byStatus.verified };
}

/** Status shown until projects carry their own health: derived only from current records. */
export function derivedStatus(stats: ProjectStats): { label: string; tone: string } {
  if (stats.waiting.length) return { label: "Needs you", tone: "attention" };
  if (stats.byStatus.in_review) return { label: "In review", tone: "amber" };
  if (stats.active) return { label: "Active", tone: "info" };
  return { label: "Quiet", tone: "neutral" };
}

function AgentStack({ agents }: { agents: string[] }) {
  if (!agents.length) return <span className="muted">—</span>;
  return (
    <span className="agent-stack" title={agents.map((a) => names[a] ?? a).join(", ")}>
      {agents.map((agent) => <span key={agent} className={`agent-avatar ${agent}`}>{(names[agent] ?? agent).charAt(0)}</span>)}
    </span>
  );
}

export function ProjectsPage({ hub, actions }: { hub: Hub; actions: React.ReactNode }) {
  const rows = hub.data.projects.map((project) => ({ project, stats: projectStats(hub, project.id) }));
  return (
    <>
      <header className="list-header">
        <div>
          <p className="eyebrow">PROJECTS</p>
          <h1>Projects<span>.</span></h1>
        </div>
        <div className="heading-actions">{actions}</div>
      </header>
      <section className="panel table-panel" aria-label="All projects">
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Project</th>
              <th scope="col">Status</th>
              <th scope="col" className="num">Needs you</th>
              <th scope="col" className="num">Open</th>
              <th scope="col" className="num">In review</th>
              <th scope="col" className="num">Passed</th>
              <th scope="col">Agents</th>
              <th scope="col">Last activity</th>
              <th scope="col">Staging</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ project, stats }) => {
              const status = derivedStatus(stats);
              return (
                <tr key={project.id}>
                  <th scope="row">
                    <a className="row-link" href={link(`/projects/${project.id}`)}>
                      <span className="project-glyph" aria-hidden="true">{project.name.charAt(0)}</span>
                      <span>
                        <strong>{project.name}</strong>
                        <small>{project.id}</small>
                      </span>
                    </a>
                  </th>
                  <td><span className={`status-pill ${status.tone}`}>{status.label}</span></td>
                  <td className={`num ${stats.waiting.length ? "attention" : ""}`}>{stats.waiting.length}</td>
                  <td className="num">{stats.active}</td>
                  <td className="num">{stats.byStatus.in_review}</td>
                  <td className="num">{stats.byStatus.verified}</td>
                  <td><AgentStack agents={stats.agents} /></td>
                  <td className="muted">{stats.lastActivity ? relativeAge(stats.lastActivity) : "—"}</td>
                  <td>{project.executor_enabled ? <Badge tone="forest">Target set up</Badge> : <span className="muted">Not set up</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
      <p className="panel-footnote">Status is worked out from each project's open questions and tasks.</p>
    </>
  );
}

function WorkBoard({ hub, tasks }: { hub: Hub; tasks: Task[] }) {
  return (
    <div className="board" role="list" aria-label="Work board">
      {STATUSES.map((status) => {
        const column = tasks.filter((task) => task.status === status);
        return (
          <section key={status} className="board-column" role="listitem" aria-label={columnName[status]}>
            <header>
              <span className={`status-dot ${status}`} aria-hidden="true" />
              <h3>{columnName[status]}</h3>
              <span className="count">{column.length}</span>
            </header>
            <div className="board-cards">
              {column.length === 0 && <p className="board-empty">Nothing here</p>}
              {column.map((task) => (
                <article key={task.id} className="board-card">
                  <strong>{task.title}</strong>
                  <p>{waitingOn(task, (id) => names[id] ?? id)}</p>
                  <footer>
                    <span className="owner">
                      <span className={`agent-avatar ${task.owner ?? ""}`}>{task.owner ? (names[task.owner] ?? "?").charAt(0) : "–"}</span>
                      {task.owner ? names[task.owner] : "Unclaimed"}
                    </span>
                    <span className="muted" title={stateAge(task)}>{relativeAge(task.state_since) ?? ""}</span>
                  </footer>
                </article>
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

interface Change { at: string; kind: "task" | "decision"; title: string; detail: string; tone: string }
function latestChanges(hub: Hub, id: string): Change[] {
  const tasks: Change[] = hub.data.tasks.filter((t) => t.project === id).map((task) => ({
    at: task.updated_at, kind: "task", title: task.title, detail: statusLabel(task), tone: statusTone(task) }));
  const decisions: Change[] = hub.data.decisions.filter((d) => d.project === id).map((decision) => ({
    at: decision.updated_at, kind: "decision", title: decision.title,
    detail: decision.status === "answered" ? `Answered: ${decision.answer}` : "Waiting on you",
    tone: decision.status === "answered" ? "forest" : "amber" }));
  return [...tasks, ...decisions].filter((c) => Number.isFinite(Date.parse(c.at)))
    .sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
}

function Timeline({ changes }: { changes: Change[] }) {
  if (!changes.length) return <EmptyState title="No changes yet." />;
  return (
    <ol className="timeline">
      {changes.map((change, index) => (
        <li key={index}>
          <span className={`timeline-dot ${change.kind}`} aria-hidden="true" />
          <div>
            <strong>{change.title}</strong>
            <span className="timeline-meta">
              {change.kind === "task" ? "Task" : "Question"} · <Badge tone={change.tone}>{change.detail}</Badge>
            </span>
          </div>
          <time dateTime={change.at} title={new Date(change.at).toLocaleString()}>{relativeAge(change.at)}</time>
        </li>
      ))}
    </ol>
  );
}

export function ProjectPage({ hub, id, tab, actions, notesProps }: {
  hub: Hub; id: string; tab: ProjectTab; actions: React.ReactNode;
  notesProps: Omit<React.ComponentProps<typeof NotesPanel>, "selectedProject">;
}) {
  const [view, setView] = useState<"board" | "list">("board");
  const project = hub.data.projects.find((item) => item.id === id);
  if (!project) {
    return (
      <>
        <nav className="crumbs" aria-label="Breadcrumb"><a href={link("/projects")}>Projects</a></nav>
        <header className="list-header"><div><p className="eyebrow">PROJECTS</p><h1>Project not found<span>.</span></h1></div></header>
        <section className="panel"><p>This project isn't in the workspace. <a className="text-link" href={link("/projects")}>Back to projects</a></p></section>
      </>
    );
  }
  const stats = projectStats(hub, id);
  const status = derivedStatus(stats);
  const changes = latestChanges(hub, id);
  const tabs: { key: ProjectTab; label: string; count?: number }[] = [
    { key: "overview", label: "Overview" },
    { key: "decisions", label: "Needs you", count: stats.waiting.length },
    { key: "work", label: "Work", count: stats.active },
    { key: "notes", label: "Notes" },
    { key: "labeling", label: "Labeling" },
    { key: "activity", label: "Activity" },
  ];
  const tabHref = (key: ProjectTab) => link(`/projects/${id}${key === "overview" ? "" : "/" + key}`);
  return (
    <>
      <nav className="crumbs" aria-label="Breadcrumb"><a href={link("/projects")}>Projects</a> <span>/</span> {project.name}</nav>
      <header className="project-header">
        <div className="project-title">
          <span className="project-glyph large" aria-hidden="true">{project.name.charAt(0)}</span>
          <div>
            <h1>{project.name}<span>.</span></h1>
            <div className="project-meta">
              <span className={`status-pill ${status.tone}`}>{status.label}</span>
              <span>{project.executor_enabled ? "Staging target set up" : "No staging target yet"}</span>
              <span>Last activity {stats.lastActivity ? relativeAge(stats.lastActivity) : "—"}</span>
              <AgentStack agents={stats.agents} />
            </div>
          </div>
        </div>
        <div className="heading-actions">{actions}</div>
      </header>
      <nav className="tabs" aria-label={`${project.name} sections`}>
        {tabs.map((item) => (
          <a key={item.key} href={tabHref(item.key)} className={tab === item.key ? "tab current" : "tab"}
            aria-current={tab === item.key ? "page" : undefined}>
            {item.label}{item.count ? <span className={`tab-count ${item.key === "decisions" ? "attention" : ""}`}>{item.count}</span> : null}
          </a>
        ))}
      </nav>

      {tab === "overview" && (
        <div className="overview-layout">
          <div className="overview-main">
            <div className="stat-row">
              <a className="stat" href={tabHref("decisions")}><span>Needs you</span><strong className={stats.waiting.length ? "attention" : ""}>{stats.waiting.length}</strong></a>
              {STATUSES.map((s) => (
                <a key={s} className="stat" href={tabHref("work")}><span>{columnName[s]}</span><strong>{stats.byStatus[s]}</strong></a>
              ))}
            </div>
            {stats.tasks.length > 0 && (
              <div className="segment-bar" aria-label="Work by state">
                {STATUSES.map((s) => stats.byStatus[s] ? (
                  <span key={s} className={`segment ${s}`} style={{ flexGrow: stats.byStatus[s] }} title={`${columnName[s]}: ${stats.byStatus[s]}`} />
                ) : null)}
              </div>
            )}
            <section className="panel" aria-label="Waiting on you">
              <div className="panel-title"><h2>Waiting on you</h2><span className="count">{stats.waiting.length}</span>
                <span className="panel-action"><a className="text-link" href={tabHref("decisions")}>Answer →</a></span></div>
              {stats.waiting.length === 0 ? <p className="panel-description">Nothing waiting on you in this project.</p> : (
                <ul className="preview-list">
                  {stats.waiting.slice(0, 5).map((decision) => (
                    <li key={decision.id}><a href={tabHref("decisions")}><span className="preview-title">{decision.title}</span>
                      <span className="preview-meta">{decision.protected ? "Protected · " : ""}{decision.options.length} choices</span></a></li>
                  ))}
                </ul>
              )}
            </section>
            <section className="panel" aria-label="Recent changes">
              <div className="panel-title"><h2>Recent changes</h2>
                <span className="panel-action"><a className="text-link" href={tabHref("activity")}>All →</a></span></div>
              <Timeline changes={changes.slice(0, 6)} />
            </section>
          </div>
          <aside className="panel details-panel" aria-label="Details">
            <h2>Details</h2>
            <dl className="properties">
              <div><dt>Status</dt><dd><span className={`status-pill ${status.tone}`}>{status.label}</span></dd></div>
              <div><dt>Project id</dt><dd><code>{project.id}</code></dd></div>
              <div><dt>Staging</dt><dd>{project.executor_enabled ? "Target set up" : "Not set up yet"}</dd></div>
              <div><dt>Health</dt><dd>{project.health === "unknown" ? "Not reported" : pretty(project.health)}</dd></div>
              <div><dt>Agents</dt><dd><AgentStack agents={stats.agents} /></dd></div>
              <div><dt>Open tasks</dt><dd>{stats.active}</dd></div>
              <div><dt>Answered</dt><dd>{hub.data.decisions.filter((d) => d.project === id && d.status === "answered").length} questions</dd></div>
              <div><dt>Last activity</dt><dd>{stats.lastActivity ? new Date(stats.lastActivity).toLocaleString() : "—"}</dd></div>
            </dl>
          </aside>
        </div>
      )}

      {tab === "decisions" && (
        <section className="panel decisions" aria-label={`${project.name} decisions`}>
          <DecisionList hub={hub} decisions={stats.waiting} emptyNote="No open questions for this project." />
          <RecentAnswers hub={hub} decisions={answeredDecisions(hub, id, 10)} />
          <p className="panel-footnote">Only your signed-in session can answer. Agents can request, not approve.</p>
        </section>
      )}

      {tab === "work" && (
        <>
          <div className="filter-bar">
            <div className="chip-row" role="group" aria-label="View">
              <button type="button" aria-pressed={view === "board"} className={view === "board" ? "chip selected" : "chip"} onClick={() => setView("board")}><Icon kind="grid" size={14} /> Board</button>
              <button type="button" aria-pressed={view === "list"} className={view === "list" ? "chip selected" : "chip"} onClick={() => setView("list")}><Icon kind="menu" size={14} /> List</button>
            </div>
          </div>
          {view === "board" ? <WorkBoard hub={hub} tasks={stats.tasks} /> : (
            <section className="panel work" aria-label={`${project.name} work`}>
              <TaskTable hub={hub} tasks={stats.tasks} empty="No tasks for this project yet." />
            </section>
          )}
        </>
      )}

      {tab === "notes" && <NotesPanel {...notesProps} selectedProject={id} />}

      {tab === "labeling" && (
        <>
          <LabelSetsPanel hub={hub} project={id} />
          <p className="panel-footnote">Labeling sets appear here when an agent prepares one for this project.</p>
        </>
      )}

      {tab === "activity" && (
        <section className="panel" aria-label="Activity">
          <div className="panel-title"><h2>Activity</h2></div>
          <p className="panel-description">The latest change to each task and question, newest first. A full history of every change needs a server update.</p>
          <Timeline changes={changes} />
        </section>
      )}
    </>
  );
}
