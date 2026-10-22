import { useEffect, useRef, useState } from "react";
import { HubApiError, type HubMode } from "./api";
import {
  isNotePath, isNoteProject, listNotes, MAX_NOTE_REQUEST_BYTES, MAX_NOTE_TITLE_CHARS, NOTE_LIST_LIMIT,
  NoteConflictError, noteRequestBytes, readNote, saveNote, type NoteDocument, type NoteList,
} from "./notesApi";

export interface NotesPanelProps {
  mode: HubMode;
  /** Sidebar selection; "all" leaves the panel on its own project. */
  selectedProject: string;
  knownProjects: { id: string; name: string }[];
  csrf: string | null;
  writable: boolean;
  /** The signed-in actor, so the viewer's own edits read "You". */
  viewerActor?: string | null;
  onUnauthorized: () => void;
}
/** expected > 0 on a new note is an accepted overwrite of exactly accepted.project/path. */
interface Draft { base: NoteDocument | null; path: string; title: string; content: string; expected: number;
  accepted: { project: string; path: string } | null }
/** Bound to the exact project and path that conflicted; never applied to another note. */
interface Conflict { project: string; path: string; current: number; latest: NoteDocument | null; latestState: "loading" | "loaded" | "missing" | "failed" }
type Search = { kind: "query" | "prefix"; text: string };

export const DEFAULT_NOTES_PROJECT = "engineering-hub";
const dirtyPrompt = "You have unsaved note edits. Discard them?";

function when(value: string): string {
  const date = new Date(value), minutes = Math.round((Date.now() - date.getTime()) / 60000);
  const age = minutes < 1 ? "just now" : minutes < 60 ? `${minutes}m ago`
    : minutes < 2880 ? `${Math.round(minutes / 60)}h ago` : `${Math.round(minutes / 1440)}d ago`;
  return `${date.toLocaleString()} (${age})`;
}
/** Cloudflare humans are "human:cf:<64 hex>"; show a short, stable label and keep the full id in a tooltip. */
export function actorLabel(actor: string, viewer?: string | null): string {
  if (viewer && actor === viewer) return "You";
  const human = /^human:cf:([a-f0-9]{8})[a-f0-9]{56}$/.exec(actor);
  return human ? `Person ${human[1]}` : actor;
}
function Actor({ id, viewer }: { id: string; viewer?: string | null }) {
  return <span title={id}>{actorLabel(id, viewer)}</span>;
}
function readError(error: unknown): string {
  if (!(error instanceof HubApiError)) return "Notes could not be read. Use Refresh to retry.";
  if (error.code === "note_not_found") return "That note no longer exists.";
  if (error.code === "notes_integrity_failure") return "The notes store failed an integrity check. Nothing was changed.";
  if (error.code === "notes_unavailable" || error.status === 503) return "The notes store is not available on this server.";
  return "Notes could not be read. Use Refresh to retry.";
}

