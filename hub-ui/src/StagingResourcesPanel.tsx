/**
 * What the staging PC is keeping for EH jobs, how long, and a two-step cleanup.
 * Removal only ever happens through a reviewed plan; protected and pinned items are never offered for removal.
 */
import { actorName } from "./actors";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { HubApiError, type HubMode } from "./api";
import {
  applyCleanup, canChangeKeep, expiryLabel, extendedUntil, formatBytes, keepResource, keptLabel, kindLabel, KEEP_REASON_MAX,
  listResources, planCleanup, planSummary, protectedLabel, readStagingStatus, recheckJob, recheckSummary, resultSummary, ResourceConflictError, startPreview, statusLine,
  type CleanupPlan, type CleanupResult, type KeepRequest, type PreviewSession, type ResourcePage, type ResourceQuery, type StagingResource,
  type HeldJob, type RecheckResult, type StagingStatus,
} from "./stagingResourcesApi";
import { DemoResourceStore } from "./stagingResourcesDemo";
import { Badge, Modal, names } from "./ui";

/** One interface over the live API and the demo store, so the panel has a single code path. */
export interface ResourceSource {
  list(query: ResourceQuery, signal: AbortSignal): Promise<ResourcePage>;
  keep(resource: StagingResource, request: KeepRequest): Promise<StagingResource>;
  plan(project: string | null): Promise<CleanupPlan>;
  apply(plan: CleanupPlan): Promise<CleanupResult>;
  preview(resource: StagingResource): Promise<PreviewSession>;
  status(signal: AbortSignal): Promise<StagingStatus>;
  recheck(jobId: string): Promise<RecheckResult>;
}
export function liveSource(csrf: string): ResourceSource {
  return {
    list: (query, signal) => listResources(query, signal),
    keep: (resource, request) => keepResource(resource, request, csrf),
    plan: (project) => planCleanup(project, csrf),
    apply: (plan) => applyCleanup(plan, csrf),
    preview: (resource) => startPreview(resource, csrf),
    status: (signal) => readStagingStatus(signal),
    recheck: (jobId) => recheckJob(jobId, csrf),
  };
}
export function demoSource(store = new DemoResourceStore()): ResourceSource {
  let demoHeld: HeldJob[] = [{ id: "5ea9e000-0000-4000-8000-000000000990", project: "engineering-hub", recipe: "vite-build",
    state: "held", phase: "dispatching", source_commit: "497021a5d3c19f086b12e52f964e22df84720040" }];
  return {
    list: async (query) => store.list(query),
    keep: async (resource, request) => store.keep(resource, request),
    plan: async (project) => store.plan(project),
    apply: async (plan) => store.apply(plan),
    // Demo mode has no preview origin, and says so rather than faking one.
    preview: async () => { throw new HubApiError(503, "preview_not_configured"); },
    status: async () => ({ configured: true, running: true, reachable: true, profiles: { "engineering-hub": ["vite-build"] },
      state: null, previews_configured: false, can_reconcile: true, held_jobs: demoHeld.map(job => ({ ...job })) }),
    recheck: async (jobId) => {
      // The demo's held job turns out to have failed; nothing re-runs.
      demoHeld = demoHeld.filter(job => job.id !== jobId);
      return { job: { id: jobId, state: "failed" }, resource_ids: {}, phase: "failed" };
    },
  };
}

