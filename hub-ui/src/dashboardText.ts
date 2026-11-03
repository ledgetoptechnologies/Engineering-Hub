/** Plain-language dashboard wording derived only from recorded Hub state. */
import { actorFamily } from "./actors";
import type { Task } from "./types";

/** Visible count note on Home: says what a passed review is not, without claiming anything ran or may run. */
export function reviewPassedNote(count: number): string {
  return `${count} review passed · a review doesn't authorize a run`;
}

/** Review is a verdict on a commit, not permission to run it; whether anything ran or may run isn't known here. */
export const REVIEW_IS_NOT_A_RUN = "A passed review doesn't start or authorize a run. Running it needs separate authorization, which this page doesn't show.";

export function relativeAge(value: string | null | undefined, now = Date.now()): string | null {
  if (!value) return null;
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp) || timestamp > now + 30_000) return null;
  const minutes = Math.max(0, Math.floor((now - timestamp) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/** Attribute an answer to the viewer only when the recorded human is exactly them. */
export function answeredBy(answeredByActor: string | null | undefined, viewerActor: string | null | undefined): string {
  if (!answeredByActor) return "(answerer not recorded)";
  if (viewerActor && answeredByActor === viewerActor) return "by you";
  if (answeredByActor.startsWith("human:")) return "by another authorized person";
  return "(answerer not recorded)";
}

/** Who or what a task is waiting on, derived only from its recorded status and review. */
export function waitingOn(task: Task, agentName: (id: string, family?: string | null) => string): string {
  const owner = task.owner ? agentName(task.owner, task.owner_family) : "";
  const ownerFamily = task.owner ? actorFamily(task.owner, task.owner_family) : null;
  if (task.status === "assigned") return "Waiting for an agent to claim it.";
  if (task.status === "claimed")
    return task.owner
      ? task.repair_count > 0
        ? `${owner} is repairing it (${task.repair_count}/3 repairs used).`
        : `${owner} is working on it.`
      : "Claimed, but no owner is recorded.";
  if (task.status === "in_review")
    return task.review
      ? `Review recorded by ${agentName(task.review.actor, task.review.family)}.`
      : `Waiting for an independent review. No reviewer assigned${task.owner
        ? `; any agent except ${ownerFamily ? `${agentName(ownerFamily)}${ownerFamily !== task.owner ? " (on any computer)" : ""}` : owner} can review` : ""}.`;
  return task.review
    ? `Review passed by ${agentName(task.review.actor, task.review.family)}. ${REVIEW_IS_NOT_A_RUN}`
    : `Review passed. ${REVIEW_IS_NOT_A_RUN}`;
}
