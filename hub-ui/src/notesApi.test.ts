import { describe, expect, it, vi } from "vitest";
import { isNotePath, listNotes, MAX_NOTE_REQUEST_BYTES, NoteConflictError, noteRequestBytes, parseNoteDocument, parseNoteList, readNote, saveNote } from "./notesApi";

const human = "human:cf:" + "e".repeat(64);
function meta(path = "runbooks/a.md") { return {
  project: "engineering-hub", path, title: "Runbook", version: 2, sha256: "c".repeat(64),
  created_by: "codex", updated_by: human, created_at: "2026-10-20T12:00:00+00:00", updated_at: "2026-10-21T01:00:00+00:00",
}; }
const doc = (extra: Record<string, unknown> = {}) => ({ ...meta(), content: "<script>alert(1)</script>\nline two", ...extra });
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { "Content-Type": "application/json" },
});
const signal = () => new AbortController().signal;

describe("shared notes client", () => {
  it("keeps a 73-character Cloudflare human author and content as inert text", () => {
    const result = parseNoteDocument(doc(), "engineering-hub", "runbooks/a.md");
    expect(result.updated_by).toBe(human);
    expect(result.content).toBe("<script>alert(1)</script>\nline two");
  });
  it.each([null, { items: [] }, { items: {}, truncated: false }, { items: [meta(), meta()], truncated: false },
    { items: [{ ...meta(), project: "treedar" }], truncated: false }, { items: [{ ...meta(), version: 0 }], truncated: false },
    { items: [{ ...meta(), path: "../escape" }], truncated: false }, { items: [{ ...meta(), sha256: "x" }], truncated: false },
    { items: [{ ...meta(), title: " " }], truncated: false }, { items: [{ ...meta(), updated_at: "soon" }], truncated: false },
    { items: Array.from({ length: 101 }, (_, i) => meta(`n/${i}`)), truncated: true }])("rejects malformed or cross-project lists %#", value => {
    expect(() => parseNoteList(value, "engineering-hub")).toThrow("invalid_notes_response");
  });
  it("rejects a document for a different path than requested", () => {
    expect(() => parseNoteDocument(doc(), "engineering-hub", "runbooks/b.md")).toThrow("invalid_notes_response");
    expect(() => parseNoteDocument(doc({ content: "a\u0000b" }), "engineering-hub", "runbooks/a.md")).toThrow("invalid_notes_response");
  });
  it.each([["a/b.md", true], ["a", true], ["/a", false], ["a//b", false], ["a/../b", false], ["./a", false],
    ["a\\b", false], ["a\nb", false], ["", false], ["x".repeat(513), false]])("mirrors server path rules for %j", (path, ok) => {
    expect(isNotePath(path)).toBe(ok);
  });
  it("lists with one same-origin GET, query taking precedence over prefix", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ items: [meta()], truncated: true }));
    const controller = new AbortController();
    const result = await listNotes({ project: "engineering-hub", query: " pilot ", path_prefix: "runbooks" }, controller.signal, fetchImpl);
    expect(result.truncated).toBe(true);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl).toHaveBeenCalledWith("/api/hub/v1/notes?project=engineering-hub&limit=50&query=pilot", {
      method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store", headers: { Accept: "application/json" },
      signal: controller.signal,
    });
  });
  it("encodes document paths as query parameters", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ ...doc(), path: "a b/c&d.md" }));
    await readNote("engineering-hub", "a b/c&d.md", signal(), fetchImpl);
    expect(fetchImpl.mock.calls[0][0]).toBe("/api/hub/v1/notes/document?project=engineering-hub&path=a+b%2Fc%26d.md");
  });
  it("saves with CSRF, no author field, and requires the next version", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response(doc({ version: 3 })));
    const saved = await saveNote({ project: "engineering-hub", path: "runbooks/a.md", title: "Runbook", content: "x",
      expected_version: 2 }, "csrf-token", fetchImpl);
    expect(saved.version).toBe(3);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("/api/hub/v1/notes");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>)["X-Hub-CSRF"]).toBe("csrf-token");
    expect(JSON.parse(init?.body as string)).toEqual({ project: "engineering-hub", path: "runbooks/a.md", title: "Runbook",
      content: "x", expected_version: 2 });
  });
  it("treats a save answer that skips versions as invalid", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response(doc({ version: 5 })));
    await expect(saveNote({ project: "engineering-hub", path: "runbooks/a.md", title: "R", content: "", expected_version: 2 },
      "csrf", fetchImpl)).rejects.toMatchObject({ code: "invalid_notes_response" });
  });
  it("refuses to send without CSRF or with an oversized or invalid draft", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    const base = { project: "engineering-hub", path: "a.md", title: "T", content: "", expected_version: 0 };
    await expect(saveNote(base, "", fetchImpl)).rejects.toMatchObject({ code: "csrf_required" });
    await expect(saveNote({ ...base, content: "é".repeat(32 * 1024 + 1) }, "c", fetchImpl)).rejects.toMatchObject({ code: "note_too_large" });
    await expect(saveNote({ ...base, path: "../x" }, "c", fetchImpl)).rejects.toMatchObject({ code: "invalid_input" });
    await expect(saveNote({ ...base, title: "  " }, "c", fetchImpl)).rejects.toMatchObject({ code: "invalid_input" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
  it("surfaces the CAS conflict with the server's current version", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ detail: { code: "notes_version_conflict", current_version: 4 } }, 409));
    const error = await saveNote({ project: "engineering-hub", path: "a.md", title: "T", content: "", expected_version: 2 }, "c", fetchImpl)
      .catch(value => value);
    expect(error).toBeInstanceOf(NoteConflictError);
    expect(error.currentVersion).toBe(4);
  });
  it.each([[503, { detail: { code: "notes_unavailable" } }, "notes_unavailable"], [503, { detail: { code: "notes_integrity_failure" } }, "notes_integrity_failure"],
    [404, { detail: "Not Found" }, "note_not_found"], [422, { detail: { code: "invalid_note" } }, "invalid_note"],
    [401, { secret: "private" }, "notes_request_failed"], [500, { detail: { code: "<b>raw</b>" } }, "notes_request_failed"],
    [409, { detail: { code: "notes_version_conflict" } }, "notes_version_conflict"]])("maps HTTP %s without reflecting the body", async (status, body, code) => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response(body, status));
    const error = await listNotes({ project: "engineering-hub" }, signal(), fetchImpl).catch(value => value);
    expect(error).toMatchObject({ status, code });
    expect(error).not.toBeInstanceOf(NoteConflictError);
  });
  it("does not mistake a login page for an empty notes list", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(new Response("login", { headers: { "Content-Type": "text/html" } }));
    await expect(listNotes({ project: "engineering-hub" }, signal(), fetchImpl)).rejects.toMatchObject({ code: "invalid_notes_response" });
  });
  it("rejects invalid projects before any request", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    await expect(listNotes({ project: "../x" }, signal(), fetchImpl)).rejects.toMatchObject({ code: "invalid_input" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
  it("budgets the whole escaped JSON request, not just the content", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    const draft = { project: "engineering-hub", path: "a.md", title: "T", content: "\n".repeat(40000), expected_version: 0 };
    expect(noteRequestBytes(draft)).toBeGreaterThan(MAX_NOTE_REQUEST_BYTES);
    await expect(saveNote(draft, "c", fetchImpl)).rejects.toMatchObject({ status: 413, code: "note_too_large" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
  it("maps a gateway 413 to note_too_large", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ detail: { code: "invalid_note" } }, 413));
    await expect(saveNote({ project: "engineering-hub", path: "a.md", title: "T", content: "x", expected_version: 0 }, "c", fetchImpl))
      .rejects.toMatchObject({ status: 413, code: "note_too_large" });
  });
});