const errorText: Record<string, string> = {
  staging_resource_conflict: "Someone changed this item meanwhile. The list has been refreshed; try again.",
  staging_resource_protected: "This item is protected, so how long it's kept can't be changed.",
  staging_resource_not_found: "This item no longer exists. The list has been refreshed.",
  staging_plan_expired: "That cleanup plan expired before it was applied. Nothing was removed; make a new plan.",
  staging_plan_changed: "Staging changed since the plan was made. Nothing was removed; make a new plan.",
  staging_unavailable: "The staging PC isn't answering. Nothing was changed.",
  staging_busy: "The staging PC is busy with another job. Nothing was changed; try again shortly.",
  staging_forbidden: "You don't have permission for that on this project. Nothing was changed.",
  staging_admin_required: "Only the Hub owner can pin, or change a pin. Nothing was changed.",
  staging_resource_not_owned: "That was made by someone else, so it can't be changed from here. Nothing was changed.",
  staging_cleanup_in_progress: "Another cleanup is already running. Nothing was changed; try again when it finishes.",
  preview_not_configured: "Previews can't be opened yet: the separate preview address hasn't been set up.",
  preview_unavailable: "This preview can't be opened right now.",
  keep_reason_required: "Say why it should be kept.",
  csrf_required: "Your session needs refreshing before changes can be saved.",
};
const describe = (error: unknown) => error instanceof HubApiError
  ? errorText[error.code] ?? "That didn't work, and nothing was changed. Try again." : "That didn't work, and nothing was changed. Try again.";
// Only a lost sign-in clears the panel; a refusal of one action (not the owner, not allowed on this project) is explained instead.
const refusals = new Set(["csrf_required", "staging_forbidden", "staging_admin_required", "staging_resource_not_owned"]);
const isAuthLoss = (error: unknown) => error instanceof HubApiError && [401, 403].includes(error.status) && !refusals.has(error.code);
/** Who made a resource: "You" only for the signed-in person. */
export const resourceMaker = (actor: string, viewer?: string | null) => actorName(actor, { viewer });

