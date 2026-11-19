import { useState } from "react";
import { link } from "../router";
import { relativeAge, waitingOn } from "../dashboardText";
import type { Hub } from "../hub";
import type { Task } from "../types";
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
  : task.status === "waiting_on_beau" ? "Waiting on Beau" : pretty(task.status);
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

export function TaskRow({ hub, task }: { hub: Hub; task: Task }) {
  const [expanded, setExpanded] = useState(false);
  const [withdrawing, setWithdrawing] = useState(false);
  return (
    <>
    <details className="task-row" onToggle={(event) => setExpanded((event.currentTarget as HTMLDetailsElement).open)}>
      <summary>
        <span>
          <strong>{task.title}</strong>
          <small><a className="project-chip" href={link(`/projects/${task.project}`)}>{hub.projectName(task.project)}</a></small>
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
