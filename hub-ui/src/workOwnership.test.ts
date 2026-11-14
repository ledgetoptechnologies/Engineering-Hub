import { describe, expect, it } from "vitest";
import { CLOCK_SKEW_MS, ownershipRows, UNCLAIMED } from "./pages/WorkOwnership";
import type { Task } from "./types";

const now = Date.parse("2026-10-30T12:00:00Z");
const ago = (days: number) => new Date(now - days * 24 * 60 * 60 * 1000).toISOString();
const inst = "installation:11111111-2222-4333-8444-555555555555";
let n = 0;
function task(change: Partial<Task>): Task {
  return { id: `t${n++}`, revision: 1, updated_at: ago(0), project: "engineering-hub", title: "T", status: "assigned",
    owner: null, candidate_commit: null, review: null, repair_count: 0, ...change };
}

describe("who's on what", () => {
  it("counts each recorded owner's open work by state", () => {
    const rows = ownershipRows([
      task({ owner: "codex", status: "claimed", state_since: ago(2) }),
      task({ owner: "codex", status: "in_review", state_since: ago(1) }),
      task({ owner: "codex", status: "assigned", state_since: ago(5) }),
      task({ owner: "claude", status: "claimed", state_since: ago(3) }),
    ], now);
    expect(rows.map(row => row.key)).toEqual(["codex", "claude"]);
    expect(rows[0]).toMatchObject({ claimed: 1, inReview: 1, assigned: 1, open: 3, oldestOpenSince: ago(5) });
  });
  it("keeps a paired installation apart from its family", () => {
    const rows = ownershipRows([
      task({ owner: "codex", status: "claimed" }),
      task({ owner: inst, owner_family: "codex", status: "claimed" }),
    ], now);
    expect(rows.map(row => row.key).sort()).toEqual(["codex", inst].sort());
    expect(rows.find(row => row.key === inst)?.family).toBe("codex");
  });
  it("puts work nobody owns last", () => {
    const rows = ownershipRows([task({ status: "assigned" }), task({ owner: "hermes", status: "claimed" })], now);
    expect(rows.map(row => row.key)).toEqual(["hermes", UNCLAIMED]);
  });
  it("counts passed review only within 7 days of a recorded time", () => {
    const rows = ownershipRows([
      task({ owner: "codex", status: "verified", state_since: ago(1) }),
      task({ owner: "codex", status: "verified", state_since: ago(8) }),
      task({ owner: "codex", status: "verified", state_since: null }),
      task({ owner: "codex", status: "verified", state_since: "not a date" }),
      task({ owner: "codex", status: "verified", state_since: ago(-2) }),
    ], now);
    expect(rows).toEqual([expect.objectContaining({ key: "codex", passedRecently: 1, open: 0, oldestOpenSince: null })]);
  });
  it("drops owners with nothing open or recent, and ignores withdrawn tasks", () => {
    expect(ownershipRows([
      task({ owner: "codex", status: "verified", state_since: ago(30) }),
      task({ owner: "claude", status: "withdrawn" }),
    ], now)).toEqual([]);
  });
  it("leaves the oldest age unknown when no open task records one", () => {
    expect(ownershipRows([task({ owner: "codex", status: "claimed", state_since: null })], now)[0].oldestOpenSince).toBeNull();
  });
  it("accepts up to the clock skew ahead, and nothing beyond it", () => {
    const at = (ms: number) => new Date(now + ms).toISOString();
    const open = (ms: number) => ownershipRows([task({ owner: "codex", status: "claimed", state_since: at(ms) })], now)[0];
    const passed = (ms: number) => ownershipRows([task({ owner: "codex", status: "verified", state_since: at(ms) })], now);
    expect(open(CLOCK_SKEW_MS).oldestOpenSince).toBe(at(CLOCK_SKEW_MS));
    expect(open(CLOCK_SKEW_MS + 1).oldestOpenSince).toBeNull();
    expect(passed(CLOCK_SKEW_MS)[0].passedRecently).toBe(1);
    expect(passed(CLOCK_SKEW_MS + 1)).toEqual([]);
  });
  it("reports incomplete state-time coverage without inventing an oldest item", () => {
    const rows = ownershipRows([
      task({ owner: "codex", status: "claimed", state_since: ago(3) }),
      task({ owner: "codex", status: "claimed", state_since: null }),
      task({ owner: "codex", status: "in_review", state_since: "invalid" }),
      task({ owner: "codex", status: "assigned", state_since: ago(-1) }),
    ], now);
    expect(rows[0]).toMatchObject({ oldestOpenSince: ago(3), unknownOpenTimes: 3, claimed: 2, inReview: 1, assigned: 1 });
  });
  it("never takes a future time as the oldest open item", () => {
    expect(ownershipRows([task({ owner: "codex", status: "claimed", state_since: ago(-1) })], now)[0].oldestOpenSince).toBeNull();
    expect(ownershipRows([
      task({ owner: "codex", status: "claimed", state_since: ago(-1) }),
      task({ owner: "codex", status: "in_review", state_since: ago(2) }),
    ], now)[0].oldestOpenSince).toBe(ago(2));
  });
});
