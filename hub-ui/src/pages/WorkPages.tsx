import { link } from "../router";
import { useEffect, useState } from "react";
import type { Hub } from "../hub";
import type { Task, WaitingResolution } from "../types";
import { Badge, PageHeader, PanelTitle, pretty } from "../ui";
import { answeredDecisions, DecisionList, openDecisions, RecentAnswers } from "./decisions";
import { TaskTable } from "./tasks";
import { WithdrawnList } from "./Withdraw";
import { ownershipRows, UNCLAIMED, WorkOwnership } from "./WorkOwnership";
import { draftFor, type ItemDraft, laterHint, WORK_FILTERS, waitingItemKey, waitingKind, waitingLevel, waitingTasks } from "../backlog";
import { statusLabel } from "./tasks";

/** One task item Beau must clear: a question he answers with one of three options or his own text, or an action he
 * marks Done. Either can be dismissed, or moved between now and later. */
export function WaitingItemCard({ hub, task, selected, onSelect }: {
  hub: Hub; task: Task; selected?: boolean; onSelect?: (selected: boolean) => void;
}) {
  // The draft belongs to one waiting item, not the task: a new item on the same task starts empty.
  const item = waitingItemKey(task);
  const [draft, setDraft] = useState<ItemDraft>({ item, text: "" });
  const text = draftFor(draft, item);
  const setText = (value: string) => setDraft({ item, text: value });
  const waiting = task.waiting_on!;
  const decision = waiting.type === "decision";
  const options = waiting.options ?? [];
  const later = waitingLevel(task) === "later";
  // Every send re-checks the draft's item, so text typed for an earlier item never reaches a newer one.
  const send = (resolution: WaitingResolution) => {
    if (draft.item !== item) { setDraft({ item, text: "" }); return; }
    void hub.resolveWaiting(task, resolution).then((ok) => { if (ok) setDraft({ item, text: "" }); });
  };
  return (
    <form className="waiting-item" onSubmit={(event) => {
      event.preventDefault();
      send(decision ? { answer: text } : { note: text });
    }}>
      <div className="waiting-head">
        {onSelect && <input type="checkbox" checked={!!selected} aria-label={`Select: ${waiting.text}`}
          onChange={(event) => onSelect(event.target.checked)} />}
        <Badge tone="amber">{waitingKind(task)}</Badge>
        {later && <Badge tone="neutral">Later</Badge>}
      </div>
      <strong>{waiting.text}</strong>
      <small><a className="project-chip" href={link(`/projects/${task.project}`)}>{hub.projectName(task.project)}</a> · {task.title}</small>
      {options.length > 0 && (
        <div className="waiting-options" role="group" aria-label="Options">
          {options.map((option, index) => (
            <button type="button" key={index} disabled={!hub.writable} onClick={() => send({ option: index, note: text })}
              className={index === waiting.recommended ? "waiting-option recommended" : "waiting-option"}>
              <span className="waiting-option-label">{option.label}</span>
              {index === waiting.recommended && <Badge tone="forest">Recommended</Badge>}
              {option.detail && <small>{option.detail}</small>}
              {index === waiting.recommended && waiting.recommended_reason && <small>{waiting.recommended_reason}</small>}
            </button>
          ))}
        </div>
      )}
      <label className="field">
        <span className="sr-only">{decision ? "Your answer" : "Note (optional)"}</span>
        <textarea rows={2} maxLength={2000} value={text} onChange={(event) => setText(event.target.value)}
          placeholder={decision ? "Your answer" : "Note (optional)"} />
      </label>
      <div className="waiting-actions">
        <button className="primary" disabled={!hub.writable || (decision && !text.trim())}>{decision ? "Send answer" : "Done"}</button>
        <button type="button" disabled={!hub.writable} onClick={() => send({ dismiss: true, note: text })}>Dismiss</button>
        <button type="button" disabled={!hub.writable} onClick={() => void hub.setWaitingLevel(task, later ? "now" : "later")}>
          {later ? "Move to now" : "Later"}
        </button>
      </div>
    </form>
  );
}