export interface StagingResourcesPanelProps {
  mode: HubMode; source?: ResourceSource; csrf: string | null; writable: boolean; stale: boolean; refreshToken: unknown;
  projects: { id: string; name: string }[]; onUnauthorized: () => void; now?: () => Date;
  /** The signed-in actor, so only their own resources read "You". */
  viewerActor?: string | null;
}
export function StagingResourcesPanel({ mode, source: given, csrf, writable, stale, refreshToken, projects, onUnauthorized, now = () => new Date(), viewerActor }:
  StagingResourcesPanelProps) {
  const source = useMemo(() => given ?? (mode === "demo" ? demoSource() : csrf ? liveSource(csrf) : null), [given, mode, csrf]);
  const [project, setProject] = useState("");
  const [items, setItems] = useState<StagingResource[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "loaded" | "unsupported" | "unconfigured" | "unavailable" | "unauthorized" | "unknown">("loading");
  const [loadingMore, setLoadingMore] = useState(false);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [pinning, setPinning] = useState<StagingResource | null>(null);
  const [rechecking, setRechecking] = useState<HeldJob | null>(null);
  const [previewing, setPreviewing] = useState<{ resource: StagingResource; session: PreviewSession } | null>(null);
  const [cleaning, setCleaning] = useState(false);
  const generation = useRef(0);
  const more = useRef<AbortController | null>(null);
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;
  const projectName = (id: string) => projects.find(item => item.id === id)?.name ?? id;

  function failed(error: unknown) {
    if (isAuthLoss(error)) { setItems([]); setNext(null); setState("unauthorized"); unauthorized.current(); return; }
    setState(error instanceof HubApiError && error.code === "staging_resources_unsupported" ? "unsupported"
      : error instanceof HubApiError && error.code === "staging_not_configured" ? "unconfigured"
      : error instanceof HubApiError && error.status === 503 ? "unavailable" : "unknown");
  }
  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    // A new list cancels any "Show more" page still loading for the old one.
    more.current?.abort(); more.current = null; setLoadingMore(false);
    if (!source) { setItems([]); setState("unauthorized"); return; }
    setState("loading");
    void source.list({ project: project || undefined }, controller.signal).then(page => {
      if (generation.current !== current || controller.signal.aborted) return;
      setItems(page.items); setNext(page.next_after); setState("loaded");
    }).catch(error => {
      if (generation.current !== current || controller.signal.aborted) return;
      setItems([]); setNext(null); failed(error);
    });
    return () => { generation.current++; controller.abort(); };
  }, [source, project, refreshToken, revision]);
  // Service status is read on its own: a failure here says "unknown", it never blocks the list or claims readiness.
  const [status, setStatus] = useState<StagingStatus | "unknown" | "unsupported" | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setStatus(null);
    if (!source) return;
    source.status(controller.signal).then(value => { if (!controller.signal.aborted) setStatus(value); })
      .catch(error => {
        if (controller.signal.aborted) return;
        setStatus(error instanceof HubApiError && error.code === "staging_resources_unsupported" ? "unsupported" : "unknown");
      });
    return () => controller.abort();
  }, [source, refreshToken, revision]);

  async function loadMore() {
    if (!source || !next || loadingMore) return;
    const current = generation.current;
    const controller = new AbortController();
    more.current = controller;
    setLoadingMore(true);
    try {
      const page = await source.list({ project: project || undefined, after: next }, controller.signal);
      if (generation.current !== current || controller.signal.aborted) return;
      setItems(existing => [...existing, ...page.items.filter(item => !existing.some(other => other.id === item.id))]);
      setNext(page.next_after);
    } catch (error) {
      if (generation.current === current && !controller.signal.aborted) {
        if (isAuthLoss(error)) failed(error); else setNotice({ text: "Couldn't load more. Try again.", error: true });
      }
    } finally {
      // Only the request that still owns the slot may release it.
      if (more.current === controller) { more.current = null; setLoadingMore(false); }
    }
  }
  async function keep(resource: StagingResource, request: KeepRequest, done: string) {
    if (!source) return false;
    setBusy(resource.id); setNotice(null);
    try {
      const saved = await source.keep(resource, request);
      setItems(existing => existing.map(item => item.id === saved.id ? saved : item));
      setNotice({ text: done, error: false });
      return true;
    } catch (error) {
      if (isAuthLoss(error)) { failed(error); return false; }
      setNotice({ text: describe(error), error: true });
      if (error instanceof ResourceConflictError || (error instanceof HubApiError && [404, 409].includes(error.status))) setRevision(value => value + 1);
      return false;
    } finally { setBusy(null); }
  }
  async function openPreview(resource: StagingResource) {
    if (!source) return;
    setBusy(resource.id); setNotice(null);
    try {
      setPreviewing({ resource, session: await source.preview(resource) });
    } catch (error) {
      if (isAuthLoss(error)) failed(error);
      else setNotice({ text: describe(error), error: true });
    } finally { setBusy(null); }
  }

  const visible = items.filter(item => item.state !== "deleted");
  const canWrite = writable && !stale && state === "loaded" && source !== null;
  const messages: Record<string, string> = {
    loading: "Reading what staging is keeping…",
    unsupported: "This Hub doesn't have staging resources yet. Nothing is being kept on the staging PC for EH jobs.",
    unconfigured: "The staging service isn't set up yet, so nothing is being kept on the staging PC for EH jobs.",
    unavailable: "The staging PC isn't answering, so what it's keeping is unknown right now.",
    unauthorized: "Staging records cleared. Sign-in or access must be verified again.",
    unknown: "What staging is keeping is unknown. Use Check to retry.",
  };
  return <section className="panel staging-resources" id="staging-resources" aria-label="Staging resources">
    <div className="panel-title"><h2>Kept on staging</h2>
      <span className="panel-action">
        <button className="secondary" type="button" disabled={state === "loading"} onClick={() => setRevision(value => value + 1)}>Check</button>
        <button className="dark-button" type="button" disabled={!canWrite} onClick={() => { setNotice(null); setCleaning(true); }}>Clean up staging…</button>
      </span></div>
    <StatusLine status={status} />
    {status !== null && typeof status === "object" && status.held_jobs.length > 0 &&
      <HeldJobs jobs={status.held_jobs} canRecheck={status.can_reconcile && canWrite} projectName={projectName}
        onRecheck={job => { setNotice(null); setRechecking(job); }} />}
    {mode === "demo" && <p role="status">Demo: illustrative items only. Nothing is read from or removed on the staging PC.</p>}
    {stale && <p role="status">Workspace events are stale, so changes are paused until the Hub reconnects.</p>}
    <div className="filter-bar">
      <label className="field inline-field"><span className="field-label">Project</span>
        <select value={project} onChange={event => { setNotice(null); setProject(event.target.value); }}>
          <option value="">All projects</option>
          {projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>
    </div>
    {notice && <p className={notice.error ? "notice error" : "notice"} role={notice.error ? "alert" : "status"}>{notice.text}</p>}
    {state !== "loaded" && <p role={state === "unknown" || state === "unavailable" ? "alert" : "status"}>{messages[state]}</p>}
    {state === "loaded" && visible.length === 0 && <p>Nothing is being kept{project ? ` for ${projectName(project)}` : ""}.</p>}
    {state === "loaded" && visible.length > 0 && <ul className="resource-list" aria-label="Kept items">
      {visible.map(resource => <ResourceRow key={resource.id} resource={resource} now={now()} viewer={viewerActor} projectName={projectName(resource.project)}
        disabled={!canWrite || busy !== null} busy={busy === resource.id}
        onExtend={() => void keep(resource, { mode: "until", until: extendedUntil(resource, now()), reason: "Kept 7 more days" },
          `${kindLabel(resource.kind)[0].toUpperCase()}${kindLabel(resource.kind).slice(1)} kept 7 more days.`)}
        onPin={() => { setNotice(null); setPinning(resource); }}
        onPreview={() => void openPreview(resource)}
        onRelease={() => void keep(resource, { mode: "none", reason: resource.keep?.mode === "pinned" ? "Unpinned" : "Let it expire" },
          "It will now expire on its normal schedule.")} />)}
    </ul>}
    {next && state === "loaded" && <button className="secondary" type="button" disabled={loadingMore} onClick={() => void loadMore()}>
      {loadingMore ? "Loading…" : "Show more"}</button>}
    <p className="panel-footnote">Sizes come from the staging PC; "size unknown" means it hasn't reported one, not zero.</p>
    {pinning && <PinDialog resource={pinning} onClose={() => setPinning(null)}
      onPin={async reason => { if (await keep(pinning, { mode: "pinned", reason }, "Pinned. It won't be removed until you unpin it.")) setPinning(null); }} />}
    {rechecking && source && <RecheckDialog job={rechecking} source={source} projectName={projectName(rechecking.project)}
      onClose={changed => { setRechecking(null); if (changed) setRevision(value => value + 1); }}
      onUnauthorized={() => { setRechecking(null); failed(new HubApiError(401, "human_session_required")); }} />}
    {previewing && <PreviewDialog resource={previewing.resource} session={previewing.session} projectName={projectName(previewing.resource.project)}
      onClose={() => setPreviewing(null)} />}
    {cleaning && source && <CleanupDialog source={source} project={project || null} projectName={project ? projectName(project) : null}
      lookup={id => items.find(item => item.id === id)} nameOf={projectName}
      onClose={changed => { setCleaning(false); if (changed) setRevision(value => value + 1); }}
      onUnauthorized={() => { setCleaning(false); failed(new HubApiError(401, "human_session_required")); }} />}
  </section>;
}

