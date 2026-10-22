import { link } from "../router";
import { relativeAge, waitingOn } from "../dashboardText";
import type { Hub } from "../hub";
import type { Task } from "../types";
import { Badge, names, pretty } from "../ui";
import { DetailsBlock } from "../DetailsBlock";

export function stateAge(task: Task): string {
  const age = relativeAge(task.state_since);
  return age ? `In this state since ${new Date(Date.parse(task.state_since!)).toLocaleString()} (${age})`
    : "Time in this state not recorded";
}

export const statusLabel = (task: Task) => task.status === "verified" ? "Review passed" : pretty(task.status);
export const statusTone = (task: Task) =>
  task.status === "verified" ? "forest" : task.status === "in_review" ? "amber" : "neutral";

export function TaskRow({ hub, task }: { hub: Hub; task: Task }) {
  return (
    <details className="task-row">
      <summary>
        <span>
          <strong>{task.title}</strong>
          <small><a className="project-chip" href={link(`/projects/${task.project}`)}>{hub.projectName(task.project)}</a></small>
          {task.scope === "staging" && task.proposed_by && <small>Staging proposal · {names[task.proposed_by]}</small>}
          <small className="task-waiting">{waitingOn(task, (id) => names[id] ?? id)}</small>
          <small title={task.state_since ?? undefined}>{stateAge(task)}</small>
        </span>
        <span className="owner">
          <span className="mini-avatar">{task.owner?.charAt(0).toUpperCase() ?? "–"}</span>
          {task.owner ? names[task.owner] : "Unclaimed"}
        </span>
        <Badge tone={statusTone(task)}>{statusLabel(task)}</Badge>
      </summary>
      <div className="task-detail">
        <DetailsBlock value={task.details} />
        <p>Candidate: <code>{task.candidate_commit ?? "Not submitted"}</code></p>
        <p>
          Independent reviewer:{" "}
          <strong>
            {task.review ? names[task.review.actor] : task.status === "in_review" ? "None assigned yet" : "Not requested yet"}
          </strong>{" "}
          · Repairs used: {task.repair_count}/3
        </p>
        <p>Review is bound to the exact commit, not permission to deploy to production.</p>
      </div>
    </details>
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
