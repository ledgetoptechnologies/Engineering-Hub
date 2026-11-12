import { useCallback, useEffect, useState, type FormEvent } from "react";
import { HubApiError } from "./api";
import { safeHref } from "./markdown";

type Evidence = { label: string; url: string };
export type TrackingTask = { id: string; project: string; title: string; status: "open" | "completion_claimed" | "completion_verified";
  revision: number;
  details?: string; links?: Evidence[]; created_by: string; execution_authorization: false;
  updates: { actor: string; progress: string; evidence_links: Evidence[]; completion_claim: boolean }[];
  reviews?: { actor: string; verdict: "verify" | "reopen"; note: string; evidence_links: Evidence[] }[] };
type Page = { project: string; tasks: TrackingTask[]; truncated: boolean; next_after: string | null };
export async function readTracking(project: string, after: string | null, signal: AbortSignal, unfinished = false): Promise<Page> {
  if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(project)) throw new HubApiError(422, "invalid_project");
  const params = new URLSearchParams({ limit: "50" });
  if (after) params.set("after_task_id", after);
  if (unfinished) params.set("unfinished", "true");
  const response = await fetch(`/api/hub/v1/projects/${project}/tracking-tasks?${params}`, {
    credentials: "same-origin", cache: "no-store", redirect: "error", headers: { Accept: "application/json" }, signal,
  });
  if (!response.ok) throw new HubApiError(response.status, "tracking_unavailable");
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) throw new HubApiError(502, "tracking_unavailable");
  const page = await response.json() as Page;
  if (page.project !== project || !Array.isArray(page.tasks) || page.tasks.length > 50 || typeof page.truncated !== "boolean"
      || (page.truncated ? typeof page.next_after !== "string" : page.next_after !== null)
      || page.tasks.some(task => task.project !== project || task.execution_authorization !== false
        || !Number.isSafeInteger(task.revision) || task.revision < 1
        || !["open", "completion_claimed", "completion_verified"].includes(task.status) || !Array.isArray(task.updates))) {
    throw new HubApiError(502, "tracking_unavailable");
  }
  return page;
}
function Links({ links }: { links?: Evidence[] }) {
  if (!links?.length) return null;
  return <ul>{links.map((link, index) => { const href = safeHref(link.url);
    return <li key={index}>{href ? <a href={href} target="_blank" rel="noopener noreferrer">{link.label}</a> : link.label}</li>;
  })}</ul>;
}
function ReviewForm({ task, csrf, onSaved }: { task: TrackingTask; csrf: string; onSaved: () => void }) {
  const [note, setNote] = useState("");
  const [checked, setChecked] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [key] = useState(() => crypto.randomUUID());
  const verifying = task.status === "completion_claimed";
  const evidence = task.updates.at(-1)?.evidence_links ?? [];
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!note.trim() || (verifying && (!checked || !evidence.length))) return;
    setBusy(true); setError("");
    try {
      const response = await fetch(`/api/hub/v1/tracking-tasks/${task.id}/review`, {
        method: "POST", credentials: "same-origin", redirect: "error",
        headers: {"Content-Type": "application/json", "X-Hub-CSRF": csrf},
        body: JSON.stringify({expected_revision: task.revision, client_request_id: key,
          verdict: verifying ? "verify" : "reopen", note: note.trim(), evidence_links: verifying ? evidence : []}),
      });
      if (!response.ok) throw new Error();
      onSaved();
    } catch { setError("Review could not be saved. Refresh to check the current task before trying again."); }
    finally { setBusy(false); }
  }
  return <details><summary>{verifying ? "Review completion claim" : "Reopen task"}</summary>
    <form onSubmit={submit}><p>{verifying ? "Review the linked evidence before verifying this exact claim." : "Reopen this task and retain the review history."}</p>
      <label>Review note <textarea required maxLength={4096} value={note} onChange={event => setNote(event.target.value)} /></label>
      {verifying && <label><input type="checkbox" checked={checked} onChange={event => setChecked(event.target.checked)} /> I reviewed the linked evidence</label>}
      <button type="submit" disabled={busy || !note.trim() || (verifying && (!checked || !evidence.length))}>{verifying ? "Verify completion" : "Reopen"}</button>
      {error && <p role="alert">{error}</p>}
    </form></details>;
}
export function TrackingItems({ tasks, csrf, onSaved }: { tasks: TrackingTask[]; csrf?: string | null; onSaved?: () => void }) {
  return <>{tasks.map(task => <article key={task.id} style={{borderTop:"1px solid var(--line)", padding:"12px 0", overflowWrap:"anywhere"}}>
        <h3>{task.title}</h3><p>{task.status === "completion_claimed" ? "Completion claimed — unverified" : task.status === "completion_verified" ? "Completion verified by reviewer" : "Open"} · Reported by {task.created_by}</p>
        {task.details && <p>{task.details}</p>}<Links links={task.links} />
        {task.updates.map((update, index) => <div key={index}><p><strong>{update.actor}</strong>{update.completion_claim && " · Completion claimed — unverified"}: {update.progress}</p><Links links={update.evidence_links} /></div>)}
        {task.reviews?.map((review, index) => <div key={index}><p><strong>{review.actor}</strong> · {review.verdict === "verify" ? "Verified claim" : "Reopened"}: {review.note}</p><Links links={review.evidence_links} /></div>)}
        {csrf && onSaved && task.status !== "open" && <ReviewForm task={task} csrf={csrf} onSaved={onSaved} />}
      </article>)}</>;
}
export function ProjectTrackingPanel({ project, demo, csrf, onUnauthorized }: { project: string; demo: boolean; csrf?: string | null; onUnauthorized: () => void }) {
  const [page, setPage] = useState<Page | null>(null);
  const [state, setState] = useState<"loading" | "loaded" | "error">("loading");
  const [cursor, setCursor] = useState<string | null>(null);
  const [unfinished, setUnfinished] = useState(true);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => { setPage(null); setCursor(null); setRevision(n => n + 1); }, []);
  useEffect(() => {
    const controller = new AbortController();
    setPage(null); setState("loading");
    if (demo) { setState("loaded"); return () => controller.abort(); }
    readTracking(project, cursor, controller.signal, unfinished).then(value => { if (!controller.signal.aborted) { setPage(value); setState("loaded"); } })
      .catch(error => { if (controller.signal.aborted) return; if (error instanceof HubApiError && [401,403].includes(error.status)) onUnauthorized(); else setState("error"); });
    return () => controller.abort();
  }, [project, cursor, revision, demo, onUnauthorized, unfinished]);
  return <section className="panel" aria-label="Project tracking">
    <h2>Project tracking</h2>
    <p>Agent-reported progress and evidence. These records do not authorize execution; each completion claim requires a separate review.</p>
    {!demo && <label>Show <select aria-label="Tracking records" value={unfinished ? "unfinished" : "all"} onChange={event => {
      setPage(null); setCursor(null); setUnfinished(event.target.value === "unfinished");
    }}><option value="unfinished">Needs work or review</option><option value="all">All tracking records</option></select></label>}
    {state === "loading" && <p role="status">Loading project tracking…</p>}
    {state === "error" && <p role="alert">Project tracking could not be loaded. <button type="button" onClick={refresh}>Retry</button></p>}
    {state === "loaded" && demo && <p>Tracking is unavailable in demo mode.</p>}
    {page && <>{page.tasks.length === 0 && <p>No tracking records on this page.</p>}
      <TrackingItems tasks={page.tasks} csrf={csrf} onSaved={refresh} />
      {page.truncated && <button type="button" onClick={() => setCursor(page.next_after)}>Next page</button>}
      {cursor && <button type="button" onClick={refresh}>First page</button>}
    </>}
  </section>;
}
