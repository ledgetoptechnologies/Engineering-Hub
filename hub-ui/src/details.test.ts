import { describe, expect, it } from "vitest";
import { attachmentUrl, listAttachments, parseAttachments, parseContext } from "./details";

const entityId = "11111111-2222-4333-8444-555555555555";
const imageId = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const image = { id: imageId, kind: "decision", entity_id: entityId, project: "treedar", title: "Plot A1 map",
  media_type: "image/png", bytes: 200_000, sha256: "c".repeat(64), by: "codex", created_at: "2026-10-22T12:00:00Z",
  entity_revision: 1, url: "https://evil.example/x" };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

describe("entity context", () => {
  it("keeps details text and safe links", () => {
    expect(parseContext({ details: "Pick **one**.", links: [{ label: "Plan", url: "/notes" },
      { label: "Docs", url: "https://example.com/a" }] })).toEqual({ summary: "Pick **one**.",
      links: [{ label: "Plan", href: "/notes" }, { label: "Docs", href: "https://example.com/a" }] });
  });

  it("drops unsafe links, caps counts and returns null when empty", () => {
    const parsed = parseContext({ details: "x", links: [{ label: "js", url: "javascript:alert(1)" },
      { label: "http", url: "http://example.com" }, { label: "data", url: "data:text/html,x" },
      { label: "proto", url: "//evil.example" }, { label: "", url: "https://example.com" }, { label: "none" }] });
    expect(parsed?.links).toEqual([]);
    expect(parseContext({ links: Array.from({ length: 20 }, (_, i) => ({ label: `L${i}`, url: `/notes#${i}` })) })?.links).toHaveLength(8);
    expect(parseContext(null)).toBeNull();
    expect(parseContext({ details: "   " })).toBeNull();
    expect(parseContext({ details: "x".repeat(4097) })).toBeNull();
  });
});

describe("attachments", () => {
  it("accepts only raster images for this entity and builds URLs from the id, never the server url", () => {
    const parsed = parseAttachments({ items: [image, { ...image, id: "not-a-uuid" }, { ...image, media_type: "image/svg+xml" },
      { ...image, entity_id: "99999999-2222-4333-8444-555555555555" }, { ...image, bytes: 50 * 1024 * 1024 }] }, entityId);
    expect(parsed).toEqual([{ id: imageId, title: "Plot A1 map", bytes: 200_000, mediaType: "image/png" }]);
    expect(attachmentUrl(imageId)).toBe(`/api/hub/v1/attachments/${imageId}/image`);
    expect(() => attachmentUrl("../x")).toThrow();
  });

  it("treats a missing route or gateway refusal as no attachments, and real access loss as an error", async () => {
    const signal = new AbortController().signal;
    expect(await listAttachments("decision", entityId, signal, async () => json({}, 404))).toBeNull();
    expect(await listAttachments("decision", entityId, signal,
      async () => json({ detail: { code: "gateway_route_forbidden" } }, 403))).toBeNull();
    await expect(listAttachments("decision", entityId, signal,
      async () => json({ detail: { code: "access_denied" } }, 403))).rejects.toMatchObject({ status: 403 });
    expect(await listAttachments("task", entityId, signal, async () => json({ items: [image] }))).toHaveLength(1);
    expect(await listAttachments("task", "not-a-uuid", signal, async () => { throw new Error("must not fetch"); })).toBeNull();
  });
});
