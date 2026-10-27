import { useEffect, useRef, useState } from "react";
import { HubApiError, type HubMode } from "./api";
import { Markdown } from "./markdown";
import { actorLabel } from "./NotesPanel";
import { isNoteProject } from "./notesApi";
import { registerRouteLeaveGuard } from "./router";
import { discoverPlanningTopics, isPlanningId, isPlanningTopic, listPlanning, MAX_PLANNING_CONTENT_BYTES, MAX_PLANNING_REQUEST_BYTES,
  PLANNING_KINDS, PLANNING_STATUSES, PlanningConflictError, planningItemId, planningRequestBytes, readPlanning, savePlanning,
  topicLabel, type PlanningDraft, type PlanningItem, type PlanningKey, type PlanningMeta } from "./planningApi";

export interface PlanningPanelProps {
  mode: HubMode;
  /** A project page's project; "all" is the cross-project page with its own project picker. */
  selectedProject: string;
  knownProjects: { id: string; name: string }[];
  /** The current discussion topic: the starting topic when a project has no plans yet. */
  topic: string;
  csrf: string | null; writable: boolean; viewerActor?: string | null; onUnauthorized: () => void;
}
type Backlog = "project" | "hub";
type Kind = PlanningKey["kind"];
interface Editor { value: PlanningDraft; base: PlanningItem | null; suffix: string; topicChoice: string; newTopic: string }
interface Conflict { current: number; latest: PlanningItem | null; failed: boolean }

export const KIND_LABEL: Record<Kind, string> = { plan: "Plan", feature: "Feature", idea: "Idea", improvement: "Improvement" };
const KIND_PLURAL: Record<Kind, string> = { plan: "Plans", feature: "Features", idea: "Ideas", improvement: "Improvements" };
export const STATUS_LABEL: Record<PlanningMeta["status"], string> = {
  proposed: "Proposed", planned: "Planned", in_progress: "In progress", done: "Done", declined: "Declined" };
