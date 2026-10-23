/** Withdrawn tasks and questions: kept for history, never shown as open work. */
import type { Decision, Overview, Task } from "./types";

export const WITHDRAW_REASON_MAX = 500;

/** Every page reads `tasks` and `decisions`; moving withdrawn items aside here keeps them out of all lists and counts. */
export function splitWithdrawn(overview: Overview): Overview {
  const tasks = overview.tasks.filter((task) => task.status !== "withdrawn");
  const decisions = overview.decisions.filter((decision) => decision.status !== "withdrawn");
  return {
    ...overview, tasks, decisions,
    withdrawn: {
      tasks: overview.tasks.filter((task) => task.status === "withdrawn"),
      decisions: overview.decisions.filter((decision) => decision.status === "withdrawn"),
    },
  };
}

/** Mirrors the server: only unstarted tasks and unanswered, unprotected questions. */
export const canWithdrawTask = (task: Task) =>
  (task.status === "assigned" || task.status === "claimed") && task.candidate_commit === null && task.review === null;
export const canWithdrawDecision = (decision: Decision) => decision.status === "open" && !decision.protected;
