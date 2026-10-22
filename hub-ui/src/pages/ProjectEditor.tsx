/**
 * Human-only project create / edit / archive. Metadata never enrolls an executor,
 * grants repository access or onboards anyone; agents only use existing projects.
 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { HubApiError } from "../api";
import type { Hub } from "../hub";
import { archiveProject, createProject, isProjectId, PROJECT_STATUSES, readProject, updateProject,
  type ProjectMetadata, type ProjectStatus } from "../projectsApi";
import { registerRouteLeaveGuard } from "../router";
import type { Project } from "../types";
import { Modal, names } from "../ui";
import { slugify } from "../NotesPanel";

export const STATUS_LABEL: Record<ProjectStatus, string> = {
  on_track: "On track", at_risk: "At risk", off_track: "Off track", paused: "Paused",
};
export const STATUS_TONE: Record<ProjectStatus, string> = {
  on_track: "info", at_risk: "amber", off_track: "attention", paused: "neutral",
};

interface Draft { id: string; idTouched: boolean; base: Project | null; value: ProjectMetadata; requestId: string }

const blank = (): ProjectMetadata => ({ name: "", description: "", repository_url: null, lead_agent: null, status: "on_track", parent_project: null });
const fromProject = (project: Project): ProjectMetadata => ({ name: project.name, description: project.description ?? "",
  repository_url: project.repository_url ?? null, lead_agent: project.lead_agent ?? null, status: project.status ?? "on_track",
  parent_project: project.parent_project ?? null });

function failureText(error: unknown): string {
  if (!(error instanceof HubApiError)) return "The request failed. Your draft is still here; nothing was confirmed saved.";
  if (error.code === "projects_unavailable") return "This server can't manage projects yet. The Hub needs an update.";
  if (error.status === 409) return "Someone changed this project after you opened it. Nothing was overwritten. Close and reopen to see the latest.";
  if (error.status === 422) return "Something in the form isn't accepted. Check the ID and repository address.";
  return "The request failed. Your draft is still here; nothing was confirmed saved.";
}

/** Create (project = null) or edit an existing project, in a dialog. */
export function ProjectEditorDialog({ hub, project, onClose }: { hub: Hub; project: Project | null; onClose: () => void }) {
  const [draft, setDraft] = useState<Draft>(() => ({ id: project?.id ?? "", idTouched: false, base: project,
    value: project ? fromProject(project) : blank(), requestId: crypto.randomUUID() }));
  const [state, setState] = useState<"ready" | "loading" | "saving">(project ? "loading" : "ready");
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [editingId, setEditingId] = useState(false);
  const dirty = JSON.stringify(draft.value) !== JSON.stringify(project ? fromProject(draft.base ?? project) : blank()) || (!project && draft.id !== "");
  const dirtyRef = useRef(dirty); dirtyRef.current = dirty;
  useEffect(() => registerRouteLeaveGuard(() => !dirtyRef.current || window.confirm("Discard unsaved project changes?")), []);
  const projectId = project?.id ?? null;
  // Edit always starts from the server's latest revision, never a stale list copy.
  // Overview refreshes replace object identities, not the person's draft. Only
  // opening a different project establishes a new editing session.
  useEffect(() => {
    setError(""); setConflict(false);
    setDraft({ id: projectId ?? "", idTouched: false, base: project,
      value: project ? fromProject(project) : blank(), requestId: crypto.randomUUID() });
    if (!projectId) { setState("ready"); return; }
    setState("loading");
    let live = true;
    readProject(projectId).then((latest) => {
      if (!live) return;
      setDraft((current) => ({ ...current, id: latest.id, base: latest, value: fromProject(latest) }));
      setState("ready");
    }).catch((reason) => {
      if (!live) return;
      if (reason instanceof HubApiError && reason.status === 401) { hub.clearPrivateWorkspace(); return; }
      setError(failureText(reason)); setState("ready");
    });
    return () => { live = false; };
  }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps
  const set = <K extends keyof ProjectMetadata>(key: K, value: ProjectMetadata[K]) =>
    setDraft((current) => ({ ...current, value: { ...current.value, [key]: value },
      // The ID follows the name until the person types their own.
      ...(key === "name" && !current.base && !current.idTouched ? { id: slugify(String(value)).replace(/[._]/g, "-").slice(0, 64) } : {}) }));
  const close = () => { if (!dirtyRef.current || window.confirm("Discard unsaved project changes?")) { dirtyRef.current = false; onClose(); } };
  async function save(event: FormEvent) {
    event.preventDefault();
    const csrf = hub.session?.csrf_token;
    if (!csrf || state !== "ready" || conflict) return;
    setState("saving"); setError("");
    try {
      if (draft.base) await updateProject(draft.base.id, { ...draft.value, expected_revision: draft.base.revision ?? 0 }, csrf);
      else await createProject({ ...draft.value, project_id: draft.id, client_request_id: draft.requestId }, csrf);
      dirtyRef.current = false;
      await hub.refresh();
      onClose();
    } catch (reason) {
      if (reason instanceof HubApiError && reason.status === 401) { hub.clearPrivateWorkspace(); return; }
      if (reason instanceof HubApiError && reason.status === 409) setConflict(true);
      setError(failureText(reason)); setState("ready");
    }
  }
  const others = hub.data.projects.filter((item) => item.id !== draft.id && !item.archived);
  const canSave = state === "ready" && !conflict && !!hub.session?.csrf_token && draft.value.name.trim() !== ""
    && isProjectId(draft.id) && (!draft.base || !!draft.base.revision);
  return (
    <Modal title={project ? `Edit ${project.name}` : "New project"} onClose={close}>
      <form className="project-editor" onSubmit={(event) => void save(event)} aria-label={project ? "Edit project" : "New project"}>
        {state === "loading" ? <p role="status">Reading the latest version…</p> : (
          <fieldset disabled={state !== "ready"}>
            <label className="field">
              <span className="field-label">Name</span>
              <input required maxLength={100} value={draft.value.name} autoFocus placeholder="e.g. Mlsna LandScript"
                onChange={(event) => set("name", event.target.value)} />
            </label>
            {/* The ID is the project's permanent address; most people never need to touch it. */}
            <div className="project-address">
              {editingId && !draft.base ? (
                <label className="field compact">
                  <span className="field-label">Project ID</span>
                  <input required maxLength={64} pattern="[a-z0-9][a-z0-9-]{0,63}" value={draft.id} autoFocus
                    onChange={(event) => setDraft((current) => ({ ...current, id: event.target.value, idTouched: true }))} />
                  <small>Lowercase letters, numbers and dashes. It can't be changed after the project is created.</small>
                </label>
              ) : (
                <p>
                  <span className="field-label">Address</span>{" "}
                  <code>/projects/{draft.id || "…"}</code>{" "}
                  {draft.base ? <small>· can't be changed</small>
                    : <button type="button" className="link-button" onClick={() => setEditingId(true)}>Change</button>}
                </p>
              )}
            </div>
            <label className="field">
              <span className="field-label">Description <small>optional</small></span>
              <textarea rows={3} maxLength={4096} value={draft.value.description}
                placeholder="What is this project for?" onChange={(event) => set("description", event.target.value)} />
            </label>
            <div className="editor-row">
              <label className="field">
                <span className="field-label">Status</span>
                <select value={draft.value.status} onChange={(event) => set("status", event.target.value as ProjectStatus)}>
                  {PROJECT_STATUSES.map((status) => <option key={status} value={status}>{STATUS_LABEL[status]}</option>)}
                </select>
              </label>
              <label className="field">
                <span className="field-label">Lead agent</span>
                <select value={draft.value.lead_agent ?? ""} onChange={(event) => set("lead_agent", (event.target.value || null) as ProjectMetadata["lead_agent"])}>
                  <option value="">No lead</option>
                  {(["codex", "hermes", "claude"] as const).map((agent) => <option key={agent} value={agent}>{names[agent]}</option>)}
                </select>
              </label>
            </div>
            <details className="more-options" open={!!(draft.value.repository_url || draft.value.parent_project) || undefined}>
              <summary>More options</summary>
              <label className="field">
                <span className="field-label">Repository <small>optional</small></span>
                <input type="url" maxLength={2048} placeholder="https://github.com/owner/repository" value={draft.value.repository_url ?? ""}
                  onChange={(event) => set("repository_url", event.target.value || null)} />
                <small>Leave blank for client or notes-only projects. A link doesn't give agents access to the repository.</small>
              </label>
              <label className="field">
                <span className="field-label">Part of <small>optional</small></span>
                <select value={draft.value.parent_project ?? ""} onChange={(event) => set("parent_project", event.target.value || null)}>
                  <option value="">Independent project</option>
                  {others.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                </select>
              </label>
            </details>
          </fieldset>
        )}
        {error && <p role="alert" className="notice error">{error}</p>}
        <footer className="dialog-footer">
          <p>Organizes notes, tasks and decisions. Doesn't turn on staging, give anyone access or start work.</p>
          <div className="dialog-actions">
            <button type="button" className="secondary" onClick={close}>Cancel</button>
            <button type="submit" className="primary" disabled={!canSave}>{state === "saving" ? "Saving…" : project ? "Save changes" : "Create project"}</button>
          </div>
        </footer>
      </form>
    </Modal>
  );
}

export function ArchiveProjectDialog({ hub, project, onClose }: { hub: Hub; project: Project; onClose: () => void }) {
  const [state, setState] = useState<"ready" | "saving">("ready");
  const [error, setError] = useState("");
  async function archive() {
    const csrf = hub.session?.csrf_token;
    if (!csrf || !project.revision) return;
    setState("saving"); setError("");
    try {
      await archiveProject(project.id, project.revision, csrf);
      await hub.refresh();
      onClose();
    } catch (reason) {
      if (reason instanceof HubApiError && reason.status === 401) { hub.clearPrivateWorkspace(); return; }
      setError(failureText(reason)); setState("ready");
    }
  }
  return (
    <Modal title={`Archive ${project.name}?`} onClose={onClose}>
      <p>It disappears from active project lists. Its notes, tasks, decisions and history are kept, and nothing is deleted.</p>
      {error && <p role="alert" className="notice error">{error}</p>}
      <div className="note-actions">
        <button type="button" className="primary" disabled={state === "saving" || !project.revision} onClick={() => void archive()}>
          {state === "saving" ? "Archiving…" : "Archive project"}</button>
        <button type="button" className="secondary" onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  );
}
