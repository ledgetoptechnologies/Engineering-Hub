/**
 * The apps, services and packages that make up a project, as cards. People add, edit and retire them;
 * agents and read-only sessions see the same cards with no edit controls. Nothing here grants access or starts work.
 */
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { HubApiError } from "./api";
import {
  COMPONENT_KINDS, COMPONENT_STATUS_LABEL, COMPONENT_STATUS_TONE, DESCRIPTION_MAX, EDITABLE_STATUSES, KIND_LABEL, NAME_MAX, URL_MAX,
  changedFields, componentSummary, createComponent, isSlug, listComponents, readComponent, retireComponent, sortComponents, updateComponent, validUrl,
  type ComponentFields, type ComponentKind, type ComponentPage, type ComponentStatus, type CreateComponentInput, type ProjectComponent, type UpdateComponentInput,
} from "./componentsApi";
import { DemoComponentStore } from "./componentsDemo";
import type { Hub } from "./hub";
import { safeHref } from "./markdown";
import { slugify } from "./NotesPanel";
import { link, registerRouteLeaveGuard } from "./router";
import type { Project } from "./types";
import { Modal } from "./ui";

/** One interface over the live API and the demo store, so the panel has a single code path. */
export interface ComponentSource {
  list(project: string, after: string | null, signal: AbortSignal): Promise<ComponentPage>;
  read(id: string): Promise<ProjectComponent>;
  create(project: string, input: CreateComponentInput): Promise<ProjectComponent>;
  update(id: string, input: UpdateComponentInput): Promise<ProjectComponent>;
  retire(id: string, revision: number): Promise<ProjectComponent>;
}
export function liveComponents(csrf: string | null): ComponentSource {
  const token = () => { if (!csrf) throw new HubApiError(403, "csrf_required"); return csrf; };
  return {
    list: (project, after, signal) => listComponents(project, after, signal),
    read: (id) => readComponent(id),
    create: (project, input) => createComponent(project, input, token()),
    update: (id, input) => updateComponent(id, input, token()),
    retire: (id, revision) => retireComponent(id, revision, token()),
  };
}
let demoStore: DemoComponentStore | null = null;
export function demoComponents(store?: DemoComponentStore): ComponentSource {
  const current = () => store ?? (demoStore ??= new DemoComponentStore());
  const find = (id: string) => {
    for (const project of ["ledge-top-ops", "treedar", "project-alpha", "engineering-hub", "3d-viewer"]) {
      const item = current().list(project).items.find((row) => row.id === id);
      if (item) return item;
    }
    throw new HubApiError(404, "component_not_found");
  };
  return {
    list: async (project) => current().list(project),
    read: async (id) => find(id),
    create: async (project, input) => current().create(project, input),
    update: async (id, input) => current().update(id, input),
    retire: async (id, revision) => current().retire(id, revision),
  };
}

const errorText: Record<string, string> = {
  component_conflict: "Someone changed this component after you opened it. Nothing was overwritten. Close and reopen to see the latest.",
  component_not_found: "This component no longer exists. The list has been refreshed.",
  component_exists: "Another component in this project already uses that address, even if it's retired. Pick a different one.",
  component_limit: "This project has reached the most components the Hub keeps. Retire or reuse one instead.",
  idempotency_conflict: "This form was already used to add a different component. Close it and start again.",
  component_changes_required: "Nothing has changed, so there's nothing to save.",
  project_archived: "This project is archived, so its components can't be changed. They stay readable.",
  invalid_component: "Something in the form isn't accepted. Check the name, address and links.",
  components_unavailable: "This Hub can't manage components yet. It needs an update.",
  forbidden: "Only a signed-in person with access to this project can change components. Nothing was changed.",
  human_session_required: "Only a signed-in person can change components. Agents can read them. Nothing was changed.",
  csrf_required: "Your session needs refreshing before changes can be saved.",
  network_unavailable: "The Hub didn't answer. Nothing was confirmed saved; try again.",
};
const describe = (error: unknown) => error instanceof HubApiError
  ? errorText[error.code] ?? (error.status === 403 ? errorText.forbidden : "That didn't work, and nothing was confirmed saved. Try again.")
  : "That didn't work, and nothing was confirmed saved. Try again.";
