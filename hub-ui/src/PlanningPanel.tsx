import { useEffect, useRef, useState } from "react";
import { HubApiError, type HubMode } from "./api";
import { actorLabel } from "./NotesPanel";
import { isNoteProject } from "./notesApi";
import { registerRouteLeaveGuard } from "./router";
import { isPlanningId, isPlanningTopic, listPlanning, MAX_PLANNING_CONTENT_BYTES, MAX_PLANNING_REQUEST_BYTES, PLANNING_KINDS, PLANNING_STATUSES,
  PlanningConflictError, planningRequestBytes, readPlanning, savePlanning, type PlanningDraft, type PlanningItem,
  type PlanningKey, type PlanningList, type PlanningMeta, type PlanningScope, type PlanningSelection } from "./planningApi";

export interface PlanningPanelProps {
  mode: HubMode; selectedProject: string; knownProjects: { id: string; name: string }[]; topic: string;
  csrf: string | null; writable: boolean; viewerActor?: string | null; onUnauthorized: () => void;
}
interface Editor { value: PlanningDraft; base: PlanningItem | null }
interface Conflict { current: number; latest: PlanningItem | null; failed: boolean }
/** Says which kind of failure happened, by fixed code only (never the server's text). */
function listFailure(error: unknown): string {
  const code = error instanceof HubApiError ? error.code : "";
  const status = error instanceof HubApiError ? error.status : 0;
  if (code === "invalid_planning_response")
    return "Plans arrived in a shape this page doesn't accept, so nothing was shown (invalid_planning_response). Tell an agent; Refresh to retry.";
  if (status === 404 || code === "planning_item_not_found")
    return "This server doesn't have plans yet (404). The Hub may need an update.";
  if (status === 503 || code === "planning_unavailable" || code === "planning_integrity_failure")
    return `The plans store is unavailable right now (${code || status}). It may be restarting; Refresh to retry.`;
  return `Plans could not be loaded (${code || status || "network"}). Refresh to retry.`;
}
const discardText = "You have unsaved planning edits. Discard them?";
const pretty = (value: string) => value.replaceAll("_", " ");
const identity = (value: PlanningKey) => `${value.scope}/${value.project}/${value.topic}/${value.kind}/${value.item_id}`;