function HeldJobs({ jobs, canRecheck, projectName, onRecheck }: {
  jobs: HeldJob[]; canRecheck: boolean; projectName: (id: string) => string; onRecheck: (job: HeldJob) => void;
}) {
  return <div className="held-jobs" role="group" aria-label="Held staging jobs">
    <p className="notice"><strong>{jobs.length === 1 ? "1 staging job is" : `${jobs.length} staging jobs are`} on hold.</strong> While a job is on hold
      the staging PC stays reserved, so other work on it waits. Holding is deliberate when the outcome isn't certain.</p>
    <ul className="resource-list">{jobs.map(job => <li key={job.id} className="resource-row" aria-label={`Held job for ${projectName(job.project)}`}>
      <div className="resource-main"><strong>{projectName(job.project)} · {job.recipe}</strong>
        <small>{job.state === "held" ? "On hold" : "Claimed"} at step "{job.phase.replace(/[_:.-]+/g, " ")}" · source {job.source_commit.slice(0, 7)} (as declared)</small></div>
      {canRecheck ? <span className="resource-actions"><button className="secondary small" type="button" onClick={() => onRecheck(job)}>Recheck outcome…</button></span>
        : <small className="resource-actions">Only the Hub owner can recheck held jobs.</small>}
    </li>)}</ul>
  </div>;
}

