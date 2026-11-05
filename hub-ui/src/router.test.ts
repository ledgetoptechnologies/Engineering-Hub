import { describe, expect, it, vi } from "vitest";
import { detectPathRouting, parseRoute, pathOf, type Route } from "./router";

describe("routes", () => {
  it.each([
    ["", { page: "home" }],
    ["/", { page: "home" }],
    ["#/", { page: "home" }],
    ["/inbox", { page: "inbox" }],
    ["#/inbox", { page: "inbox" }],
    ["/work", { page: "work" }],
    ["/projects", { page: "projects" }],
    ["/projects/treedar", { page: "project", id: "treedar", tab: "overview" }],
    ["#/projects/treedar/work", { page: "project", id: "treedar", tab: "work" }],
    ["/projects/treedar/activity", { page: "project", id: "treedar", tab: "activity" }],
    ["/projects/treedar/labels/crowns-oct", { page: "labels", project: "treedar", set: "crowns-oct" }],
    ["/notes", { page: "notes" }],
    ["/planning", { page: "planning" }],
    ["#/planning", { page: "planning" }],
    ["/projects/treedar/plans", { page: "project", id: "treedar", tab: "plans" }],
    ["/agents", { page: "agents" }],
    ["/staging", { page: "staging" }],
    ["/orchestration", { page: "orchestration" }],
    ["#/orchestration/req-demo-viewer-refresh", { page: "orchestration-request", id: "req-demo-viewer-refresh" }],
    ["/orchestration/<script>", { page: "orchestration" }],
    ["/orchestration/a/b", { page: "orchestration" }],
  ])("parses %s", (value, route) => {
    expect(parseRoute(value)).toEqual(route);
  });
  it.each([
    ["#decisions", "inbox"], ["#work", "work"], ["#notes", "notes"], ["#agents", "agents"],
    ["#staging", "staging"], ["#software-queue", "staging-queues"],
  ])("keeps old single-page anchor %s working", (hash, page) => {
    expect(parseRoute(hash).page).toBe(page);
  });
  it.each(["/projects/../inbox", "/projects/UPPER", "/projects/<script>", "/nope", "/projects/" + "a".repeat(80)])(
    "never turns an unexpected path into a project: %s", (value) => {
      expect(parseRoute(value).page === "project").toBe(false);
    });
  it("falls back to a project's overview for an unknown tab", () => {
    expect(parseRoute("/projects/treedar/secrets")).toEqual({ page: "project", id: "treedar", tab: "overview" });
  });
  it("round-trips through pathOf", () => {
    const routes: Route[] = [{ page: "home" }, { page: "inbox" }, { page: "project", id: "3d-viewer", tab: "overview" },
      { page: "project", id: "treedar", tab: "notes" }, { page: "labels", project: "treedar", set: "crowns" }];
    for (const route of routes) expect(parseRoute(pathOf(route))).toEqual(route);
  });
});

describe("path routing detection", () => {
  it("turns on only when the server returns HTML for a client route", async () => {
    const html = vi.fn<typeof fetch>().mockResolvedValue(new Response("<!doctype html>", { headers: { "Content-Type": "text/html" } }));
    expect(await detectPathRouting(html)).toBe(true);
    expect(html.mock.calls[0][0]).toBe("/projects");
    for (const response of [new Response("{}", { status: 404 }), new Response("{}", { headers: { "Content-Type": "application/json" } }),
      new Response("denied", { status: 403, headers: { "Content-Type": "text/html" } })]) {
      expect(await detectPathRouting(vi.fn<typeof fetch>().mockResolvedValue(response))).toBe(false);
    }
    expect(await detectPathRouting(vi.fn<typeof fetch>().mockRejectedValue(new Error("offline")))).toBe(false);
  });
});

describe("orchestration routes", () => {
  it("round-trips a request route", () => {
    const route: Route = { page: "orchestration-request", id: "req-demo-billing-export" };
    expect(pathOf(route)).toBe("/orchestration/req-demo-billing-export");
    expect(parseRoute(pathOf(route))).toEqual(route);
    expect(pathOf({ page: "orchestration" })).toBe("/orchestration");
  });
});

describe("section sub-page routes", () => {
  it.each([
    ["/agents/installations", { page: "agent-installations" }],
    ["#/agents/installations", { page: "agent-installations" }],
    ["/agents/map", { page: "agent-map" }],
    ["/staging/queues", { page: "staging-queues" }],
    ["#/staging/resources", { page: "staging-resources" }],
  ])("parses %s", (value, route) => {
    expect(parseRoute(value)).toEqual(route);
  });
  it("round-trips every sub-page", () => {
    const routes: Route[] = [{ page: "agent-installations" }, { page: "staging-queues" }, { page: "staging-resources" }];
    for (const route of routes) expect(parseRoute(pathOf(route))).toEqual(route);
  });
  // Every address that worked before the sidebar gained sub-pages still lands on the same place, or on the sub-page that now holds it.
  it.each([
    ["/agents", { page: "agents" }],
    ["#/agents", { page: "agents" }],
    ["/staging", { page: "staging" }],
    ["#/staging", { page: "staging" }],
    ["#software-queue", { page: "staging-queues" }],
    ["/work", { page: "work" }],
    ["/inbox", { page: "inbox" }],
    ["/notes", { page: "notes" }],
    ["/planning", { page: "planning" }],
    ["/infrastructure", { page: "infrastructure" }],
    ["/infrastructure/build-pc", { page: "device", id: "build-pc" }],
    ["/orchestration", { page: "orchestration" }],
    ["/projects/treedar/decisions", { page: "project", id: "treedar", tab: "decisions" }],
    ["/projects/treedar/notes", { page: "project", id: "treedar", tab: "notes" }],
    ["/projects/treedar/labeling", { page: "project", id: "treedar", tab: "labeling" }],
  ])("keeps old address %s working", (value, route) => {
    expect(parseRoute(value)).toEqual(route);
  });
  it.each(["/agents/unknown", "/agents/installations/x", "/staging/nope", "/staging/queues/x"])(
    "falls back to the section overview for an unknown sub-page: %s", (value) => {
      expect(["agents", "staging"]).toContain(parseRoute(value).page);
    });
});