const isSignOut = (error: unknown) => error instanceof HubApiError && error.status === 401;

/** People can change components in a writable live session; the demo store also allows it. Agents never get controls. */
export const canEditComponents = (hub: Hub, project: Project) =>
  !project.archived && hub.writable && (hub.demo || (!!hub.session?.csrf_token && !!hub.session.actor?.startsWith("human:")));

export function useComponentSource(hub: Hub, given?: ComponentSource): ComponentSource {
  const csrf = hub.session?.csrf_token ?? null;
  return useMemo(() => given ?? (hub.demo ? demoComponents() : liveComponents(csrf)), [given, hub.demo, csrf]);
}

export function ComponentsPanel({ hub, project, source: given }: { hub: Hub; project: Project; source?: ComponentSource }) {
  const source = useComponentSource(hub, given);
  const [items, setItems] = useState<ProjectComponent[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "loaded" | "unsupported" | "error">("loading");
  const [loadingMore, setLoadingMore] = useState(false);
  const [showRetired, setShowRetired] = useState(false);
  const [revision, setRevision] = useState(0);
  const [dialog, setDialog] = useState<{ kind: "edit"; item: ProjectComponent | null } | { kind: "retire"; item: ProjectComponent } | null>(null);
  const [notice, setNotice] = useState("");
  const generation = useRef(0);
  const editable = canEditComponents(hub, project);
  const clear = useRef(hub.clearPrivateWorkspace);
  clear.current = hub.clearPrivateWorkspace;

  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    setState((value) => value === "loaded" ? value : "loading");
    source.list(project.id, null, controller.signal).then((page) => {
      if (generation.current !== current) return;
      setItems(page.items); setNext(page.next_after); setState("loaded");
    }).catch((error) => {
      if (generation.current !== current || controller.signal.aborted) return;
      if (isSignOut(error)) { clear.current(); return; }
      setItems([]); setNext(null);
      setState(error instanceof HubApiError && error.code === "components_unavailable" ? "unsupported" : "error");
    });
    return () => { generation.current++; controller.abort(); };
  }, [source, project.id, revision]);

  async function more() {
    if (!next || loadingMore) return;
    const current = generation.current;
    setLoadingMore(true);
    try {
      const page = await source.list(project.id, next, new AbortController().signal);
      if (generation.current !== current) return;
      setItems((rows) => [...rows, ...page.items.filter((item) => !rows.some((row) => row.id === item.id))]);
      setNext(page.next_after);
    } catch (error) {
      if (isSignOut(error)) { clear.current(); return; }
      setNotice("More components couldn't be loaded. Try again.");
    } finally { setLoadingMore(false); }
  }
  function saved(item: ProjectComponent, text: string) {
    setItems((rows) => rows.some((row) => row.id === item.id) ? rows.map((row) => row.id === item.id ? item : row) : [...rows, item]);
    setDialog(null); setNotice(text);
  }

  const sorted = sortComponents(items);
  const retired = sorted.filter((item) => item.status === "retired");
  const shown = showRetired ? sorted : sorted.filter((item) => item.status !== "retired");
  return (
    <section className="panel components-panel" aria-label="Components">
      <div className="panel-title"><h2>Components</h2>{state === "loaded" && <span className="count">{items.length - retired.length}</span>}
        {editable && state === "loaded" && <span className="panel-action">
          <button type="button" className="secondary" onClick={() => { setNotice(""); setDialog({ kind: "edit", item: null }); }}>Add component</button></span>}
      </div>
      {state === "loading" && <p role="status" className="panel-description">Reading components…</p>}
      {state === "unsupported" && <p className="panel-description">This Hub doesn't list components yet. It needs an update.</p>}
      {state === "error" && <p role="alert" className="notice error">Components couldn't be loaded.{" "}
        <button type="button" className="link-button" onClick={() => setRevision((value) => value + 1)}>Try again</button></p>}
      {state === "loaded" && <>
        <p className="panel-description">{items.length
          ? `${componentSummary(items)}. The apps, services and packages that make up ${project.name}.`
          : editable ? `Nothing listed yet. Add the apps, services and packages that make up ${project.name}.` : "No components are listed for this project."}</p>
        {notice && <p role="status" className="notice">{notice}</p>}
        {shown.length > 0 && <ul className="component-grid" aria-label={`${project.name} components`}>
          {shown.map((item) => <ComponentCard key={item.id} item={item} hub={hub} editable={editable}
            onEdit={() => { setNotice(""); setDialog({ kind: "edit", item }); }}
            onRetire={() => { setNotice(""); setDialog({ kind: "retire", item }); }} />)}
        </ul>}
        <div className="component-footer">
          {retired.length > 0 && <button type="button" className="link-button" aria-expanded={showRetired} onClick={() => setShowRetired((value) => !value)}>
            {showRetired ? "Hide retired" : `Show retired (${retired.length})`}</button>}
          {next && <button type="button" className="secondary" disabled={loadingMore} onClick={() => void more()}>
            {loadingMore ? "Loading…" : "Show more"}</button>}
        </div>
      </>}
      {dialog?.kind === "edit" && <ComponentEditorDialog hub={hub} project={project} source={source} component={dialog.item}
        existing={items} onClose={() => setDialog(null)} onSaved={(item, created) => saved(item, created ? `Added ${item.name}.` : `Saved ${item.name}.`)}
        onGone={() => { setDialog(null); setNotice(errorText.component_not_found); setRevision((value) => value + 1); }} />}
      {dialog?.kind === "retire" && <RetireComponentDialog hub={hub} source={source} component={dialog.item}
        onClose={() => setDialog(null)} onSaved={(item) => saved(item, `Retired ${item.name}. It's kept for reference.`)}
        onConflict={() => setRevision((value) => value + 1)} />}
    </section>
  );
}

