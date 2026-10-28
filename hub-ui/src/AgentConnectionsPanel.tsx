/**
 * Owner-managed agent installations and their sessions (staging). The owner approves a pairing by
 * checking a public code and fingerprint shown on that computer; no credential ever reaches this
 * page. Every change waits for the server's answer: nothing here is shown as done before it is.
 */
import { useEffect, useRef, useState } from "react";
import { HubApiError } from "./api";
import {
  actionText, demoAgentConnections, FAMILIES, FAMILY_NAME, httpAgentConnections, NAME_MAX, reasonText, STATE_LABEL, STATE_TONE,
  untilText, writeFailureText, type AgentConnectionsClient, type AuditEvent, type Capabilities, type Installation, type Session,
} from "./agentConnectionsApi";
import { relativeAge } from "./dashboardText";
import { httpHubGrants, type HubCatalog, type HubGrantsClient } from "./hubGrantsApi";
import { InstallationHubAccess } from "./InstallationHubAccess";
export { untilText };
import type { Hub } from "./hub";
import { Badge, Modal } from "./ui";

type Load = "checking" | "unavailable" | "not_owner" | "loading" | "loaded" | "failed";
type Dialog = { kind: "approve" | "revoke"; item: Installation } | { kind: "revoke-session"; item: Session; installation: Installation };

const exact = (value: string) => new Date(value).toLocaleString();
const nameFits = (value: string) => value.trim() !== "" && [...value.trim()].length <= NAME_MAX;
/** Who did it, in words: the viewer, an installation by its name, or the recorded id as a last resort. */
export function actorName(actor: string, items: Installation[], viewer?: string | null): string {
  if (viewer && actor === viewer) return "You";
  const installation = items.find(item => item.id === actor);
  if (installation) return installation.name;
  if (actor.startsWith("human:")) return "A person";
  return FAMILY_NAME[actor as keyof typeof FAMILY_NAME] ?? actor;
}

