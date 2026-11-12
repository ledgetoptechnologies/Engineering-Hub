/**
 * Release evidence for one container-pilot run, for the Hub owner and the independent reviewer.
 * Read-only: it reads on load and on Refresh, and offers no deploy, rollback or approval action.
 * Every step result is the worker's own authenticated report, and the view says so on every page.
 */
import { useEffect, useRef, useState } from "react";
import { HubApiError } from "./api";
import { readReleaseEvidence, reasonText, runStatusText, stepStateText, type ReleaseEvidence, type StepEvidence } from "./releaseEvidenceApi";
import { Badge } from "./ui";

type Reader = (runId: string, signal: AbortSignal) => Promise<ReleaseEvidence>;
type Load = "loading" | "loaded" | "failed" | "hidden";

const STEPS: { key: "candidate_deploy" | "candidate_health_check" | "rollback_deploy" | "rollback_health_check" | "candidate_cleanup" | "rollback_cleanup"; label: string }[] = [
  { key: "candidate_deploy", label: "Deploy the new version" },
  { key: "candidate_health_check", label: "Health check of the new version" },
  { key: "rollback_deploy", label: "Roll back to the previous version" },
  { key: "rollback_health_check", label: "Health check after rollback" },
  { key: "candidate_cleanup", label: "Stop disposable candidate" },
  { key: "rollback_cleanup", label: "Stop disposable rollback" },
];

const short = (value: string) => value.startsWith("sha256:") ? value.slice(7, 19) : value.slice(0, 7);

function ImageCell({ step }: { step: StepEvidence }) {
  if (step.reported_image_config_id === null)
    return <span title={step.expected_image_config_id}>Expected {short(step.expected_image_config_id)} · nothing reported</span>;
  const same = step.reported_image_config_id === step.expected_image_config_id;
  return <span title={`expected ${step.expected_image_config_id}\nreported ${step.reported_image_config_id}`}>
    {same ? <>Reported {short(step.reported_image_config_id)}, the expected image</>
      : <strong>Reported {short(step.reported_image_config_id)}, but expected {short(step.expected_image_config_id)}</strong>}
  </span>;
}

function checks(step: StepEvidence): string {
  if (step.state === "not_started") return "No request sent";
  if (!step.protocol_verified) return step.state === "inconsistent" ? "Receipt doesn't match the request" : "No receipt yet";
  if (step.state === "ack_pending") return "Response retained; acknowledgment not confirmed";
  return step.journal_verified ? "Receipt matches the request and the Hub's journal" : "Receipt doesn't match the Hub's journal";
}

function health(step: StepEvidence): string {
  if (step.reported_health === null) return "—";
  return step.reported_health === "healthy" ? "Healthy (worker report)"
    : step.reported_health === "unhealthy" ? "Unhealthy (worker report)" : "Unknown";
}