function ComponentCard({ item, hub, editable, onEdit, onRetire }: {
  item: ProjectComponent; hub: Hub; editable: boolean; onEdit: () => void; onRetire: () => void;
}) {
  const repository = item.repository_url ? safeHref(item.repository_url) : null;
  const packageLink = item.package_url ? safeHref(item.package_url) : null;
  const legacy = item.legacy_project_id ? hub.data.projects.find((project) => project.id === item.legacy_project_id) : null;
  const retired = item.status === "retired";
  return (
    <li className={`component-card${retired ? " retired" : ""}`}>
      <div className="component-head">
        <h3>{item.name}</h3>
        <span className={`status-pill ${COMPONENT_STATUS_TONE[item.status]}`}>{COMPONENT_STATUS_LABEL[item.status]}</span>
      </div>
      <p className="component-kind">{KIND_LABEL[item.kind]} · <code>{item.slug}</code></p>
      {item.description && <p className="component-description">{item.description}</p>}
      {(repository || packageLink || item.legacy_project_id) && <ul className="component-links">
        {repository && <li><a className="text-link" href={repository} target="_blank" rel="noopener noreferrer">Repository</a></li>}
        {packageLink && <li><a className="text-link" href={packageLink} target="_blank" rel="noopener noreferrer">Package</a></li>}
        {item.legacy_project_id && <li>Was its own project: {legacy
          ? <a className="text-link" href={link(`/projects/${legacy.id}`)}>{legacy.name}</a> : <code>{item.legacy_project_id}</code>}</li>}
      </ul>}
      {editable && <div className="component-actions">
        <button type="button" className="secondary" onClick={onEdit} aria-label={`Edit ${item.name}`}>Edit</button>
        {!retired && <button type="button" className="link-button" onClick={onRetire} aria-label={`Retire ${item.name}`}>Retire</button>}
      </div>}
    </li>
  );
}

