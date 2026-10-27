/**
 * Owner-managed agent installations and their sessions (staging). The owner approves a pairing by
 * checking a public code and fingerprint shown on that computer; no credential ever reaches this
 * page. Every change waits for the server's answer: nothing here is shown as done before it is.
 */
import { useEffect, useRef, useState } from "react";
import { HubApiError } from "./api";
import {
  actionText, demoAgentConnections, FAMILIES, FAMILY_NAME, httpAgentConnections, NAME_MAX, reasonText, STATE_LABEL, STATE_TONE,
  writeFailureText, type AgentConnectionsClient, type AuditEvent, type Capabilities, type Installation, type Session,
} from "./agentConnectionsApi";
import { relativeAge } from "./dashboardText";
import type { Hub } from "./hub";
import { Badge, Modal } from "./ui";

type Load = "checking" | "unavailable" | "not_owner" | "loading" | "loaded" | "failed";
type Dialog = { kind: "approve" | "revoke"; item: Installation } | { kind: "revoke-session"; item: Session; installation: Installation };

/** How long until a time, in words; past times read as "ended". Clock is this browser's. */
export function untilText(value: string | null, now: number): string {
  if (!value) return "";
  const ms = Date.parse(value) - now;
  if (ms <= 0) return "ended";
  const minutes = Math.ceil(ms / 60_000);
  return minutes < 60 ? `in ${minutes} min` : minutes < 2880 ? `in ${Math.round(minutes / 60)} h` : `in ${Math.round(minutes / 1440)} days`;
}
const exact = (value: string) => new Date(value).toLocaleString();
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
  const csrf = hub.session?.csrf_token ?? "";
  const canChange = !demo && hub.writable && !!csrf && capabilities?.owner === true;

  // Ages and pairing deadlines move with this browser's clock; nothing is re-read from the server.
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 15_000); return () => window.clearInterval(timer); }, []);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; generation.current++; }; }, []);

  function denied(error: unknown): boolean {
    if (error instanceof HubApiError && error.status === 401) { hub.clearPrivateWorkspace(); return true; }
    return false;
  }
  async function refresh(keepSessions = false) {
    if (demo) return;
    const current = ++generation.current, controller = new AbortController();
    setFailure("");
    let probing = capabilities === null;
    try {
      const found = capabilities ?? await client.capabilities(controller.signal);
      probing = false;
      if (current !== generation.current || !mounted.current) return;
      setCapabilities(found);
      if (!found.owner) { setLoad("not_owner"); return; }
      setLoad(previous => previous === "loaded" ? "loaded" : "loading");
      const [nextItems, nextAudit] = await Promise.all([client.installations(controller.signal), client.audit(controller.signal)]);
      if (current !== generation.current || !mounted.current) return;
      setItems(nextItems); setAudit(nextAudit); setReadAt(Date.now()); setNow(Date.now()); setLoad("loaded");
      // Session lists are re-read only when someone opens them again.
      if (!keepSessions) { setSessions({}); setOpen(null); }
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
        : "Agent connections couldn't be read. Refresh to retry.");
    }
  }
  useEffect(() => { void refresh(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function readSessions(installation: Installation) {
    if (open === installation.id) { setOpen(null); return; }
    setOpen(installation.id);
    if (demo || sessions[installation.id]?.state === "loaded") return;
    setSessions(previous => ({ ...previous, [installation.id]: { state: "loading", items: [] } }));
    try {
      const found = await client.sessions(installation.id, AbortSignal.timeout(15_000));
      if (mounted.current) setSessions(previous => ({ ...previous, [installation.id]: { state: "loaded", items: found } }));
    } catch (error) {
      if (!mounted.current || denied(error)) return;
      setSessions(previous => ({ ...previous, [installation.id]: { state: "failed", items: [] } }));
    }
  }
  /** One change at a time; the shown record changes only to what the server returned. */
  async function change(key: string, run: () => Promise<void>, done: string, keepSessions = false) {
    if (busy) return;
    setBusy(key); setNotice(null);
    try {
      await run();
      if (!mounted.current) return;
      setDialog(null); setRenaming(null); setNotice({ error: false, text: done, for: key });
      void refresh(keepSessions);
    } catch (error) {
      if (!mounted.current || denied(error)) return;
      setNotice({ error: true, text: writeFailureText(error), for: key });
    } finally { if (mounted.current) setBusy(null); }
  }
  const replace = (next: Installation) => setItems(previous => previous.map(item => item.id === next.id ? next : item));
  const approve = (item: Installation) => change(`approve:${item.id}`, async () => replace(await client.approve(item.id, item.revision, csrf)),
    `Approved ${item.name}. It can now start sessions.`);
  const revoke = (item: Installation) => change(`revoke:${item.id}`, async () => replace(await client.revokeInstallation(item.id, item.revision, csrf)),
    item.state === "pending" ? `Declined the pairing request from ${item.name}.` : `Revoked ${item.name}. The Hub refuses its sessions from now on.`);
  const rename = (item: Installation, name: string) => change(`rename:${item.id}`, async () => replace(await client.rename(item.id, item.revision, name, csrf)),
    "Name saved.");
  const revokeSession = (session: Session, installation: Installation) => change(`session:${session.id}`, async () => {
    const next = await client.revokeSession(session.id, session.revision, csrf);
    setSessions(previous => ({ ...previous, [installation.id]: { state: "loaded",
      items: (previous[installation.id]?.items ?? []).map(item => item.id === next.id ? next : item) } }));
  }, "Session revoked. The installation's other sessions keep working.", true);

  if (load === "unavailable" || load === "checking") return null;
  const header = <div className="panel-title"><h2>Installations</h2><Badge tone="amber">STAGING</Badge>
    <span className="panel-action">
      {readAt && <span className="muted">Read {relativeAge(new Date(readAt).toISOString()) ?? "just now"}</span>}
      <button type="button" className="secondary small" disabled={demo || load === "loading"} onClick={() => void refresh()}>Refresh</button>
    </span></div>;
  if (load === "not_owner") return <section className="panel installations" aria-label="Installations">{header}
    <p className="panel-description">Only the staging owner can see and manage agent installations.</p></section>;

  const pendingAt = (item: Installation) => item.state === "pending" && !!item.expires_at && Date.parse(item.expires_at) <= now;
  return (
    <section className="panel installations" aria-label="Installations">
      {header}
      <p className="panel-description">Each agent install on each computer has its own connection. A new install pairs by showing a code on
        that computer; you approve it here only if the code and fingerprint match. Revoking an install ends all its sessions; revoking one
        session leaves the install working. No passwords or tokens are shown on this page.</p>
      <p className="notice">Staging only: this is the identity and messaging foundation. A connection approved here can send and receive its own
        messages, but it can't use Hub tasks or reviews, and it doesn't wake an agent's chat automatically.</p>
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
                    <input id={`rename-${item.id}`} value={renaming.name} maxLength={NAME_MAX} autoFocus
                      onChange={event => setRenaming({ id: item.id, name: event.target.value })} />
                    <button className="primary small" disabled={!renaming.name.trim() || busy !== null}>{busy === `rename:${item.id}` ? "Saving…" : "Save"}</button>
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
                    {(item.state === "active" || (item.state === "pending" && !lapsed)) && <button type="button" className="secondary small danger"
                      disabled={!canChange || busy !== null} onClick={() => { setNotice(null); setDialog({ kind: "revoke", item }); }}>
                      {item.state === "pending" ? "Decline…" : "Revoke…"}</button>}
                    {item.state !== "pending" && <button type="button" className="text-button" aria-expanded={open === item.id}
                      onClick={() => void readSessions(item)}>{open === item.id ? "Hide sessions" : "Sessions"}</button>}
                  </div>
                </div>
                {open === item.id && <SessionList installation={item} entry={sessions[item.id]} now={now}
                  canChange={canChange && item.state === "active"} busy={busy}
                  onRevoke={session => { setNotice(null); setDialog({ kind: "revoke-session", item: session, installation: item }); }} />}
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
        listening, ready, or doing work. Revoking stops the Hub accepting that install or session; it can't stop a program already running on
        that computer.</p>

      {dialog?.kind === "approve" && <ApproveDialog item={dialog.item} now={now} busy={busy === `approve:${dialog.item.id}`}
        error={notice?.error && notice.for === `approve:${dialog.item.id}` ? notice.text : ""}
        onApprove={() => void approve(dialog.item)} onClose={() => setDialog(null)} />}
      {dialog?.kind === "revoke" && <Modal title={dialog.item.state === "pending" ? `Decline ${dialog.item.name}?` : `Revoke ${dialog.item.name}?`}
        onClose={() => setDialog(null)}>
        {dialog.item.state === "pending"
          ? <p>This pairing request is refused and can't be approved later. Nothing else changes.</p>
          : <p>The Hub will refuse every session from this installation, and it can't start new ones. Other installations, including other
            {" "}{FAMILY_NAME[dialog.item.family]} installs, keep working.</p>}
        <p className="panel-footnote">This can't be undone here: to use that computer again, pair it again. It doesn't stop a program already running there.</p>
        {notice?.error && notice.for === `revoke:${dialog.item.id}` && <p role="alert" className="notice error">{notice.text}</p>}
        <div className="note-actions">
          <button type="button" className="primary danger" disabled={busy !== null} onClick={() => void revoke(dialog.item)}>
            {busy === `revoke:${dialog.item.id}` ? (dialog.item.state === "pending" ? "Declining…" : "Revoking…")
              : dialog.item.state === "pending" ? "Decline request" : "Revoke installation"}</button>
          <button type="button" className="secondary" onClick={() => setDialog(null)}>Cancel</button>
        </div>
      </Modal>}
      {dialog?.kind === "revoke-session" && <Modal title="Revoke this session?" onClose={() => setDialog(null)}>
        <p>The Hub will refuse this one session{dialog.item.label ? ` (${dialog.item.label})` : ""}. {dialog.installation.name} stays approved and its
          other sessions keep working.</p>
        {notice?.error && notice.for === `session:${dialog.item.id}` && <p role="alert" className="notice error">{notice.text}</p>}
        <div className="note-actions">
          <button type="button" className="primary danger" disabled={busy !== null} onClick={() => void revokeSession(dialog.item, dialog.installation)}>
            {busy === `session:${dialog.item.id}` ? "Revoking…" : "Revoke session"}</button>
          <button type="button" className="secondary" onClick={() => setDialog(null)}>Cancel</button>
        </div>
      </Modal>}
    </section>
  );
}

function ApproveDialog({ item, now, busy, error, onApprove, onClose }: {
  item: Installation; now: number; busy: boolean; error: string; onApprove: () => void; onClose: () => void;
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
      <button type="button" className="primary" disabled={!checked || busy || lapsed} onClick={onApprove}>{busy ? "Approving…" : "Approve"}</button>
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
          onClick={() => onRevoke(session)}>Revoke…</button>}</td>
      </tr>;
    })}</tbody>
  </table>;
}
