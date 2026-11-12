import { useCallback, useEffect, useState } from "react";
import { HubApiError } from "./api";
import { safeHref } from "./markdown";

type Evidence = { label: string; url: string };
export type TrackingTask = { id: string; project: string; title: string; status: "open" | "completion_claimed";
  details?: string; links?: Evidence[]; created_by: string; execution_authorization: false;
  updates: { actor: string; progress: string; evidence_links: Evidence[]; completion_claim: boolean }[] };
type Page = { project: string; tasks: TrackingTask[]; truncated: boolean; next_after: string | null };
export async function readTracking(project: string, after: string | null, signal: AbortSignal): Promise<Page> {
  if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(project)) throw new HubApiError(422, "invalid_project");
  const params = new URLSearchParams({ limit: "50" });
  if (after) params.set("after_task_id", after);
  const response = await fetch(`/api/hub/v1/projects/${project}/tracking-tasks?${params}`, {
    credentials: "same-origin", cache: "no-store", redirect: "error", headers: { Accept: "application/json" }, signal,
  });
  if (!response.ok) throw new HubApiError(response.status, "tracking_unavailable");
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) throw new HubApiError(502, "tracking_unavailable");
  const page = await response.json() as Page;
  if (page.project !== project || !Array.isArray(page.tasks) || page.tasks.length > 50 || typeof page.truncated !== "boolean"
      || (page.truncated ? typeof page.next_after !== "string" : page.next_after !== null)
      || page.tasks.some(task => task.project !== project || task.execution_authorization !== false
        || !["open", "completion_claimed"].includes(task.status) || !Array.isArray(task.updates))) {
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
export function TrackingItems({ tasks }: { tasks: TrackingTask[] }) {
  return <>{tasks.map(task => <article key={task.id} style={{borderTop:"1px solid var(--line)", padding:"12px 0", overflowWrap:"anywhere"}}>
        <h3>{task.title}</h3><p>{task.status === "completion_claimed" ? "Completion claimed — unverified" : "Open"} · Reported by {task.created_by}</p>
        {task.details && <p>{task.details}</p>}<Links links={task.links} />
        {task.updates.map((update, index) => <div key={index}><p><strong>{update.actor}</strong>{update.completion_claim && " · Completion claimed — unverified"}: {update.progress}</p><Links links={update.evidence_links} /></div>)}
      </article>)}</>;
}
export function ProjectTrackingPanel({ project, demo, onUnauthorized }: { project: string; demo: boolean; onUnauthorized: () => void }) {
  const [page, setPage] = useState<Page | null>(null);
  const [state, setState] = useState<"loading" | "loaded" | "error">("loading");
  const [cursor, setCursor] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => { setPage(null); setCursor(null); setRevision(n => n + 1); }, []);
  useEffect(() => {
    const controller = new AbortController();
    setPage(null); setState("loading");
    if (demo) { setState("loaded"); return () => controller.abort(); }
    readTracking(project, cursor, controller.signal).then(value => { if (!controller.signal.aborted) { setPage(value); setState("loaded"); } })
      .catch(error => { if (controller.signal.aborted) return; if (error instanceof HubApiError && [401,403].includes(error.status)) onUnauthorized(); else setState("error"); });
    return () => controller.abort();
  }, [project, cursor, revision, demo, onUnauthorized]);
  return <section className="panel" aria-label="Project tracking">
    <h2>Project tracking</h2>
    <p>Agent-reported progress and evidence. These records do not authorize execution; completion claims have not been verified.</p>
    {state === "loading" && <p role="status">Loading project tracking…</p>}
    {state === "error" && <p role="alert">Project tracking could not be loaded. <button type="button" onClick={refresh}>Retry</button></p>}
    {state === "loaded" && demo && <p>Tracking is unavailable in demo mode.</p>}
    {page && <>{page.tasks.length === 0 && <p>No tracking records on this page.</p>}
      <TrackingItems tasks={page.tasks} />
      {page.truncated && <button type="button" onClick={() => setCursor(page.next_after)}>Next page</button>}
      {cursor && <button type="button" onClick={refresh}>First page</button>}
    </>}
  </section>;
}