const blank = (): ComponentFields => ({ name: "", description: "", kind: "web_app", status: "active", repository_url: null, package_url: null,
  legacy_project_id: null });
const fromComponent = (item: ProjectComponent): ComponentFields => ({ name: item.name, description: item.description, kind: item.kind,
  status: item.status, repository_url: item.repository_url, package_url: item.package_url, legacy_project_id: item.legacy_project_id });
const toSlug = (name: string) => slugify(name).replace(/[._]/g, "-").replace(/^-+|-+$/g, "").slice(0, 64).replace(/-+$/, "");

export function ComponentEditorDialog({ hub, project, source, component, existing, onClose, onSaved, onGone }: {
  hub: Hub; project: Project; source: ComponentSource; component: ProjectComponent | null; existing: ProjectComponent[];
  onClose: () => void; onSaved: (item: ProjectComponent, created: boolean) => void; onGone: () => void;
}) {
  const [base, setBase] = useState<ProjectComponent | null>(component);
  const [value, setValue] = useState<ComponentFields>(() => component ? fromComponent(component) : blank());
  const [slug, setSlug] = useState(""); const [slugTouched, setSlugTouched] = useState(false);
  const [requestId] = useState(() => crypto.randomUUID());
  const [state, setState] = useState<"loading" | "ready" | "saving">(component ? "loading" : "ready");
  const [error, setError] = useState(""); const [conflict, setConflict] = useState(false);
  const dirty = JSON.stringify(value) !== JSON.stringify(base ? fromComponent(base) : blank()) || (!component && slug !== "");
  const dirtyRef = useRef(dirty); dirtyRef.current = dirty;
  useEffect(() => registerRouteLeaveGuard(() => !dirtyRef.current || window.confirm("Discard unsaved component changes?")), []);
  const componentId = component?.id ?? null;
  // Editing always starts from the server's latest revision, never the list's copy.
  useEffect(() => {
    if (!componentId) return;
    let live = true;
    source.read(componentId).then((latest) => {
      if (!live) return;
      setBase(latest); setValue(fromComponent(latest)); setState("ready");
    }).catch((reason) => {
      if (!live) return;
      if (isSignOut(reason)) { hub.clearPrivateWorkspace(); return; }
      if (reason instanceof HubApiError && reason.status === 404) { onGone(); return; }
      setError(describe(reason)); setState("ready");
    });
    return () => { live = false; };
  }, [componentId]); // eslint-disable-line react-hooks/exhaustive-deps
  const set = <K extends keyof ComponentFields>(key: K, next: ComponentFields[K]) => {
    setValue((current) => ({ ...current, [key]: next }));
    // The address follows the name until the person types their own.
    if (key === "name" && !component && !slugTouched) setSlug(toSlug(String(next)));
  };
  // After a conflict the draft can't be saved anyway, so closing doesn't ask.
  const close = () => { if (conflict || !dirtyRef.current || window.confirm("Discard unsaved component changes?")) { dirtyRef.current = false; onClose(); } };
  const slugClash = !component && existing.some((item) => item.slug === slug);
  const linksOk = validUrl(value.repository_url) && validUrl(value.package_url);
  const changes = base ? changedFields(base, value) : null;
  const canSave = state === "ready" && !conflict && value.name.trim() !== "" && linksOk
    && (component ? !!base && Object.keys(changes ?? {}).length > 0 : isSlug(slug) && !slugClash);
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!canSave) return;
    setState("saving"); setError("");
    try {
      const item = base
        ? await source.update(base.id, { ...changes, expected_revision: base.revision })
        : await source.create(project.id, { ...value, slug, client_request_id: requestId });
      dirtyRef.current = false;
      onSaved(item, !base);
    } catch (reason) {
      if (isSignOut(reason)) { hub.clearPrivateWorkspace(); return; }
      if (reason instanceof HubApiError && reason.code === "component_conflict") setConflict(true);
      setError(describe(reason)); setState("ready");
    }
  }
  return (
    <Modal title={component ? `Edit ${component.name}` : `Add a component to ${project.name}`} onClose={close}>
      <form className="project-editor component-editor" onSubmit={(event) => void save(event)} aria-label={component ? "Edit component" : "New component"}>
        {state === "loading" ? <p role="status">Reading the latest version…</p> : (
          <fieldset disabled={state !== "ready" || conflict}>
            <label className="field">
              <span className="field-label">Name</span>
              <input required maxLength={NAME_MAX} value={value.name} autoFocus placeholder="e.g. Operations Viewer"
                onChange={(event) => set("name", event.target.value)} />
            </label>
            <div className="project-address">
              {component ? <p><span className="field-label">Address</span> <code>{component.slug}</code> <small>· can't be changed</small></p> : (
                <label className="field compact">
                  <span className="field-label">Address</span>
                  <input required maxLength={64} pattern="[a-z0-9][a-z0-9-]{0,63}" value={slug} aria-invalid={slugClash || undefined}
                    onChange={(event) => { setSlug(event.target.value); setSlugTouched(true); }} />
                  <small>{slugClash ? "Another component here already uses this address." : "Lowercase letters, numbers and dashes. It can't be changed later."}</small>
                </label>
              )}
            </div>
            <div className="editor-row">
              <label className="field">
                <span className="field-label">Kind</span>
                <select value={value.kind} onChange={(event) => set("kind", event.target.value as ComponentKind)}>
                  {COMPONENT_KINDS.map((kind) => <option key={kind} value={kind}>{KIND_LABEL[kind]}</option>)}
                </select>
              </label>
              <label className="field">
                <span className="field-label">Status</span>
                <select value={value.status} onChange={(event) => set("status", event.target.value as ComponentStatus)}>
                  {/* Retiring has its own button; a retired component is restored by picking another status. */}
                  {(base?.status === "retired" ? [...EDITABLE_STATUSES, "retired" as const] : EDITABLE_STATUSES)
                    .map((status) => <option key={status} value={status}>{COMPONENT_STATUS_LABEL[status]}</option>)}
                </select>
              </label>
            </div>
            <label className="field">
              <span className="field-label">Description <small>optional</small></span>
              <textarea rows={3} maxLength={DESCRIPTION_MAX} value={value.description} placeholder="What does it do, and for whom?"
                onChange={(event) => set("description", event.target.value)} />
            </label>
            <details className="more-options" open={!!(value.repository_url || value.package_url || value.legacy_project_id) || undefined}>
              <summary>Links</summary>
              <label className="field">
                <span className="field-label">Repository <small>optional</small></span>
                <input type="url" maxLength={URL_MAX} placeholder="https://github.com/owner/repository" value={value.repository_url ?? ""}
                  aria-invalid={!validUrl(value.repository_url) || undefined} onChange={(event) => set("repository_url", event.target.value || null)} />
              </label>
              <label className="field">
                <span className="field-label">Package <small>optional</small></span>
                <input type="url" maxLength={URL_MAX} placeholder="https://…" value={value.package_url ?? ""}
                  aria-invalid={!validUrl(value.package_url) || undefined} onChange={(event) => set("package_url", event.target.value || null)} />
              </label>
              <small>Links must start with https://. A link doesn't give agents access to anything.</small>
              <label className="field">
                <span className="field-label">Was its own project <small>optional</small></span>
                <select value={value.legacy_project_id ?? ""} onChange={(event) => set("legacy_project_id", event.target.value || null)}>
                  <option value="">No earlier project</option>
                  {hub.data.projects.filter((item) => item.id !== project.id)
                    .map((item) => <option key={item.id} value={item.id}>{item.name}{item.archived ? " (archived)" : ""}</option>)}
                  {value.legacy_project_id && !hub.data.projects.some((item) => item.id === value.legacy_project_id)
                    && <option value={value.legacy_project_id}>{value.legacy_project_id}</option>}
                </select>
                <small>Keeps a link for context. Nothing is moved from that project.</small>
              </label>
            </details>
          </fieldset>
        )}
        {error && <p role="alert" className="notice error">{error}</p>}
        <footer className="dialog-footer">
          <p>Describes the project. Doesn't turn on staging, give anyone access or start work.</p>
          <div className="dialog-actions">
            <button type="button" className="secondary" onClick={close}>{conflict ? "Close" : "Cancel"}</button>
            <button type="submit" className="primary" disabled={!canSave}>{state === "saving" ? "Saving…" : component ? "Save changes" : "Add component"}</button>
          </div>
        </footer>
      </form>
    </Modal>
  );
}

