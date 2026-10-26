/**
 * Whether the shared staging queue is open, for the Hub owner. It reads on load and on Refresh only;
 * the one action retries the check of earlier work, and only when the Hub says that check is needed.
 */
import { useEffect, useRef, useState } from "react";
import { HubApiError, type HubMode } from "./api";
import { readSharedStaging, recoverFailureText, recoverSharedStaging, stateText, type SharedStagingStatus } from "./sharedStagingApi";
import { Badge } from "./ui";

type Reader = (signal: AbortSignal) => Promise<SharedStagingStatus>;
type Recoverer = (csrf: string) => Promise<SharedStagingStatus>;
type Load = "loading" | "loaded" | "failed" | "hidden";

export interface SharedStagingPanelProps {
  mode: HubMode; csrf: string | null; writable: boolean; stale: boolean; onUnauthorized: () => void;
  read?: Reader; recover?: Recoverer;
}

/** Owner-only and only on Hubs with shared staging set up: anything else steps aside rather than explaining itself. */
const hides = (error: unknown) => error instanceof HubApiError && (error.status === 403 || error.code === "shared_staging_unsupported"
  || error.code === "shared_staging_not_configured");

export function SharedStagingPanel({ mode, csrf, writable, stale, onUnauthorized, read: givenRead, recover: givenRecover }: SharedStagingPanelProps) {
  const read = givenRead ?? (mode === "demo" ? async () => ({ state: "active", reason: null }) as SharedStagingStatus : readSharedStaging);
  const recover = givenRecover ?? ((token: string) => recoverSharedStaging(token));
  const [load, setLoad] = useState<Load>("loading");
  const [status, setStatus] = useState<SharedStagingStatus | null>(null);
  const [notice, setNotice] = useState("");
  const [retrying, setRetrying] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const reader = useRef(read); reader.current = read;
  const unauthorized = useRef(onUnauthorized); unauthorized.current = onUnauthorized;
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);

  function signedOut() { setStatus(null); setLoad("hidden"); unauthorized.current(); }

  useEffect(() => {
    const controller = new AbortController();
    setLoad("loading");
    reader.current(controller.signal).then(value => {
      if (controller.signal.aborted) return;
      setStatus(value); setNotice(""); setLoad("loaded");
    }).catch(error => {
      if (controller.signal.aborted) return;
      if (error instanceof HubApiError && error.status === 401) return signedOut();
      if (hides(error)) { setStatus(null); setLoad("hidden"); return; }
      setNotice("The shared staging queue's state couldn't be read. Use Refresh to try again.");
      setLoad("failed");
    });
    return () => controller.abort();
  }, [mode, attempt]);

  async function retry() {
    if (!csrf || retrying || mode === "demo") return;
    setRetrying(true); setNotice("");
    try {
      const value = await recover(csrf);
      if (!mounted.current) return;
      setStatus(value); setLoad("loaded");
    } catch (error) {
      if (!mounted.current) return;
      if (error instanceof HubApiError && error.status === 401) return signedOut();
      if (hides(error)) { setStatus(null); setLoad("hidden"); return; }
      setNotice(recoverFailureText(error));
    } finally {
      if (mounted.current) setRetrying(false);
    }
  }

  if (load === "hidden") return null;
  const text = status ? stateText[status.state] : null;
  const canRetry = status?.state === "recovery_required" && mode !== "demo";
  const retryBlocked = !csrf || !writable || stale;
  return <section className="panel shared-staging" id="shared-staging" aria-label="Shared staging queue">
    <div className="panel-title"><h2>Shared staging queue</h2>
      <span className="panel-action">
        <button className="secondary" type="button" disabled={load === "loading" || retrying} onClick={() => setAttempt(value => value + 1)}>
          {load === "loading" ? "Reading…" : "Refresh"}</button>
      </span></div>
    <p className="panel-description">Staging builds and app deployments share one queue and take the staging PC one job at a time.
      This shows whether that queue is taking jobs. It doesn't approve any job or deployment.</p>
    {mode === "demo" && <p role="status">Demo: an illustrative state only. Nothing is read from the Hub's staging queue.</p>}
    {load === "loading" && !status && <p role="status">Reading the shared staging queue…</p>}
    {notice && <p className="notice error" role="alert">{notice}{status && " What's shown below may be out of date."}</p>}
    {status && text && <div className="shared-staging-state">
      <Badge tone={text.tone}>{text.title}</Badge>
      <p className={text.tone === "amber" ? "shared-staging-line amber" : "shared-staging-line"}>{text.line}</p>
      {status.state === "pending" && <p className="panel-footnote">The session check is in “Review staging session” on this page.</p>}
    </div>}
    {retrying && <p role="status">Rechecking earlier work. This can take several minutes; it carries on even if you leave this page.</p>}
    {canRetry && <div className="button-row">
      <button type="button" disabled={retrying || retryBlocked} onClick={() => void retry()}>Retry checking earlier work</button>
      {retryBlocked && <small>{stale ? "Hub observations are out of date. Refresh the Hub first." : "Retrying needs a signed-in, writable Hub session."}</small>}
    </div>}
  </section>;
}
