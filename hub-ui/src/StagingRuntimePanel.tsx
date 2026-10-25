/**
 * What's running on the staging PC, for the Hub owner. Read-only: no start, stop or remove,
 * and an observation is never shown as readiness. Refreshes only when asked.
 */
import { useEffect, useRef, useState } from "react";
import { HubApiError, type HubMode } from "./api";
import {
  ago, containerStateLabel, containerTone, countLine, imageLabel, readStagingRuntime, sourceViews,
  type RuntimeSnapshot, type SourceView, type StagingRuntime,
} from "./stagingRuntimeApi";
import { createDemoRuntime } from "./stagingRuntimeDemo";
import { Badge } from "./ui";

export type RuntimeReader = (signal: AbortSignal) => Promise<StagingRuntime>;
type Load = "loading" | "loaded" | "failed" | "hidden";

export interface StagingRuntimePanelProps { mode: HubMode; read?: RuntimeReader; onUnauthorized: () => void; now?: () => Date }
export function StagingRuntimePanel({ mode, read: given, onUnauthorized, now = () => new Date() }: StagingRuntimePanelProps) {
  const read = given ?? (mode === "demo" ? async () => createDemoRuntime() : readStagingRuntime);
  const [load, setLoad] = useState<Load>("loading");
  const [runtime, setRuntime] = useState<StagingRuntime | null>(null);
  const [failure, setFailure] = useState("");
  const [attempt, setAttempt] = useState(0);
  const reader = useRef(read);
  reader.current = read;
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;

  useEffect(() => {
    const controller = new AbortController();
    setLoad("loading");
    reader.current(controller.signal).then(value => {
      if (controller.signal.aborted) return;
      setRuntime(value); setLoad("loaded");
    }).catch(error => {
      if (controller.signal.aborted) return;
      if (error instanceof HubApiError && error.status === 401) { setRuntime(null); setLoad("hidden"); unauthorized.current(); return; }
      // Only the Hub owner may see this, and older Hubs don't have it: either way the panel steps aside.
      if (error instanceof HubApiError && (error.status === 403 || error.code === "staging_runtime_unsupported")) {
        setRuntime(null); setLoad("hidden"); return;
      }
      // A failed refresh keeps the earlier observation on screen, labelled with when it was seen.
      setFailure(error instanceof HubApiError && error.status === 503
        ? "The staging PC isn't answering, so what's running on it is unknown right now."
        : "What's running on the staging PC couldn't be read. Use Refresh to try again.");
      setLoad("failed");
    });
    return () => controller.abort();
  }, [mode, attempt]);

  if (load === "hidden") return null;
  const snapshot = runtime?.snapshot ?? null;
  return <section className="panel staging-runtime" id="staging-runtime" aria-label="Running on the staging PC">
    <div className="panel-title"><h2>Running on the staging PC</h2>
      <span className="panel-action">
        <button className="secondary" type="button" disabled={load === "loading"} onClick={() => setAttempt(value => value + 1)}>
          {load === "loading" ? "Reading…" : "Refresh"}</button>
      </span></div>
    <p className="panel-description">What the staging PC reported when it was last checked. This is an observation only:
      it doesn't mean the PC is ready for work, and nothing can be started or stopped from here.</p>
    {mode === "demo" && <p role="status">Demo: illustrative programs only. Nothing is read from the staging PC.</p>}
    {load === "loading" && !runtime && <p role="status">Reading what's running on the staging PC…</p>}
    {load === "failed" && <p className="notice error" role="alert">{failure}
      {snapshot && ` Showing what was seen ${ago(snapshot.observed_at, now())}.`}</p>}
    {runtime && <RuntimeBody runtime={runtime} now={now()} />}
  </section>;
}

function RuntimeBody({ runtime, now }: { runtime: StagingRuntime; now: Date }) {
  const { snapshot } = runtime;
  const lead = !runtime.configured || runtime.state === "not_configured"
    ? "Watching the staging PC isn't set up yet, so what runs on it isn't shown."
    : runtime.state === "unavailable"
      ? `The staging PC isn't answering${snapshot ? `; below is what was seen ${ago(snapshot.observed_at, now)}, which may have changed` : ", so what's running on it is unknown"}.`
      : null;
  return <>
    {lead && <p className="staging-status" role="status">{lead}</p>}
    {snapshot && <Snapshot snapshot={snapshot} now={now} />}
  </>;
}

function Snapshot({ snapshot, now }: { snapshot: RuntimeSnapshot; now: Date }) {
  return <div className="runtime-sources">
    <p className="panel-footnote">Checked <time dateTime={snapshot.observed_at} title={new Date(snapshot.observed_at).toLocaleString()}>
      {ago(snapshot.observed_at, now)}</time>.</p>
    {sourceViews(snapshot, now).map(view => <SourceSection key={view.id} view={view} />)}
  </div>;
}

function SourceSection({ view }: { view: SourceView }) {
  const empty = view.containers.length === 0 && view.services.length === 0;
  return <section className="runtime-source" aria-label={view.title}>
    <div className="runtime-source-head">
      <div><h3>{view.title}</h3><small>{view.description}</small></div>
      {view.listed && <Badge tone={view.tone === "forest" ? "neutral" : view.tone}>{countLine(view.containers, view.services)}</Badge>}
    </div>
    <p className={view.tone === "amber" ? "runtime-source-line amber" : "runtime-source-line"}>{view.line}</p>
    {view.listed && empty && <p className="runtime-empty">Nothing was reported here.</p>}
    {view.listed && !empty && <ul className="resource-list" aria-label={`${view.title}: programs`}>
      {view.containers.map(item => <li key={item.id} className="resource-row" aria-label={item.name}>
        <div className="resource-main"><strong>{item.name}</strong><small>Container · {imageLabel(item.image)}</small></div>
        <Badge tone={containerTone(item.state)}>{containerStateLabel(item.state)}</Badge>
      </li>)}
      {view.services.map(item => <li key={`service:${item.name}`} className="resource-row" aria-label={item.name}>
        <div className="resource-main"><strong>{item.name}</strong><small>Service</small></div>
        <Badge tone="forest">Running</Badge>
      </li>)}
    </ul>}
  </section>;
}
