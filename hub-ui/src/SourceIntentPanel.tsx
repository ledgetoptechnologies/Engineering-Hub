import { useEffect, useReducer, useRef, useState } from "react";
import { HubApiError } from "./api";
import { readSourceIntentPage, type SourceIntentPage } from "./sourceIntentApi";

type Reader = (after: string | null, signal: AbortSignal) => Promise<SourceIntentPage>;
type Load = "idle" | "loading" | "loaded" | "failed" | "hidden";
export type SourceIntentState = { load: Load; page: SourceIntentPage | null };
export type SourceIntentAction = { type: "loading" | "failed" | "hidden" } | { type: "loaded"; page: SourceIntentPage };
export function sourceIntentReducer(_state: SourceIntentState, action: SourceIntentAction): SourceIntentState {
  return action.type === "loaded" ? { load: "loaded", page: action.page } : { load: action.type, page: null };
}

/** Owner-only information. A response is local journal evidence, not worker authority. */
export function SourceIntentPanel({ onUnauthorized, read = readSourceIntentPage }: {
  onUnauthorized: () => void; read?: Reader;
}) {
  const [{ load, page }, dispatch] = useReducer(sourceIntentReducer, { load: "idle", page: null });
  const [after, setAfter] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);
  const reader = useRef(read); reader.current = read;
  const unauthorized = useRef(onUnauthorized); unauthorized.current = onUnauthorized;
  useEffect(() => {
    if (!generation) return;
    const controller = new AbortController();
    let timedOut = false;
    let alive = true;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 15_000);
    dispatch({ type: "loading" });
    reader.current(after, controller.signal).then(result => {
      if (!alive || controller.signal.aborted) return;
      if (result.nextCursor !== null && result.nextCursor === after) throw new HubApiError(502, "invalid_source_intent_handoff");
      dispatch({ type: "loaded", page: result });
    }).catch(error => {
      if (!alive || (controller.signal.aborted && !timedOut)) return;
      if (error instanceof HubApiError && error.status === 401) {
        dispatch({ type: "hidden" }); unauthorized.current(); return;
      }
      if (error instanceof HubApiError && (error.status === 403 || error.code === "source_handoff_unsupported")) {
        dispatch({ type: "hidden" }); return;
      }
      dispatch({ type: "failed" });
    }).finally(() => clearTimeout(timer));
    return () => { alive = false; clearTimeout(timer); controller.abort(); };
  }, [after, generation]);
  if (load === "hidden") return null;
  const fetchPage = (cursor: string | null) => { setAfter(cursor); setGeneration(value => value + 1); };
  return <section className="panel" aria-label="Uncertain source deposits">
    <div className="panel-title"><h2>Uncertain source deposits</h2>
      <span className="panel-action"><button className="secondary" type="button" disabled={load === "loading"}
        onClick={() => fetchPage(null)}>{load === "loading" ? "Reading…" : "Check records"}</button></span></div>
    <p className="panel-footnote">Owner-only local records. They do not verify the worker or whether a deposit happened.</p>
    {load === "idle" && <p>Check for requests that need an operator to investigate.</p>}
    {load === "loading" && <p role="status">Reading local records…</p>}
    {load === "failed" && <p role="alert">These records could not be read. Check again when the Hub is available.</p>}
    {load === "loaded" && page && <SourceIntentPageView page={page} onNext={() => fetchPage(page.nextCursor)} />}
  </section>;
}

export function SourceIntentPageView({ page, onNext }: { page: SourceIntentPage; onNext: () => void }) {
  return <>
    <p className="notice">The worker identity and deposit outcome are unverified. Do not retry, release, or start work based on this list.</p>
    {page.items.length === 0 && <p role="status">{page.nextCursor
      ? "No uncertain deposits in this page. More records remain to check."
      : "No uncertain deposits in this page."}</p>}
    {page.items.length > 0 && <ul className="resource-list">{page.items.map(item => <li key={item.submissionId} className="resource-row">
      {item.state === "unreadable" ? <div><strong>Record needs inspection</strong><p>Submission <code>{item.submissionId}</code> cannot be read safely.</p></div>
        : <div><strong>{item.project} · {item.recipe}</strong><p>Deposit outcome unknown · task review is historical, not current.</p>
          <details><summary>Recorded identifiers</summary><dl>
            <dt>Submission</dt><dd><code>{item.submissionId}</code></dd>
            <dt>Request</dt><dd><code>{item.requestId}</code> · {item.actor}</dd>
            <dt>Source, from journal only</dt><dd><code>{item.sourceId}</code></dd>
            <dt>Reserved command, not proof of dispatch</dt><dd><code>{item.commandId}</code></dd>
            <dt>Reviewed task</dt><dd><code>{item.taskId}</code> · revision {item.taskRevision}</dd>
            <dt>Source commit</dt><dd><code>{item.sourceCommit}</code></dd>
            <dt>Source digest</dt><dd><code>{item.sourceDigest}</code></dd>
            <dt>Git tree</dt><dd><code>{item.treeOid}</code></dd>
            <dt>Commit object digest</dt><dd><code>{item.commitObjectDigest}</code></dd>
            <dt>Archive contents</dt><dd>{item.entries} entries · {item.expandedBytes} expanded bytes</dd>
            <dt>Profile</dt><dd>generation {item.profileGeneration} · <code>{item.profileDigest}</code></dd>
          </dl></details></div>}
    </li>)}</ul>}
    {page.nextCursor && <button className="secondary" type="button" onClick={onNext}>Check next page</button>}
  </>;
}
