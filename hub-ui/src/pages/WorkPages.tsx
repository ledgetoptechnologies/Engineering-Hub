import { link } from "../router";
import { useState } from "react";
import type { Hub } from "../hub";
import type { Task } from "../types";
import { Badge, PageHeader, PanelTitle, pretty } from "../ui";
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
            {hub.data.projects.filter((item) => !item.archived).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
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

export function StagingBadge({ hub, id }: { hub: Hub; id: string }) {
  const item = hub.data.projects.find((project) => project.id === id);
  if (!item) return null;
  return (
    <Badge tone={item.health === "healthy" ? "forest" : "neutral"}>
      {!item.executor_enabled && item.health === "unknown" ? "No staging target"
        : item.health === "unknown" ? "Health unknown" : pretty(item.health)}
    </Badge>
  );
}