function RetireComponentDialog({ hub, source, component, onClose, onSaved, onConflict }: {
  hub: Hub; source: ComponentSource; component: ProjectComponent; onClose: () => void;
  onSaved: (item: ProjectComponent) => void; onConflict: () => void;
}) {
  const [state, setState] = useState<"ready" | "saving" | "stale">("ready");
  const [error, setError] = useState("");
  async function retire() {
    setState("saving"); setError("");
    try { onSaved(await source.retire(component.id, component.revision)); }
    catch (reason) {
      if (isSignOut(reason)) { hub.clearPrivateWorkspace(); return; }
      setError(describe(reason));
      if (reason instanceof HubApiError && [404, 409].includes(reason.status)) { setState("stale"); onConflict(); } else setState("ready");
    }
  }
  return (
    <Modal title={`Retire ${component.name}?`} onClose={onClose}>
      <p className="modal-copy">It moves to the retired list. Its history is kept and nothing is deleted. Retiring can't be undone from here.</p>
      {error && <p role="alert" className="notice error">{error}</p>}
      <div className="note-actions">
        <button type="button" className="primary" disabled={state !== "ready"} onClick={() => void retire()}>
          {state === "saving" ? "Retiring…" : "Retire component"}</button>
        <button type="button" className="secondary" onClick={onClose}>{state === "stale" ? "Close" : "Cancel"}</button>
      </div>
    </Modal>
  );
}

