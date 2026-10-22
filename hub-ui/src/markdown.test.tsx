import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Markdown } from "./markdown";
import { groupByFolder } from "./NotesPanel";
import { isMissingRoute } from "./api";
import { listTopics, parseTopicList } from "./topicsApi";
import { listLabelSets } from "./labelSetsApi";

const html = (source: string) => renderToStaticMarkup(<Markdown source={source} />);
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

describe("safe markdown", () => {
  it("formats headings, lists, code, quotes and tables", () => {
    const out = html("# Title\n\n- one\n- [x] done\n\n1. first\n\n```\nlet x = 1;\n```\n\n> note\n\n| a | b |\n|---|---|\n| 1 | **2** |");
    expect(out).toContain('<h2 class="md-h1">Title</h2>');
    expect(out).toContain("<ul><li>one</li>");
    expect(out).toContain('type="checkbox"');
    expect(out).toContain("<ol><li>first</li></ol>");
    expect(out).toContain('<pre class="md-code"><code>let x = 1;</code></pre>');
    expect(out).toContain("<blockquote>note</blockquote>");
    expect(out).toContain("<td><strong>2</strong></td>");
  });

  it("never turns note text into markup", () => {
    const out = html('<img src=x onerror="alert(1)"> <script>alert(1)</script> `<b>`');
    expect(out).not.toContain("<img");
    expect(out).not.toContain("<script");
    expect(out).toContain("&lt;img");
    expect(out).toContain("<code>&lt;b&gt;</code>");
  });

  it("links only https and Hub paths", () => {
    expect(html("[ok](https://example.com/a)")).toContain('href="https://example.com/a"');
    expect(html("[hub](/projects/treedar)")).toContain('href="/projects/treedar"');
    for (const bad of ["javascript:alert(1)", "http://example.com", "data:text/html,x", "//evil.example"]) {
      const out = html(`[x](${bad})`);
      expect(out).not.toContain("href=");
    }
  });
});

describe("notes folder tree", () => {
  it("groups by folder, root first, folders sorted", () => {
    const groups = groupByFolder([{ path: "z/a.md" }, { path: "top.md" }, { path: "a/b/c.md" }, { path: "z/b.md" }]);
    expect(groups.map((g) => [g.folder, g.items.length])).toEqual([["", 1], ["a/b", 1], ["z", 2]]);
  });
});

describe("gateway responses are not sign-outs", () => {
  const forbidden = () => json({ detail: { code: "gateway_route_forbidden" } }, 403);
  it("treats a gateway route refusal as a missing route", async () => {
    expect(await isMissingRoute(forbidden())).toBe(true);
    expect(await isMissingRoute(json({ detail: { code: "gateway_invalid_query" } }, 400))).toBe(true);
    expect(await isMissingRoute(new Response("", { status: 404 }))).toBe(true);
  });
  it("keeps real access failures as failures", async () => {
    expect(await isMissingRoute(json({ detail: { code: "access_denied" } }, 403))).toBe(false);
    expect(await isMissingRoute(new Response("forbidden", { status: 403, headers: { "Content-Type": "text/html" } }))).toBe(false);
    expect(await isMissingRoute(json({}, 401))).toBe(false);
  });
  it("topic list and label sets report 'not on this server' for a gateway refusal", async () => {
    const signal = new AbortController().signal;
    expect(await listTopics(signal, async () => forbidden())).toBeNull();
    await expect(listLabelSets("treedar", signal, async () => forbidden())).rejects.toMatchObject({ status: 404, code: "label_sets_unavailable" });
    await expect(listTopics(signal, async () => json({ detail: { code: "access_denied" } }, 403))).rejects.toMatchObject({ status: 403 });
  });
});

describe("topic list", () => {
  it("parses server topics and rejects bad rows", () => {
    expect(parseTopicList({ items: [{ topic: "eh-1", last_activity_at: "2026-10-21T00:00:00Z", peer_count: 2 }] }))
      .toEqual([{ topic: "eh-1", last_activity_at: "2026-10-21T00:00:00Z", peer_count: 2, source: "server" }]);
    expect(() => parseTopicList({ items: [{ topic: "bad topic" }] })).toThrow();
    expect(() => parseTopicList({ items: [{ topic: "a" }, { topic: "a" }] })).toThrow();
  });
});
