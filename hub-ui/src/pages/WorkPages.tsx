import { link } from "../router";
import { useEffect, useState } from "react";
import type { Hub } from "../hub";
import type { Task } from "../types";
import { Badge, PageHeader, PanelTitle, pretty } from "../ui";
import { answeredDecisions, DecisionList, openDecisions, RecentAnswers } from "./decisions";
import { TaskTable } from "./tasks";
import { WithdrawnList } from "./Withdraw";
import { ownershipRows, UNCLAIMED, WorkOwnership } from "./WorkOwnership";
import { draftFor, type ItemDraft, WORK_FILTERS, waitingItemKey, waitingKind, waitingTasks } from "../backlog";
import { statusLabel } from "./tasks";

/** One task item Beau must clear: an action he marks Done, or a decision he answers in the note. */
export function WaitingItemCard({ hub, task }: { hub: Hub; task: Task }) {
  // The draft belongs to one waiting item, not the task: a new item on the same task starts empty.
  const item = waitingItemKey(task);
  const [draft, setDraft] = useState<ItemDraft>({ item, text: "" });
  const note = draftFor(draft, item);
  const setNote = (text: string) => setDraft({ item, text });
  const decision = task.waiting_on?.type === "decision";
  return (
    <form className="waiting-item" onSubmit={(event) => {
      event.preventDefault();
      if (draft.item !== item) { setDraft({ item, text: "" }); return; }
      void hub.resolveWaiting(task, note.trim()).then((ok) => { if (ok) setDraft({ item, text: "" }); });
    }}>
      <Badge tone="amber">{waitingKind(task)}</Badge>
      <strong>{task.waiting_on?.text}</strong>
      <small><a className="project-chip" href={link(`/projects/${task.project}`)}>{hub.projectName(task.project)}</a> · {task.title}</small>
      <label className="field">
        <span className="sr-only">{decision ? "Your answer" : "Note (optional)"}</span>
        <textarea rows={2} maxLength={2000} value={note} onChange={(event) => setNote(event.target.value)}
          placeholder={decision ? "Your answer" : "Note (optional)"} />
      </label>
      <button className="primary" disabled={!hub.writable || (decision && !note.trim())}>
        {decision ? "Send answer" : "Done"}
      </button>
    </form>
  );
}

export function InboxPage({ hub, actions }: { hub: Hub; actions: React.ReactNode }) {
  const [project, setProject] = useState("all");
  const all = openDecisions(hub);
  const waiting = project === "all" ? all : all.filter((item) => item.project === project);
  const allItems = waitingTasks(hub.data.tasks);
  const items = project === "all" ? allItems : allItems.filter((task) => task.project === project);
  const count = (id: string) => all.filter((decision) => decision.project === id).length
    + allItems.filter((task) => task.project === id).length;
  const projectsWithItems = hub.data.projects.filter((item) => count(item.id) > 0);
  return (
    <>
      <PageHeader eyebrow="INBOX" title="Needs you"
        subtitle="Questions that need a human, kept in one place.">{actions}</PageHeader>
      <div className="filter-bar" role="group" aria-label="Filter by project">
        <button className={project === "all" ? "chip selected" : "chip"} onClick={() => setProject("all")}>
          All <span>{all.length + allItems.length}</span>
        </button>
        {projectsWithItems.map((item) => (
          <button key={item.id} className={project === item.id ? "chip selected" : "chip"} onClick={() => setProject(item.id)}>
            {item.name} <span>{count(item.id)}</span>
          </button>
        ))}
      </div>
      {items.length > 0 && (
        <section className="panel waiting-items" id="task-items">
          <PanelTitle title="From tasks" count={items.length} />
          <div className="decision-list">{items.map((task) => <WaitingItemCard key={waitingItemKey(task)} hub={hub} task={task} />)}</div>
        </section>
      )}
      <section className="panel decisions" id="decisions">
        <PanelTitle title="Needs Beau" count={waiting.length} />
        <p className="panel-description">Pick an answer and record it. Recording an answer never deploys anything.</p>
        {(waiting.length > 0 || items.length === 0) && <DecisionList hub={hub} decisions={waiting} />}
        <RecentAnswers hub={hub} decisions={answeredDecisions(hub, project === "all" ? undefined : project)} />
        <WithdrawnList hub={hub} kind="decision" project={project === "all" ? undefined : project} />
        <p className="panel-footnote">Only your signed-in session can answer. Agents can request, not approve.</p>
      </section>
    </>
  );
}

const statuses = WORK_FILTERS;

export function WorkPage({ hub, actions }: { hub: Hub; actions: React.ReactNode }) {
  const [project, setProject] = useState("all");
  const [status, setStatus] = useState<(typeof statuses)[number]>("all");
  const [query, setQuery] = useState("");
  const [chosenOwner, setOwner] = useState("all");
  const needle = query.trim().toLowerCase();
  const inProject = hub.data.tasks.filter((task) => project === "all" || task.project === project);
  const owners = ownershipRows(inProject);
  // A refresh can remove the chosen owner's last summary card (withdrawn, or passed review over a week
  // ago); then the filter, and its "Show everyone" control, would be stranded. Fall back to everyone.
  const owner = owners.some((row) => row.key === chosenOwner) ? chosenOwner : "all";
  useEffect(() => { if (owner !== chosenOwner) setOwner("all"); }, [owner, chosenOwner]);
  const byOwner = inProject.filter((task) => owner === "all" || (task.owner ?? UNCLAIMED) === owner);
  const tasks = byOwner.filter((task) => (status === "all" || task.status === status)
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
          <select value={project} onChange={(event) => { setProject(event.target.value); setOwner("all"); }}>
            <option value="all">All projects</option>
            {hub.data.projects.filter((item) => !item.archived).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        <div className="chip-row" role="group" aria-label="Filter by state">
          {statuses.map((value) => (
            <button key={value} className={status === value ? "chip selected" : "chip"} onClick={() => setStatus(value)}>
              {value === "all" ? "All" : statusLabel({ status: value } as Task)}{" "}
              <span>{byOwner.filter((task) => value === "all" || task.status === value).length}</span>
            </button>
          ))}
        </div>
      </div>
      <WorkOwnership hub={hub} rows={owners} selected={owner} onSelect={setOwner} />
      <section className="panel work" id="work">
        <PanelTitle title="Work in motion" count={tasks.length} />
        <p className="panel-description">An owner, an exact candidate, and a separate reviewer.</p>
        <TaskTable hub={hub} tasks={tasks} empty={needle || status !== "all" || owner !== "all" ? "No tasks match these filters." : "No assigned work for this project."} />
        <WithdrawnList hub={hub} kind="task" project={project === "all" ? undefined : project} />
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