export function PlanningPanel({ mode, selectedProject, knownProjects, topic: initialTopic, csrf, writable, viewerActor, onUnauthorized }: PlanningPanelProps) {
  const firstProject = selectedProject !== "all" && isNoteProject(selectedProject) ? selectedProject : "engineering-hub";
  const [scope, setScope] = useState<PlanningScope>("topic");
  const [kindFilter, setKindFilter] = useState<"all" | PlanningKey["kind"]>("all");
  const [project, setProject] = useState(firstProject), [projectInput, setProjectInput] = useState(firstProject);
  const [topic, setTopic] = useState(initialTopic), [topicInput, setTopicInput] = useState(initialTopic);
  const [list, setList] = useState<PlanningList | null>(null), [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<PlanningMeta | null>(null), [document, setDocument] = useState<PlanningItem | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null), [conflict, setConflict] = useState<Conflict | null>(null);
  const [saving, setSaving] = useState(false), [reading, setReading] = useState(false), [revision, setRevision] = useState(0);
  const [notice, setNotice] = useState<{ error: boolean; text: string } | null>(null);
  const generation = useRef(0), readGeneration = useRef(0), saveGeneration = useRef(0);
  const unauthorized = useRef(onUnauthorized); unauthorized.current = onUnauthorized;
  const dirty = editor !== null && (editor.base === null ? true
    : editor.value.title !== editor.base.title || editor.value.content !== editor.base.content || editor.value.status !== editor.base.status);
  const dirtyRef = useRef(dirty); dirtyRef.current = dirty;
  const selection: PlanningSelection = scope === "hub_improvements" ? { scope, project: "engineering-hub", topic: null } : { scope, project, topic };
  const canWrite = mode === "live" && writable && !!csrf;
  const confirmDiscard = () => !dirtyRef.current || window.confirm(discardText);
  function clearEditor() { saveGeneration.current++; setEditor(null); setConflict(null); setSaving(false); }
  function clearSelection() { readGeneration.current++; clearEditor(); setSelected(null); setDocument(null); setReading(false); setNotice(null); }
  function deny(error: unknown): boolean {
    if (!(error instanceof HubApiError) || ![401, 403].includes(error.status)) return false;
    generation.current++; readGeneration.current++; clearSelection(); setList(null); setLoading(false); unauthorized.current(); return true;
  }
  useEffect(() => registerRouteLeaveGuard(() => !dirtyRef.current || window.confirm(discardText)), []);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn); return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  useEffect(() => () => { generation.current++; readGeneration.current++; saveGeneration.current++; }, []);
  useEffect(() => {
    const current = ++generation.current, controller = new AbortController();
    setList(null); setLoading(mode !== "demo");
    if (mode === "demo") return () => { generation.current++; controller.abort(); };
    if (!isNoteProject(selection.project) || (selection.scope === "topic" && !isPlanningTopic(selection.topic ?? ""))) {
      setLoading(false); setNotice({ error: true, text: "Choose a valid project and topic before loading plans." });
      return () => { generation.current++; controller.abort(); };
    }
    void listPlanning(selection, controller.signal, kindFilter === "all" ? undefined : kindFilter).then(value => {
      if (current !== generation.current || controller.signal.aborted) return;
      setList(value); setLoading(false);
    }).catch(error => {
      if (current !== generation.current || controller.signal.aborted || deny(error)) return;
      setLoading(false); setNotice({ error: true, text: listFailure(error) });
    });
    return () => { generation.current++; controller.abort(); };
  }, [mode, scope, project, topic, kindFilter, revision]);

  function changeScope(next: PlanningScope) { if (next === scope || !confirmDiscard()) return; clearSelection(); setKindFilter("all"); setScope(next); }
  function openTopic(event: React.FormEvent) {
    event.preventDefault();
    if (!isNoteProject(projectInput) || !isPlanningTopic(topicInput) || !confirmDiscard()) return;
    clearSelection(); setKindFilter("all"); setProject(projectInput); setTopic(topicInput); setRevision(value => value + 1);
  }
  async function open(item: PlanningMeta) {
    if (!confirmDiscard()) return;
    clearEditor(); setSelected(item); setDocument(null); setNotice(null); setReading(true);
    const current = ++readGeneration.current;
    try {
      const value = await readPlanning(item, new AbortController().signal);
      if (current !== readGeneration.current) return;
      setDocument(value); setReading(false);
    } catch (error) {
      if (current !== readGeneration.current || deny(error)) return;
      setReading(false); setNotice({ error: true, text: "This record could not be read. No edits were made." });
    }
  }
  function create() {
    if (!confirmDiscard()) return;
    clearSelection(); setEditor({ base: null, value: { ...selection, kind: scope === "hub_improvements" ? "improvement" : "idea",
      item_id: crypto.randomUUID(), title: "", content: "", status: "proposed", expected_version: 0 } });
  }
  function edit() {
    if (!document) return;
    setNotice(null); setConflict(null); setEditor({ base: document, value: {
      scope: document.scope, project: document.project, topic: document.topic, kind: document.kind, item_id: document.item_id,
      title: document.title, content: document.content, status: document.status, expected_version: document.version } });
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!editor || saving || !canWrite || !csrf) return;
    const current = ++saveGeneration.current, value = { ...editor.value };
    setSaving(true); setNotice(null);
    try {
      const saved = await savePlanning(value, csrf);
      if (current !== saveGeneration.current) return;
      setEditor(null); setConflict(null); setSaving(false); setDocument(saved); setSelected(saved);
      setRevision(value => value + 1); setNotice({ error: false, text: `Saved version ${saved.version}. No work was approved or started.` });
    } catch (error) {
      if (current !== saveGeneration.current || deny(error)) return;
      setSaving(false);
      if (error instanceof PlanningConflictError) {
        setConflict({ current: error.currentVersion, latest: null, failed: false });
        try {
          const latest = await readPlanning(value, new AbortController().signal);
          if (current === saveGeneration.current) setConflict({ current: latest.version, latest, failed: false });
        } catch (readError) {
          if (current !== saveGeneration.current || deny(readError)) return;
          setConflict({ current: error.currentVersion, latest: null, failed: true });
        }
        return;
      }
      setNotice({ error: true, text: "This record could not be saved. Check its size and fields; your draft is still here." });
    }
  }
  function acceptMergedDraft() {
    if (!editor || !conflict?.latest || identity(editor.value) !== identity(conflict.latest)) return;
    setEditor({ ...editor, value: { ...editor.value, expected_version: conflict.latest.version } });
    setNotice({ error: false, text: `Your next save replaces version ${conflict.latest.version}. Review your merged draft first.` }); setConflict(null);
  }
  const bytes = editor ? planningRequestBytes(editor.value) : 0;
  const contentBytes = editor ? new TextEncoder().encode(editor.value.content).length : 0;
  const valid = editor && editor.value.title.trim() && isPlanningId(editor.value.item_id) && bytes <= MAX_PLANNING_REQUEST_BYTES
    && contentBytes <= MAX_PLANNING_CONTENT_BYTES;
  return <section className="panel notes" aria-label="Plans and ideas">
    <div className="panel-title"><h2>Plans and ideas</h2><span className="panel-action">
      <button className="secondary" disabled={loading || mode === "demo"} onClick={() => setRevision(value => value + 1)}>Refresh</button>
      {canWrite && <button className="primary" onClick={create}>New item</button>}</span></div>
    <p className="panel-description">Topic plans, feature proposals and ideas share one durable backlog. Hub improvements have their own section.
      These records are context only: status changes do not approve tasks, change permissions or start work.</p>
    <div className="notes-controls">
      <label>Backlog <select value={scope} onChange={event => changeScope(event.target.value as PlanningScope)}>
        <option value="topic">Topic plans and ideas</option><option value="hub_improvements">Engineering Hub improvements</option></select></label>
      <label>Show kind <select aria-label="Show kind" value={kindFilter} onChange={event => setKindFilter(event.target.value as typeof kindFilter)}>
        <option value="all">All kinds</option>{PLANNING_KINDS.map(kind => <option key={kind} value={kind}>{pretty(kind)}</option>)}</select></label>
      {scope === "topic" ? <form className="notes-project" onSubmit={openTopic}>
        <label htmlFor="planning-project">Project</label><select id="planning-project" value={projectInput} onChange={event => setProjectInput(event.target.value)} required>
          {(knownProjects.some(item => item.id === projectInput) ? knownProjects : [{ id: projectInput, name: projectInput }, ...knownProjects])
            .map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
        <label htmlFor="planning-topic">Topic</label><input id="planning-topic" value={topicInput} maxLength={128}
          onChange={event => setTopicInput(event.target.value)} required />
        <button className="secondary small" disabled={!isNoteProject(projectInput) || !isPlanningTopic(topicInput)}>Open topic</button>
      </form> : <p className="panel-footnote">Engineering Hub’s own improvement backlog — separate from project topics.</p>}
    </div>
    <p className="panel-footnote">{scope === "topic" ? `${project} / ${topic}` : "engineering-hub / Hub improvements"}. This is organization, not a separate permission boundary.</p>
    {notice && <p role={notice.error ? "alert" : "status"} className={notice.error ? "notice error" : "notice"}>{notice.text}</p>}
    {mode === "demo" ? <p role="status">Demo: planning records are not loaded or saved.</p> : <div className="notes-layout">
      <div className="notes-list" aria-label="Planning records">
        {loading && <p role="status">Reading plans…</p>}
        {list && <><p className="panel-footnote">{list.items.length ? `${list.items.length} item(s).` : "No items yet."}
          {list.truncated && " Showing the first 50; narrow Show kind or use shared-note paths to inspect more."}</p>
          <ul>{list.items.map(item => <li key={item.path}><button className={selected?.path === item.path ? "note-link current" : "note-link"}
            type="button" onClick={() => void open(item)}><strong>{item.title}</strong><span>{pretty(item.kind)} · {pretty(item.status)}</span>
            <span>v{item.version} · {actorLabel(item.updated_by, viewerActor)}</span></button></li>)}</ul></>}
      </div><div className="notes-detail">
        {editor ? <form className="note-editor" aria-label={editor.base ? "Edit planning item" : "New planning item"} onSubmit={submit}><fieldset disabled={saving || !canWrite}>
          <legend>{editor.base ? "Edit planning item" : "New planning item"}</legend>
          <label className="field">Kind<select aria-label="Kind" value={editor.value.kind} disabled={editor.base !== null || conflict !== null}
            onChange={event => setEditor({ ...editor, value: { ...editor.value, kind: event.target.value as PlanningDraft["kind"] } })}>
            {PLANNING_KINDS.map(kind => <option key={kind} value={kind}>{pretty(kind)}</option>)}</select></label>
          <label className="field">Stable ID<input value={editor.value.item_id} maxLength={64} spellCheck={false} disabled={editor.base !== null || conflict !== null}
            onChange={event => setEditor({ ...editor, value: { ...editor.value, item_id: event.target.value } })} /></label>
          <label className="field">Title<input value={editor.value.title} maxLength={200} required
            onChange={event => setEditor({ ...editor, value: { ...editor.value, title: event.target.value } })} /></label>
          <label className="field">Status (advisory)<select aria-label="Status (advisory)" value={editor.value.status}
            onChange={event => setEditor({ ...editor, value: { ...editor.value, status: event.target.value as PlanningDraft["status"] } })}>
            {PLANNING_STATUSES.map(status => <option key={status} value={status}>{pretty(status)}</option>)}</select></label>
          <label className="field">Plan or suggestion<textarea rows={14} value={editor.value.content}
            onChange={event => setEditor({ ...editor, value: { ...editor.value, content: event.target.value } })} /></label>
          <p className={valid ? "panel-footnote" : "panel-footnote notice"}>{Math.ceil(contentBytes / 1024)} of48 KB content; {Math.ceil(bytes / 1024)} of64 KB including request fields. {dirty ? "Unsaved changes." : ""}</p>
        </fieldset>
          {conflict && <div role="alert" className="notice error"><p>Version {conflict.current} was saved by someone else. Nothing was overwritten; your draft is retained.</p>
            {!conflict.latest && <p>{conflict.failed ? "The latest version could not be read. Cancel and reopen when available; copy any draft you need first." : "Reading the latest version…"}</p>}
            {conflict.latest && <><details open><summary>Latest version {conflict.latest.version}</summary><h4>{conflict.latest.title}</h4>
              <p>Status: {pretty(conflict.latest.status)}</p><pre className="note-content">{conflict.latest.content}</pre></details>
              <button type="button" className="secondary" onClick={acceptMergedDraft}>I reviewed or merged it — save my draft against this version</button></>}
          </div>}
          <div className="note-actions"><button className="primary" disabled={!valid || saving || !canWrite || conflict !== null}>{saving ? "Saving…" : "Save item"}</button>
            <button type="button" className="secondary" disabled={saving} onClick={() => { if (confirmDiscard()) clearEditor(); }}>Cancel</button></div>
        </form> : reading ? <p role="status">Reading item…</p> : document ? <article>
          <div className="item-overline">{pretty(document.kind)}<span>{pretty(document.status)}</span></div><h3>{document.title}</h3>
          <p className="panel-footnote">v{document.version} · {actorLabel(document.updated_by, viewerActor)} · {new Date(document.updated_at).toLocaleString()}</p>
          <pre className="note-content">{document.content}</pre>{canWrite && <button type="button" className="secondary" onClick={edit}>Edit</button>}
        </article> : <p className="panel-footnote">Select a record to read it, or add a plan, feature or idea.</p>}
      </div></div>}
  </section>;
}
