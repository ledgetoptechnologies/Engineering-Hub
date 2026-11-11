import { useEffect, useRef, useState } from "react";
import { HubApiError } from "./api";
import { link } from "./router";
import { readQueueSnapshot, type QueueSnapshot } from "./workQueueApi";
import type { Session } from "./types";

/** The same authenticated, read-only projection used by the software queue. */
export function HeldQueueInbox({ enabled, session, stale, refreshToken, onUnauthorized }: {
  enabled: boolean; session: Session | null; stale: boolean; refreshToken: unknown; onUnauthorized: () => void;
}) {
  const [saved, setSnapshot] = useState<{ session: Session | null; value: QueueSnapshot } | null>(null);
  const snapshot = saved?.session === session ? saved.value : null;
  const [state, setState] = useState("loading");
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;
  useEffect(() => {
    const controller = new AbortController();
    if (!enabled) {
      setSnapshot(null);
      setState("loading");
      return () => controller.abort();
    }
    setState("checking");
    void readQueueSnapshot(controller.signal).then(value => {
      if (!controller.signal.aborted) { setSnapshot({ session, value }); setState("loaded"); }
    }).catch(error => {
      if (controller.signal.aborted) return;
      setSnapshot(null);
      if (error instanceof HubApiError && [401, 403].includes(error.status)) {
        setState("unauthorized"); unauthorized.current();
      } else setState(error instanceof HubApiError && error.status === 423 ? "unconfigured" : "unknown");
    });
    return () => controller.abort();
  }, [enabled, session, refreshToken]);
  if (!enabled) return null;
  if (saved && saved.session !== session) return null;
  if (state === "unconfigured" || ((state === "loading" || state === "checking") && !snapshot)
      || (!stale && snapshot && (snapshot.counts.held ?? 0) === 0)) return null;
  const held = snapshot?.jobs.filter(job => job.state === "held") ?? [];
  return <section className="panel" aria-label="Held software queue jobs">
    <h2>Held software queue jobs</h2>
    {stale ? <p role="status">Workspace updates are stale. Check the software queue for current status.</p>
      : !snapshot ? <p role="status">Queue status is unavailable.</p>
      : <>
        <p>{state === "checking" ? "Checking again; last check" : "Held at this check"}: {snapshot.counts.held ?? 0}.</p>
        <p>All projects; held jobs among the latest {snapshot.jobs.length}, up to 50. Other jobs may also require reconciliation.</p>
        {held.length > 0 && <ul>{held.map(job => <li key={job.id}>Job {job.id} · {job.machine_id} · held</li>)}</ul>}
        {(snapshot.counts.held ?? 0) > held.length && <p>Some held jobs are outside the displayed recent jobs.</p>}
      </>}
    <a href={link("/staging")}>Open software queue</a>
  </section>;
}
