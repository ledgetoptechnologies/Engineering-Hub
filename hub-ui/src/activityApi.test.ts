import { describe, expect, it } from "vitest";
import { describeVerb, parseActivity, readActivity } from "./activityApi";

const row = { sequence: 12, action: "task.claimed", entity_id: "t1", revision: 2, created_at: "2026-10-22T12:00:00Z",
  kind: "task", actor: "codex", current_title: "Count crowns" };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

describe("activity", () => {
  it("parses audited events and drops malformed rows", () => {
    const page = parseActivity({ project: "treedar", items: [row, { ...row, action: "task.<script>" },
      { ...row, kind: "decision" }, { ...row, sequence: -1 }, { ...row, created_at: "nope" }], next_before: 12 }, "treedar");
    expect(page.items).toEqual([{ sequence: 12, kind: "task", verb: "claimed", entityId: "t1", revision: 2,
      at: "2026-10-22T12:00:00Z", actor: "codex", title: "Count crowns" }]);
    expect(page.nextBefore).toBe(12);
  });
  it("rejects another project's page", () => {
    expect(() => parseActivity({ project: "other", items: [] }, "treedar")).toThrow();
  });
  it("reads with cursor, and a missing route or gateway refusal means unsupported", async () => {
    const signal = new AbortController().signal;
    let seen = "";
    await readActivity("treedar", 12, signal, async (url) => { seen = String(url); return json({ project: "treedar", items: [] }); });
    expect(seen).toBe("/api/hub/v1/activity?project=treedar&limit=50&before=12");
    expect(await readActivity("treedar", null, signal, async () => json({}, 404))).toBeNull();
    expect(await readActivity("treedar", null, signal, async () => json({ detail: { code: "gateway_route_forbidden" } }, 403))).toBeNull();
    await expect(readActivity("treedar", null, signal, async () => json({}, 401))).rejects.toMatchObject({ status: 401 });
  });
  it("describes known verbs in plain words", () => {
    expect(describeVerb("candidate")).toBe("submitted a candidate for");
    expect(describeVerb("some_new_thing")).toBe("some new thing");
  });
});