/** On an umbrella project: the projects grouped under it, each with its component count. */
export function GroupedProjectsPanel({ hub, project, source: given }: { hub: Hub; project: Project; source?: ComponentSource }) {
  const source = useComponentSource(hub, given);
  const children = hub.data.projects.filter((item) => item.parent_project === project.id && !item.archived);
  const key = children.map((item) => item.id).join(",");
  const [summaries, setSummaries] = useState<Record<string, string>>({});
  useEffect(() => {
    const controller = new AbortController();
    setSummaries({});
    // Bounded: an umbrella shows at most 24 grouped projects' counts.
    for (const child of children.slice(0, 24)) {
      source.list(child.id, null, controller.signal)
        .then((page) => { if (!controller.signal.aborted) setSummaries((current) => ({ ...current, [child.id]: componentSummary(page.items) })); })
        .catch(() => { if (!controller.signal.aborted) setSummaries((current) => ({ ...current, [child.id]: "" })); });
    }
    return () => controller.abort();
  }, [source, key]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!children.length) return null;
  return (
    <section className="panel" aria-label="Projects in this group">
      <div className="panel-title"><h2>Projects in this group</h2><span className="count">{children.length}</span></div>
      <ul className="preview-list">
        {children.map((child) => (
          <li key={child.id}><a href={link(`/projects/${child.id}`)}>
            <span className="preview-title">{child.name}</span>
            <span className="preview-meta">{summaries[child.id] ?? "Reading components…"}</span></a></li>
        ))}
      </ul>
    </section>
  );
}
