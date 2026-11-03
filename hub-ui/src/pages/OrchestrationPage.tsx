/**
 * Orchestration: Beau's requests and the per-role dispatches an orchestrator split them into.
 * Read-only and demo-only for now (see orchestrationEnabled). Agent-written text is rendered as
 * plain React text; nothing here uses markdown or HTML.
 */
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { HubMode } from "../api";
import { relativeAge } from "../dashboardText";
import type { Hub } from "../hub";
import {
  checklistPercent, FINISHED_STATES, formatEta, isAgentCreated, isStale, liveOrchestrationReader, orchestrationEnabled,
  type Dispatch, type DispatchState, type OrchestrationReader, type RequestDetail, type RequestSummary, type RoleLease,
} from "../orchestrationApi";
import { demoOrchestrationReader } from "../orchestrationDemo";
import { href } from "../router";
import { Badge, EmptyState, PageHeader, PanelTitle } from "../ui";

const STATE_TEXT: Record<DispatchState, { label: string; tone: "forest" | "amber" | "neutral" }> = {
  dispatched: { label: "Dispatched", tone: "neutral" },
  working: { label: "Working", tone: "forest" },
  blocked: { label: "Blocked", tone: "amber" },
  asked: { label: "Asked Beau", tone: "amber" },
  done: { label: "Done", tone: "forest" },
  failed: { label: "Failed", tone: "amber" },
  stale: { label: "Stale", tone: "amber" },
};

export function ProgressBar({ label, percent }: { label: string; percent: number | null }) {
  const valueText = percent === null ? "No checklist yet" : `${percent}% done`;
  return (
    <div className="machine-meter-track orchestration-progress" role="progressbar" aria-label={label}
      aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined} aria-valuetext={valueText}>
      {percent !== null && <span style={{ width: `${percent}%` }} />}
    </div>
  );
}

export function RequestListView({ items, truncated, selected, now = Date.now() }: {
  items: RequestSummary[]; truncated: boolean; selected: string | null; now?: number;
}) {
  if (!items.length) return <EmptyState title="No requests yet">When you ask the orchestrator for something, it appears here.</EmptyState>;
  return (
    <>
      <ul className="orchestration-requests">
        {items.map(item => (
          <li key={item.request_id}>
            <a className={item.request_id === selected ? "orchestration-request selected" : "orchestration-request"}
              href={href({ page: "orchestration-request", id: item.request_id })}
              aria-current={item.request_id === selected ? "page" : undefined}>
              <strong>{item.title}</strong>
              <small>
                {item.done_count} of {item.dispatch_count} done
                {" · updated "}{relativeAge(item.updated_at, now) ?? "at an unknown time"}
              </small>
              <span className="orchestration-badges">
                {item.waiting_decision_count > 0 && <Badge tone="amber">{item.waiting_decision_count} waiting on you</Badge>}
                {item.stale_count > 0 && <Badge tone="amber">{item.stale_count} stale</Badge>}
              </span>
            </a>
          </li>
        ))}
      </ul>
      {truncated && <p className="panel-footnote">Showing the most recent requests only.</p>}
    </>
  );
}

function leaseText(lease: RoleLease | undefined): ReactNode {
  return lease?.lease_holder ? <span className="muted">Held by {lease.lease_holder}</span> : <Badge tone="amber">Unstaffed</Badge>;
}

export function DispatchRow({ dispatch, lease, now = Date.now() }: { dispatch: Dispatch; lease: RoleLease | undefined; now?: number }) {
  const { task } = dispatch;
  const percent = checklistPercent(task.checklist);
  const state = STATE_TEXT[dispatch.state];
  const finished = FINISHED_STATES.includes(dispatch.state);
  const stale = isStale(dispatch);
  return (
    <li className="orchestration-dispatch">
      <div className="orchestration-dispatch-head">
        <strong className="orchestration-role">{dispatch.role}</strong>
        <Badge tone={state.tone}>{state.label}</Badge>
        {stale && dispatch.state !== "stale" && <Badge tone="amber">Stale</Badge>}
        {isAgentCreated(task.created_by) && <Badge>Agent-created</Badge>}
        <span className="orchestration-lease">{leaseText(lease)}</span>
      </div>
      <p className="orchestration-task">{task.title}</p>
      <div className="machine-meter">
        <div className="machine-meter-label">
          <span>{percent === null ? "No checklist yet" : `${percent}%`}</span>
          <span>{task.checklist.filter(item => item.done).length} of {task.checklist.length} steps</span>
        </div>
        <ProgressBar label={`${dispatch.role} progress`} percent={percent} />
      </div>
      {!finished && (
        <p className="orchestration-eta">
          ETA {formatEta(task.eta_seconds)}{stale && task.eta_seconds !== null ? " (may be out of date)" : ""}
          {task.agent_eta_guess_seconds !== null && <span className="muted"> · agent guess: {formatEta(task.agent_eta_guess_seconds)}</span>}
        </p>
      )}
      {task.status_line && <p className="orchestration-status">{task.status_line}</p>}
      {task.blocked_on && <p className="orchestration-blocked">Blocked on: {task.blocked_on}</p>}
      <p className="machine-footnote">
        Updated {relativeAge(task.updated_at, now) ?? "at an unknown time"} · created by {task.created_by.id}
      </p>
    </li>
  );
}

