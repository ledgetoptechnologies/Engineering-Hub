import { describe, expect, it, vi } from "vitest";
import { isPlanningId, isPlanningTopic, listPlanning, parsePlanningItem, parsePlanningList, PlanningConflictError,
  planningPath, planningRequestBytes, readPlanning, savePlanning, validPlanningSelection, type PlanningDraft, type PlanningSelection } from "./planningApi";

const topic: PlanningSelection = { scope: "topic", project: "treedar", topic: "TreeDAR-v1" };
const hub: PlanningSelection = { scope: "hub_improvements", project: "engineering-hub", topic: null };
const draft: PlanningDraft = { ...topic, kind: "idea", item_id: "classify-trees", title: "Classification idea",
  content: "<script>inert</script>\nproposal", status: "proposed", expected_version: 0 };
function meta(selection = topic) { return { ...selection, kind: "idea", item_id: "classify-trees", title: "Classification idea", status: "proposed",
  path: planningPath({ ...selection, kind: "idea", item_id: "classify-trees" }), version: 1, sha256: "a".repeat(64),
  created_by: "codex", updated_by: "human:cf:" + "b".repeat(64), created_at: "2026-10-22T12:00:00Z", updated_at: "2026-10-22T12:00:00Z" }; }
const full = (extra: Record<string, unknown> = {}) => ({ ...meta(), content: draft.content, ...extra });
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const signal = () => new AbortController().signal;

