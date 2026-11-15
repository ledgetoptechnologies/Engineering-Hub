/**
 * The owner's one-step review of which approved installations are their own agents. Internal
 * agents can read Hub-wide things such as machine health; project-only ones keep only their
 * project grants. Nothing is pre-selected from the agent family: every Claude, Codex or Hermes
 * could be someone else's. One Confirm sends the whole selection, and the server applies it all
 * or nothing.
 */
import { useEffect, useRef, useState } from "react";
import { HubApiError } from "./api";
import {
  FAMILY_NAME, httpInstallationScopes, scopeFailureText, STATE_LABEL,
  type Installation, type InstallationScope, type InstallationScopesClient, type ScopeKind,
} from "./agentConnectionsApi";
import { relativeAge } from "./dashboardText";
import { Badge, Modal } from "./ui";

export const KIND_LABEL: Record<ScopeKind, string> = { internal: "Our agent", project_only: "Project only", unclassified: "Not set" };
const KIND_TONE: Record<ScopeKind, string> = { internal: "forest", project_only: "neutral", unclassified: "amber" };
const KIND_ACTION: Record<ScopeKind, string> = { internal: "our own agents", project_only: "project only", unclassified: "not set" };

type Load = "loading" | "hidden" | "failed" | "loaded";

export function InstallationScopeReview({ items, epoch, canChange, allowed, onDenied, client: given }: {
  items: Installation[]; epoch: number; canChange: boolean;
  allowed: () => { canChange: boolean; csrf: string }; onDenied: (error: unknown) => boolean; client?: InstallationScopesClient;
}) {
  const client = useRef(given ?? httpInstallationScopes()).current;
  const [load, setLoad] = useState<Load>("loading");
  const [scopes, setScopes] = useState<InstallationScope[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [kind, setKind] = useState<ScopeKind>("internal");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ error: boolean; text: string } | null>(null);
  const reads = useRef(0), mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; reads.current++; }; }, []);

  async function reread(keepNotice = false) {
    const current = ++reads.current;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 15_000);
    if (!keepNotice) setNotice(null);
    try {
      const found = await client.list(controller.signal);
      if (!mounted.current || current !== reads.current) return;
      setScopes(found.installations); setTruncated(found.truncated); setLoad("loaded");
      // A selection only ever names rows that still exist.
      setSelected(previous => new Set([...previous].filter(key => found.installations.some(row => row.installation_id === key))));
    } catch (error) {
      if (!mounted.current || current !== reads.current || onDenied(error)) return;
      // Before the Hub offers this list, or for someone who can't use it, the section isn't shown.
      if (error instanceof HubApiError && (error.status === 404 || error.status === 403)) { setLoad("hidden"); return; }
      setLoad(previous => previous === "loaded" ? "loaded" : "failed");
      setNotice({ error: true, text: "Which installations are your own agents couldn't be read. Refresh to retry." });
    } finally { window.clearTimeout(timer); }
  }
  useEffect(() => { void reread(); }, [epoch]); // eslint-disable-line react-hooks/exhaustive-deps

  if (load === "hidden" || (load === "loading" && scopes.length === 0)) return null;
  const byId = new Map(items.map(item => [item.id, item]));
  const rows = scopes.map(scope => ({ scope, item: byId.get(scope.installation_id) ?? null }))
    .sort((a, b) => Number(b.item?.state === "active") - Number(a.item?.state === "active")
      || (a.item?.name ?? a.scope.installation_id).localeCompare(b.item?.name ?? b.scope.installation_id));
  // Only an active installation can be marked as yours; anything can be set back to project only or not set.
  const eligible = (row: typeof rows[number]) => row.scope.kind !== kind && (kind !== "internal" || row.item?.state === "active");
  const chosen = rows.filter(row => selected.has(row.scope.installation_id) && eligible(row));
  const toggle = (key: string, on: boolean) => setSelected(previous => {
    const next = new Set(previous); if (on) next.add(key); else next.delete(key); return next; });

  async function confirm() {
    if (busy || chosen.length === 0) return;
    const { canChange: may, csrf } = allowed();
    if (!may || !csrf) { setNotice({ error: true, text: "Changes aren't available right now: sign in again or Refresh, then try again. Nothing was sent." }); return; }
    setBusy(true); setNotice(null);
    // A list read still open was asked before this write, so its answer must not replace what the write returns.
    reads.current++;
    try {
      const saved = await client.set(kind, chosen.map(row => ({ installation_id: row.scope.installation_id, expected_revision: row.scope.revision })), csrf);
      if (!mounted.current) return;
      reads.current++;
      const next = new Map(saved.map(row => [row.installation_id, row]));
      setScopes(previous => previous.map(row => next.get(row.installation_id) ?? row));
      setSelected(new Set()); setConfirming(false);
      setNotice({ error: false, text: `Marked ${saved.length} installation${saved.length === 1 ? "" : "s"} as ${KIND_ACTION[kind]}.` });
    } catch (error) {
      if (!mounted.current || onDenied(error)) return;
      setNotice({ error: true, text: scopeFailureText(error) });
      if (error instanceof HubApiError && error.code === "scope_revision_conflict") { setConfirming(false); void reread(true); }
    } finally { if (mounted.current) setBusy(false); }
  }

  const ours = scopes.filter(row => row.internal_effective).length;
  return <section className="installation-scopes" aria-label="Our own agents">
    <h3>Our own agents <span className="tab-count">{ours}</span></h3>
    {notice && <p role={notice.error ? "alert" : "status"} className={notice.error ? "notice error" : "notice"}>{notice.text}</p>}
    {load === "failed" && scopes.length === 0 ? null : <>
      <div className="filter-bar">
        <label className="field inline">Mark selected as
          <select value={kind} onChange={event => setKind(event.target.value as ScopeKind)}>
            <option value="internal">Our own agents</option>
            <option value="project_only">Project only</option>
            <option value="unclassified">Not set</option>
          </select></label>
        <button type="button" className="secondary small" disabled={!canChange || busy}
          onClick={() => setSelected(new Set(rows.filter(eligible).map(row => row.scope.installation_id)))}>
          {kind === "internal" ? "Select all active" : "Select all"}</button>
        {selected.size > 0 && <button type="button" className="text-button" onClick={() => setSelected(new Set())}>Clear selection</button>}
      </div>
      {rows.length === 0 ? <p className="panel-footnote">No approved installations yet.</p> : <ul className="installation-list">
        {rows.map(row => {
          const key = row.scope.installation_id, can = eligible(row);
          return <li key={key} className="installation-row">
            <label className="checkbox-field installation-main">
              <input type="checkbox" checked={selected.has(key) && can} disabled={!canChange || busy || !can}
                onChange={event => toggle(key, event.target.checked)} />
              <span><strong>{row.item?.name ?? "Unknown installation"}</strong>
                <span className="installation-meta">{row.item ? <>{FAMILY_NAME[row.item.family]} · says it runs on <code>{row.item.host_label}</code> · {STATE_LABEL[row.item.state]}</>
                  : <code>{key}</code>}
                  {row.scope.updated_at && <> · set {relativeAge(row.scope.updated_at)}</>}</span></span>
            </label>
            <div className="installation-side">
              <Badge tone={KIND_TONE[row.scope.kind]}>{KIND_LABEL[row.scope.kind]}</Badge>
              {row.scope.kind === "internal" && !row.scope.internal_effective && <span className="muted">Not active, so not used</span>}
            </div>
          </li>;
        })}
      </ul>}
      {truncated && <p className="panel-footnote">Only {scopes.length} installations are listed: active ones first, then the newest.
        Older ones are left out.</p>}
      <div className="note-actions">
        <button type="button" className="primary" disabled={!canChange || busy || chosen.length === 0} onClick={() => { setNotice(null); setConfirming(true); }}>
          {chosen.length === 0 ? "Select installations to change" : `Mark ${chosen.length} as ${KIND_ACTION[kind]}…`}</button>
      </div>
    </>}
    {confirming && <Modal title={`Mark ${chosen.length} installation${chosen.length === 1 ? "" : "s"} as ${KIND_ACTION[kind]}?`} onClose={() => setConfirming(false)}>
      <ul>{chosen.map(row => <li key={row.scope.installation_id}><strong>{row.item?.name ?? row.scope.installation_id}</strong>
        {row.item && <> on <code>{row.item.host_label}</code></>}: {KIND_LABEL[row.scope.kind]} → {KIND_LABEL[kind]}</li>)}</ul>
      <p className="panel-footnote">{kind === "internal" ? "These agents will be able to read Hub-wide information such as machine health. "
        : "These agents lose Hub-wide reads; their project access is unchanged. "}All of these change together, or none do.</p>
      {notice?.error && <p role="alert" className="notice error">{notice.text}</p>}
      <div className="note-actions">
        <button type="button" className="primary" disabled={!canChange || busy} onClick={() => void confirm()}>{busy ? "Saving…" : "Confirm"}</button>
        <button type="button" className="secondary" onClick={() => setConfirming(false)}>Cancel</button>
      </div>
    </Modal>}
  </section>;
}
