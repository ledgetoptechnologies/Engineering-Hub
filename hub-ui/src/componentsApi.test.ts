import { describe, expect, it, vi } from "vitest";
import { HubApiError } from "./api";
import {
  changedFields, componentSummary, createComponent, listComponents, parseComponent, parseComponentPage, readComponent, retireComponent,
  sortComponents, updateComponent, validUrl, type ProjectComponent,
} from "./componentsApi";
import { DemoComponentStore } from "./componentsDemo";
import wire from "./fixtures/components-wire-v1.json";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const item = (overrides: Partial<ProjectComponent> = {}): ProjectComponent => ({ ...(wire.single.body as ProjectComponent), ...overrides });
const id = wire.single.body.id;

describe("component wire contract", () => {
  it("parses the backend's real fixture rows", () => {
    const page = parseComponentPage(wire.list.body, "treedar");
    expect(page.items).toHaveLength(1);
    expect(page.items[0]).toMatchObject({ name: "Pointcloud review", kind: "web_app", status: "planned", legacy_project_id: null, package_url: null });
    expect(parseComponent(wire.single.body, { id }).revision).toBe(1);
  });
  it("maps the fixture's error codes to honest states", async () => {
    const call = (entry: { status: number; body: unknown }) => readComponent(id, vi.fn<typeof fetch>().mockResolvedValue(json(entry.body, entry.status)));
    await expect(call(wire.conflict)).rejects.toMatchObject({ status: 409, code: "component_conflict" });
    await expect(call(wire.forbidden)).rejects.toMatchObject({ status: 403, code: "human_session_required" });
    await expect(call(wire.validation)).rejects.toMatchObject({ status: 422, code: "invalid_component" });
  });
  it("rejects rows from another project, bad ids and unsafe links", () => {
    expect(() => parseComponentPage(wire.list.body, "project-alpha")).toThrow(HubApiError);
    expect(() => parseComponent({ ...wire.single.body, id: "not-a-uuid" })).toThrow(HubApiError);
    expect(() => parseComponent({ ...wire.single.body, repository_url: "javascript:alert(1)" })).toThrow(HubApiError);
    expect(() => parseComponent({ ...wire.single.body, package_url: "https://user:pw@example.com/p" })).toThrow(HubApiError);
    expect(() => parseComponent({ ...wire.single.body, kind: "plugin" })).toThrow(HubApiError);
    expect(() => parseComponent({ ...wire.single.body, revision: 0 })).toThrow(HubApiError);
    expect(() => parseComponentPage({ items: [wire.single.body, wire.single.body], next_after: null }, "treedar")).toThrow(HubApiError);
    expect(() => parseComponentPage({ items: [], next_after: "../x" }, "treedar")).toThrow(HubApiError);
  });
  it("accepts only https links without credentials or spaces", () => {
    expect(validUrl(null)).toBe(true);
    expect(validUrl("https://github.com/example/repo")).toBe(true);
    expect(validUrl("http://github.com/example/repo")).toBe(false);
    expect(validUrl("https://exa mple.com")).toBe(false);
    expect(validUrl("https://example.com/\\x")).toBe(false);
  });
});

