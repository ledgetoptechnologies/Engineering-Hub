import { describe, expect, it, vi } from "vitest";
import { imageUrl, parseLabelSet, parseLabelSetList, saveLabel } from "./labelSetsApi";

const choices = [{ key: "conifer", label: "Conifer" }, { key: "broadleaf", label: "Broadleaf" }, { key: "unsure", label: "Not sure" }];
function set(extra: Record<string, unknown> = {}) {
  return { id: "crowns-oct", project: "treedar", title: "Crown types", instructions: "Tap one per crown.", choices,
    updated_at: "2026-10-21T20:00:00Z",
    items: [{ id: "91", meta: { area: "A1", h: 11.89 }, label: null, revision: 0 },
      { id: "92", meta: { area: "A2", h: 9.1 }, label: { choice: "conifer", by: "human:cf:x", at: "2026-10-21T20:00:00Z" }, revision: 1 }],
    ...extra };
}
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

describe("labelling sets client", () => {
  it("accepts any agent-defined choices and meta fields", () => {
    const parsed = parseLabelSet(set({ choices: [{ key: "a", label: "Alpha" }, { key: "b", label: "Beta" }],
      items: [{ id: "q1", meta: { chapter: "3", difficulty: 2 }, label: { choice: "b", by: "human:1", at: "2026-10-21T20:00:00Z" }, revision: 4 }] }));
    expect(parsed.choices.map((c) => c.key)).toEqual(["a", "b"]);
    expect(parsed.items[0].meta).toEqual({ chapter: "3", difficulty: 2 });
  });
  it.each([
    { choices: [choices[0]] }, { choices: [...choices, ...choices] },
    { items: [{ id: "91", meta: {}, label: { choice: "palm", by: "x", at: "2026-10-21T20:00:00Z" }, revision: 1 }] },
    { items: [{ id: "../x", meta: {}, label: null, revision: 0 }] },
    { items: [{ id: "1", meta: { "bad key": "x" }, label: null, revision: 0 }] },
    { items: [{ id: "1", meta: {}, label: null, revision: -1 }] },
    { items: [{ id: "1", meta: {}, label: null, revision: 0 }, { id: "1", meta: {}, label: null, revision: 0 }] },
    { title: " " },
  ])("rejects malformed sets %#", (change) => {
    expect(() => parseLabelSet(set(change))).toThrow("invalid_label_set");
  });
  it("rejects a set or list for another id or project", () => {
    expect(() => parseLabelSet(set(), "other")).toThrow("invalid_label_set");
    expect(() => parseLabelSetList({ items: [{ id: "s", project: "3d-viewer", title: "T", item_count: 1, labeled_count: 0,
      updated_at: "2026-10-21T20:00:00Z" }] }, "treedar")).toThrow("invalid_label_set");
  });
  it("builds image URLs only from safe ids", () => {
    expect(imageUrl("crowns-oct", "91")).toBe("/api/hub/v1/label-sets/crowns-oct/items/91/image");
    expect(() => imageUrl("../x", "91")).toThrow();
  });
  it("saves with CSRF and the item revision, and requires the next revision back", async () => {
    const parsed = parseLabelSet(set());
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ id: "91", meta: { area: "A1", h: 11.89 },
      label: { choice: "broadleaf", by: "human:cf:x", at: "2026-10-21T21:00:00Z" }, revision: 1 }));
    const saved = await saveLabel(parsed.id, parsed.items[0], "broadleaf", "csrf", parsed.choices, fetchImpl);
    expect(saved.label?.choice).toBe("broadleaf");
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("/api/hub/v1/label-sets/crowns-oct/items/91/label");
    expect(init?.method).toBe("PUT");
    expect((init?.headers as Record<string, string>)["X-Hub-CSRF"]).toBe("csrf");
    expect(JSON.parse(init?.body as string)).toEqual({ choice: "broadleaf", expected_revision: 0 });
  });
  it("refuses unknown choices and missing CSRF before sending", async () => {
    const parsed = parseLabelSet(set());
    const fetchImpl = vi.fn<typeof fetch>();
    await expect(saveLabel(parsed.id, parsed.items[0], "palm", "csrf", parsed.choices, fetchImpl)).rejects.toMatchObject({ status: 422 });
    await expect(saveLabel(parsed.id, parsed.items[0], "conifer", "", parsed.choices, fetchImpl)).rejects.toMatchObject({ status: 403 });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
  it("treats a save answer with the wrong choice or revision as invalid", async () => {
    const parsed = parseLabelSet(set());
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ id: "91", meta: {}, label: null, revision: 5 }));
    await expect(saveLabel(parsed.id, parsed.items[0], "conifer", "csrf", parsed.choices, fetchImpl))
      .rejects.toMatchObject({ code: "invalid_label_set" });
  });
});