/** Asks the staging PC what happened to the original operation. Never re-runs it and never forces the machine free. */
function RecheckDialog({ job, source, projectName, onClose, onUnauthorized }: {
  job: HeldJob; source: ResourceSource; projectName: string; onClose: (changed: boolean) => void; onUnauthorized: () => void;
}) {
  const [step, setStep] = useState<"confirm" | "checking" | "done" | "error">("confirm");
  const [text, setText] = useState("");
  const changed = useRef(false);
  async function recheck() {
    setStep("checking");
    try {
      const summary = recheckSummary(await source.recheck(job.id));
      changed.current = true;
      setText(summary.text); setStep("done");
    } catch (error) {
      if (isAuthLoss(error)) { onUnauthorized(); return; }
      setText(describe(error)); setStep("error");
    }
  }
  return <Modal title={`Recheck the held job for ${projectName}?`} onClose={() => onClose(changed.current)}>
    {(step === "confirm" || step === "checking") && <>
      <p>This asks the staging PC what happened to the original <strong>{job.recipe}</strong> job.</p>
      <p className="panel-footnote">It won't run the job again, and it won't force the staging PC free. If the original outcome is confirmed, the hold
        clears; if not, the job stays on hold for an operator to review.</p>
      <footer className="dialog-footer">
        <button type="button" className="secondary" disabled={step === "checking"} onClick={() => onClose(false)}>Cancel</button>
        <button type="button" className="dark-button" disabled={step === "checking"} onClick={() => void recheck()}>
          {step === "checking" ? "Checking…" : "Recheck outcome"}</button>
      </footer></>}
    {(step === "done" || step === "error") && <>
      <p className={step === "error" ? "notice error" : "notice"} role={step === "error" ? "alert" : "status"}>{text}</p>
      <footer className="dialog-footer"><button type="button" className="dark-button" onClick={() => onClose(changed.current)}>Close</button></footer></>}
  </Modal>;
}

function StatusLine({ status }: { status: StagingStatus | "unknown" | "unsupported" | null }) {
  if (status === null || status === "unsupported") return null;
  if (status === "unknown") return <p className="staging-status" role="status">Staging service status is unknown right now.</p>;
  const line = statusLine(status);
  return <div className="staging-status" role="status">
    <Badge tone={line.tone}>{line.tone === "forest" ? "Service reachable" : "Service not ready"}</Badge> <span>{line.text}</span>
    {status.previews_configured === false && <small>Previews can't be opened yet: the separate preview address hasn't been set up.</small>}
  </div>;
}

