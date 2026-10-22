import { useState } from "react";
import type { Hub } from "../hub";
import type { Task } from "../types";
import { Badge, PageHeader, PanelTitle, pretty } from "../ui";
import { NotesPanel } from "../NotesPanel";
import { answeredDecisions, DecisionList, openDecisions, RecentAnswers } from "./decisions";
import { TaskTable } from "./tasks";

export function InboxPage({ hub, actions }: { hub: Hub; actions: React.ReactNode }) {
  const [project, setProject] = useState("all");
  const all = openDecisions(hub);
  const waiting = project === "all" ? all : all.filter((item) => item.project === project);
  const projectsWithItems = hub.data.projects.filter((item) => all.some((decision) => decision.project === item.id));
  return (
    <>
      <PageHeader eyebrow="INBOX" title="Needs you"
        subtitle="Questions that need a human, kept in one place.">{actions}</PageHeader>
      <div className="filter-bar" role="group" aria-label="Filter by project">
        <button className={project === "all" ? "chip selected" : "chip"} onClick={() => setProject("all")}>
          All <span>{all.length}</span>
        </button>
        {projectsWithItems.map((item) => (
          <button key={item.id} className={project === item.id ? "chip selected" : "chip"} onClick={() => setProject(item.id)}>
            {item.name} <span>{all.filter((decision) => decision.project === item.id).length}</span>
          </button>
        ))}
      </div>
      <section className="panel decisions" id="decisions">
        <PanelTitle title="Needs Beau" count={waiting.length} />
        <p className="panel-description">Pick an answer and record it. Recording an answer never deploys anything.</p>
        <DecisionList hub={hub} decisions={waiting} />
        <RecentAnswers hub={hub} decisions={answeredDecisions(hub, project === "all" ? undefined : project)} />
        <p className="panel-footnote">Only your signed-in session can answer. Agents can request, not approve.</p>
      </section>
    </>
  );
}

const statuses: (Task["status"] | "all")[] = ["all", "assigned", "claimed", "in_review", "verified"];

export function WorkPage({ hub, actions }: { hub: Hub; actions: React.ReactNode }) {
  const [project, setProject] = useState("all");
  const [status, setStatus] = useState<(typeof statuses)[number]>("all");
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const tasks = hub.data.tasks.filter((task) => (project === "all" || task.project === project)
    && (status === "all" || task.status === status)
    && (!needle || task.title.toLowerCase().includes(needle)));
  return (
    <>
      <PageHeader eyebrow="WORK" title="Work in motion"
        subtitle="Every task: who owns it, what it's waiting on, and how long it's been there.">{actions}</PageHeader>
      <div className="filter-bar">
        <label className="search-field">
          <span className="sr-only">Search tasks</span>
          <input type="search" placeholder="Search tasks" value={query} onChange={(event) => setQuery(event.target.value)} />
        </label>
        <label className="select-field">
          <span className="sr-only">Project</span>
          <select value={project} onChange={(event) => setProject(event.target.value)}>
            <option value="all">All projects</option>
            {hub.data.projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        <div className="chip-row" role="group" aria-label="Filter by state">
          {statuses.map((value) => (
            <button key={value} className={status === value ? "chip selected" : "chip"} onClick={() => setStatus(value)}>
              {value === "all" ? "All" : value === "verified" ? "Review passed" : pretty(value)}{" "}
              <span>{hub.data.tasks.filter((task) => (value === "all" || task.status === value)
                && (project === "all" || task.project === project)).length}</span>
            </button>
          ))}
        </div>
      </div>
      <section className="panel work" id="work">
        <PanelTitle title="Work in motion" count={tasks.length} />
        <p className="panel-description">An owner, an exact candidate, and a separate reviewer.</p>
        <TaskTable hub={hub} tasks={tasks} empty={needle || status !== "all" ? "No tasks match these filters." : "No assigned work for this project."} />
      </section>
    </>
  );
}

export function ProjectsPage({ hub, actions }: { hub: Hub; actions: React.ReactNode }) {
  return (
    <>
      <PageHeader eyebrow="PROJECTS" title="Projects" subtitle="Each project's open questions, work and staging status.">{actions}</PageHeader>
      <div className="project-cards">
        {hub.data.projects.map((item) => {
          const open = openDecisions(hub, item.id).length;
          const tasks = hub.data.tasks.filter((task) => task.project === item.id);
          const active = tasks.filter((task) => task.status !== "verified").length;
          return (
            <a className="project-card" key={item.id} href={`#/projects/${item.id}`}>
              <div className="project-card-head">
                <span className="project-square" />
                <h2>{item.name}</h2>
                <StagingBadge hub={hub} id={item.id} />
              </div>
              <dl>
                <div><dt>Waiting on you</dt><dd className={open ? "attention" : ""}>{open}</dd></div>
                <div><dt>Open tasks</dt><dd>{active}</dd></div>
                <div><dt>Review passed</dt><dd>{tasks.length - active}</dd></div>
              </dl>
            </a>
          );
        })}
      </div>
    </>
  );
}

export function StagingBadge({ hub, id }: { hub: Hub; id: string }) {
  const item = hub.data.projects.find((project) => project.id === id);
  if (!item) return null;
  return (
    <Badge tone={item.health === "healthy" ? "forest" : "neutral"}>
      {!item.executor_enabled && item.health === "unknown" ? "Not set up yet"
        : item.health === "unknown" ? "Health unknown" : pretty(item.health)}
    </Badge>
  );
}

export function ProjectPage({ hub, id, actions, notesProps }: {
  hub: Hub; id: string; actions: React.ReactNode;
  notesProps: Omit<React.ComponentProps<typeof NotesPanel>, "selectedProject">;
}) {
  const item = hub.data.projects.find((project) => project.id === id);
  if (!item) {
    return (
      <>
        <PageHeader eyebrow="PROJECTS" title="Project not found">{actions}</PageHeader>
        <section className="panel"><p>This project isn't in the workspace. <a className="text-link" href="#/projects">Back to projects</a></p></section>
      </>
    );
  }
  const waiting = openDecisions(hub, id);
  const tasks = hub.data.tasks.filter((task) => task.project === id);
  return (
    <>
      <nav className="crumbs" aria-label="Breadcrumb"><a href="#/projects">Projects</a> <span>/</span> {item.name}</nav>
      <PageHeader eyebrow="PROJECT" title={item.name}
        subtitle={item.executor_enabled ? "Staging target enabled."
          : "No staging target yet. Setting one up needs an inventory of the target and your acceptance."}>
        <StagingBadge hub={hub} id={id} />
        {actions}
      </PageHeader>
      <div className="project-layout">
        <section className="panel decisions" aria-label={`${item.name} decisions`}>
          <PanelTitle title="Needs you" count={waiting.length} />
          <DecisionList hub={hub} decisions={waiting} emptyNote="No open questions for this project." />
          <RecentAnswers hub={hub} decisions={answeredDecisions(hub, id)} />
        </section>
        <section className="panel work" aria-label={`${item.name} work`}>
          <PanelTitle title="Work" count={tasks.length} />
          <TaskTable hub={hub} tasks={tasks} empty="No tasks for this project yet." />
        </section>
      </div>
      <NotesPanel {...notesProps} selectedProject={id} />
    </>
  );
}