export function AgentConnectionsPanel({ hub, client: given }: { hub: Hub; client?: AgentConnectionsClient }) {
  const client = useRef(given ?? httpAgentConnections()).current;
  const demo = hub.demo;
  const [load, setLoad] = useState<Load>(demo ? "loaded" : "checking");
  const [capabilities, setCapabilities] = useState<Capabilities | null>(demo ? { enabled: true, environment: "staging", owner: true } : null);
  const [items, setItems] = useState<Installation[]>(() => demo ? demoAgentConnections().installations : []);
  const [audit, setAudit] = useState<AuditEvent[]>(() => demo ? demoAgentConnections().audit : []);
  const [failure, setFailure] = useState("");
  const [readAt, setReadAt] = useState<number | null>(demo ? Date.now() : null);
  const [now, setNow] = useState(Date.now());
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ error: boolean; text: string; for: string } | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [sessions, setSessions] = useState<Record<string, { state: "loading" | "loaded" | "failed"; items: Session[] }>>(() =>
    demo ? Object.fromEntries(Object.entries(demoAgentConnections().sessions).map(([key, value]) => [key, { state: "loaded" as const, items: value }])) : {});
  const generation = useRef(0), mounted = useRef(true);
  // One counter per installation's session list: a newer read, a refresh or a confirmed change retires older reads.
  const sessionReads = useRef<Record<string, number>>({});
  const grants = useRef<HubGrantsClient>(httpHubGrants()).current;
  // The Hub-access bridge is optional: absent or unclear, its section simply isn't offered.
  const [catalog, setCatalog] = useState<HubCatalog | null>(null);
  const [access, setAccess] = useState<string | null>(null);
  const [accessEpoch, setAccessEpoch] = useState(0);
  const csrf = hub.session?.csrf_token ?? "";
  const canChange = !demo && hub.writable && !!csrf && capabilities?.owner === true;
  // Read again at submit time: a dialog opened while allowed must not act after sign-in, CSRF or ownership is lost.
  const allowed = useRef({ canChange, csrf });
  allowed.current = { canChange, csrf };
  function retireSessionReads(installationId?: string) {
    for (const key of installationId ? [installationId] : Object.keys(sessionReads.current)) {
      sessionReads.current[key] = (sessionReads.current[key] ?? 0) + 1;
      // A retired read is cancelled too, not just ignored when it answers.
      sessionCalls.current[key]?.abort();
      delete sessionCalls.current[key];
    }
  }

  // Ages and pairing deadlines move with this browser's clock; nothing is re-read from the server.
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 15_000); return () => window.clearInterval(timer); }, []);
  // Every panel read has a deadline, and leaving the page cancels whatever is still open.
  const inflight = useRef(new Set<AbortController>());
  const refreshCall = useRef<AbortController | null>(null);
  const catalogCall = useRef<AbortController | null>(null);
  const sessionCalls = useRef<Record<string, AbortController>>({});
  function bounded() {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), REFRESH_DEADLINE_MS);
    inflight.current.add(controller);
    return { controller, done: () => { window.clearTimeout(timer); inflight.current.delete(controller); } };
  }
  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; generation.current++;
    for (const controller of inflight.current) controller.abort();
    inflight.current.clear();
  }; }, []);

  function denied(error: unknown): boolean {
    if (error instanceof HubApiError && error.status === 401) { hub.clearPrivateWorkspace(); return true; }
    return false;
  }
  async function refresh(keepSessions = false) {
    if (demo) return;
    const current = ++generation.current;
    // A newer refresh retires the older one's requests instead of leaving them open.
    refreshCall.current?.abort();
    const call = bounded(), controller = call.controller;
    refreshCall.current = controller;
    setFailure(""); setAccessEpoch(value => value + 1);
    let probing = capabilities === null;
    try {
      const found = capabilities ?? await client.capabilities(controller.signal);
      probing = false;
      if (current !== generation.current || !mounted.current) return;
      setCapabilities(found);
      if (!found.owner) { setLoad("not_owner"); return; }
      if (!catalog) {
        // Only the newest refresh's catalog probe stays open.
        catalogCall.current?.abort();
        const probe = bounded();
        catalogCall.current = probe.controller;
        void grants.catalog(probe.controller.signal).then(next => { if (mounted.current && current === generation.current) setCatalog(next); })
          .catch(error => { if (mounted.current && current === generation.current) denied(error); })
          .finally(() => { probe.done(); if (catalogCall.current === probe.controller) catalogCall.current = null; });
      }
      setLoad(previous => previous === "loaded" ? "loaded" : "loading");
      const [nextItems, nextAudit] = await Promise.all([client.installations(controller.signal), client.audit(controller.signal)]);
      if (current !== generation.current || !mounted.current) return;
      setItems(nextItems); setAudit(nextAudit); setReadAt(Date.now()); setNow(Date.now()); setLoad("loaded");
      // Session lists are re-read only when someone opens them again.
      if (!keepSessions) { retireSessionReads(); setSessions({}); setOpen(null); }
    } catch (error) {
      if (current !== generation.current || !mounted.current || denied(error)) return;
      if (error instanceof HubApiError && error.status === 404 && error.code === "agent_connections_unavailable") { setLoad("unavailable"); return; }
      if (error instanceof HubApiError && error.status === 403 && (!probing || error.code === "agent_connections_owner_required")) {
        setLoad("not_owner"); return; }
      // An optional staging module appears only once this server confirms it; an unclear probe keeps it hidden.
      if (probing) { setLoad("unavailable"); return; }
      setLoad(previous => previous === "loaded" ? "loaded" : "failed");
      setFailure(error instanceof HubApiError && error.code === "invalid_agent_connections_response"
        ? "The Hub answered in a shape this page doesn't accept, so nothing new is shown. Tell an agent; Refresh to retry."
        : controller.signal.aborted ? "The Hub didn't answer in time, so nothing new is shown. Refresh to retry."
        : "Agent connections couldn't be read. Refresh to retry.");
    } finally {
      // If one of the paired reads failed early, the other is cancelled rather than left open.
      controller.abort();
      call.done();
      if (refreshCall.current === controller) refreshCall.current = null;
    }
  }
  useEffect(() => { void refresh(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function readSessions(installation: Installation) {
    if (open === installation.id) { setOpen(null); return; }
    setOpen(installation.id);
    if (demo || sessions[installation.id]?.state === "loaded") return;
    retireSessionReads(installation.id);
    const current = sessionReads.current[installation.id] = (sessionReads.current[installation.id] ?? 0) + 1;
    const latest = () => mounted.current && sessionReads.current[installation.id] === current;
    // Same deadline as before, now owned: a newer read, a refresh or leaving the page cancels it.
    const probe = bounded();
    sessionCalls.current[installation.id] = probe.controller;
    setSessions(previous => ({ ...previous, [installation.id]: { state: "loading", items: [] } }));
    try {
      const found = await client.sessions(installation.id, probe.controller.signal);
      if (latest()) setSessions(previous => ({ ...previous, [installation.id]: { state: "loaded", items: found } }));
    } catch (error) {
      if (!latest()) return;
      // Never left on "Reading sessions…": a refused read shows as failed even while sign-in is being cleared.
      setSessions(previous => ({ ...previous, [installation.id]: { state: "failed", items: [] } }));
      denied(error);
    } finally {
      probe.done();
      if (sessionCalls.current[installation.id] === probe.controller) delete sessionCalls.current[installation.id];
    }
  }
  /** One change at a time; the shown record changes only to what the server returned. */
  async function change(key: string, run: (csrf: string) => Promise<void>, done: string, keepSessions = false) {
    if (busy) return;
    const { canChange: may, csrf: token } = allowed.current;
    if (!may || !token) {
      setNotice({ error: true, for: key, text: "Changes aren't available right now: sign in again or Refresh, then try again. Nothing was sent." });
      return;
    }
    setBusy(key); setNotice(null);
    try {
      await run(token);
      if (!mounted.current) return;
      retireSessionReads();
      setDialog(null); setRenaming(null); setNotice({ error: false, text: done, for: key });
      void refresh(keepSessions);
    } catch (error) {
      if (!mounted.current || denied(error)) return;
      setNotice({ error: true, text: writeFailureText(error), for: key });
    } finally { if (mounted.current) setBusy(null); }
  }
  const replace = (next: Installation) => setItems(previous => previous.map(item => item.id === next.id ? next : item));
  const approve = (item: Installation) => change(`approve:${item.id}`, async token => replace(await client.approve(item.id, item.revision, token)),
    `Approved ${item.name}. It can now start sessions.`);
  const revoke = (item: Installation) => change(`revoke:${item.id}`, async token => replace(await client.revokeInstallation(item.id, item.revision, token)),
    item.state === "pending" ? `Declined the pairing request from ${item.name}.`
      : `Disconnected ${item.name}. Its sessions have ended; other installations keep working.`);
  const rename = (item: Installation, name: string) => change(`rename:${item.id}`, async token => replace(await client.rename(item.id, item.revision, name, token)),
    "Name saved.");
  const revokeSession = (session: Session, installation: Installation) => change(`session:${session.id}`, async token => {
    const next = await client.revokeSession(session.id, session.revision, token);
    retireSessionReads(installation.id);
    setSessions(previous => ({ ...previous, [installation.id]: { state: "loaded",
      items: (previous[installation.id]?.items ?? []).map(item => item.id === next.id ? next : item) } }));
  }, "Session ended. The installation and its other sessions keep working.", true);

  if (load === "unavailable" || load === "checking") return null;
  const header = <div className="panel-title"><h2>Installations</h2><Badge tone="amber">STAGING</Badge>
    <span className="panel-action">
      {readAt && <span className="muted">Read {relativeAge(new Date(readAt).toISOString()) ?? "just now"}</span>}
      <button type="button" className="secondary small" disabled={demo || load === "loading"} onClick={() => void refresh()}>Refresh</button>
    </span></div>;
  if (load === "not_owner") return <section className="panel installations" aria-label="Installations">{header}
    <p className="panel-description">Only the staging owner can see and manage agent installations.</p>
    <p className="panel-footnote">Managing installations needs the configured owner's own Hub sign-in on this Hub. If that's you, <a
      className="text-link" href="/api/hub/v1/auth/login">sign in to this Hub</a>, then come back to Agents.</p></section>;

  const pendingAt = (item: Installation) => item.state === "pending" && !!item.expires_at && Date.parse(item.expires_at) <= now;
  return (
    <section className="panel installations" aria-label="Installations">
      {header}
      <p className="panel-description">Each agent installation has its own connection, so one computer can have separate Codex and Claude
        installations. Ending a session stops just that session. Disconnecting an installation ends all of its sessions and needs a fresh,
        approved pairing to use again; other installations, even on the same computer, keep working. No passwords or tokens are shown here.</p>
      <PairingGuide open={load === "loaded" && items.length === 0} />
      {catalog ? <p className="notice">Staging only. A connection approved here can send and receive its own messages. It can use Hub tasks,
        reviews and other project operations only where you grant them under Hub access. Running work and waking an agent's chat stay separate.</p>
        : <p className="notice">Staging only: this is the identity and messaging foundation. A connection approved here can send and receive its own
        messages, but it can't use Hub tasks or reviews, and it doesn't wake an agent's chat automatically.</p>}
      {demo && <p className="notice">Demo: these installations are examples. Nothing can be approved or revoked.</p>}
      {failure && <p role="alert" className="notice error">{failure}</p>}
      {notice && !dialog && <p role={notice.error ? "alert" : "status"} className={notice.error ? "notice error" : "notice"}>{notice.text}</p>}
      {load === "loading" && <p role="status">Reading installations…</p>}
      {load === "failed" && !items.length ? null : FAMILIES.map(family => {
        const rows = items.filter(item => item.family === family);
        return <section key={family} className="installation-family" aria-label={`${FAMILY_NAME[family]} installations`}>
          <h3>{FAMILY_NAME[family]} <span className="tab-count">{rows.length}</span></h3>
          {rows.length === 0 ? <p className="panel-footnote">No {FAMILY_NAME[family]} installations yet.</p> : <ul className="installation-list">
            {rows.map(item => {
              const lapsed = pendingAt(item);
              return <li key={item.id} className={`installation-row state-${item.state}`}>
                <div className="installation-main">
                  {renaming?.id === item.id ? <form className="installation-rename" onSubmit={event => {
                    event.preventDefault(); void rename(item, renaming.name); }}>
                    <label className="sr-only" htmlFor={`rename-${item.id}`}>Name</label>
                    <input id={`rename-${item.id}`} value={renaming.name} autoFocus
                      onChange={event => setRenaming({ id: item.id, name: event.target.value })} />
                    <button className="primary small" disabled={!canChange || !nameFits(renaming.name) || busy !== null}>{busy === `rename:${item.id}` ? "Saving…" : "Save"}</button>
                    <button type="button" className="secondary small" onClick={() => setRenaming(null)}>Cancel</button>
                  </form> : <strong>{item.name}</strong>}
                  <span className="installation-meta">Says it runs on <code>{item.host_label}</code> · added {relativeAge(item.created_at)}
                    {item.approved_by && <> · approved by {actorName(item.approved_by, items, hub.session?.actor)}</>}</span>
                  {item.state === "pending" && <div className="pairing" aria-label="Pairing check">
                    <span>Pairing code <code>{item.pairing_code}</code></span>
                    <span>Fingerprint <code>{item.key_fingerprint}</code></span>
                    <span className={lapsed ? "notice error" : "muted"}>{lapsed ? "This request ran out. Start pairing again on that computer."
                      : `Request ends ${untilText(item.expires_at, now)}.`}</span>
                  </div>}
                  {item.state === "active" && item.expires_at && <span className="installation-meta">Approval ends {untilText(item.expires_at, now)}
                    <span title={exact(item.expires_at)}> ({exact(item.expires_at)})</span></span>}
                  {item.reason && <span className="installation-meta">{reasonText(item.reason)}</span>}
                  {item.revoked_at && <span className="installation-meta">Revoked {relativeAge(item.revoked_at)}{item.revoked_by ? ` by ${actorName(item.revoked_by, items, hub.session?.actor)}` : ""}.</span>}
                </div>
                <div className="installation-side">
                  <Badge tone={lapsed ? "neutral" : STATE_TONE[item.state]}>{lapsed ? "Request ran out" : STATE_LABEL[item.state]}</Badge>
                  <div className="note-actions">
                    {item.state === "pending" && !lapsed && <button type="button" className="primary small" disabled={!canChange || busy !== null}
                      onClick={() => { setNotice(null); setDialog({ kind: "approve", item }); }}>Check and approve…</button>}
                    {(item.state === "active" || item.state === "pending") && <button type="button" className="secondary small" disabled={!canChange || busy !== null}
                      onClick={() => setRenaming({ id: item.id, name: item.name })}>Rename</button>}
                    {item.state === "pending" && !lapsed && <button type="button" className="secondary small danger"
                      disabled={!canChange || busy !== null} onClick={() => { setNotice(null); setDialog({ kind: "revoke", item }); }}>Decline…</button>}
                    {item.state !== "pending" && <button type="button" className="text-button" aria-expanded={open === item.id}
                      onClick={() => void readSessions(item)}>{open === item.id ? "Hide sessions" : "Show sessions"}</button>}
                    {catalog && item.state !== "pending" && <button type="button" className="text-button" aria-expanded={access === item.id}
                      onClick={() => setAccess(previous => previous === item.id ? null : item.id)}>{access === item.id ? "Hide Hub access" : "Hub access"}</button>}
                  </div>
                </div>
                {access === item.id && catalog && <InstallationHubAccess key={`${item.id}:${accessEpoch}`} hub={hub} installation={item} catalog={catalog} client={grants}
                  allowed={() => allowed.current} sessions={sessions[item.id]?.items} onDenied={denied} />}
                {open === item.id && <SessionList installation={item} entry={sessions[item.id]} now={now}
                  canChange={canChange && item.state === "active"} busy={busy}
                  onRevoke={session => { setNotice(null); setDialog({ kind: "revoke-session", item: session, installation: item }); }} />}
                {item.state === "active" && <div className="installation-disconnect">
                  <span className="muted">Whole installation</span>
                  <button type="button" className="text-button danger" disabled={!canChange || busy !== null}
                    onClick={() => { setNotice(null); setDialog({ kind: "revoke", item }); }}>Disconnect this installation…</button>
                </div>}
              </li>;
            })}
          </ul>}
        </section>;
      })}
      <details className="installation-history">
        <summary>History <span className="tab-count">{audit.length}</span></summary>
        {audit.length === 0 ? <p className="panel-footnote">No changes recorded yet.</p> : <ol className="timeline" aria-label="Installation history">
          {[...audit].sort((a, b) => Date.parse(b.at) - Date.parse(a.at)).map(event => {
            const target = items.find(item => item.id === event.installation_id);
            return <li key={event.id}><span className="timeline-dot kind-task" aria-hidden="true" />
              <div><strong>{actorName(event.actor, items, hub.session?.actor)} {actionText(event.action)}</strong>
                <span className="timeline-meta">{target ? target.name : event.installation_id ?? "Hub"}{event.session_id ? ` · session ${event.session_id}` : ""}
                  {event.message_id ? ` · message ${event.message_id}` : ""}</span></div>
              <time dateTime={event.at} title={exact(event.at)}>{relativeAge(event.at)}</time></li>;
          })}
        </ol>}
      </details>
      <p className="panel-footnote">"Last used" is when a session last called the Hub. It's a transport observation: it doesn't prove the agent is
        listening, ready, or doing work. Ending a session or disconnecting an installation stops the Hub accepting it; it can't stop a program
        already running on that computer.</p>

      {dialog?.kind === "approve" && <ApproveDialog item={dialog.item} now={now} busy={busy === `approve:${dialog.item.id}`} canChange={canChange}
        error={notice?.error && notice.for === `approve:${dialog.item.id}` ? notice.text : ""}
        onApprove={() => void approve(dialog.item)} onClose={() => setDialog(null)} />}
      {dialog?.kind === "revoke" && dialog.item.state === "pending" && <Modal title={`Decline ${dialog.item.name}?`} onClose={() => setDialog(null)}>
        <p>This pairing request is refused and can't be approved later. Nothing else changes.</p>
        {notice?.error && notice.for === `revoke:${dialog.item.id}` && <p role="alert" className="notice error">{notice.text}</p>}
        <div className="note-actions">
          <button type="button" className="primary danger" disabled={!canChange || busy !== null} onClick={() => void revoke(dialog.item)}>
            {busy === `revoke:${dialog.item.id}` ? "Declining…" : "Decline request"}</button>
          <button type="button" className="secondary" onClick={() => setDialog(null)}>Cancel</button>
        </div>
      </Modal>}
      {dialog?.kind === "revoke" && dialog.item.state !== "pending" && <DisconnectDialog item={dialog.item} entry={sessions[dialog.item.id]} now={now}
        others={items.filter(other => other.id !== dialog.item.id && other.host_label === dialog.item.host_label && other.state === "active").length}
        canChange={canChange} busy={busy === `revoke:${dialog.item.id}`}
        error={notice?.error && notice.for === `revoke:${dialog.item.id}` ? notice.text : ""}
        onConfirm={() => void revoke(dialog.item)} onClose={() => setDialog(null)} />}
      {dialog?.kind === "revoke-session" && <Modal title={dialog.item.label ? `End session “${dialog.item.label}”?` : "End this session?"} onClose={() => setDialog(null)}>
        <p>Only this session ends. <strong>{dialog.installation.name}</strong> stays approved, and its other sessions keep working. The agent can
          start a new session from that computer whenever it needs one.</p>
        {notice?.error && notice.for === `session:${dialog.item.id}` && <p role="alert" className="notice error">{notice.text}</p>}
        <div className="note-actions">
          <button type="button" className="primary danger" disabled={!canChange || busy !== null} onClick={() => void revokeSession(dialog.item, dialog.installation)}>
            {busy === `session:${dialog.item.id}` ? "Ending…" : "End this session"}</button>
          <button type="button" className="secondary" onClick={() => setDialog(null)}>Cancel</button>
        </div>
      </Modal>}
    </section>
  );
}

function ApproveDialog({ item, now, busy, canChange, error, onApprove, onClose }: {
  item: Installation; now: number; busy: boolean; canChange: boolean; error: string; onApprove: () => void; onClose: () => void;
}) {
  const [checked, setChecked] = useState(false);
  const lapsed = !!item.expires_at && Date.parse(item.expires_at) <= now;
  return <Modal title={`Approve ${item.name}?`} onClose={onClose}>
    <p>On the computer that says it is <code>{item.host_label}</code>, the {FAMILY_NAME[item.family]} pairing screen should show:</p>
    <dl className="properties pairing-check">
      <div><dt>Pairing code</dt><dd><code>{item.pairing_code}</code></dd></div>
      <div><dt>Fingerprint</dt><dd><code>{item.key_fingerprint}</code></dd></div>
    </dl>
    <p className="panel-footnote">Approve only if both match exactly. If they don't, or you didn't start this pairing, leave it: the request ends
      {" "}{untilText(item.expires_at, now)} by itself. Approval lasts one day.</p>
    <label className="checkbox-field"><input type="checkbox" checked={checked} onChange={event => setChecked(event.target.checked)} />
      I checked that computer and both match</label>
    {lapsed && <p role="alert" className="notice error">This request ran out. Start pairing again on that computer.</p>}
    {error && <p role="alert" className="notice error">{error}</p>}
    <div className="note-actions">
      <button type="button" className="primary" disabled={!canChange || !checked || busy || lapsed} onClick={onApprove}>{busy ? "Approving…" : "Approve"}</button>
      <button type="button" className="secondary" onClick={onClose}>Cancel</button>
    </div>
  </Modal>;
}

function SessionList({ installation, entry, now, canChange, busy, onRevoke }: {
  installation: Installation; entry: { state: "loading" | "loaded" | "failed"; items: Session[] } | undefined; now: number;
  canChange: boolean; busy: string | null; onRevoke: (session: Session) => void;
}) {
  if (!entry || entry.state === "loading") return <p role="status" className="installation-sessions">Reading sessions…</p>;
  if (entry.state === "failed") return <p role="alert" className="notice error installation-sessions">Sessions couldn't be read. Open them again to retry.</p>;
  if (!entry.items.length) return <p className="panel-footnote installation-sessions">{installation.name} has no sessions.</p>;
  return <table className="data-table installation-sessions" aria-label={`Sessions for ${installation.name}`}>
    <thead><tr><th scope="col">Session</th><th scope="col">State</th><th scope="col">Started</th><th scope="col">Ends</th>
      <th scope="col">Last used</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
    <tbody>{entry.items.map(session => {
      const ended = session.state === "active" && Date.parse(session.expires_at) <= now;
      return <tr key={session.id}>
        <th scope="row">{session.label || <span className="muted">Unnamed</span>}<small><code>{session.id}</code></small></th>
        <td><Badge tone={session.state === "active" && !ended ? "forest" : "neutral"}>{ended ? "Ended" : session.state === "active" ? "Active"
          : session.state === "revoked" ? "Revoked" : "Expired"}</Badge></td>
        <td title={exact(session.issued_at)}>{relativeAge(session.issued_at)}</td>
        <td title={exact(session.expires_at)}>{untilText(session.expires_at, now)}</td>
        <td>{session.last_used_at ? <span title={exact(session.last_used_at)}>{relativeAge(session.last_used_at)}</span> : <span className="muted">Not yet</span>}</td>
        <td>{session.state === "active" && !ended && <button type="button" className="secondary small danger" disabled={!canChange || busy !== null}
          onClick={() => onRevoke(session)}>End session…</button>}</td>
      </tr>;
    })}</tbody>
  </table>;
}

/** How long any one panel read may take before it is cancelled and reported. */
export const REFRESH_DEADLINE_MS = 15_000;

/** How many of an installation's sessions are live, only when its session list has actually loaded. */
export function liveSessionText(entry: { state: "loading" | "loaded" | "failed"; items: Session[] } | undefined, now: number): string {
  if (!entry) return "Its sessions haven't been loaded on this page, so the count isn't shown. All of them end.";
  if (entry.state === "loading") return "Its sessions are still loading, so the count isn't shown. All of them end.";
  if (entry.state === "failed") return "Its sessions couldn't be read, so the count isn't shown. All of them end.";
  const live = entry.items.filter(session => session.state === "active" && Date.parse(session.expires_at) > now).length;
  return live === 0 ? "It has no live sessions right now." : `Its ${live} live session${live === 1 ? "" : "s"} end${live === 1 ? "s" : ""}.`;
}

function DisconnectDialog({ item, entry, now, others, canChange, busy, error, onConfirm, onClose }: {
  item: Installation; entry: { state: "loading" | "loaded" | "failed"; items: Session[] } | undefined; now: number; others: number;
  canChange: boolean; busy: boolean; error: string; onConfirm: () => void; onClose: () => void;
}) {
  const [typed, setTyped] = useState("");
  const matches = typed.trim() !== "" && typed.trim() === item.name.trim();
  return <Modal title={`Disconnect ${FAMILY_NAME[item.family]} installation “${item.name}”?`} onClose={onClose}>
    <p>This disconnects only this installation: <strong>{FAMILY_NAME[item.family]} · {item.name}</strong>, which says it runs on
      {" "}<code>{item.host_label}</code>. {liveSessionText(entry, now)} It can't start new sessions.</p>
    <p>Other installations keep working{others > 0 ? `, including ${others} other on ${item.host_label}` : ", including any other agent on the same computer"}.
      {" "}To use this installation again, pair it again from that computer and approve the new request. Its history stays.</p>
    <p className="panel-footnote">This can't be undone here, and it doesn't stop a program already running on that computer.</p>
    <label className="field">Type the installation name to confirm: <strong>{item.name}</strong>
      <input value={typed} autoComplete="off" spellCheck={false} onChange={event => setTyped(event.target.value)} /></label>
    {error && <p role="alert" className="notice error">{error}</p>}
    <div className="note-actions">
      <button type="button" className="primary danger" disabled={!canChange || busy || !matches} onClick={onConfirm}>
        {busy ? "Disconnecting…" : "Disconnect installation"}</button>
      <button type="button" className="secondary" onClick={onClose}>Cancel</button>
    </div>
  </Modal>;
}

/** Short, accurate pairing steps with this Hub's own helper downloads. The full command reference lives with the helper. */
function PairingGuide({ open }: { open: boolean }) {
  return <details className="pairing-guide" open={open}>
    <summary>Pair a new agent installation</summary>
    <ol>
      <li>On the computer that runs the agent, download the <a href="/api/agent-sessions/v1/client/agent_session_client.py" download>helper</a> and
        its <a href="/api/agent-sessions/v1/client/manifest" download>manifest</a> from this Hub. Check the helper's size and SHA-256 against the
        manifest before running it. It uses the computer's installed Python and never runs on its own.</li>
      <li>Run the helper's <code>enroll</code> command with this Hub's address, the agent (Codex or Claude), a name and a host label. It prints a
        six-digit pairing code and a fingerprint.</li>
      <li>A request appears here. Approve it only if its code and fingerprint match what that computer shows.</li>
      <li>Approval doesn't start anything: run the helper's <code>connect</code> command to start a session. Sessions last up to 15 minutes and
        approval lasts a day; neither renews silently.</li>
    </ol>
    <p className="panel-footnote">The credential stays on that computer: protected for the current user on Windows, owner-only on Linux. It's never shown
      or pasted here.</p>
  </details>;
}
