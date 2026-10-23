import { describe, expect, it } from "vitest";
import { createDemoOverview } from "./mocks";
import { canWithdrawDecision, canWithdrawTask, splitWithdrawn } from "./withdrawn";

describe("splitWithdrawn", () => {
  it("moves withdrawn tasks and decisions aside so lists and counts skip them", () => {
    const overview = createDemoOverview(new Date());
    overview.tasks[0].status = "withdrawn";
    overview.decisions[0].status = "withdrawn";
    const split = splitWithdrawn(overview);
    expect(split.tasks.some((task) => task.status === "withdrawn")).toBe(false);
    expect(split.decisions.some((decision) => decision.status === "withdrawn")).toBe(false);
    expect(split.withdrawn?.tasks.map((task) => task.id)).toEqual([overview.tasks[0].id]);
    expect(split.withdrawn?.decisions.map((decision) => decision.id)).toEqual([overview.decisions[0].id]);
    expect(split.tasks.length + split.withdrawn!.tasks.length).toBe(overview.tasks.length);
  });
});

describe("withdraw eligibility mirrors the server", () => {
  const task = createDemoOverview(new Date()).tasks[0];
  const decision = createDemoOverview(new Date()).decisions[0];
  it("allows only unstarted tasks", () => {
    expect(canWithdrawTask({ ...task, status: "assigned", candidate_commit: null, review: null })).toBe(true);
    expect(canWithdrawTask({ ...task, status: "claimed", candidate_commit: null, review: null })).toBe(true);
    expect(canWithdrawTask({ ...task, status: "claimed", candidate_commit: "a".repeat(40), review: null })).toBe(false);
    expect(canWithdrawTask({ ...task, status: "in_review", candidate_commit: null, review: null })).toBe(false);
    expect(canWithdrawTask({ ...task, status: "verified", candidate_commit: null, review: null })).toBe(false);
  });
  it("allows only open, unprotected questions", () => {
    expect(canWithdrawDecision({ ...decision, status: "open", protected: false })).toBe(true);
    expect(canWithdrawDecision({ ...decision, status: "open", protected: true })).toBe(false);
    expect(canWithdrawDecision({ ...decision, status: "answered", protected: false })).toBe(false);
  });
});