function ResourceRow({ resource, now, projectName, disabled, busy, viewer, onExtend, onPin, onPreview, onRelease }: {
  resource: StagingResource; now: Date; projectName: string; disabled: boolean; busy: boolean; viewer?: string | null;
  onExtend: () => void; onPin: () => void; onPreview: () => void; onRelease: () => void;
}) {
  const previewable = resource.kind === "preview" && resource.state === "ready";
  const expiry = expiryLabel(resource, now);
  const label = kindLabel(resource.kind);
  return <li className="resource-row" aria-label={`${label} for ${projectName}`}>
    <div className="resource-main">
      <strong>{label[0].toUpperCase() + label.slice(1)}</strong>
      <small>{projectName} · made by {resourceMaker(resource.created_by, viewer)} · {formatBytes(resource.size_bytes)}
        {resource.state !== "ready" ? ` · ${resource.state.replaceAll("_", " ")}` : ""}</small>
      {resource.protected.length > 0 && <small>Kept automatically: {resource.protected.map(item =>
        protectedLabel(item.reason) + (item.ref ? ` (${item.ref})` : "")).join(", ")}.</small>}
    </div>
    <Badge tone={expiry.tone}>{expiry.text}</Badge>
    {(canChangeKeep(resource) || previewable) && <span className="resource-actions">
      {previewable && <button className="secondary small" type="button" disabled={disabled} onClick={onPreview}>Open preview</button>}
      {canChangeKeep(resource) && <>
      <button className="secondary small" type="button" disabled={disabled} onClick={onExtend}>{busy ? "Saving…" : "Keep 7 more days"}</button>
      {resource.keep?.mode !== "pinned" && <button className="secondary small" type="button" disabled={disabled} onClick={onPin}>Pin…</button>}
      {resource.keep && <button className="secondary small" type="button" disabled={disabled} onClick={onRelease}>
        {resource.keep.mode === "pinned" ? "Unpin" : "Let it expire"}</button>}
      </>}
    </span>}
  </li>;
}

/**
 * The preview runs on its own origin in an opaque sandbox: scripts only, no same-origin, forms, popups or
 * top navigation, no referrer and no credentials. The URL was already checked to be a different origin from EH.
 */
function PreviewDialog({ resource, session, projectName, onClose }: {
  resource: StagingResource; session: PreviewSession; projectName: string; onClose: () => void;
}) {
  const until = new Date(session.expires_at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return <Modal title={`Preview for ${projectName}`} size="full" onClose={onClose}>
    <p className="panel-footnote">Runs isolated from the Hub: it can't see your sign-in or the Hub, and can't sign in itself.
      This view works until {until}; reopen it after that. Source {resource.source_commit.slice(0, 7)} (as declared).</p>
    <iframe className="preview-frame" title={`Preview for ${projectName}`} src={session.url} sandbox="allow-scripts"
      referrerPolicy="no-referrer" allow="" {...{ credentialless: "" }} />
  </Modal>;
}

function PinDialog({ resource, onClose, onPin }: { resource: StagingResource; onClose: () => void; onPin: (reason: string) => Promise<void> }) {
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!reason.trim()) return;
    setSaving(true);
    await onPin(reason.trim());
    setSaving(false);
  }
  return <Modal title={`Pin this ${kindLabel(resource.kind)}?`} onClose={onClose}>
    <form onSubmit={submit}>
      <p className="panel-footnote">A pinned item is never removed by cleanup, however old it gets, until someone unpins it.</p>
      <fieldset disabled={saving}>
        <label className="field"><span className="field-label">Why keep it (required)</span>
          <textarea value={reason} rows={3} maxLength={KEEP_REASON_MAX} required autoFocus
            placeholder="e.g. Compare against the next release" onChange={event => setReason(event.target.value)} />
          <small>{reason.length} / {KEEP_REASON_MAX}</small></label>
        <footer className="dialog-footer">
          <button type="button" className="secondary" onClick={onClose}>Cancel</button>
          <button type="submit" className="dark-button" disabled={!reason.trim()}>Pin</button>
        </footer>
      </fieldset>
    </form>
  </Modal>;
}

