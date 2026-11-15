import { describe, expect, it } from "vitest";
import { navKey, projectPlace, sectionOf, workspaceNav, type NavItem } from "./navModel";
import type { Route } from "./router";

const ids = (items: NavItem[]) => items.flatMap((item) => [item.id, ...(item.children ?? []).map((child) => child.id)]);
const base = { infrastructure: false, orchestration: false };

describe("workspace sections", () => {
  it("hides Infrastructure and its sub-items without the capability", () => {
    expect(ids(workspaceNav({ ...base, deviceId: "build-pc" })).some((id) => id.startsWith("infrastructure"))).toBe(false);
    expect(ids(workspaceNav({ ...base, infrastructure: true }))).toContain("infrastructure");
  });
  it("shows Orchestration under Work only when the demo gate allows it", () => {
    expect(ids(workspaceNav(base))).not.toContain("work/orchestration");
    expect(ids(workspaceNav({ ...base, orchestration: true }))).toContain("work/orchestration");
  });
  it("shows the viewed device under Infrastructure", () => {
    const infra = workspaceNav({ ...base, infrastructure: true, deviceId: "build-pc" }).find((item) => item.id === "infrastructure");
    expect(infra?.children?.map((child) => child.route)).toEqual([{ page: "device", id: "build-pc" }]);
  });
  it("has a sub-item key for every route that belongs to one", () => {
    const all = ids(workspaceNav({ ...base, infrastructure: true, orchestration: true, deviceId: "x" }));
    const routes: Route[] = [{ page: "home" }, { page: "inbox" }, { page: "work" }, { page: "orchestration" },
      { page: "orchestration-request", id: "r" }, { page: "agents" }, { page: "agent-installations" }, { page: "agent-map" }, { page: "staging" },
      { page: "staging-queues" }, { page: "staging-resources" }, { page: "infrastructure" }, { page: "device", id: "x" }];
    for (const route of routes) expect(all).toContain(navKey(route));
  });
  it("puts project pages in the Projects section and leaves notes and planning outside every section", () => {
    expect(sectionOf(navKey({ page: "project", id: "treedar", tab: "work" }))).toBe("projects");
    expect(sectionOf(navKey({ page: "review", project: "treedar", id: "x" }))).toBe("projects");
    expect(navKey({ page: "notes" })).toBe("");
    expect(navKey({ page: "planning" })).toBe("");
  });
});

describe("project drill-down", () => {
  it("places labels under Labeling and a review under the overview, only for that project", () => {
    expect(projectPlace({ page: "labels", project: "treedar", set: "s" }, "treedar")).toBe("labeling");
    expect(projectPlace({ page: "review", project: "treedar", id: "x" }, "treedar")).toBe("overview");
    expect(projectPlace({ page: "project", id: "treedar", tab: "plans" }, "treedar")).toBe("plans");
    expect(projectPlace({ page: "project", id: "treedar", tab: "plans" }, "other")).toBeNull();
    expect(projectPlace({ page: "work" }, "treedar")).toBeNull();
  });
});