describe("component requests", () => {
  it("lists with retired rows and pages by UUID", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json(wire.list.body));
    await listComponents("treedar", id, undefined, fetchImpl);
    expect(fetchImpl.mock.calls[0][0]).toBe(`/api/hub/v1/projects/treedar/components?include_retired=true&limit=100&after=${id}`);
    await expect(listComponents("treedar", "not-a-uuid", undefined, fetchImpl)).rejects.toMatchObject({ code: "invalid_component" });
    await expect(listComponents("../etc", null, undefined, fetchImpl)).rejects.toMatchObject({ code: "invalid_project" });
  });
  it("creates with CSRF, a request key and only known fields", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json(wire.single.body, 201));
    await createComponent("treedar", { slug: "pointcloud-review", client_request_id: "req-1", name: "Pointcloud review", description: "",
      kind: "web_app", status: "planned", repository_url: null, package_url: null, legacy_project_id: null }, "csrf", fetchImpl);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("/api/hub/v1/projects/treedar/components");
    expect(init!.method).toBe("POST");
    expect((init!.headers as Record<string, string>)["X-Hub-CSRF"]).toBe("csrf");
    expect(JSON.parse(init!.body as string)).toEqual({ slug: "pointcloud-review", client_request_id: "req-1", name: "Pointcloud review",
      description: "", kind: "web_app", status: "planned", repository_url: null, package_url: null, legacy_project_id: null });
  });
  it("edits only what changed, with the expected revision", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json({ ...wire.single.body, revision: 2, status: "active" }));
    await updateComponent(id, { expected_revision: 1, status: "active" }, "csrf", fetchImpl);
    expect(fetchImpl.mock.calls[0][0]).toBe(`/api/hub/v1/components/${id}`);
    expect(JSON.parse(fetchImpl.mock.calls[0][1]!.body as string)).toEqual({ expected_revision: 1, status: "active" });
    await expect(updateComponent(id, { expected_revision: 0 }, "csrf", fetchImpl)).rejects.toMatchObject({ code: "invalid_component_revision" });
  });
  it("retires by revision and refuses without CSRF", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json({ ...wire.single.body, revision: 2, status: "retired" }));
    expect((await retireComponent(id, 1, "csrf", fetchImpl)).status).toBe("retired");
    expect(fetchImpl.mock.calls[0][0]).toBe(`/api/hub/v1/components/${id}/retire`);
    expect(JSON.parse(fetchImpl.mock.calls[0][1]!.body as string)).toEqual({ expected_revision: 1 });
    await expect(retireComponent(id, 1, "", fetchImpl)).rejects.toMatchObject({ status: 403, code: "csrf_required" });
  });
  it("treats a gateway refusal as 'not available', never a sign-out", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json({ detail: { code: "gateway_route_forbidden" } }, 403));
    await expect(listComponents("treedar", null, undefined, fetchImpl)).rejects.toMatchObject({ status: 404, code: "components_unavailable" });
    const unnamed = vi.fn<typeof fetch>().mockResolvedValue(new Response("nope", { status: 404 }));
    await expect(listComponents("treedar", null, undefined, unnamed)).rejects.toMatchObject({ code: "components_unavailable" });
    const named = vi.fn<typeof fetch>().mockResolvedValue(json({ detail: { code: "not_found" } }, 404));
    await expect(readComponent(id, named)).rejects.toMatchObject({ code: "component_not_found" });
  });
});

describe("component wording and helpers", () => {
  it("sends only changed fields", () => {
    const base = item();
    expect(changedFields(base, { name: base.name, description: base.description, kind: base.kind, status: "active",
      repository_url: base.repository_url, package_url: base.package_url, legacy_project_id: base.legacy_project_id })).toEqual({ status: "active" });
  });
  it("orders active first and summarizes without retired items", () => {
    const rows = [item({ name: "Zed", status: "retired" }), item({ name: "beta", status: "planned" }), item({ name: "Alpha", status: "active" })];
    expect(sortComponents(rows).map((row) => row.name)).toEqual(["Alpha", "beta", "Zed"]);
    expect(componentSummary(rows)).toBe("2 components · 1 planned");
    expect(componentSummary([])).toBe("No components yet");
    expect(componentSummary([item({ status: "retired" })])).toBe("No current components");
  });
  it("demo store keeps slugs reserved and checks revisions", () => {
    const store = new DemoComponentStore();
    const [first] = store.list("project-alpha").items;
    expect(() => store.create("project-alpha", { slug: first.slug, client_request_id: "x", name: "Copy", description: "", kind: "other",
      status: "planned", repository_url: null, package_url: null, legacy_project_id: null })).toThrow(expect.objectContaining({ code: "component_exists" }));
    expect(() => store.retire(first.id, first.revision + 5)).toThrow(expect.objectContaining({ code: "component_conflict" }));
    expect(store.retire(first.id, first.revision).status).toBe("retired");
    // Demo rows have no live addresses.
    expect([...store.list("ledge-top-ops").items, ...store.list("treedar").items].every((row) => !row.repository_url && !row.package_url)).toBe(true);
  });
});
