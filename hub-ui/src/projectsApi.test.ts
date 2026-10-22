import { describe, expect, it, vi } from "vitest";
import { archiveProject, createProject, isProjectId, parseProject, readProject, updateProject, type CreateProjectInput } from "./projectsApi";

const fixture = (changes: Record<string, unknown> = {}) => ({ id: "client-notes", name: "Client notes", description: "Project without code",
  repository_url: null, lead_agent: null, status: "on_track", parent_project: null, archived: false, revision: 1,
  environment: "notes_only", executor_enabled: false, health: "not_configured", reason: "notes_only", ...changes });
const input: CreateProjectInput = { project_id: "client-notes", name: "Client notes", description: "Project without code",
  repository_url: null, lead_agent: null, status: "on_track", parent_project: null, client_request_id: "stable-request-1" };
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

describe("human project API", () => {
  it("accepts stable project identifiers only", () => {
    for (const id of ["client-notes", "treedar", "a0"]) expect(isProjectId(id)).toBe(true);
    for (const id of ["Client", "client_notes", "../escape", "a\n", "a".repeat(65), ""]) expect(isProjectId(id)).toBe(false);
  });
  it("creates notes-only metadata with same-origin CSRF and no supplied identity", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response(fixture()));
    await createProject({ ...input, actor: "claude", executor_enabled: true } as CreateProjectInput, "human-csrf", fetchImpl);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("/api/hub/v1/projects");
    expect(init).toMatchObject({ method: "POST", credentials: "same-origin", redirect: "error", cache: "no-store",
      headers: { "X-Hub-CSRF": "human-csrf", "Content-Type": "application/json" } });
    expect(JSON.parse(init!.body as string)).toEqual(input);
    expect(init!.signal).toBeInstanceOf(AbortSignal);
  });
  it("reuses the exact request ID on a retry and does not mutate the draft", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockRejectedValueOnce(new TypeError("network")).mockResolvedValueOnce(response(fixture()));
    await expect(createProject(input, "csrf", fetchImpl)).rejects.toThrow("network_unavailable");
    await createProject(input, "csrf", fetchImpl);
    expect(fetchImpl.mock.calls[0][1]!.body).toBe(fetchImpl.mock.calls[1][1]!.body);
    expect(input.client_request_id).toBe("stable-request-1");
  });
  it("edits with an exact revision, keeps the immutable ID outside the body, and preserves parent metadata", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response(fixture({ revision: 2, parent_project: "operations" })));
    await updateProject("client-notes", { expected_revision: 1, parent_project: "operations" }, "csrf", fetchImpl);
    expect(fetchImpl.mock.calls[0][0]).toBe("/api/hub/v1/projects/client-notes");
    expect(fetchImpl.mock.calls[0][1]!.method).toBe("PATCH");
    expect(JSON.parse(fetchImpl.mock.calls[0][1]!.body as string)).toEqual({ expected_revision: 1, parent_project: "operations" });
  });
  it("archives by POST/CAS without deleting data and refuses the system project", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response(fixture({ revision: 2, archived: true })));
    await archiveProject("client-notes", 1, "csrf", fetchImpl);
    expect(fetchImpl.mock.calls[0][0]).toBe("/api/hub/v1/projects/client-notes/archive");
    expect(fetchImpl.mock.calls[0][1]!.method).toBe("POST");
    expect(JSON.parse(fetchImpl.mock.calls[0][1]!.body as string)).toEqual({ expected_revision: 1 });
    await expect(archiveProject("engineering-hub", 1, "csrf", fetchImpl)).rejects.toThrow("invalid_project_archive");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
  it("reads exact project metadata without adding bearer credentials", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response(fixture()));
    await readProject("client-notes", fetchImpl);
    expect(fetchImpl.mock.calls[0][1]).toMatchObject({ method: "GET", credentials: "same-origin", redirect: "error", headers: { Accept: "application/json" } });
    expect(fetchImpl.mock.calls[0][1]!.body).toBeUndefined();
  });
  it("rejects mutation without CSRF before fetch", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    await expect(createProject(input, "", fetchImpl)).rejects.toThrow("csrf_required");
    await expect(updateProject("client-notes", { expected_revision: 1, name: "x" }, "", fetchImpl)).rejects.toThrow("csrf_required");
    await expect(archiveProject("client-notes", 1, "", fetchImpl)).rejects.toThrow("csrf_required");
    expect(fetchImpl).not.toHaveBeenCalled();
  });
  it.each(["javascript:alert(1)", "http://example.test/repo", "https://user:secret@example.test/repo"])("refuses unsafe repository URL %s", async repository_url => {
    const fetchImpl = vi.fn<typeof fetch>();
    await expect(createProject({ ...input, repository_url }, "csrf", fetchImpl)).rejects.toThrow("invalid_project_metadata");
    expect(fetchImpl).not.toHaveBeenCalled();
  });
  it("returns safe conflict codes without reflecting server markup", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(new Response("<script>danger</script>", { status: 409 }));
    await expect(updateProject("client-notes", { expected_revision: 1, name: "draft" }, "csrf", fetchImpl)).rejects.toMatchObject({ status: 409, code: "revision_conflict" });
  });
  it.each([null, fixture({ id: "../wrong" }), fixture({ revision: 0 }), fixture({ archived: "no" }), fixture({ executor_enabled: "false" }), fixture({ repository_url: "javascript:1" })])("rejects malformed response %#", row => {
    expect(() => parseProject(row, "client-notes")).toThrow("invalid_project_response");
  });
});
