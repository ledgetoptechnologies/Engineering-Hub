import { useEffect, useRef, useState } from "react";
import { HubApiError } from "./api";
import { decideSessionReconciliation, readSessionReconciliation, type SessionReconciliationSnapshot } from "./sessionReconciliationApi";

export function sessionReconciliationError(error: unknown): string {
  const status = error instanceof HubApiError ? error.status : 0;
  if (status === 403) return "Only the designated staging operator may review this session. Your current account cannot accept it.";
  if (status === 404) return "Staging session review is not configured on this Hub. No session has been opened here.";
  if (status === 410) return "This staging session is closed or expired. A new review must be prepared by the trusted launcher.";
  if (status === 409) return "This review changed or was already answered. Refresh to read its current state before making a decision.";
  if (status === 401) return "Sign in to the Hub to review the staging session.";
  return "The session state could not be confirmed. Refresh before trying again; do not assume your decision was applied.";
}

/** Presentation is exported so tests can inspect the exact human boundary without simulating live inventory. */
export function SessionReconciliationView({ snapshot, remainingSeconds, busy, error, csrfAvailable, readOnlyReason, onRefresh, onDecide }:
  { snapshot: SessionReconciliationSnapshot | null; remainingSeconds: number; busy: boolean; error: string | null; csrfAvailable: boolean;
    readOnlyReason?: string; onRefresh: () => void; onDecide: (action: "accept" | "decline") => void }) {
  const expired = snapshot !== null && remainingSeconds <= 0;
  const state = expired ? "Closed" : snapshot?.state === "accepted" ? "Accepted" : snapshot ? "Pending review" : "Not confirmed";
  const canDecide = snapshot?.state === "pending" && !expired && !busy && !error && csrfAvailable;
  return <section className="panel session-reconciliation-panel" aria-label="Staging session reconciliation">
    <div className="panel-title"><h2>Review staging session</h2><span>{state}</span></div>
    <p>This opens a bounded staging session only. It does not approve tasks, deployment, or production changes. Tasks still need their independent review.</p>
    <p>The evidence below was retained by the trusted launcher. This screen does not independently verify host inventory, restored data, or exclusive access.</p>
    {error && <p role="alert">{error}</p>}
    {busy && <p role="status">Checking session state…</p>}
    {snapshot && <>
      <h3>Reconciliation evidence</h3>
      <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{snapshot.evidence}</pre>
      <p className="small-text">Evidence digest: <code>{snapshot.report_digest}</code></p>
      <p>{expired ? "Review window ended. Refresh to confirm the server state." : `Review window remaining: ${Math.floor(remainingSeconds / 60)}m ${remainingSeconds % 60}s. The server enforces the final deadline.`}</p>
      {snapshot.state === "accepted" && !expired && <p role="status">The operator accepted this session. This is not a claim that the executor is healthy or that any task is authorized.</p>}
    </>}
    {!csrfAvailable && <p>{readOnlyReason ?? "Session decisions are unavailable until your human session is ready."}</p>}
    <div className="button-row">
      <button type="button" onClick={onRefresh} disabled={busy}>Refresh session</button>
      <button type="button" onClick={() => onDecide("decline")} disabled={!canDecide}>Decline session</button>
      <button type="button" onClick={() => onDecide("accept")} disabled={!canDecide}>Accept staging session only</button>
    </div>
  </section>;
}

export function SessionReconciliationPanel({ csrf, enabled, writable, stale, onAuthError, fetchImpl = globalThis.fetch }:
  { csrf: string | null; enabled: boolean; writable: boolean; stale: boolean; onAuthError?: () => void; fetchImpl?: typeof fetch }) {
  const [snapshot, setSnapshot] = useState<SessionReconciliationSnapshot | null>(null);
  const [deadline, setDeadline] = useState(0);
  const [now, setNow] = useState(Date.now);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const inFlight = useRef(false);
  async function refresh() {
    if (!enabled || inFlight.current) return;
    inFlight.current = true;
    const current = ++generation.current;
    const started = Date.now();
    setBusy(true); setError(null); setSnapshot(null);
    try {
      const value = await readSessionReconciliation(fetchImpl);
      if (current !== generation.current) return;
      setSnapshot(value); setDeadline(started + value.expires_in_seconds * 1000); setNow(Date.now());
    } catch (failure) { if (current === generation.current) {
      setError(sessionReconciliationError(failure));
      if (failure instanceof HubApiError && failure.status === 401) onAuthError?.();
    } }
    finally { if (current === generation.current) { inFlight.current = false; setBusy(false); } }
  }
  useEffect(() => {
    if (enabled) void refresh();
    else { setSnapshot(null); setError(null); setBusy(false); }
    return () => { generation.current += 1; inFlight.current = false; };
  }, [fetchImpl, csrf, enabled]);
  useEffect(() => {
    if (!snapshot) return;
    const tick = () => setNow(previous => Math.max(previous, Date.now()));
    const timer = window.setInterval(tick, 1000);
    window.addEventListener("focus", tick);
    document.addEventListener("visibilitychange", tick);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", tick); document.removeEventListener("visibilitychange", tick); };
  }, [snapshot]);
  async function decide(action: "accept" | "decline") {
    if (!enabled || !writable || stale || !snapshot || snapshot.state !== "pending" || !csrf || inFlight.current || error || Date.now() >= deadline) return;
    inFlight.current = true;
    const current = ++generation.current;
    setBusy(true);
    try {
      const result = await decideSessionReconciliation({ request_id: snapshot.request_id, expected_revision: snapshot.revision, action }, csrf, fetchImpl);
      if (current !== generation.current) return;
      setSnapshot({ ...snapshot, revision: result.revision, state: "accepted" });
      if (result.state === "declined") { setDeadline(0); setError("Session declined and closed. No task or deployment approval was granted."); }
    } catch (failure) {
      if (current === generation.current) {
        setSnapshot(null); setError(sessionReconciliationError(failure));
        if (failure instanceof HubApiError && failure.status === 401) onAuthError?.();
      }
    } finally { if (current === generation.current) { inFlight.current = false; setBusy(false); } }
  }
  if (!enabled) return <section className="panel" aria-label="Staging session reconciliation"><h2>Review staging session</h2>
    <p>Live human-session review is unavailable in this view. No staging session can be opened here.</p></section>;
  return <SessionReconciliationView snapshot={snapshot} remainingSeconds={Math.max(0, Math.ceil((deadline - Math.max(now, Date.now())) / 1000))}
    busy={busy} error={error} csrfAvailable={Boolean(csrf) && writable && !stale}
    readOnlyReason={stale ? "Hub observations are stale. Refresh the Hub before deciding." : !writable ? "This Hub view is read-only. Session decisions are unavailable." : undefined}
    onRefresh={() => void refresh()} onDecide={action => void decide(action)} />;
}
