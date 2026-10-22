import { describe, expect, it } from "vitest";
import { imageVersionsView } from "./imageVersions";

const now = Date.parse("2026-10-22T01:00:00Z");
const stamp = (offset = 0) => new Date(now + offset).toISOString();
const digest = (letter = "a") => `sha256:${letter.repeat(64)}`;
function report() {
  return {
    configured: true, source: "agent_observation_report", reason: "available",
    reported_by: "hermes", report_updated_at: stamp(), advisory_only: true,
    items: [{ project: "engineering-hub", host: "nas", service: "hub", environment: "production",
      repository: "ghcr.io/ledgetoptechnologies/engineering-hub-control", reference: "latest",
      state: "up_to_date", reason: "same_image", checked_at: stamp(),
      running_observed_at: stamp(), published_observed_at: stamp(), valid_until: stamp(600_000),
      digest_kind: "config", current_digest: digest(), available_digest: digest() }],
  };
}

describe("advisory image version display", () => {
  it("renders matching fresh production observations", () => {
    const view = imageVersionsView(report(), false, now);
    expect(view.state).toBe("up_to_date");
    expect(view.label).toBe("Up to date");
    expect(view.reportedBy).toBe("hermes");
    expect(view.rows).toHaveLength(1);
  });
  it("shows a different published image as neutral update availability", () => {
    const input = report();
    Object.assign(input.items[0], { state: "update_available", reason: "different_image", available_digest: digest("b") });
    const view = imageVersionsView(input, false, now);
    expect(view.state).toBe("update_available");
    expect(view.label).toBe("Update available");
  });
  for (const input of [undefined, null, {}, [], { configured: false },
    { ...report(), items: [] }, { ...report(), configured: false },
    { ...report(), source: "untrusted" }, { ...report(), advisory_only: false },
    { ...report(), items: Array(33).fill(report().items[0]) }]) {
    it(`does not turn missing or malformed input into an all-clear (${JSON.stringify(input)?.slice(0, 60)})`, () => {
      expect(imageVersionsView(input, false, now).state).toBe("unknown");
    });
  }
  it("does not show staging-only inventory as production up to date", () => {
    const input = report(); input.items[0].environment = "staging";
    expect(imageVersionsView(input, false, now).state).toBe("unknown");
  });
  it("invalidates expired evidence and stale workspace snapshots", () => {
    expect(imageVersionsView(report(), false, now + 600_001).state).toBe("unknown");
    expect(imageVersionsView(report(), true, now).state).toBe("unknown");
  });
  for (const field of ["checked_at", "running_observed_at", "published_observed_at", "valid_until"] as const) {
    it(`rejects invalid ${field}`, () => {
      const input = report(); input.items[0][field] = "not-a-date";
      expect(imageVersionsView(input, false, now).state).toBe("unknown");
    });
  }
  for (const field of ["checked_at", "running_observed_at", "published_observed_at"] as const) {
    it(`does not trust future ${field}`, () => {
      const input = report(); input.items[0][field] = stamp(60_000);
      expect(imageVersionsView(input, false, now).state).toBe("unknown");
    });
  }
  it("does not trust positive states with inconsistent or missing digest evidence", () => {
    const input = report(); input.items[0].available_digest = digest("b");
    expect(imageVersionsView(input, false, now).state).toBe("unknown");
    input.items[0].available_digest = "";
    expect(imageVersionsView(input, false, now).state).toBe("unknown");
  });
  it("never trusts an excessive freshness window", () => {
    const input = report(); input.items[0].valid_until = stamp(1_800_001);
    expect(imageVersionsView(input, false, now).state).toBe("unknown");
  });
});