/** Now items with bulk answer/dismiss, then later items collapsed behind their count. */
export function WaitingItemList({ hub, tasks, later }: { hub: Hub; tasks: Task[]; later: Task[] }) {
  const [selected, setSelected] = useState<string[]>([]);
  const [showLater, setShowLater] = useState(false);
  const shown = showLater ? [...tasks, ...later] : tasks;
  // Selection is by item identity: a refreshed task with a newer item is not selected.
  const chosen = shown.filter((task) => selected.includes(waitingItemKey(task)));
  const select = (task: Task, on: boolean) => setSelected((keys) =>
    on ? [...keys, waitingItemKey(task)] : keys.filter((key) => key !== waitingItemKey(task)));
  const bulk = (resolution: (task: Task) => WaitingResolution) =>
    void hub.resolveWaitingMany(chosen.map((task) => ({ task, resolution: resolution(task) })))
      .then((ok) => { if (ok) setSelected([]); });
  const recommendable = chosen.length > 0 && chosen.every((task) => typeof task.waiting_on?.recommended === "number");
  const card = (task: Task) => <WaitingItemCard key={waitingItemKey(task)} hub={hub} task={task}
    selected={selected.includes(waitingItemKey(task))} onSelect={(on) => select(task, on)} />;
  return (
    <>
      {chosen.length > 0 && (
        <div className="waiting-bulk" role="group" aria-label="Selected items">
          <span>{chosen.length} selected</span>
          <button className="primary" disabled={!hub.writable || !recommendable}
            onClick={() => bulk((task) => ({ option: task.waiting_on!.recommended! }))}>Accept recommended</button>
          <button disabled={!hub.writable} onClick={() => bulk(() => ({ dismiss: true }))}>Dismiss</button>
        </div>
      )}
      <div className="decision-list">{tasks.map(card)}</div>
      {later.length > 0 && (
        <button className="chip waiting-later" aria-expanded={showLater} onClick={() => setShowLater(!showLater)}>
          Later <span>{later.length}</span>
        </button>
      )}
      {showLater && later.length > 0 && <div className="decision-list">{later.map(card)}</div>}
    </>
  );
}

export function InboxPage({ hub, actions }: { hub: Hub; actions: React.ReactNode }) {
  const [project, setProject] = useState("all");
  const all = openDecisions(hub);
  const waiting = project === "all" ? all : all.filter((item) => item.project === project);
  const allItems = waitingTasks(hub.data.tasks);
  const items = project === "all" ? allItems : allItems.filter((task) => task.project === project);
  const later = waitingTasks(hub.data.tasks, project === "all" ? undefined : project, "later");
  const count = (id: string) => all.filter((decision) => decision.project === id).length
    + allItems.filter((task) => task.project === id).length;
  // A project whose items are all later still gets its chip; its count stays the now count.
  const projectsWithItems = hub.data.projects.filter((item) => count(item.id) > 0 || laterHint(hub.data.tasks, item.id));
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
            {item.name} <span>{count(item.id)}</span>{laterHint(hub.data.tasks, item.id) && <small className="later-hint">{laterHint(hub.data.tasks, item.id)}</small>}
          </button>
        ))}
      </div>
      {items.length + later.length > 0 && (
        <section className="panel waiting-items" id="task-items">
          <PanelTitle title="From tasks" count={items.length} />
          <WaitingItemList hub={hub} tasks={items} later={later} />
        </section>
      )}
      <section className="panel decisions" id="decisions">
        <PanelTitle title="Needs Beau" count={waiting.length} />
        <p className="panel-description">Pick an answer and record it. Recording an answer never deploys anything.</p>
        {(waiting.length > 0 || items.length + later.length === 0) && <DecisionList hub={hub} decisions={waiting} />}
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
