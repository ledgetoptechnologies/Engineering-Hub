/** Backlog views over tasks: what waits on Beau, what is still moving, and the Work page filters. */
import type { Task } from "./types";

export const waitingLevel = (task: Task) => task.waiting_on?.level ?? "now";
/** Later items stay out of the counts but are never silent: "2 later", or empty when there are none. */
export const laterHint = (tasks: Task[], project?: string) => {
  const count = waitingTasks(tasks, project, "later").length;
  return count ? `${count} later` : "";
};

/** Tasks parked on Beau with an item he must clear; counts use the "now" level, "later" items wait behind them. */
export const waitingTasks = (tasks: Task[], project?: string, level: "now" | "later" = "now") =>
  tasks.filter((task) => task.status === "waiting_on_beau" && !!task.waiting_on && (!project || task.project === project)
    && waitingLevel(task) === level);

/** Still in motion: not review-passed, done or withdrawn. */
export const isActive = (task: Task) => !["verified", "done", "withdrawn"].includes(task.status);

export const WORK_FILTERS: (Task["status"] | "all")[] =
  ["all", "assigned", "claimed", "in_review", "verified", "waiting_on_beau", "done"];

/** One acceptance criterion per non-blank line. */
export const parseAcceptance = (text: string) => text.split("\n").map((line) => line.trim()).filter(Boolean);

export const waitingKind = (task: Task) => task.waiting_on?.type === "action" ? "Action" : "Decision";

/** Identity of one waiting item: the task plus the server time the item was raised. */
export const waitingItemKey = (task: Task) =>
  task.waiting_on ? `${task.id}|${task.waiting_on.at}|${task.waiting_on.by}` : task.id;

export interface ItemDraft { item: string; text: string }
/** A draft written for an earlier item is never shown, or sent, for a later one. */
export const draftFor = (draft: ItemDraft, item: string) => draft.item === item ? draft.text : "";