/** Plan, then confirm, then an honest result. Applying sends only the reviewed plan's ID and digest. */
export function CleanupDialog({ source, project, projectName, lookup, nameOf, onClose, onUnauthorized }: {
  source: ResourceSource; project: string | null; projectName: string | null;
  /** The plan carries IDs only; rows already listed supply the project and kind. */
  lookup: (id: string) => StagingResource | undefined; nameOf: (project: string) => string;
  onClose: (changed: boolean) => void; onUnauthorized: () => void;
}) {
  const where = (id: string) => { const found = lookup(id); return found ? ` · ${nameOf(found.project)}` : ""; };
  const [step, setStep] = useState<"planning" | "review" | "applying" | "done" | "error">("planning");
  const [plan, setPlan] = useState<CleanupPlan | null>(null);
  const [result, setResult] = useState<CleanupResult | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const changed = useRef(false);
  useEffect(() => {
    let live = true;
    setStep("planning"); setPlan(null); setResult(null);
    source.plan(project).then(value => { if (live) { setPlan(value); setStep("review"); } })
      .catch(failure => { if (!live) return; if (isAuthLoss(failure)) onUnauthorized(); else { setError(describe(failure)); setStep("error"); } });
    return () => { live = false; };
  }, [source, project, attempt]);
  async function apply() {
    if (!plan) return;
    if (Date.parse(plan.expires_at) <= Date.now()) { setError(errorText.staging_plan_expired); setStep("error"); return; }
    setStep("applying");
    try {
      const value = await source.apply(plan);
      changed.current = true;
      setResult(value); setStep("done");
    } catch (failure) {
      if (isAuthLoss(failure)) { onUnauthorized(); return; }
      // An unknown outcome may have removed some items, so the list must be re-read either way.
      changed.current = true;
      setError(describe(failure)); setStep("error");
    }
  }
  const summary = plan ? planSummary(plan) : null;
  const scope = projectName ? ` for ${projectName}` : "";
  return <Modal title={`Clean up staging${scope}`} onClose={() => onClose(changed.current)}>
    {step === "planning" && <p role="status">Working out what can be removed…</p>}
    {step === "error" && <>
      <p className="notice error" role="alert">{error}</p>
      <footer className="dialog-footer">
        <button type="button" className="secondary" onClick={() => onClose(changed.current)}>Close</button>
        <button type="button" className="dark-button" onClick={() => setAttempt(value => value + 1)}>Make a new plan</button>
      </footer></>}
    {(step === "review" || step === "applying") && plan && summary && <>
      <p><strong>{summary.remove}</strong></p>
      {summary.keep && <p>{summary.keep}</p>}
      {plan.remove.length > 0 && <details><summary>What will be removed ({plan.remove.length})</summary>
        <ul className="plan-list">{plan.remove.map(item => <li key={item.id}>{kindLabel(item.kind)}{where(item.id)} · {formatBytes(item.size_bytes)} · expired</li>)}</ul></details>}
      {plan.kept.length > 0 && <details><summary>What will be kept ({plan.kept.length})</summary>
        <ul className="plan-list">{plan.kept.map(item => { const found = lookup(item.id);
          return <li key={item.id}>{found ? kindLabel(found.kind) : "item"}{where(item.id)} · {keptLabel(item.why)}</li>; })}</ul></details>}
      <p className="panel-footnote">Removed items keep a record in history: what it was, its size, and who removed it when.</p>
      <footer className="dialog-footer">
        <button type="button" className="secondary" disabled={step === "applying"} onClick={() => onClose(changed.current)}>Cancel</button>
        {plan.remove.length > 0 && <button type="button" className="dark-button" disabled={step === "applying"} onClick={() => void apply()}>
          {step === "applying" ? "Removing…" : `Remove ${plan.remove.length} item${plan.remove.length === 1 ? "" : "s"}`}</button>}
      </footer></>}
    {step === "done" && result && <>
      <p role="status"><strong>{resultSummary(result)}</strong></p>
      {result.failed.length > 0 && <p className="notice error">Items that failed are still on the staging PC and will be tried again at the next cleanup.</p>}
      <footer className="dialog-footer"><button type="button" className="dark-button" onClick={() => onClose(true)}>Done</button></footer></>}
  </Modal>;
}
