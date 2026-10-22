import { useEffect, useState } from "react";
import { imageVersionsView } from "./imageVersions";
import { Badge } from "./ui";

function observed(value: string | null) {
  return value === null ? "Not observed" : new Date(value).toLocaleString();
}

/** No fetching or mutation: overview refresh supplies observations; this timer only expires them. */
export function ImageVersionsPanel({ summary, stale }: { summary: unknown; stale: boolean }) {
  const [clock, setClock] = useState(Date.now);
  const now = Math.max(clock, Date.now());
  const view = imageVersionsView(summary, stale, now);
  useEffect(() => {
    const refreshClock = () => setClock(Date.now());
    const deadline = view.nextExpiry;
    const timer = deadline === null ? undefined : window.setTimeout(refreshClock, Math.max(1, deadline - Date.now() + 1));
    document.addEventListener("visibilitychange", refreshClock);
    window.addEventListener("pageshow", refreshClock);
    window.addEventListener("focus", refreshClock);
    return () => {
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", refreshClock);
      window.removeEventListener("pageshow", refreshClock);
      window.removeEventListener("focus", refreshClock);
    };
  }, [summary, stale, view.nextExpiry]);
  return (
    <section className="panel" aria-label="Production image versions">
      <div className="panel-title"><h2>Production images</h2><Badge>{view.label}</Badge></div>
      <p className="panel-description">{view.message}</p>
      {view.reportedBy !== null && (
        <p className="small-text">Agent report by {view.reportedBy} · Updated {observed(view.reportUpdatedAt)}. Not independent host attestation.</p>
      )}
      {view.rows.length > 0 && (
        <ul className="preview-list">
          {view.rows.map(row => (
            <li key={JSON.stringify([row.project, row.host, row.service])}>
              <div>
                <div className="preview-title"><strong>{row.host} / {row.service}</strong></div>
                <div className="preview-meta">{row.project} · {row.repository}{row.reference.startsWith("sha256:") ? "@" : ":"}{row.reference}</div>
                <div className="preview-meta">Running observed: {observed(row.runningObservedAt)} · Published observed: {observed(row.publishedObservedAt)}</div>
              </div>
              <Badge tone={row.tone}>{row.label}</Badge>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
