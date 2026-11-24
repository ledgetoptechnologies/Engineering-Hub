import { useRef, useState } from "react";
import { TASK_PRIORITIES } from "../backlog";
import { link } from "../router";
import { relativeAge, waitingOn } from "../dashboardText";
import type { Hub } from "../hub";
import type { Task, TaskPriority } from "../types";
import { actorAvatar, actorName } from "../actors";
import { Badge, pretty } from "../ui";
import { DetailsBlock } from "../DetailsBlock";
import { canWithdrawTask } from "../withdrawn";
import { WithdrawDialog } from "./Withdraw";

export function stateAge(task: Task): string {
  const age = relativeAge(task.state_since);
  return age ? `In this state since ${new Date(Date.parse(task.state_since!)).toLocaleString()} (${age})`
    : "Time in this state not recorded";
}

export const statusLabel = (task: Task) => task.status === "verified" ? "Review passed"
  : task.status === "waiting_on_beau" ? "Waiting on Beau"
  : task.status === "assigned" ? (task.owner ? "Assigned · not started" : "Unassigned") : pretty(task.status);
export const statusTone = (task: Task) =>
  task.status === "verified" || task.status === "done" ? "forest"
    : task.status === "in_review" || task.status === "waiting_on_beau" ? "amber" : "neutral";

/** Reviewer, acceptance criteria, Beau's item and completion: the backlog part of a task. */
export function BacklogDetail({ hub, task }: { hub: Hub; task: Task }) {
  const name = (id: string) => actorName(id, { viewer: hub.session?.actor });
  return (
    <>
      {task.reviewer && <p>Named reviewer: <strong>{name(task.reviewer)}</strong></p>}
      {task.request_text && (
        <p>Request, as quoted by {task.proposed_by ? actorName(task.proposed_by, { family: task.proposed_family, viewer: hub.session?.actor }) : "its creator"}: {task.request_text}</p>
      )}
      {!!task.acceptance?.length && (
        <div className="acceptance">
          <strong>Acceptance criteria</strong>
          <ul>{task.acceptance.map((item, index) => <li key={index}>{item}</li>)}</ul>
        </div>
      )}
      {task.waiting_on && <p>Waiting on Beau ({task.waiting_on.type}): {task.waiting_on.text}</p>}
      {task.status === "done" && (
        <p>Done{task.completed_by ? ` · closed by ${name(task.completed_by)}` : ""}
          {task.completion_evidence ? ` · ${task.completion_evidence}` : ""}</p>
      )}
      {!!task.notes?.length && <p>Latest note: {task.notes[task.notes.length - 1].text}</p>}
    </>
  );
}

export function TaskPriorityField({ value, onChange, disabled }: {
  value: TaskPriority; onChange: (value: TaskPriority) => void; disabled?: boolean;
}) {
  return <label className="field">
    Priority
    <select value={value} onChange={(event) => onChange(event.target.value as TaskPriority)} disabled={disabled}>
      {TASK_PRIORITIES.map((priority) => <option key={priority} value={priority}>{pretty(priority)}</option>)}
    </select>
  </label>;
}

function TaskPriorityEditor({ hub, task }: { hub: Hub; task: Task }) {
  type Choice = { revision: number; priority: TaskPriority };
  const [draft, setDraft] = useState<Choice | null>(null);
  const request = useRef<(Choice & { key: string }) | null>(null);
  const savedPriority = task.priority ?? "normal";
  // An open editor must never apply an old selection to a newer task revision.
  const choice = draft?.revision === task.revision ? draft.priority : savedPriority;
  const canSave = hub.writable && choice !== savedPriority;
  return <form className="task-priority-form" onSubmit={(event) => {
    event.preventDefault();
    if (!canSave) return;
    if (request.current?.revision !== task.revision || request.current.priority !== choice)
      request.current = { revision: task.revision, priority: choice, key: crypto.randomUUID() };
    // Retry the same intent under the same key if delivery could not be confirmed.
    void hub.updateTaskPriority(task, choice, request.current.key);
  }}>
    <TaskPriorityField value={choice} onChange={(priority) => setDraft({ revision: task.revision, priority })}
      disabled={!hub.writable} />
    <button type="submit" className="secondary" disabled={!canSave}>Save priority</button>
  </form>;
}

export function TaskRow({ hub, task }: { hub: Hub; task: Task }) {
  const [expanded, setExpanded] = useState(false);
  const [withdrawing, setWithdrawing] = useState(false);
  const priority = task.priority ?? "normal";
  return (
    <>
    <details className="task-row" onToggle={(event) => setExpanded((event.currentTarget as HTMLDetailsElement).open)}>
      <summary>
        <span>
          <strong>{task.title}</strong>
          <small className="task-project-meta">
            <a className="project-chip" href={link(`/projects/${task.project}`)}>{hub.projectName(task.project)}</a>
            <Badge tone={priority === "urgent" || priority === "high" ? "amber" : "neutral"}>{pretty(priority)} priority</Badge>
          </small>
          {task.scope === "staging" && task.proposed_by && <small>Staging proposal · {actorName(task.proposed_by, { family: task.proposed_family, viewer: hub.session?.actor })}</small>}
          <small className="task-waiting">{waitingOn(task, (id, family) => actorName(id, { family, viewer: hub.session?.actor }))}</small>
          <small title={task.state_since ?? undefined}>{stateAge(task)}</small>
        </span>
        <span className="owner">
          <span className="mini-avatar">{actorAvatar(task.owner, task.owner_family).letter}</span>
          {task.owner ? actorName(task.owner, { family: task.owner_family, viewer: hub.session?.actor }) : "Unclaimed"}
        </span>
        <Badge tone={statusTone(task)}>{statusLabel(task)}</Badge>
      </summary>
      <div className="task-detail">
        <DetailsBlock entity={task} kind="task" load={expanded && !hub.demo} onUnauthorized={hub.clearPrivateWorkspace} />
        <BacklogDetail hub={hub} task={task} />
        <p>Candidate: <code>{task.candidate_commit ?? "Not submitted"}</code></p>
        <p>
          Independent reviewer:{" "}
          <strong>
            {task.review ? actorName(task.review.actor, { family: task.review.family, viewer: hub.session?.actor }) : task.status === "in_review" ? "None assigned yet" : "Not requested yet"}
          </strong>{" "}
          · Repairs used: {task.repair_count}/3
        </p>
        <p>Review is bound to the exact commit, not permission to deploy to production.</p>
        {task.status !== "done" && task.status !== "withdrawn" && !hub.data.projects.find((item) => item.id === task.project)?.archived && (
          <TaskPriorityEditor hub={hub} task={task} />
        )}
        {canWithdrawTask(task) && hub.writable && (
          <p><button type="button" className="link-button" onClick={() => setWithdrawing(true)}>
            No longer needed? Withdraw this task</button></p>
        )}
      </div>
    </details>
    {withdrawing && <WithdrawDialog hub={hub} kind="task" item={task} onClose={() => setWithdrawing(false)} />}
    </>
  );
}

export function TaskTable({ hub, tasks, empty }: { hub: Hub; tasks: Task[]; empty: string }) {
  return (
    <div className="task-table">
      <div className="table-header">
        <span>TASK / PROJECT</span>
        <span>OWNER</span>
        <span>STATE</span>
      </div>
      {tasks.length === 0 && <div className="empty-inline">{empty}</div>}
      {tasks.map((task) => <TaskRow key={task.id} hub={hub} task={task} />)}
    </div>
  );
}
