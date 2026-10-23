import { describe, expect, it } from "vitest";
import { stagingSummary } from "./pages/SystemPages";

describe("stagingSummary", () => {
  it("states configuration only, never readiness", () => {
    expect(stagingSummary([{ executor_enabled: false }])).toMatch(/^No projects are connected to the staging PC yet\./);
    expect(stagingSummary([{ executor_enabled: true }, { executor_enabled: false }])).toBe("1 of 2 projects have a staging target enabled.");
    expect(stagingSummary([{ executor_enabled: true }])).toBe("1 of 1 project has a staging target enabled.");
  });
});
