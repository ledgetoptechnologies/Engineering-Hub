import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { TrackingItems, type TrackingTask } from "./ProjectTrackingPanel";

describe("project tracking presentation", () => {
  it("shows progress, evidence and an explicitly unverified completion claim", () => {
    const task: TrackingTask = { id: "1", project: "demo", title: "Tracked work", status: "completion_claimed",
      created_by: "codex", execution_authorization: false, updates: [{ actor: "codex", progress: "Tests passed",
        completion_claim: true, evidence_links: [{label: "Evidence", url: "https://example.com/report"},
          {label: "Unsafe", url: "javascript:alert(1)"}] }] };
    const html = renderToStaticMarkup(<TrackingItems tasks={[task]} />);
    expect(html).toContain("Completion claimed — unverified");
    expect(html).toContain("Tests passed");
    expect(html).toContain('href="https://example.com/report"');
    expect(html).not.toContain('href="javascript:');
  });
});
