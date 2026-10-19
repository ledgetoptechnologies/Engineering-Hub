import { useEffect, useRef, useState } from "react";
import { HubApiError, type HubMode } from "./api";
import { readQueueSnapshot, type QueueSnapshot } from "./workQueueApi";

export interface WorkQueuePanelProps {
  mode: HubMode;
  refreshToken: unknown;
  stale: boolean;
  onUnauthorized: () => void;
}
export function WorkQueuePanel({ mode, refreshToken, stale, onUnauthorized }: WorkQueuePanelProps) {
  const [snapshot, setSnapshot] = useState<QueueSnapshot | null>(null);
  const [state, setState] = useState("loading");
  const [revision, setRevision] = useState(0);
  const generation = useRef(0);
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;
  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    setSnapshot(null);
    if (mode === "demo") {
      setState("demo");
      return () => { generation.current++; controller.abort(); };
    }
    setState("loading");
    void readQueueSnapshot(controller.signal).then(value => {
      if (generation.current === current && !controller.signal.aborted) {
        setSnapshot(value); setState("loaded");
      }
    }).catch(error => {
      if (generation.current !== current || controller.signal.aborted) return;
      setSnapshot(null);
      if (error instanceof HubApiError && [401, 403].includes(error.status)) {
        setState("unauthorized"); unauthorized.current();
      } else setState(error instanceof HubApiError && error.status === 423 ? "unconfigured"
        : error instanceof HubApiError && error.status === 404 ? "unsupported" : "unknown");
    });
    return () => { generation.current++; controller.abort(); };
  }, [mode, refreshToken, revision]);
  const messages: Record<string, string> = {
    loading: "Reading queue status…", demo: "Demo: no live queue is queried or executor readiness asserted.",
    unconfigured: "Software queue is not configured. This does not indicate a running executor.",
    unsupported: "This server does not expose the software queue endpoint.",
    unauthorized: "Queue records cleared. Sign-in or access must be verified again.",
    unknown: "Queue status is unknown. No claim is made about machine availability; use Check to retry.",
  };
  return <section className="panel software-queue" id="software-queue" aria-label="Software validation queue">
    <div className="panel-title"><h2>Software validation queue</h2>
      <span className="panel-action"><button className="secondary" type="button" disabled={mode === "demo" || state === "loading"} onClick={() => setRevision(value => value + 1)}>Check queue</button></span></div>
    <p className="panel-description">Read-only coordination, separate from agent presence. No deployment, approval or execution controls.</p>
    {stale && <p role="status">Workspace events are stale. Queue information is not continuously verified.</p>}
    {state !== "loaded" && <p role={state === "unknown" ? "alert" : "status"}>{messages[state]}</p>}
    {snapshot && <>
      <p>Master pause: {snapshot.master_paused ? "On" : "Off"} · Queue pause: {snapshot.queue_paused ? "On" : "Off"} · Effective pause: {snapshot.paused ? "On" : "Off"}</p>
      <p>Claim admission: {snapshot.claim_admission_state === "unconfigured" ? "Not configured — claims are closed"
        : snapshot.claim_admission_state === "worker_policy" ? "Worker-specific policy — runtime not verified here"
        : "Trusted local composition — runtime not verified here"}.</p>
      <p>Queued: {snapshot.counts.queued ?? 0} · Claimed: {snapshot.counts.claimed ?? 0} · Held: {snapshot.counts.held ?? 0}</p>
      <p className="panel-footnote">Totals cover all jobs; showing the latest {snapshot.jobs.length}, up to 50. Eligibility is advisory, never executor readiness or production authorization.</p>
      {snapshot.jobs.length === 0 && <p>No recorded queue jobs.</p>}
      <div className="project-list">{snapshot.jobs.map(job => <article key={job.id} style={{ padding: "12px 0", borderBottom: "1px solid var(--line)", overflowWrap: "anywhere" }}>
        <strong>{job.action_key} · {job.state}</strong>
        <p>{job.machine_id} · {job.environment} · Job {job.id}</p>
        <p>{job.requires_reconciliation ? "Reconciliation required — machine reservation remains held."
          : job.blocked_reason ? `Blocked: ${job.blocked_reason.replaceAll("_", " ")}`
          : job.eligible ? "Conditionally eligible — admission, ownership and execution proof still required."
          : "Not eligible for a new claim."}</p>
        {job.eligible_requires_worker_admission && <p>Exact worker enrollment must still be checked.</p>}
        <details><summary>Exact task and artifact</summary><p>Task: {job.target.task_id}</p>
          <p>Commit: {job.target.source_commit}</p><p>Artifact: {job.target.artifact_digest}</p></details>
      </article>)}</div>
    </>}
  </section>;
}
