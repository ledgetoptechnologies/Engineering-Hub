import { describe, expect, it } from "vitest";
import { attachmentUrl, parseDetails } from "./details";

const image = { kind: "image", id: "plot-a1-map", title: "Plot A1 map", width: 1600, height: 1200, bytes: 400_000 };

describe("decision and task details", () => {
  it("keeps summary, safe links and valid images", () => {
    const parsed = parseDetails({ summary: "Pick **one**.", links: [{ label: "Plan", url: "/notes" },
      { label: "Docs", url: "https://example.com/a" }], attachments: [image] });
    expect(parsed).toEqual({ summary: "Pick **one**.", links: [{ label: "Plan", href: "/notes" },
      { label: "Docs", href: "https://example.com/a" }], images: [{ id: "plot-a1-map", title: "Plot A1 map", width: 1600, height: 1200, bytes: 400_000 }] });
  });

  it("drops unsafe links and non-image or malformed attachments", () => {
    const parsed = parseDetails({ summary: "x", links: [
      { label: "js", url: "javascript:alert(1)" }, { label: "http", url: "http://example.com" },
      { label: "data", url: "data:text/html,x" }, { label: "proto-relative", url: "//evil.example" },
      { label: "", url: "https://example.com" }, { label: "no url" }],
    attachments: [{ ...image, kind: "svg" }, { ...image, id: "../escape" }, { ...image, width: 0 },
      { ...image, bytes: 50 * 1024 * 1024 }, { ...image, id: "ok-one" }] });
    expect(parsed?.links).toEqual([]);
    expect(parsed?.images.map((item) => item.id)).toEqual(["ok-one"]);
  });

  it("caps counts and lengths and returns null when nothing is left", () => {
    expect(parseDetails(null)).toBeNull();
    expect(parseDetails("text")).toBeNull();
    expect(parseDetails({ summary: "   " })).toBeNull();
    expect(parseDetails({ summary: "x".repeat(4001) })).toBeNull();
    const many = parseDetails({ links: Array.from({ length: 20 }, (_, i) => ({ label: `L${i}`, url: `/notes#${i}` })),
      attachments: Array.from({ length: 10 }, (_, i) => ({ ...image, id: `img-${i}` })) });
    expect(many?.links).toHaveLength(10);
    expect(many?.images).toHaveLength(6);
  });

  it("builds attachment URLs only from safe ids", () => {
    expect(attachmentUrl("plot-a1-map")).toBe("/api/hub/v1/attachments/plot-a1-map");
    expect(() => attachmentUrl("../x")).toThrow();
  });
});
