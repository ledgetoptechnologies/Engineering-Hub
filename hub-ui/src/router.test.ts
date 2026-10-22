import { describe, expect, it } from "vitest";
import { href, parseRoute } from "./router";

describe("hash routes", () => {
  it.each([
    ["", { page: "home" }],
    ["#/", { page: "home" }],
    ["#/inbox", { page: "inbox" }],
    ["#/work", { page: "work" }],
    ["#/projects", { page: "projects" }],
    ["#/projects/treedar", { page: "project", id: "treedar" }],
    ["#/notes", { page: "notes" }],
    ["#/planning", { page: "planning" }],
    ["#/agents", { page: "agents" }],
    ["#/staging", { page: "staging" }],
  ])("parses %s", (hash, route) => {
    expect(parseRoute(hash)).toEqual(route);
  });
  it.each([
    ["#decisions", "inbox"], ["#work", "work"], ["#notes", "notes"], ["#agents", "agents"],
    ["#staging", "staging"], ["#software-queue", "staging"],
  ])("keeps old single-page anchor %s working", (hash, page) => {
    expect(parseRoute(hash).page).toBe(page);
  });
  it.each(["#/projects/../inbox", "#/projects/UPPER", "#/projects/a/b", "#/projects/<script>", "#/nope", "#/projects/" + "a".repeat(80)])(
    "never turns an unexpected path into a project id: %s", (hash) => {
      const route = parseRoute(hash);
      expect(route.page === "project").toBe(false);
    });
  it("round-trips through href", () => {
    for (const route of [{ page: "home" }, { page: "inbox" }, { page: "project", id: "3d-viewer" }] as const)
      expect(parseRoute(href(route))).toEqual(route);
  });
});