/** How many topics "All topics" reads at once; a topic filter reaches the rest. */
export const MAX_SHOWN_TOPICS = 12;
const NEW_TOPIC = "__new_topic__";
const PROGRESS_ORDER: Record<PlanningMeta["status"], number> = { in_progress: 0, planned: 1, proposed: 2, done: 3, declined: 4 };
/** Open work first (in progress, planned, proposed), then shipped, then declined, so finished items stop crowding the backlog. */
export function groupByProgress<T extends Pick<PlanningMeta, "status">>(items: T[]): { key: string; label: string; items: T[] }[] {
  const sorted = [...items].sort((a, b) => PROGRESS_ORDER[a.status] - PROGRESS_ORDER[b.status]);
  return [
    { key: "open", label: "Open", items: sorted.filter(item => item.status !== "done" && item.status !== "declined") },
    { key: "done", label: "Done", items: sorted.filter(item => item.status === "done") },
    { key: "declined", label: "Declined", items: sorted.filter(item => item.status === "declined") },
  ];
}
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
const identity = (value: PlanningKey) => `${value.scope}/${value.project}/${value.topic}/${value.kind}/${value.item_id}`;
const randomSuffix = () => crypto.randomUUID().replaceAll("-", "").slice(0, 6);
/** Letters, digits and _.:- only, as the topic contract allows; spaces become dashes. */
const topicSlug = (value: string) => value.trim().replace(/[^A-Za-z0-9_.:-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, 128);
function age(value: string): string {
  const minutes = Math.round((Date.now() - new Date(value).getTime()) / 60000);
  return minutes < 1 ? "just now" : minutes < 60 ? `${minutes}m ago`
    : minutes < 2880 ? `${Math.round(minutes / 60)}h ago` : `${Math.round(minutes / 1440)}d ago`;
}
function TopicName({ topic }: { topic: string }) { return <span title={topic}>{topicLabel(topic)}</span>; }

export function PlanningPanel({ mode, selectedProject, knownProjects, topic: discussionTopic, csrf, writable, viewerActor, onUnauthorized }: PlanningPanelProps) {
  const scoped = selectedProject !== "all";
  const [pickedProject, setPickedProject] = useState("engineering-hub");
  const project = scoped && isNoteProject(selectedProject) ? selectedProject : pickedProject;
  const projectName = knownProjects.find(item => item.id === project)?.name ?? project;
  const [backlog, setBacklog] = useState<Backlog>("project");
  const hubBacklog = backlog === "hub" && project === "engineering-hub";
  // Keyed by project so a previous project's topics are never read against the next one.
  const [found, setFound] = useState<{ project: string; topics: string[] } | null>(null);
  const topics = found?.project === project ? found.topics : null;
  const setTopics = (value: string[] | null) => setFound(value === null ? null : { project, topics: value });
  const [topicsNote, setTopicsNote] = useState("");
  const [topicFilter, setTopicFilter] = useState("all");
  const [kindFilter, setKindFilter] = useState<"all" | Kind>("all");
  const [items, setItems] = useState<PlanningMeta[] | null>(null);
  const [listNotes, setListNotes] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<PlanningMeta | null>(null), [document, setDocument] = useState<PlanningItem | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null), [conflict, setConflict] = useState<Conflict | null>(null);
  const [saving, setSaving] = useState(false), [reading, setReading] = useState(false), [revision, setRevision] = useState(0);
  const [plain, setPlain] = useState(false);
  const [notice, setNotice] = useState<{ error: boolean; text: string } | null>(null);
  const topicGeneration = useRef(0), generation = useRef(0), readGeneration = useRef(0), saveGeneration = useRef(0);
  const unauthorized = useRef(onUnauthorized); unauthorized.current = onUnauthorized;
  const dirty = editor !== null && (editor.base === null ? true
    : editor.value.title !== editor.base.title || editor.value.content !== editor.base.content || editor.value.status !== editor.base.status);
  const dirtyRef = useRef(dirty); dirtyRef.current = dirty;
  const canWrite = mode === "live" && writable && !!csrf;
  const confirmDiscard = () => !dirtyRef.current || window.confirm(discardText);
  function clearEditor() { saveGeneration.current++; setEditor(null); setConflict(null); setSaving(false); }
  function clearSelection() { readGeneration.current++; clearEditor(); setSelected(null); setDocument(null); setReading(false); setNotice(null); }
  function deny(error: unknown): boolean {
    if (!(error instanceof HubApiError) || ![401, 403].includes(error.status)) return false;
    topicGeneration.current++; generation.current++; readGeneration.current++;
    clearSelection(); setItems(null); setTopics(null); setLoading(false); unauthorized.current(); return true;
  }
  useEffect(() => registerRouteLeaveGuard(() => !dirtyRef.current || window.confirm(discardText)), []);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn); return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  useEffect(() => () => { topicGeneration.current++; generation.current++; readGeneration.current++; saveGeneration.current++; }, []);

  // A project page follows its project; unsaved edits are only dropped after the leave guard agreed.
  const followed = useRef(project);
  useEffect(() => {
    if (followed.current === project) return;
    followed.current = project;
    clearSelection(); setBacklog("project"); setTopicFilter("all"); setKindFilter("all");
  });

  // Which topics hold this project's plans. Paths only, read again on Refresh or after a save.
  useEffect(() => {
    const current = ++topicGeneration.current, controller = new AbortController();
    setTopics(null); setTopicsNote("");
    if (mode === "demo" || !isNoteProject(project)) return () => { topicGeneration.current++; controller.abort(); };
    void discoverPlanningTopics(project, controller.signal).then(found => {
      if (current !== topicGeneration.current || controller.signal.aborted) return;
      setTopics(found.topics);
      if (!found.complete) setTopicsNote("This project has many plan records; some topics may be missing from the topic list.");
    }).catch(error => {
      if (current !== topicGeneration.current || controller.signal.aborted || deny(error)) return;
      // Fall back to the current discussion topic rather than showing nothing.
      setTopics(isPlanningTopic(discussionTopic) ? [discussionTopic] : []);
      setTopicsNote("This project's topics couldn't be listed, so only the current discussion topic is shown. Refresh to retry.");
    });
    return () => { topicGeneration.current++; controller.abort(); };
  }, [mode, project, revision]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const current = ++generation.current, controller = new AbortController();
    setItems(null); setListNotes([]);
    if (mode === "demo" || (!hubBacklog && topics === null)) {
      setLoading(mode !== "demo");
      return () => { generation.current++; controller.abort(); };
    }
    const kind = kindFilter === "all" ? undefined : kindFilter;
    const all = topics ?? [];
    const targets = hubBacklog ? [null] : topicFilter === "all" ? all.slice(0, MAX_SHOWN_TOPICS) : [topicFilter];
    setLoading(true);
    void Promise.all(targets.map(topic => listPlanning(topic === null
      ? { scope: "hub_improvements", project: "engineering-hub", topic: null }
      : { scope: "topic", project, topic }, controller.signal, kind))).then(lists => {
      if (current !== generation.current || controller.signal.aborted) return;
      setItems(lists.flatMap(list => list.items)); setLoading(false);
      const notes: string[] = [];
      if (!hubBacklog && topicFilter === "all" && all.length > MAX_SHOWN_TOPICS)
        notes.push(`Showing the ${MAX_SHOWN_TOPICS} most recently updated topics. Choose a topic to see the others.`);
      if (lists.some(list => list.truncated))
        notes.push("Some lists stopped at 50 items. Choose a topic or a type to narrow them.");
      setListNotes(notes);
    }).catch(error => {
      if (current !== generation.current || controller.signal.aborted || deny(error)) return;
      setLoading(false); setNotice({ error: true, text: listFailure(error) });
    });
    return () => { generation.current++; controller.abort(); };
  }, [mode, project, hubBacklog, topics, topicFilter, kindFilter]);

  function changeBacklog(next: Backlog) {
    if (next === backlog || !confirmDiscard()) return;
    clearSelection(); setKindFilter("all"); setTopicFilter("all"); setBacklog(next);
  }
  function changeProject(next: string) {
    if (next === pickedProject || !isNoteProject(next) || !confirmDiscard()) return;
    setPickedProject(next);
  }
  function changeTopic(next: string) {
    if (next === topicFilter || !confirmDiscard()) return;
    clearSelection(); setTopicFilter(next);
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
    clearSelection();
    const suffix = randomSuffix();
    const startTopic = topicFilter !== "all" ? topicFilter : topics?.[0] ?? (isPlanningTopic(discussionTopic) ? discussionTopic : "");
    setEditor({ base: null, suffix, topicChoice: startTopic || NEW_TOPIC, newTopic: "", value: {
      scope: hubBacklog ? "hub_improvements" : "topic", project: hubBacklog ? "engineering-hub" : project,
      topic: hubBacklog ? null : startTopic, kind: hubBacklog ? "improvement" : "idea",
      item_id: planningItemId("", suffix), title: "", content: "", status: "proposed", expected_version: 0 } });
  }
  function edit() {
    if (!document) return;
    setNotice(null); setConflict(null); setEditor({ base: document, suffix: "", topicChoice: document.topic ?? "", newTopic: "", value: {
      scope: document.scope, project: document.project, topic: document.topic, kind: document.kind, item_id: document.item_id,
      title: document.title, content: document.content, status: document.status, expected_version: document.version } });
  }
  /** A new record's id and topic follow the form until it is first saved or conflicts. */
  const placeLocked = !editor || editor.base !== null || conflict !== null || editor.value.expected_version > 0;
  function setTitle(title: string) {
    if (!editor) return;
    setEditor({ ...editor, value: { ...editor.value, title, ...(placeLocked ? {} : { item_id: planningItemId(title, editor.suffix) }) } });
  }
  function setTopicChoice(choice: string, newTopic = editor?.newTopic ?? "") {
    if (!editor || placeLocked) return;
    setEditor({ ...editor, topicChoice: choice, newTopic, value: { ...editor.value, topic: choice === NEW_TOPIC ? topicSlug(newTopic) : choice } });
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
  const topicValid = !editor || editor.value.scope !== "topic" || isPlanningTopic(editor.value.topic ?? "");
  const valid = editor && editor.value.title.trim() && isPlanningId(editor.value.item_id) && topicValid
    && bytes <= MAX_PLANNING_REQUEST_BYTES && contentBytes <= MAX_PLANNING_CONTENT_BYTES;
  const showTopic = !hubBacklog && topicFilter === "all";
  const kinds = hubBacklog ? PLANNING_KINDS : PLANNING_KINDS.filter(kind => kind !== "improvement");
  const where = hubBacklog ? "Engineering Hub improvements"
    : topicFilter === "all" ? `${projectName}, all topics` : `${projectName}, ${topicLabel(topicFilter)}`;

  return <section className="panel notes" aria-label="Plans and ideas">
    <div className="panel-title"><h2>{hubBacklog ? "Hub improvements" : "Plans and ideas"}</h2><span className="panel-action">
      <button className="secondary" disabled={loading || mode === "demo"} onClick={() => setRevision(value => value + 1)}>Refresh</button>
      {canWrite && <button className="primary" onClick={create}>New item</button>}</span></div>
    <p className="panel-description">{hubBacklog
      ? "Engineering Hub's own improvement backlog, kept apart from project plans."
      : "Plans, feature proposals and ideas for this project, grouped by the discussion topic they came from."} These
      records are context only: status changes do not approve tasks, change permissions or start work.</p>
    <div className="notes-controls planning-controls">
      {!scoped && <label>Project <select value={project} onChange={event => changeProject(event.target.value)}>
        {(knownProjects.some(item => item.id === project) ? knownProjects : [{ id: project, name: project }, ...knownProjects])
          .map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}
      {project === "engineering-hub" && <div className="segmented" role="group" aria-label="Backlog">
        <button type="button" aria-pressed={!hubBacklog} onClick={() => changeBacklog("project")}>Project plans</button>
        <button type="button" aria-pressed={hubBacklog} onClick={() => changeBacklog("hub")}>Hub improvements</button>
      </div>}
      {!hubBacklog && <label>Topic <select value={topicFilter} disabled={!topics?.length} onChange={event => changeTopic(event.target.value)}>
        <option value="all">All topics{topics ? ` (${topics.length})` : ""}</option>
        {topics?.map(topic => <option key={topic} value={topic} title={topic}>{topicLabel(topic)}</option>)}</select></label>}
      <label>Show <select value={kindFilter} onChange={event => setKindFilter(event.target.value as typeof kindFilter)}>
        <option value="all">Everything</option>{kinds.map(kind => <option key={kind} value={kind}>{KIND_PLURAL[kind]}</option>)}</select></label>
    </div>
    <p className="panel-footnote">{where}. Grouping by project and topic is organization, not a separate permission boundary.</p>
    {topicsNote && <p className="notice">{topicsNote}</p>}
    {notice && <p role={notice.error ? "alert" : "status"} className={notice.error ? "notice error" : "notice"}>{notice.text}</p>}
    {mode === "demo" ? <p role="status">Demo: planning records are not loaded or saved.</p> : <div className="notes-layout">
      <div className="notes-list" aria-label="Planning records">
        {loading && <p role="status">Reading plans…</p>}
        {items && <><p className="panel-footnote">{items.length
          ? `${items.length} item${items.length === 1 ? "" : "s"}.`
          : hubBacklog ? "No Hub improvements yet."
          : topics?.length === 0 ? `No plans for ${projectName} yet.` : "Nothing matches this topic and type."}</p>
          {listNotes.map(text => <p key={text} className="panel-footnote">{text}</p>)}
          {groupByProgress(items).map(group => group.items.length > 0 && <section key={group.key} className="plan-group" aria-label={group.label}>
            <h3 className="plan-group-title">{group.label} <span>({group.items.length})</span></h3>
            <ul>{group.items.map(item => <li key={item.path}><button className={selected?.path === item.path ? "note-link current" : "note-link"}
              type="button" onClick={() => void open(item)}><strong>{item.status === "done" && <span className="plan-done-mark" aria-hidden="true">✓ </span>}{item.title}</strong>
              <span>{KIND_LABEL[item.kind]} · <span className={`plan-status plan-status-${item.status}`}>{STATUS_LABEL[item.status]}</span>
                {showTopic && item.topic && <> · <TopicName topic={item.topic} /></>}</span>
              <span>{actorLabel(item.updated_by, viewerActor)} · <time dateTime={item.updated_at} title={new Date(item.updated_at).toLocaleString()}>{age(item.updated_at)}</time></span>
            </button></li>)}</ul></section>)}</>}
      </div><div className="notes-detail">
        {editor ? <form className="note-editor" aria-label={editor.base ? "Edit planning item" : "New planning item"} onSubmit={submit}><fieldset disabled={saving || !canWrite}>
          <legend>{editor.base ? "Edit planning item" : hubBacklog ? "New Hub improvement" : `New item for ${projectName}`}</legend>
          {editor.value.scope === "topic" && (placeLocked
            ? <p className="panel-footnote">Topic: <TopicName topic={editor.value.topic ?? ""} /></p>
            : <><label className="field">Topic<select value={editor.topicChoice} onChange={event => setTopicChoice(event.target.value)}>
                {topics?.map(topic => <option key={topic} value={topic} title={topic}>{topicLabel(topic)}</option>)}
                {isPlanningTopic(discussionTopic) && !topics?.includes(discussionTopic) &&
                  <option value={discussionTopic} title={discussionTopic}>{topicLabel(discussionTopic)} (current discussion)</option>}
                <option value={NEW_TOPIC}>New topic…</option></select></label>
              {editor.topicChoice === NEW_TOPIC && <label className="field">New topic name<input value={editor.newTopic} maxLength={128}
                placeholder="for example: treedar-milestone-2" onChange={event => setTopicChoice(NEW_TOPIC, event.target.value)} /></label>}
              {!topicValid && <p className="panel-footnote notice">Choose a topic, or name a new one using letters, numbers and dashes.</p>}</>)}
          <label className="field">Type<select value={editor.value.kind} disabled={editor.base !== null || conflict !== null}
            onChange={event => setEditor({ ...editor, value: { ...editor.value, kind: event.target.value as PlanningDraft["kind"] } })}>
            {PLANNING_KINDS.map(kind => <option key={kind} value={kind}>{KIND_LABEL[kind]}</option>)}</select></label>
          <label className="field">Title<input value={editor.value.title} maxLength={200} required
            onChange={event => setTitle(event.target.value)} /></label>
          <label className="field">Status<select value={editor.value.status}
            onChange={event => setEditor({ ...editor, value: { ...editor.value, status: event.target.value as PlanningDraft["status"] } })}>
            {PLANNING_STATUSES.map(status => <option key={status} value={status}>{STATUS_LABEL[status]}</option>)}</select></label>
          <p className="panel-footnote">Status is for tracking only. It doesn't approve or start anything.</p>
          <label className="field">Details<textarea rows={14} value={editor.value.content}
            onChange={event => setEditor({ ...editor, value: { ...editor.value, content: event.target.value } })} /></label>
          <p className={valid ? "panel-footnote" : "panel-footnote notice"}>{Math.ceil(contentBytes / 1024)} of 48 KB of details; {Math.ceil(bytes / 1024)} of 64 KB
            with the other fields. {dirty ? "Unsaved changes." : ""}</p>
        </fieldset>
          {conflict && <div role="alert" className="notice error"><p>Version {conflict.current} was saved by someone else. Nothing was overwritten; your draft is retained.</p>
            {!conflict.latest && <p>{conflict.failed ? "The latest version could not be read. Cancel and reopen when available; copy any draft you need first." : "Reading the latest version…"}</p>}
            {conflict.latest && <><details open><summary>Latest version {conflict.latest.version}</summary><h4>{conflict.latest.title}</h4>
              <p>Status: {STATUS_LABEL[conflict.latest.status]}</p><pre className="note-content">{conflict.latest.content}</pre></details>
              <button type="button" className="secondary" onClick={acceptMergedDraft}>I reviewed or merged it — save my draft against this version</button></>}
          </div>}
          <div className="note-actions"><button className="primary" disabled={!valid || saving || !canWrite || conflict !== null}>{saving ? "Saving…" : "Save item"}</button>
            <button type="button" className="secondary" disabled={saving} onClick={() => { if (confirmDiscard()) clearEditor(); }}>Cancel</button></div>
        </form> : reading ? <p role="status">Reading item…</p> : document ? <article className="note-view" aria-label={document.title}>
          <div className="item-overline">{KIND_LABEL[document.kind]}<span>{STATUS_LABEL[document.status]}</span></div>
          <div className="note-view-head"><h3>{document.title}</h3>
            <div className="segmented" role="group" aria-label="Item view">
              <button type="button" aria-pressed={!plain} onClick={() => setPlain(false)}>Formatted</button>
              <button type="button" aria-pressed={plain} onClick={() => setPlain(true)}>Plain text</button>
            </div></div>
          <p className="panel-footnote">{document.topic ? <>Topic <TopicName topic={document.topic} /> · </> : null}Updated
            by {actorLabel(document.updated_by, viewerActor)}, {new Date(document.updated_at).toLocaleString()} · version {document.version}</p>
          {plain ? <pre className="note-content">{document.content}</pre> : <div className="note-content formatted"><Markdown source={document.content} /></div>}
          {canWrite && <div className="note-actions"><button type="button" className="secondary" onClick={edit}>Edit</button></div>}
        </article> : <p className="panel-footnote">Select an item to read it{canWrite ? ", or add a plan, feature or idea" : ""}.</p>}
      </div></div>}
  </section>;
}
