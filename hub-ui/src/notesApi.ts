/** Shared project notes. Notes are context, never task, decision or run authority. */
import { HubApiError } from "./api";

export interface NoteMeta {
  project: string; path: string; title: string; version: number; sha256: string;
  created_by: string; updated_by: string; created_at: string; updated_at: string;
}
export interface NoteDocument extends NoteMeta { content: string }
export interface NoteList { items: NoteMeta[]; truncated: boolean }
export interface NoteQuery { project: string; query?: string; path_prefix?: string; limit?: number;
  /** Continue a path-ordered list after this path (not for text search). */
  after?: string }

export const NOTE_LIST_LIMIT = 50;
export const MAX_NOTE_CONTENT_BYTES = 64 * 1024;
export const MAX_NOTE_TITLE_CHARS = 200;
export const MAX_NOTE_PATH_CHARS = 512;
/** The private gateway caps the whole JSON request body, not just the content. */
export const MAX_NOTE_REQUEST_BYTES = 65536;
const projectPattern = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const actorPattern = /^[^\u0000-\u001f\u007f]{1,128}$/;
const knownCodes = new Set(["notes_version_conflict", "notes_unavailable", "notes_integrity_failure", "note_not_found",
  "invalid_note", "choose_note_search_or_prefix", "note_too_large", "human_session_required", "forbidden", "csrf_required"]);

export function isNoteProject(value: string): boolean { return projectPattern.exec(value)?.[0] === value; }
/** Mirrors KnowledgeRepository.validate_path so the form can explain errors before saving. */
export function isNotePath(value: string): boolean {
  if (!value || value.length > MAX_NOTE_PATH_CHARS || value.startsWith("/") || value.includes("\\")) return false;
  if ([...value].some(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)) return false;
  return value.split("/").every(part => part !== "" && part !== "." && part !== "..");
}
export function noteContentBytes(value: string): number { return new TextEncoder().encode(value).length; }

export class NoteConflictError extends HubApiError {
  constructor(public readonly currentVersion: number) { super(409, "notes_version_conflict"); this.name = "NoteConflictError"; }
}
function invalid(): never { throw new HubApiError(502, "invalid_notes_response"); }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  return value as Record<string, unknown>;
}
function string(value: unknown, max: number): string {
  return typeof value === "string" && value.length <= max ? value : invalid();
}
function matching(value: unknown, test: (text: string) => boolean): string {
  return typeof value === "string" && test(value) ? value : invalid();
}
function version(value: unknown): number {
  return Number.isSafeInteger(value) && (value as number) >= 1 ? value as number : invalid();
}
function timestamp(value: unknown): string {
  const text = string(value, 64);
  return Number.isNaN(Date.parse(text)) ? invalid() : text;
}
function parseMeta(value: unknown): NoteMeta {
  const row = object(value);
  const title = string(row.title, MAX_NOTE_TITLE_CHARS);
  if (!title.trim()) return invalid();
  return {
    project: matching(row.project, isNoteProject), path: matching(row.path, isNotePath), title,
    version: version(row.version), sha256: matching(row.sha256, text => /^[a-f0-9]{64}$/.test(text)),
    created_by: matching(row.created_by, text => actorPattern.test(text)),
    updated_by: matching(row.updated_by, text => actorPattern.test(text)),
    created_at: timestamp(row.created_at), updated_at: timestamp(row.updated_at),
  };
}
export function parseNoteList(value: unknown, project: string): NoteList {
  const row = object(value);
  if (!Array.isArray(row.items) || row.items.length > 100 || typeof row.truncated !== "boolean") return invalid();
  const items = row.items.map(parseMeta);
  // A list for another project would show the wrong project's notes after a race.
  if (items.some(item => item.project !== project)) return invalid();
  if (new Set(items.map(item => item.path)).size !== items.length) return invalid();
  return { items, truncated: row.truncated };
}
export function parseNoteDocument(value: unknown, project: string, path: string): NoteDocument {
  const row = object(value);
  const meta = parseMeta(row);
  if (meta.project !== project || meta.path !== path) return invalid();
  const content = string(row.content, MAX_NOTE_CONTENT_BYTES);
  if (content.includes("\u0000")) return invalid();
  return { ...meta, content };
}

