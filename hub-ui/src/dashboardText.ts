/** Plain-language dashboard wording derived only from recorded Hub state. */
import type { Task } from "./types";

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
export function waitingOn(task: Task, agentName: (id: string) => string): string {
  if (task.status === "assigned") return "Waiting for an agent to claim it.";
  if (task.status === "claimed")
    return task.owner
      ? task.repair_count > 0
        ? `${agentName(task.owner)} is repairing it (${task.repair_count}/3 repairs used).`
        : `${agentName(task.owner)} is working on it.`
      : "Claimed, but no owner is recorded.";
  if (task.status === "in_review")
    return task.review
      ? `Review recorded by ${agentName(task.review.actor)}.`
      : `Waiting for an independent review. No reviewer assigned${task.owner ? `; any agent except ${agentName(task.owner)} can review` : ""}.`;
  return task.review
    ? `Review passed by ${agentName(task.review.actor)}. Running it is separate and needs its own authorization.`
    : "Review passed. Running it is separate and needs its own authorization.";
}
