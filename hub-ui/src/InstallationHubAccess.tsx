/**
 * What one paired installation may do in the Hub, kept apart from whether it may connect.
 * The owner grants explicit projects and rights with an expiry; the server says what is in effect.
 * Stranded task claims can be handed to another live session of the same installation.
 */
import { useEffect, useRef, useState } from "react";
import { actorName } from "./actors";
import { untilText, type Installation, type Session } from "./agentConnectionsApi";
import {
  capabilityLabel, EFFECTIVE_TEXT, GRANT_LIFETIMES, grantFailureText, MAX_GRANT_PROJECTS, RECOVERY_REASON_MAX, recoveryBlockText,
  type Claim, type Grant, type HubCatalog, type HubGrantsClient, type Recovery,
} from "./hubGrantsApi";
import { relativeAge } from "./dashboardText";
import type { Hub } from "./hub";
import { Badge, Modal } from "./ui";

export interface Allowed { canChange: boolean; csrf: string }
const STATE_TONE: Record<Grant["effective"]["state"], string> = { active: "forest", absent: "neutral", inactive: "neutral", revoked: "neutral", expired: "amber" };
const exact = (value: string) => new Date(value).toLocaleString();
const short = (id: string) => id.slice(0, 8);

export function InstallationHubAccess({ hub, installation, catalog, client, allowed, sessions, onDenied }: {
  hub: Hub; installation: Installation; catalog: HubCatalog; client: HubGrantsClient;
  /** Read at submit time, never cached from when a form opened. */
  allowed: () => Allowed;
  sessions: Session[] | undefined; onDenied: (error: unknown) => boolean;
}) {
  const [grant, setGrant] = useState<Grant | null>(null);
  const [grantState, setGrantState] = useState<"loading" | "loaded" | "failed">("loading");
  const [claims, setClaims] = useState<Claim[]>([]);
  const [claimsNext, setClaimsNext] = useState<string | null>(null);
  const [claimsState, setClaimsState] = useState<"idle" | "loading" | "loaded" | "failed">("idle");
  const [editing, setEditing] = useState<{ projects: string[]; capabilities: string[]; lifetime: number } | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [recovering, setRecovering] = useState<Claim | null>(null);
  const [history, setHistory] = useState<{ taskId: string; state: "loading" | "loaded" | "failed"; items: Recovery[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ error: boolean; text: string } | null>(null);
  const grantReads = useRef(0), claimReads = useRef(0), historyReads = useRef(0), mounted = useRef(true);
  // Access ages by this browser's clock between reads; nothing is re-read on a timer.
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 15_000); return () => window.clearInterval(timer); }, []);
  const { canChange } = allowed();
  const projectName = (id: string) => hub.data.projects.find(project => project.id === id)?.name ?? id;
  const activeProjects = hub.data.projects.filter(project => !project.archived);
  const viewer = hub.session?.actor;
  const sessionName = (id: string | null) => {
    if (id === null) return "an unknown session";
    const session = sessions?.find(item => item.id === id);
    return session?.label ? `${session.label} (${short(id)})` : `Session ${short(id)}`;
  };

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; grantReads.current++; claimReads.current++; historyReads.current++; }; }, []);
  async function readGrant() {
    const current = ++grantReads.current;
    setGrantState(previous => previous === "loaded" ? "loaded" : "loading");
    try {
      const found = await client.grant(installation.id, AbortSignal.timeout(15_000));
      if (mounted.current && current === grantReads.current) { setGrant(found); setGrantState("loaded"); }
    } catch (error) {
      if (!mounted.current || current !== grantReads.current || onDenied(error)) return;
      setGrantState(previous => previous === "loaded" ? "loaded" : "failed");
    }
  }
  async function readClaims(after: string | null) {
    const current = ++claimReads.current;
    setClaimsState("loading");
    try {
      const page = await client.claims(installation.id, after, AbortSignal.timeout(15_000));
      if (!mounted.current || current !== claimReads.current) return;
      setClaims(previous => after ? [...previous, ...page.items.filter(item => !previous.some(seen => seen.task.id === item.task.id))] : page.items);
      setClaimsNext(page.next_after); setClaimsState("loaded");
    } catch (error) {
      if (!mounted.current || current !== claimReads.current || onDenied(error)) return;
      setClaimsState("failed");
    }
  }
  useEffect(() => { void readGrant(); if (catalog.taskRecovery) void readClaims(null); }, [installation.id]); // eslint-disable-line react-hooks/exhaustive-deps

  /** One change at a time, permission re-read now, and the record replaced only by the server's answer. */
  async function change(run: (csrf: string) => Promise<string | void>, done: string) {
    if (busy) return false;
    const { canChange: may, csrf } = allowed();
    if (!may || !csrf) { setNotice({ error: true, text: "Changes aren't available right now: sign in again or Refresh, then try again. Nothing was sent." }); return false; }
    setBusy(true); setNotice(null);
    try {
      const override = await run(csrf);
      if (!mounted.current) return false;
      setNotice({ error: false, text: override || done });
      return true;
    } catch (error) {
      if (!mounted.current || onDenied(error)) return false;
      setNotice({ error: true, text: grantFailureText(error) });
      return false;
    } finally { if (mounted.current) setBusy(false); }
  }
  const save = async () => {
    if (!grant || !editing) return;
    const body = { expected_revision: grant.revision, projects: [...editing.projects].sort(), capabilities: [...editing.capabilities].sort(),
      lifetime_seconds: editing.lifetime };
    if (await change(async csrf => { grantReads.current++; setGrant(await client.saveGrant(installation.id, body, csrf)); }, "Hub access saved.")) setEditing(null);
  };
  const revoke = async () => {
    if (!grant) return;
    if (await change(async csrf => { grantReads.current++; setGrant(await client.revokeGrant(installation.id, grant.revision, csrf)); },
      "Hub access removed. The earlier choices stay visible for reference.")) setConfirmRevoke(false);
  };
  async function openHistory(taskId: string) {
    if (history?.taskId === taskId) { setHistory(null); return; }
    const current = ++historyReads.current;
    setHistory({ taskId, state: "loading", items: [] });
    try {
      const found = await client.history(taskId, AbortSignal.timeout(15_000));
      if (mounted.current && current === historyReads.current) setHistory({ taskId, state: "loaded", items: found.recoveries });
    } catch (error) {
      if (!mounted.current || current !== historyReads.current || onDenied(error)) return;
      setHistory({ taskId, state: "failed", items: [] });
    }
  }

  const served = grant?.effective;
  const lapsed = served?.state === "active" && !!served.expires_at && Date.parse(served.expires_at) <= now;
  const effective = served && lapsed ? { ...served, state: "expired" as const, reason: "grant_expired" as const, projects: [], capabilities: [] } : served;
  const canEdit = canChange && installation.state === "active" && grant !== null;
  const startEdit = () => grant && setEditing({
    projects: grant.state === "active" ? grant.projects.filter(id => activeProjects.some(project => project.id === id)) : [],
    capabilities: grant.state === "active" ? grant.capabilities.filter(id => catalog.capabilities.includes(id)) : [],
    lifetime: GRANT_LIFETIMES[1].seconds });
  const toggle = (list: string[], value: string) => list.includes(value) ? list.filter(item => item !== value) : [...list, value];

  return <div className="hub-access" aria-label={`Hub access for ${installation.name}`} role="group">
    <h4>Hub access <span className="muted">· separate from approving the connection</span></h4>
    {notice && <p role={notice.error ? "alert" : "status"} className={notice.error ? "notice error" : "notice"}>{notice.text}</p>}
    {grantState === "loading" && !grant && <p role="status">Reading Hub access…</p>}
    {grantState === "failed" && !grant && <p role="alert" className="notice error">Hub access couldn't be read. Close and open it again to retry.</p>}
    {grant && effective && <>
      <div className="hub-access-effective">
        <Badge tone={STATE_TONE[effective.state]}>{effective.state === "active" ? "In effect" : "No access"}</Badge>
        <span>{EFFECTIVE_TEXT[effective.reason]}{lapsed ? " (by this browser's clock; Refresh to confirm)" : ""}</span>
      </div>
      {effective.state === "active" && <dl className="properties hub-access-rights">
        <div><dt>Projects</dt><dd>{effective.projects.map(projectName).join(", ")}</dd></div>
        <div><dt>Can</dt><dd><ul>{effective.capabilities.map(id => <li key={id}>{capabilityLabel(id)}</li>)}</ul></dd></div>
        {effective.expires_at && <div><dt>Ends</dt><dd>{untilText(effective.expires_at, now)} ({exact(effective.expires_at)})</dd></div>}
      </dl>}
      {(lapsed || (grant.state !== "absent" && grant.state !== "active")) && grant.projects.length > 0 && <p className="panel-footnote">
        Earlier choices (not in effect): {grant.projects.map(projectName).join(", ")} · {grant.capabilities.map(capabilityLabel).join(", ")}.</p>}
      {grant.revision > 0 && <p className="panel-footnote">Last set by {grant.approved_by ? actorName(grant.approved_by, { viewer }) : "unknown"}
        {grant.approved_at ? ` ${relativeAge(grant.approved_at)}` : ""} · version {grant.revision}</p>}
      {!editing && <div className="note-actions">
        <button type="button" className="secondary small" disabled={!canEdit || busy} onClick={startEdit}>
          {grant.state === "active" ? "Change access…" : "Grant access…"}</button>
        {(grant.state === "active" || grant.state === "expired") && <button type="button" className="secondary small danger"
          disabled={!canChange || busy} onClick={() => { setNotice(null); setConfirmRevoke(true); }}>Remove all access…</button>}
      </div>}
      {installation.state !== "active" && <p className="panel-footnote">Access can only be granted while the connection is active.</p>}
      {editing && <form className="hub-access-form" aria-label="Hub access choices" onSubmit={event => { event.preventDefault(); void save(); }}>
        <fieldset disabled={busy}>
          <legend>Projects it may work in</legend>
          {editing.projects.length >= MAX_GRANT_PROJECTS && <p className="panel-footnote">At most {MAX_GRANT_PROJECTS} projects per grant.</p>}
          {activeProjects.map(project => { const chosen = editing.projects.includes(project.id);
            return <label key={project.id} className="checkbox-field">
              <input type="checkbox" checked={chosen} disabled={!chosen && editing.projects.length >= MAX_GRANT_PROJECTS}
                onChange={() => setEditing({ ...editing, projects: toggle(editing.projects, project.id) })} />{project.name}</label>; })}
        </fieldset>
        <fieldset disabled={busy}>
          <legend>What it may do there</legend>
          <p className="panel-footnote">Each right is separate: claiming tasks doesn't include reading them.</p>
          {catalog.capabilities.includes("decision.read") && <p className="panel-footnote">Reading questions includes their answers, which may contain sensitive information.</p>}
          {catalog.capabilities.map(id => <label key={id} className="checkbox-field">
            <input type="checkbox" checked={editing.capabilities.includes(id)}
              onChange={() => setEditing({ ...editing, capabilities: toggle(editing.capabilities, id) })} />{capabilityLabel(id)}</label>)}
        </fieldset>
        <label className="field">Lasts for
          <select value={editing.lifetime} onChange={event => setEditing({ ...editing, lifetime: Number(event.target.value) })}>
            {GRANT_LIFETIMES.map(option => <option key={option.seconds} value={option.seconds}>{option.label}</option>)}
          </select></label>
        <p className="panel-footnote">It never lasts past the connection's own approval{installation.expires_at ? `, which ends ${exact(installation.expires_at)}` : ""}.
          This lets it act only through the rights above; it still can't run work, create projects or approve anything.</p>
        <div className="note-actions">
          <button className="primary small" disabled={!canChange || busy || !editing.projects.length || !editing.capabilities.length
            || editing.projects.length > MAX_GRANT_PROJECTS}>
            {busy ? "Saving…" : "Save access"}</button>
          <button type="button" className="secondary small" disabled={busy} onClick={() => setEditing(null)}>Cancel</button>
        </div>
      </form>}
    </>}

    {catalog.taskRecovery && <section className="hub-claims" aria-label={`Tasks claimed by ${installation.name}`}>
      <h4>Tasks it holds</h4>
      {claimsState === "loading" && !claims.length && <p role="status">Reading tasks…</p>}
      {claimsState === "failed" && <p role="alert" className="notice error">Tasks couldn't be read. Close and open Hub access again to retry.</p>}
      {claimsState === "loaded" && !claims.length && <p className="panel-footnote">It holds no tasks.</p>}
      {claims.length > 0 && <ul className="hub-claim-list">{claims.map(claim => <li key={claim.task.id}>
        <div className="hub-claim-main"><strong>{claim.task.title}</strong>
          <span className="installation-meta">{projectName(claim.task.project)} · {claim.task.status.replace("_", " ")} · held by {sessionName(claim.claim_session_id)}
            {" "}({claim.claim_state === "active" ? "live" : claim.claim_state === "unknown" ? "can't be confirmed" : claim.claim_state})</span>
          {claim.recovery.state === "blocked" && <span className="installation-meta">{recoveryBlockText(claim.recovery.reason)}</span>}</div>
        <div className="note-actions">
          {claim.recovery.state === "recoverable" && <button type="button" className="secondary small" disabled={!canChange || busy}
            onClick={() => { setNotice(null); setRecovering(claim); }}>Hand over…</button>}
          <button type="button" className="text-button" aria-expanded={history?.taskId === claim.task.id}
            onClick={() => void openHistory(claim.task.id)}>{history?.taskId === claim.task.id ? "Hide history" : "History"}</button>
        </div>
        {history?.taskId === claim.task.id && <RecoveryHistoryList entry={history} sessionName={sessionName} viewer={viewer} />}
      </li>)}</ul>}
      {claimsNext && <button type="button" className="secondary small" disabled={claimsState === "loading"} onClick={() => void readClaims(claimsNext)}>
        {claimsState === "loading" ? "Loading…" : "Show more"}</button>}
    </section>}

    {confirmRevoke && grant && <Modal title={`Remove all Hub access for ${installation.name}?`} onClose={() => setConfirmRevoke(false)}>
      <p>It can no longer read or change anything in the Hub. Its connection stays approved and its messages keep working. Your earlier
        choices stay visible for reference.</p>
      {notice?.error && <p role="alert" className="notice error">{notice.text}</p>}
      <div className="note-actions">
        <button type="button" className="primary danger" disabled={!canChange || busy} onClick={() => void revoke()}>{busy ? "Removing…" : "Remove access"}</button>
        <button type="button" className="secondary" onClick={() => setConfirmRevoke(false)}>Cancel</button>
      </div>
    </Modal>}
    {recovering && <RecoverDialog claim={recovering} installation={installation} canChange={canChange} busy={busy} sessionName={sessionName}
      viewer={viewer} error={notice?.error ? notice.text : ""}
      onSubmit={(body) => change(async csrf => {
        const result = await client.recover(recovering.task.id, body, csrf);
        claimReads.current++; historyReads.current++;
        setClaims(previous => previous.map(item => item.task.id === result.task.id
          ? { ...item, task: result.task, claim_session_id: result.recovery.target_session_id, claim_state: "active",
              recovery: { state: "blocked", reason: "claim_session_active", target_session_ids: [] } } : item));
        setHistory(null);
        if (result.replayed) return "This hand-over had already happened, so nothing changed this time.";
      }, `Handed “${recovering.task.title}” to ${sessionName(body.target_session_id)}. It must be resubmitted and reviewed again.`)
        .then(done => { if (done) setRecovering(null); })}
      onClose={() => setRecovering(null)} />}
  </div>;
}

function RecoverDialog({ claim, installation, canChange, busy, error, sessionName, viewer, onSubmit, onClose }: {
  claim: Claim; installation: Installation; canChange: boolean; busy: boolean; error: string; viewer?: string | null;
  sessionName: (id: string | null) => string; onSubmit: (body: { expected_revision: number; target_session_id: string; reason: string; client_request_id: string }) => void;
  onClose: () => void;
}) {
  const [target, setTarget] = useState(claim.recovery.target_session_ids[0] ?? "");
  const [reason, setReason] = useState("");
  const [checked, setChecked] = useState(false);
  // One request id per intent: the same choices retry safely as a replay; changing them is a new request.
  const request = useRef<{ key: string; id: string } | null>(null);
  const reasonFits = reason.trim() !== "" && [...reason.trim()].length <= RECOVERY_REASON_MAX;
  const review = claim.task.review;
  function submit() {
    const key = JSON.stringify([claim.task.id, claim.task.revision, target, reason.trim()]);
    if (request.current?.key !== key) request.current = { key, id: `recover-${crypto.randomUUID()}` };
    onSubmit({ expected_revision: claim.task.revision, target_session_id: target, reason: reason.trim(), client_request_id: request.current.id });
  }
  return <Modal title={`Hand over “${claim.task.title}”?`} onClose={onClose}>
    <p>The session that claimed it has {claim.claim_state === "revoked" ? "been revoked" : "ended"}. This gives the task to another live
      session of the same installation, <strong>{installation.name}</strong>. It can't go to another computer or another agent.</p>
    <dl className="properties recovery-ids">
      <div><dt>Installation</dt><dd>{installation.name} <code aria-label="Installation id">{installation.id}</code></dd></div>
      <div><dt>From</dt><dd>{sessionName(claim.claim_session_id)} ({claim.claim_state}) <code aria-label="From session id">{claim.claim_session_id}</code></dd></div>
      <div><dt>To</dt><dd><select aria-label="Hand over to" value={target} onChange={event => { setTarget(event.target.value); setChecked(false); }}>
        {claim.recovery.target_session_ids.map(id => <option key={id} value={id}>{sessionName(id)}</option>)}</select>
        {" "}<code aria-label="To session id">{target}</code></dd></div>
      <div><dt>Candidate</dt><dd>{claim.task.candidate_commit ? <>Kept: <code>{claim.task.candidate_commit.slice(0, 12)}</code></> : "None submitted"}</dd></div>
      <div><dt>Review</dt><dd>{review ? `The current review (${review.verdict === "approve" ? "approved" : "rejected"} by ${actorName(review.actor, { family: review.family, viewer })}) is cleared and kept in the task's history.`
        : "No current review."}</dd></div>
    </dl>
    <p className="panel-footnote">The task goes back to Claimed, so the work has to be submitted and reviewed again. Nothing is run.</p>
    <label className="field">Reason (kept with the task's history)
      <textarea rows={3} value={reason} onChange={event => setReason(event.target.value)} /></label>
    {reason.trim() !== "" && !reasonFits && <p className="panel-footnote notice">Keep the reason to {RECOVERY_REASON_MAX} characters.</p>}
    <label className="checkbox-field"><input type="checkbox" checked={checked} onChange={event => setChecked(event.target.checked)} />
      I understand the review is cleared and the work must be reviewed again</label>
    {error && <p role="alert" className="notice error">{error}</p>}
    <div className="note-actions">
      <button type="button" className="primary" disabled={!canChange || busy || !checked || !reasonFits || !target} onClick={submit}>
        {busy ? "Handing over…" : "Hand over"}</button>
      <button type="button" className="secondary" onClick={onClose}>Cancel</button>
    </div>
  </Modal>;
}

function RecoveryHistoryList({ entry, sessionName, viewer }: {
  entry: { state: "loading" | "loaded" | "failed"; items: Recovery[] }; sessionName: (id: string | null) => string; viewer?: string | null;
}) {
  if (entry.state === "loading") return <p role="status">Reading history…</p>;
  if (entry.state === "failed") return <p role="alert" className="notice error">History couldn't be read. Open it again to retry.</p>;
  if (!entry.items.length) return <p className="panel-footnote">Never handed over.</p>;
  return <ol className="timeline" aria-label="Hand-over history">{entry.items.map(item => <li key={item.id}>
    <span className="timeline-dot kind-task" aria-hidden="true" />
    <div><strong>{actorName(item.actor, { viewer })} handed it from {sessionName(item.prior_session_id)} to {sessionName(item.target_session_id)}</strong>
      <span className="timeline-meta">“{item.reason}”{item.prior_review ? ` · cleared review: ${item.prior_review.verdict === "approve" ? "approved" : "rejected"} by ${actorName(item.prior_review.actor, { family: item.prior_review.family })}` : ""}
        {item.prior_candidate_commit ? ` · candidate ${item.prior_candidate_commit.slice(0, 12)} kept` : ""} · version {item.prior_revision} → {item.resulting_revision}</span></div>
    <time dateTime={item.at} title={new Date(item.at).toLocaleString()}>{relativeAge(item.at)}</time></li>)}</ol>;
}

