/** Backlog views over tasks: what waits on Beau, what is still moving, and the Work page filters. */
import type { Task } from "./types";

/** Tasks parked on Beau with a decision or action item he must clear. */
export const waitingTasks = (tasks: Task[], project?: string) =>
  tasks.filter((task) => task.status === "waiting_on_beau" && !!task.waiting_on && (!project || task.project === project));

/** Open tasks whose waiting question the raising agent withdrew; Beau can see why and undo it. */
export const withdrawnWaitingTasks = (tasks: Task[], project?: string) =>
  tasks.filter((task) => !!task.waiting_withdrawn && task.status !== "waiting_on_beau"
    && task.status !== "done" && task.status !== "withdrawn" && (!project || task.project === project));

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
