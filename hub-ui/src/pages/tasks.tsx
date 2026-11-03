import { useState } from "react";
import { link } from "../router";
import { REVIEW_IS_NOT_A_RUN, relativeAge, waitingOn } from "../dashboardText";
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

export const statusLabel = (task: Task) => task.status === "verified" ? "Review passed" : pretty(task.status);
/** Hover text for the status badge: a passed review says nothing about running. */
export const statusNote = (task: Task) => task.status === "verified" ? REVIEW_IS_NOT_A_RUN : undefined;
export const statusTone = (task: Task) =>
  task.status === "verified" ? "forest" : task.status === "in_review" ? "amber" : "neutral";

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
        <Badge tone={statusTone(task)} title={statusNote(task)}>{statusLabel(task)}</Badge>
      </summary>
      <div className="task-detail">
        <DetailsBlock entity={task} kind="task" load={expanded && !hub.demo} onUnauthorized={hub.clearPrivateWorkspace} />
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