describe("planning client", () => {
  it("keeps exact case topics and separates the Hub namespace", () => {
    expect(isPlanningTopic("TreeDAR-v1")).toBe(true);
    expect(isPlanningTopic("project:discussion")).toBe(true);
    expect(isPlanningTopic("tree/dar")).toBe(false);
    expect(isPlanningTopic("a\n")).toBe(false);
    expect(isPlanningId("new-idea_1")).toBe(true);
    expect(isPlanningId("UpperCase")).toBe(false);
    expect(validPlanningSelection(hub)).toBe(true);
    expect(validPlanningSelection({ ...hub, project: "treedar" })).toBe(false);
    expect(validPlanningSelection({ ...hub, topic: "TreeDAR-v1" })).toBe(false);
    expect(planningPath(draft)).toBe("planning/topics/TreeDAR-v1/idea/classify-trees.json");
    expect(planningPath({ ...hub, kind: "idea", item_id: "classify-trees" })).toBe("planning/hub-improvements/idea/classify-trees.json");
  });
  it("parses inert text and author provenance without treating status as authority", () => {
    const item = parsePlanningItem(full(), draft);
    expect(item.content).toBe(draft.content);
    expect(item.updated_by).toHaveLength(73);
    expect(item.status).toBe("proposed");
  });
  it.each([
    { items: [meta(), meta()], truncated: false },
    { items: [{ ...meta(), project: "engineering-hub" }], truncated: false },
    { items: [{ ...meta(), topic: "treedar-v1" }], truncated: false },
    { items: [{ ...meta(), scope: "hub_improvements" }], truncated: false },
    { items: [{ ...meta(), path: "planning/topics/other/idea/x.json" }], truncated: false },
    { items: [{ ...meta(), status: "approved" }], truncated: false },
    { items: [{ ...meta(), kind: "command" }], truncated: false },
    { items: [{ ...meta(), updated_at: "later" }], truncated: false },
    { items: [{ ...meta(), version: 0 }], truncated: false },
    { items: [{ ...meta(), item_id: "../escape" }], truncated: false },
    { items: [], truncated: "no" }, null,
  ])("rejects malformed or wrong-scope list %#", value => {
    expect(() => parsePlanningList(value, topic)).toThrow("invalid_planning_response");
  });
  it("rejects filtered kind mismatch and wrong document identity", () => {
    expect(() => parsePlanningList({ items: [meta()], truncated: false }, topic, "plan")).toThrow();
    expect(() => parsePlanningItem(full({ item_id: "other" }), draft)).toThrow();
    expect(() => parsePlanningItem(full({ content: "x\u0000y" }), draft)).toThrow();
  });
  it("lists only the selected exact scope with a bounded same-origin request", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ items: [meta()], truncated: true }));
    const result = await listPlanning(topic, signal(), "idea", fetchImpl);
    expect(result.truncated).toBe(true);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl.mock.calls[0][0]).toBe("/api/hub/v1/planning?scope=topic&project=treedar&topic=TreeDAR-v1&limit=50&kind=idea");
    expect(fetchImpl.mock.calls[0][1]).toMatchObject({ method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store" });
  });
  it("never emits a topic for the separate Hub backlog", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ items: [meta(hub)], truncated: false }));
    await listPlanning(hub, signal(), undefined, fetchImpl);
    expect(fetchImpl.mock.calls[0][0]).toBe("/api/hub/v1/planning?scope=hub_improvements&project=engineering-hub&limit=50");
  });
  it("reads by the exact stable identity", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response(full()));
    await readPlanning(draft, signal(), fetchImpl);
    expect(fetchImpl.mock.calls[0][0]).toBe("/api/hub/v1/planning/document?scope=topic&project=treedar&topic=TreeDAR-v1&kind=idea&item_id=classify-trees");
  });
  it("saves with CSRF and CAS but never a caller-supplied author", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response(full()));
    await savePlanning({ ...draft, author: "hermes" } as PlanningDraft, "csrf-token", fetchImpl);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("/api/hub/v1/planning");
    expect(JSON.parse(init?.body as string)).toEqual(draft);
    expect(init).toMatchObject({ credentials: "same-origin", redirect: "error", cache: "no-store", method: "POST" });
    expect((init?.headers as Record<string, string>)["X-Hub-CSRF"]).toBe("csrf-token");
  });
  it("rejects invalid scopes, IDs, sizes or missing CSRF without a request", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    await expect(savePlanning(draft, "", fetchImpl)).rejects.toMatchObject({ code: "csrf_required" });
    await expect(savePlanning({ ...draft, item_id: "../x" }, "c", fetchImpl)).rejects.toMatchObject({ code: "invalid_input" });
    await expect(savePlanning({ ...draft, content: "é".repeat(24577) }, "c", fetchImpl)).rejects.toMatchObject({ code: "planning_too_large" });
    await expect(savePlanning({ ...draft, scope: "hub_improvements" }, "c", fetchImpl)).rejects.toMatchObject({ code: "invalid_input" });
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(planningRequestBytes({ ...draft, content: "é" })).toBeGreaterThan(planningRequestBytes({ ...draft, content: "a" }));
  });
  it("does not accept a skipped response version", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response(full({ version: 3 })));
    await expect(savePlanning(draft, "c", fetchImpl)).rejects.toMatchObject({ code: "invalid_planning_response" });
  });
  it("surfaces a concurrent edit without automatically retrying", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ detail: { code: "planning_version_conflict", current_version: 4 } }, 409));
    const error = await savePlanning(draft, "c", fetchImpl).catch(value => value);
    expect(error).toBeInstanceOf(PlanningConflictError);
    expect(error.currentVersion).toBe(4);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
  it("does not reflect HTML proxy failures or trust an unrelated conflict code", async () => {
    const html = vi.fn<typeof fetch>().mockResolvedValue(new Response("<h1>secret diagnostic</h1>", { status: 503 }));
    await expect(readPlanning(draft, signal(), html)).rejects.toMatchObject({ code: "planning_unavailable" });
    const other = vi.fn<typeof fetch>().mockResolvedValue(response({ detail: { code: "other_conflict", current_version: 4 } }, 409));
    const error = await savePlanning(draft, "c", other).catch(value => value);
    expect(error).not.toBeInstanceOf(PlanningConflictError);
  });
});