export function ReleaseEvidenceView({ evidence }: { evidence: ReleaseEvidence }) {
  const reviewMatches = evidence.review.commit === evidence.source_commit;
  return <div className="release-evidence">
    <div className="release-evidence-basis">
      <Badge tone="amber">Worker-reported, not independently verified</Badge>
      <p>These results come from the staging worker's authenticated reports. The Hub checks that each report answers the exact
        request it sent and agrees with its own journal, but it doesn't look at the staging machine itself.</p>
      <p>This view does not verify a successful CI run or establish that the tested source produced the new image.</p>
      <p>Run, manifest and step records are read separately and may change during refresh; this is not an atomic snapshot.</p>
      {evidence.run_status === "held" && <p>The run is on hold. A reported cleanup does not prove quiescence or release the reservation.</p>}
    </div>
    <dl className="release-evidence-facts">
      <dt>Project</dt><dd>{evidence.project}</dd>
      <dt>Run</dt><dd>{runStatusText[evidence.run_status]}</dd>
      <dt>Source commit</dt><dd><code title={evidence.source_commit}>{short(evidence.source_commit)}</code></dd>
      <dt>Review</dt><dd>Approved by {evidence.review.actor} for <code title={evidence.review.commit}>{short(evidence.review.commit)}</code>
        {!reviewMatches && <strong role="alert"> This isn't the commit being deployed.</strong>}</dd>
      <dt>New image</dt><dd><code title={evidence.candidate_image_config_id}>{short(evidence.candidate_image_config_id)}</code></dd>
      <dt>Rollback image</dt><dd><code title={evidence.rollback_image_config_id}>{short(evidence.rollback_image_config_id)}</code></dd>
      <dt>Pilot manifest</dt><dd><code title={evidence.pilot_manifest_digest}>{short(evidence.pilot_manifest_digest)}</code></dd>
    </dl>
    <table className="release-evidence-steps">
      <thead><tr><th scope="col">Step</th><th scope="col">Result</th><th scope="col">Image</th><th scope="col">Health</th><th scope="col">Hub checks</th></tr></thead>
      <tbody>{STEPS.map(({ key, label }) => {
        const step = evidence[key];
        if (!step) return null; // Cleanup is only present for explicitly disposable pilots.
        const text = stepStateText[step.state];
        return <tr key={key}>
          <th scope="row">{label}</th>
          <td><Badge tone={text.tone}>{text.title}</Badge>{step.reason && <small> {reasonText[step.reason]}</small>}</td>
          <td><ImageCell step={step} /></td>
          <td>{health(step)}</td>
          <td>{checks(step)}</td>
        </tr>;
      })}</tbody>
    </table>
  </div>;
}

export interface ReleaseEvidencePanelProps { runId: string; onUnauthorized: () => void; read?: Reader }

/** Requires a human session and an enabled owner-scoped pilot: otherwise the panel stays hidden. */
const hides = (error: unknown) => error instanceof HubApiError && (error.status === 403
  || error.code === "release_evidence_unsupported" || error.code === "container_pilot_not_enabled");

export function ReleaseEvidencePanel({ runId, onUnauthorized, read = readReleaseEvidence }: ReleaseEvidencePanelProps) {
  const [load, setLoad] = useState<Load>("loading");
  const [evidence, setEvidence] = useState<ReleaseEvidence | null>(null);
  const [notice, setNotice] = useState("");
  const [attempt, setAttempt] = useState(0);
  const reader = useRef(read); reader.current = read;
  const unauthorized = useRef(onUnauthorized); unauthorized.current = onUnauthorized;

  useEffect(() => {
    const controller = new AbortController();
    setLoad("loading");
    reader.current(runId, controller.signal).then(value => {
      if (controller.signal.aborted) return;
      setEvidence(value); setNotice(""); setLoad("loaded");
    }).catch(error => {
      if (controller.signal.aborted) return;
      if (error instanceof HubApiError && error.status === 401) { setEvidence(null); setLoad("hidden"); unauthorized.current(); return; }
      if (hides(error)) { setEvidence(null); setLoad("hidden"); return; }
      setNotice(error instanceof HubApiError && error.status === 404 ? "This run wasn't found."
        : "The release evidence couldn't be read. Use Refresh to try again.");
      setLoad("failed");
    });
    return () => controller.abort();
  }, [runId, attempt]);

  if (load === "hidden") return null;
  return <section className="panel" aria-label="Release evidence">
    <div className="panel-title"><h2>Release evidence</h2>
      <span className="panel-action">
        <button className="secondary" type="button" disabled={load === "loading"} onClick={() => setAttempt(value => value + 1)}>
          {load === "loading" ? "Reading…" : "Refresh"}</button>
      </span></div>
    <p className="panel-description">What happened when this run deployed its new version, checked its health, and rolled back.
      Read-only: nothing here deploys, rolls back or approves anything.</p>
    {load === "loading" && !evidence && <p role="status">Reading release evidence…</p>}
    {notice && <p className="notice error" role="alert">{notice}{evidence && " What's shown below may be out of date."}</p>}
    {evidence && <ReleaseEvidenceView evidence={evidence} />}
  </section>;
}

/** On a run in the list: reads nothing until the owner asks, so a long run list never fans out requests. */
export function RunReleaseEvidence({ runId, onUnauthorized }: { runId: string; onUnauthorized: () => void }) {
  const [open, setOpen] = useState(false);
  if (!open) return <button className="secondary" type="button" onClick={() => setOpen(true)}>Show release evidence</button>;
  return <ReleaseEvidencePanel runId={runId} onUnauthorized={onUnauthorized} />;
}