async function failure(response: Response): Promise<never> {
  let code = response.status === 413 ? "note_too_large" : response.status === 503 ? "notes_unavailable" : response.status === 404 ? "note_not_found"
    : response.status === 409 ? "notes_version_conflict" : response.status === 422 ? "invalid_note" : "notes_request_failed";
  let current: number | null = null;
  if (response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
    try {
      const detail = object(object(await response.json()).detail);
      if (typeof detail.code === "string" && knownCodes.has(detail.code) && response.status !== 413) code = detail.code;
      if (Number.isSafeInteger(detail.current_version) && (detail.current_version as number) >= 0)
        current = detail.current_version as number;
    } catch { /* Never reflect proxy HTML or raw server exceptions. */ }
  }
  if (response.status === 409 && code === "notes_version_conflict" && current !== null) throw new NoteConflictError(current);
  throw new HubApiError(response.status, code);
}
async function json(response: Response): Promise<unknown> {
  if (!response.ok) return failure(response);
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return invalid();
  try { return await response.json(); } catch { return invalid(); }
}
const getInit = (signal: AbortSignal): RequestInit => ({
  method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store", headers: { Accept: "application/json" }, signal,
});

export async function listNotes(request: NoteQuery, signal: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch): Promise<NoteList> {
  if (!isNoteProject(request.project)) throw new HubApiError(422, "invalid_input");
  const params = new URLSearchParams({ project: request.project, limit: String(request.limit ?? NOTE_LIST_LIMIT) });
  // The backend accepts a text query or a path prefix, never both.
  const query = request.query?.trim();
  if (query) params.set("query", query);
  else {
    if (request.path_prefix) params.set("path_prefix", request.path_prefix);
    if (request.after) params.set("after", request.after);
  }
  const response = await fetchImpl.call(globalThis, `/api/hub/v1/notes?${params}`, getInit(signal));
  return parseNoteList(await json(response), request.project);
}
export async function readNote(project: string, path: string, signal: AbortSignal,
  fetchImpl: typeof fetch = globalThis.fetch): Promise<NoteDocument> {
  if (!isNoteProject(project) || !isNotePath(path)) throw new HubApiError(422, "invalid_input");
  const params = new URLSearchParams({ project, path });
  const response = await fetchImpl.call(globalThis, `/api/hub/v1/notes/document?${params}`, getInit(signal));
  return parseNoteDocument(await json(response), project, path);
}
export interface NoteDraft { project: string; path: string; title: string; content: string; expected_version: number }
function requestBody(draft: NoteDraft): string {
  return JSON.stringify({ project: draft.project, path: draft.path, title: draft.title,
    content: draft.content, expected_version: draft.expected_version });
}
/** Exact bytes the browser will send, including JSON escaping of newlines and quotes. */
export function noteRequestBytes(draft: NoteDraft): number { return noteContentBytes(requestBody(draft)); }
export async function saveNote(draft: NoteDraft, csrf: string, fetchImpl: typeof fetch = globalThis.fetch): Promise<NoteDocument> {
  if (!csrf) throw new HubApiError(403, "csrf_required");
  if (!isNoteProject(draft.project) || !isNotePath(draft.path) || !draft.title.trim()
      || draft.title.length > MAX_NOTE_TITLE_CHARS || !Number.isSafeInteger(draft.expected_version) || draft.expected_version < 0)
    throw new HubApiError(422, "invalid_input");
  const body = requestBody(draft);
  if (noteContentBytes(draft.content) > MAX_NOTE_CONTENT_BYTES || noteContentBytes(body) > MAX_NOTE_REQUEST_BYTES)
    throw new HubApiError(413, "note_too_large");
  const response = await fetchImpl.call(globalThis, "/api/hub/v1/notes", {
    method: "POST", credentials: "same-origin", redirect: "error", cache: "no-store",
    headers: { Accept: "application/json", "Content-Type": "application/json", "X-Hub-CSRF": csrf },
    body,
  });
  const saved = parseNoteDocument(await json(response), draft.project, draft.path);
  if (saved.version !== draft.expected_version + 1) return invalid();
  return saved;
}
