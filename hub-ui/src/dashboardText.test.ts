import { describe, expect, it } from "vitest";
import { relativeAge, waitingOn } from "./dashboardText";
import type { Task } from "./types";

const name = (id: string) => ({ codex: "Codex", claude: "Claude", hermes: "Hermes" })[id] ?? id;
const commit = "a".repeat(40);
function task(change: Partial<Task>): Task {
  return { id: "t", revision: 1, updated_at: "2026-10-20T00:00:00Z", project: "treedar", title: "T", status: "assigned",
    owner: null, candidate_commit: null, review: null, repair_count: 0, ...change };
}

describe("dashboard wording", () => {
  it.each([
    [{ status: "assigned" }, "Waiting for an agent to claim it."],
    [{ status: "claimed", owner: "codex" }, "Codex is working on it."],
    [{ status: "claimed", owner: "codex", repair_count: 2 }, "Codex is repairing it (2/3 repairs used)."],
    [{ status: "in_review", owner: "claude", candidate_commit: commit },
      "Waiting for an independent review. No reviewer assigned; any agent except Claude can review."],
    [{ status: "verified", owner: "codex", review: { actor: "claude", commit, verdict: "approve" } },
      "Review passed by Claude. Running it is separate and needs its own authorization."],
  ] as [Partial<Task>, string][])("derives waiting-on from recorded state %#", (change, text) => {
    expect(waitingOn(task(change), name)).toBe(text);
  });
  it("never presents an unreviewed in-review task as actively reviewed", () => {
    expect(waitingOn(task({ status: "in_review", owner: "codex" }), name)).toContain("No reviewer assigned");
  });
  it("formats relative age from real timestamps only", () => {
    const now = Date.parse("2026-10-21T01:00:00Z");
    expect(relativeAge("2026-10-21T00:59:40Z", now)).toBe("just now");
    expect(relativeAge("2026-10-21T00:15:00Z", now)).toBe("45m ago");
    expect(relativeAge("2026-10-20T12:08:00Z", now)).toBe("12h ago");
    expect(relativeAge("2026-10-17T01:00:00Z", now)).toBe("4d ago");
    expect(relativeAge(null, now)).toBeNull();
    expect(relativeAge("not a time", now)).toBeNull();
    expect(relativeAge("2026-10-21T02:00:00Z", now)).toBeNull();
  });
});
