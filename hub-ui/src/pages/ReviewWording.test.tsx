import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Hub } from "../hub";
import type { Task } from "../types";
import { reviewPassedNote } from "../dashboardText";
import { TaskRow } from "./tasks";

describe("visible review authorization wording", () => {
  it("keeps the Home count qualifier for zero and nonzero counts", () => {
    expect(reviewPassedNote(0)).toBe("0 review passed · a review doesn't authorize a run");
    expect(reviewPassedNote(3)).toBe("3 review passed · a review doesn't authorize a run");
  });
  it("shows the separate authorization requirement in the visible task summary", () => {
    const task: Task = { id: "task", revision: 1, updated_at: "2026-11-01T00:00:00Z", project: "engineering-hub", title: "Example",
      status: "verified", owner: "codex", candidate_commit: "a".repeat(40), repair_count: 0,
      review: { actor: "claude", commit: "a".repeat(40), verdict: "approve" } };
    const hub = { projectName: () => "Engineering Hub", session: null, demo: true, writable: false } as unknown as Hub;
    const html = renderToStaticMarkup(<TaskRow hub={hub} task={task} />);
    expect(html).toContain("Review passed by Claude. A passed review doesn&#x27;t start or authorize a run.");
    expect(html).toContain("Running it needs separate authorization, which this page doesn&#x27;t show.");
    expect(html).not.toMatch(/has (not )?run|never ran|was run/i);
  });
});