export function RequestDetailView({ detail, now = Date.now() }: { detail: RequestDetail; now?: number }) {
  const leases = new Map(detail.roles.map(role => [role.role, role]));
  const projects = new Map(detail.projects.map(project => [project.id, project]));
  const byProject = [...new Set(detail.dispatches.map(item => item.task.project))];
  return (
    <section className="panel orchestration-detail" aria-label={detail.request.title}>
      <PanelTitle title={detail.request.title} count={detail.dispatches.length} />
      {detail.decisions.length > 0 && (
        <div className="orchestration-decisions" role="region" aria-label="Decisions waiting on you">
          <h3>Waiting on you</h3>
          <ul>
            {detail.decisions.map(item => (
              <li key={item.decision_id}>
                <span>{item.question}</span>
                <small className="muted"> · asked by {item.asked_by_role} {relativeAge(item.asked_at, now) ?? ""}</small>
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="panel-description">
        {byProject.map((id, index) => {
          const project = projects.get(id);
          return (
            <span key={id}>
              {index > 0 && " · "}
              {project?.name ?? id}
              {project && isAgentCreated(project.created_by) && <> <Badge>Agent-created project</Badge></>}
            </span>
          );
        })}
      </p>
      {detail.dispatches.length
        ? <ul className="orchestration-dispatches">
            {detail.dispatches.map(item => <DispatchRow key={item.dispatch_id} dispatch={item} lease={leases.get(item.role)} now={now} />)}
          </ul>
        : <EmptyState title="Not split up yet">The orchestrator hasn't dispatched any roles for this request.</EmptyState>}
    </section>
  );
}

type Load<T> = { state: "loading" } | { state: "loaded"; value: T } | { state: "failed" };

/** The gated body: in live mode it renders a notice and never reads anything. */
export function OrchestrationView({ mode, requestId, reader }: { mode: HubMode; requestId: string | null; reader?: OrchestrationReader }) {
  if (!orchestrationEnabled(mode))
    return <p className="notice">Orchestration isn't available in the live dashboard yet. It can be previewed in the demo.</p>;
  return <OrchestrationLoader requestId={requestId} reader={reader ?? (mode === "demo" ? demoOrchestrationReader : liveOrchestrationReader)} />;
}

function OrchestrationLoader({ requestId, reader }: { requestId: string | null; reader: OrchestrationReader }) {
  const current = useRef(reader);
  current.current = reader;
  const [list, setList] = useState<Load<{ items: RequestSummary[]; truncated: boolean }>>({ state: "loading" });
  const [detail, setDetail] = useState<Load<RequestDetail> | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    current.current.list(controller.signal).then(value => { if (!controller.signal.aborted) setList({ state: "loaded", value }); },
      () => { if (!controller.signal.aborted) setList({ state: "failed" }); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!requestId) { setDetail(null); return; }
    const controller = new AbortController();
    setDetail({ state: "loading" });
    current.current.read(requestId, controller.signal).then(value => { if (!controller.signal.aborted) setDetail({ state: "loaded", value }); },
      () => { if (!controller.signal.aborted) setDetail({ state: "failed" }); });
    return () => controller.abort();
  }, [requestId]);
  return (
    <div className="orchestration-layout">
      <section className="panel" aria-label="Requests">
        <PanelTitle title="Requests" count={list.state === "loaded" ? list.value.items.length : undefined} />
        {list.state === "loading" && <p className="small-text">Loading requests…</p>}
        {list.state === "failed" && <p className="notice error" role="alert">Requests couldn't be read.</p>}
        {list.state === "loaded" && <RequestListView items={list.value.items} truncated={list.value.truncated} selected={requestId} />}
      </section>
      {detail === null && <p className="small-text">Pick a request to see its dispatches.</p>}
      {detail?.state === "loading" && <p className="small-text">Loading request…</p>}
      {detail?.state === "failed" && <p className="notice error" role="alert">This request couldn't be read.</p>}
      {detail?.state === "loaded" && <RequestDetailView detail={detail.value} />}
    </div>
  );
}

export function OrchestrationPage({ hub, actions, requestId = null }: { hub: Hub; actions: ReactNode; requestId?: string | null }) {
  return (
    <>
      <PageHeader eyebrow="ORCHESTRATION · DEMO" title="Requests in flight"
        subtitle="Each request, split into one dispatch per role. Read-only; sample data until the backend exists.">{actions}</PageHeader>
      <OrchestrationView mode={hub.demo ? "demo" : "live"} requestId={requestId} />
    </>
  );
}