export function NotesPanel({ mode, selectedProject, knownProjects, csrf, writable, viewerActor, onUnauthorized }: NotesPanelProps) {
  const detailRef = useRef<HTMLDivElement>(null);
  // A project page opens on that project's notes; elsewhere the default.
  const initialProject = selectedProject !== "all" && isNoteProject(selectedProject) ? selectedProject : DEFAULT_NOTES_PROJECT;
  const [project, setProject] = useState(initialProject);
  const [projectDraft, setProjectDraft] = useState(initialProject);
  const [searchDraft, setSearchDraft] = useState<Search>({ kind: "query", text: "" });
  const [search, setSearch] = useState<Search>({ kind: "query", text: "" });
  const [list, setList] = useState<NoteList | null>(null);
  const [listState, setListState] = useState("loading");
  const [listRevision, setListRevision] = useState(0);
  // Bumped to re-read the selected document (Refresh, or re-opening the open note).
  const [docRevision, setDocRevision] = useState(0);
  const [listError, setListError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [doc, setDoc] = useState<NoteDocument | null>(null);
  const [docState, setDocState] = useState("idle");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [saving, setSaving] = useState(false);
  // stale_read marks the warning from a failed re-read, so only a later
  // successful read clears it; other notices are left alone.
  const [notice, setNotice] = useState<{ kind: "status" | "alert"; text: string; stale_read?: true } | null>(null);
  const listGeneration = useRef(0), docGeneration = useRef(0), saveGeneration = useRef(0);
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;
  const dirty = draft !== null && (draft.base === null
    ? draft.path !== "" || draft.title !== "" || draft.content !== ""
    : draft.title !== draft.base.title || draft.content !== draft.base.content);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  function deny(error: unknown): boolean {
    if (error instanceof HubApiError && [401, 403].includes(error.status)) {
      setList(null); setDoc(null); setDraft(null); setConflict(null);
      setListState("unauthorized"); unauthorized.current();
      return true;
    }
    return false;
  }
  function confirmDiscard(): boolean {
    return !dirtyRef.current || window.confirm(dirtyPrompt);
  }
  function closeEditor() {
    saveGeneration.current++; setDraft(null); setConflict(null); setSaving(false);
  }
  function switchProject(next: string) {
    if (next === project || !confirmDiscard()) { setProjectDraft(project); return; }
    closeEditor(); setSelected(null); setDoc(null); setDocState("idle"); setNotice(null);
    setSearch({ kind: "query", text: "" }); setSearchDraft({ kind: "query", text: "" });
    setProject(next); setProjectDraft(next);
  }

  // Follow the sidebar filter, but never silently drop unsaved edits.
  const followed = useRef(selectedProject);
  useEffect(() => {
    if (followed.current === selectedProject) return;
    followed.current = selectedProject;
    if (selectedProject !== "all" && isNoteProject(selectedProject)) switchProject(selectedProject);
  });

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    const current = ++listGeneration.current;
    const controller = new AbortController();
    setList(null);
    if (mode === "demo") { setListState("demo"); return () => { listGeneration.current++; controller.abort(); }; }
    setListState("loading");
    void listNotes({ project, ...(search.text.trim() ? search.kind === "query"
      ? { query: search.text.trim() } : { path_prefix: search.text.trim() } : {}) }, controller.signal).then(value => {
      if (listGeneration.current !== current || controller.signal.aborted) return;
      setList(value); setListState("loaded");
    }).catch(error => {
      if (listGeneration.current !== current || controller.signal.aborted) return;
      if (deny(error)) return;
      setListError(readError(error));
      setListState(error instanceof HubApiError && error.status === 404 ? "unsupported"
        : error instanceof HubApiError && error.code === "invalid_note" ? "invalid_search" : "failed");
    });
    return () => { listGeneration.current++; controller.abort(); };
  }, [mode, project, search, listRevision]);

  const shownKey = useRef<string | null>(null);
  useEffect(() => {
    const current = ++docGeneration.current;
    const controller = new AbortController();
    const key = selected === null ? null : `${project}
${selected}`;
    // A re-read of the same note keeps it visible until the fresh copy arrives;
    // switching notes or projects clears it so another note's body never shows.
    if (selected === null || mode === "demo") {
      setDoc(null); shownKey.current = null; setDocState("idle");
      return () => { docGeneration.current++; controller.abort(); };
    }
    const rereading = key === shownKey.current;
    if (!rereading) { setDoc(null); shownKey.current = null; }
    if (!rereading) setDocState("loading");
    void readNote(project, selected, controller.signal).then(value => {
      if (docGeneration.current !== current || controller.signal.aborted) return;
      shownKey.current = key; setDoc(value); setDocState("loaded");
      setNotice(previous => previous?.stale_read ? null : previous);
    }).catch(error => {
      if (docGeneration.current !== current || controller.signal.aborted) return;
      if (deny(error)) return;
      if (rereading) {
        setNotice({ kind: "alert", text: `${readError(error)} The note shown may be out of date.`, stale_read: true });
        return;
      }
      setDocState("failed"); setNotice({ kind: "alert", text: readError(error) });
    });
    return () => { docGeneration.current++; controller.abort(); };
  }, [mode, project, selected, docRevision]);

  function open(path: string) {
    if (!confirmDiscard()) return;
    // Opening the note that is already open re-reads it.
    closeEditor(); setNotice(null); setSelected(path); setDocRevision(value => value + 1);
    if (window.matchMedia("(max-width: 1150px)").matches)
      requestAnimationFrame(() => detailRef.current?.scrollIntoView({ block: "start", behavior: "smooth" }));
  }
  function startNew() {
    if (!confirmDiscard()) return;
    closeEditor(); setNotice(null);
    setDraft({ base: null, path: "", title: "", content: "", expected: 0, accepted: null });
  }
  function startEdit() {
    if (!doc) return;
    setNotice(null); setConflict(null);
    setDraft({ base: doc, path: doc.path, title: doc.title, content: doc.content, expected: doc.version,
      accepted: { project: doc.project, path: doc.path } });
  }
  function cancelEdit() {
    if (!confirmDiscard()) return;
    closeEditor();
  }
  async function loadLatest(current: number, conflictProject: string, path: string, generation: number) {
    try {
      const latest = await readNote(conflictProject, path, new AbortController().signal);
      if (saveGeneration.current === generation)
        setConflict({ project: conflictProject, path, current, latest, latestState: "loaded" });
    } catch (error) {
      if (saveGeneration.current !== generation || deny(error)) return;
      setConflict({ project: conflictProject, path, current, latest: null,
        latestState: error instanceof HubApiError && error.code === "note_not_found" ? "missing" : "failed" });
    }
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!draft || saving || !csrf) return;
    const generation = ++saveGeneration.current, savedProject = project;
    // Defense in depth: a version is only ever sent for the note it was accepted for.
    if (draft.expected > 0 && (draft.accepted?.project !== savedProject || draft.accepted.path !== draft.path)) {
      setDraft({ ...draft, expected: 0, accepted: null });
      setNotice({ kind: "alert", text: "The path changed after a version was accepted. Nothing was saved. Review and save again." });
      return;
    }
    setSaving(true); setNotice(null);
    try {
      const saved = await saveNote({ project: savedProject, path: draft.path, title: draft.title,
        content: draft.content, expected_version: draft.expected }, csrf);
      if (saveGeneration.current !== generation) return;
      setDraft(null); setConflict(null); setSaving(false);
      docGeneration.current++; shownKey.current = `${savedProject}
${saved.path}`;
      setSelected(saved.path); setDoc(saved); setDocState("loaded");
      setListRevision(value => value + 1);
      setNotice({ kind: "status", text: `Saved "${saved.title}" as version ${saved.version}.` });
    } catch (error) {
      if (saveGeneration.current !== generation) return;
      setSaving(false);
      if (deny(error)) return;
      if (error instanceof NoteConflictError) {
        setConflict({ project: savedProject, path: draft.path, current: error.currentVersion, latest: null, latestState: "loading" });
        void loadLatest(error.currentVersion, savedProject, draft.path, generation);
        return;
      }
      setNotice({ kind: "alert", text: error instanceof HubApiError && error.code === "note_too_large"
        ? "This note is too large for the Hub to accept. Shorten it and try again. Your edits are still here."
        : error instanceof HubApiError && (error.code === "invalid_note" || error.status === 422)
        ? "The server rejected this note. Check the path, title and size, then try again."
        : `${error instanceof HubApiError && error.status === 503 ? readError(error) : "The note could not be saved."} Your edits are still here.` });
    }
  }
  function keepMine() {
    if (!draft || !conflict || conflict.project !== project || conflict.path !== draft.path) return;
    if (conflict.latestState === "loaded" && conflict.latest?.path !== draft.path) return;
    // Explicit, visible choice to replace the newer version the user has just seen, for this path only.
    setDraft({ ...draft, expected: conflict.current, accepted: { project: conflict.project, path: conflict.path } });
    setConflict(null);
    setNotice({ kind: "status", text: `Saving now replaces version ${conflict.current}.` });
  }
  function useLatest() {
    if (!conflict?.latest) return;
    const latest = conflict.latest;
    closeEditor(); docGeneration.current++;
    shownKey.current = `${project}
${latest.path}`;
    setSelected(latest.path); setDoc(latest); setDocState("loaded"); setListRevision(value => value + 1);
  }

  const pathValid = draft ? isNotePath(draft.path) : true;
  const bytes = draft ? noteRequestBytes({ project, path: draft.path, title: draft.title, content: draft.content,
    expected_version: draft.expected }) : 0;
  const draftValid = !!draft && pathValid && draft.title.trim() !== "" && bytes <= MAX_NOTE_REQUEST_BYTES;
  const pathLocked = !!draft && (draft.base !== null || conflict !== null || draft.expected > 0);
  const canWrite = writable && !!csrf && mode === "live";
  const listMessages: Record<string, string> = {
    loading: "Reading notes…", demo: "Demo: notes are not loaded in demo mode.",
    unsupported: "This server does not have shared notes yet. Update the Hub to enable them.",
    unauthorized: "Notes cleared. Sign-in or access must be verified again.",
    invalid_search: "That search could not be used. Try simpler words or a different path prefix.",
    failed: listError,
  };

  return <section className="panel notes" id="notes" aria-label="Shared notes">
    <div className="panel-title"><h2>Shared notes</h2>
      <span className="panel-action">
        <button className="secondary" type="button" disabled={mode === "demo" || listState === "loading"}
          onClick={() => { setListRevision(value => value + 1); if (selected !== null) setDocRevision(value => value + 1); }}>Refresh</button>
        {canWrite && <button className="primary" type="button" onClick={startNew}>New note</button>}
      </span></div>
    <p className="panel-description">Durable project notes shared with the agents. Notes are context only. They do not approve
      tasks, answer decisions or authorize runs.</p>
    <div className="notes-controls">
      <form className="notes-project" onSubmit={event => { event.preventDefault();
        if (isNoteProject(projectDraft)) switchProject(projectDraft); }}>
        <label htmlFor="notes-project">Project</label>
        <input id="notes-project" list="notes-projects" value={projectDraft} spellCheck={false} maxLength={64}
          pattern="[A-Za-z0-9][A-Za-z0-9._\-]{0,63}" required onChange={event => setProjectDraft(event.target.value)} />
        <datalist id="notes-projects">{knownProjects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</datalist>
        <button className="secondary small" type="submit" disabled={projectDraft === project}>Open</button>
      </form>
      <form className="notes-search" role="search" onSubmit={event => { event.preventDefault(); setSearch({ ...searchDraft }); }}>
        <label htmlFor="notes-search-kind" className="sr-only">Search by</label>
        <select id="notes-search-kind" value={searchDraft.kind}
          onChange={event => setSearchDraft({ ...searchDraft, kind: event.target.value as Search["kind"] })}>
          <option value="query">Title and text</option><option value="prefix">Path starts with</option>
        </select>
        <label htmlFor="notes-search" className="sr-only">Search notes</label>
        <input id="notes-search" type="search" value={searchDraft.text} maxLength={200} placeholder="Search notes"
          onChange={event => setSearchDraft({ ...searchDraft, text: event.target.value })} />
        <button className="secondary small" type="submit">Search</button>
        {search.text && <button className="secondary small" type="button" onClick={() => {
          setSearch({ kind: "query", text: "" }); setSearchDraft({ kind: "query", text: "" }); }}>Clear</button>}
      </form>
    </div>
    {notice && <p role={notice.kind} className={notice.kind === "alert" ? "notice error" : "notice"}>{notice.text}</p>}
    <div className="notes-layout">
      <div className="notes-list" aria-label={`Notes in ${project}`}>
        {listState !== "loaded" && <p role={listState === "failed" ? "alert" : "status"}>{listMessages[listState]}</p>}
        {list && <>
          <p className="panel-footnote">{list.items.length === 0
            ? search.text ? "No notes match this search." : `No notes in ${project} yet.`
            : `${list.items.length} note${list.items.length === 1 ? "" : "s"}${search.text ? " matching" : ""} in ${project}.`}
            {list.truncated && ` Showing the first ${NOTE_LIST_LIMIT}. Narrow the search or path prefix to see the rest.`}</p>
          <ul>{list.items.map(item => <li key={item.path}>
            <button type="button" className={item.path === selected ? "note-link current" : "note-link"}
              aria-current={item.path === selected ? "true" : undefined} onClick={() => open(item.path)}>
              <strong>{item.title}</strong>
              <span>{item.path}</span>
              <span>v{item.version} · <Actor id={item.updated_by} viewer={viewerActor} /> · {when(item.updated_at)}</span>
            </button></li>)}</ul>
        </>}
      </div>
      <div className="notes-detail" ref={detailRef}>
        {draft ? <form className="note-editor" onSubmit={event => void submit(event)} aria-label={draft.base ? "Edit note" : "New note"}>
          <h3>{draft.base ? `Editing ${draft.base.path}` : `New note in ${project}`}</h3>
          {draft.base && <p className="panel-footnote">Based on version {draft.base.version}. If someone saves first, you will
            be asked before anything is replaced.</p>}
          <fieldset disabled={saving}>
            <label className="field">Path
              <input value={draft.path} readOnly={pathLocked} maxLength={512} spellCheck={false} required
                placeholder="runbooks/staging-checklist.md" aria-invalid={!pathValid}
                onChange={event => { if (!pathLocked) setDraft({ ...draft, path: event.target.value, expected: 0, accepted: null }); }} />
            </label>
            {draft.path !== "" && !pathValid && <p className="notice error">Use a relative path like
              folder/name.md with no leading slash, backslashes, empty parts, "." or "..".</p>}
            <label className="field">Title
              <input value={draft.title} maxLength={MAX_NOTE_TITLE_CHARS} required
                onChange={event => setDraft({ ...draft, title: event.target.value })} />
            </label>
            <label className="field">Content
              <textarea value={draft.content} rows={14} spellCheck
                onChange={event => setDraft({ ...draft, content: event.target.value })} />
            </label>
            {draft.base === null && draft.expected > 0 && <p className="panel-footnote">Saving replaces version {draft.expected} of
              this path. The path is locked; cancel to choose another.</p>}
            <p className={bytes > MAX_NOTE_REQUEST_BYTES ? "notice error" : "panel-footnote"}>
              {Math.ceil(bytes / 1024)} of {MAX_NOTE_REQUEST_BYTES / 1024} KB, including path and title.
              {bytes > MAX_NOTE_REQUEST_BYTES ? " Too large to save." : ""}{dirty ? " Unsaved changes." : ""}</p>
          </fieldset>
          {conflict && <div role="alert" className="notice error note-conflict">
            <p>{conflict.current === 0 ? "This note was deleted after you opened it."
              : draft.base ? `Someone saved version ${conflict.current} after you opened version ${draft.base.version}.`
              : `A note already exists at this path (version ${conflict.current}).`} Nothing was saved. Your text is still here.</p>
            {conflict.latestState === "loading" && <p>Loading the latest version…</p>}
            {conflict.latestState === "failed" && <p>The latest version could not be loaded.</p>}
            {conflict.latest && <details open><summary>Latest saved version (v{conflict.latest.version} by <Actor id={conflict.latest.updated_by} viewer={viewerActor} />, {when(conflict.latest.updated_at)})</summary>
              <h4>{conflict.latest.title}</h4><pre className="note-content">{conflict.latest.content}</pre></details>}
            {draft.base === null && <p>The path is locked while this conflict is open. Cancel to choose a different path.</p>}
            <div className="note-actions">
              {conflict.latest && <button type="button" className="secondary" onClick={keepMine}>Replace version {conflict.current} with mine</button>}
              {conflict.latest && <button type="button" className="secondary" onClick={useLatest}>Discard mine, keep latest</button>}
              {conflict.latestState === "missing" && <button type="button" className="secondary" onClick={keepMine}>Save mine as a new note</button>}
            </div>
          </div>}
          <div className="note-actions">
            <button className="primary" type="submit" disabled={!draftValid || saving || conflict !== null || !canWrite}>
              {saving ? "Saving…" : "Save note"}</button>
            <button className="secondary" type="button" disabled={saving} onClick={cancelEdit}>Cancel</button>
          </div>
        </form>
        : selected === null ? <p className="panel-footnote">Select a note to read it{canWrite ? ", or create a new one" : ""}.</p>
        : docState === "loading" ? <p role="status">Reading note…</p>
        : doc ? <article className="note-view" aria-label={doc.title}>
          <div className="item-overline">{doc.project}<span>v{doc.version}</span></div>
          <h3>{doc.title}</h3>
          <p className="panel-footnote">{doc.path} · Updated by <Actor id={doc.updated_by} viewer={viewerActor} />, {when(doc.updated_at)} · Created by <Actor id={doc.created_by} viewer={viewerActor} />, {when(doc.created_at)}</p>
          <pre className="note-content">{doc.content}</pre>
          {canWrite && <div className="note-actions"><button className="secondary" type="button" onClick={startEdit}>Edit</button></div>}
        </article>
        : <p className="panel-footnote">This note could not be shown.</p>}
      </div>
    </div>
  </section>;
}
