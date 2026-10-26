/**
 * Infrastructure: what devices the Hub knows about, what its checks last saw, and why one is down.
 * Reads on open, on Refresh and when the Hub's event feed invalidates the workspace; never polls.
 * Declared facts, probe observations and maintenance notes are shown separately, and nothing here
 * powers, restarts or changes a device.
 */
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { HubApiError } from "../api";
import type { Hub } from "../hub";
import { httpInfrastructure, writeFailureText, type DeviceDraft, type InfrastructureClient } from "../infrastructureApi";
import { demoInfrastructure } from "../infrastructureDemo";
import {
  actorLabel, ago, inNetwork, isHostname, isIpAddress, isNetwork, isSlug, maintenanceView, needsAttention, observationView,
  TEXT_MAX, type Device, type DeviceDetail, type DeviceList, type Maintenance,
} from "../infrastructureModel";
import { href, navigate, registerRouteLeaveGuard } from "../router";
import { Badge, PageHeader, PanelTitle } from "../ui";

type Load<T> = { state: "loading" } | { state: "loaded"; value: T } | { state: "failed"; message: string } | { state: "unavailable" };
type Filter = "all" | "attention" | "maintenance";

/** Ages badges and overdue notes by this browser's clock without reading anything. */
function useNow(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 15_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

/** The same message for "not allowed" and "not set up", so the page never reveals whether devices exist. */
const UNAVAILABLE = "Infrastructure isn't available to you on this Hub.";
function useClient(hub: Hub): InfrastructureClient {
  return useMemo(() => hub.demo ? demoInfrastructure() : httpInfrastructure(), [hub.demo]);
}
function useRead<T>(hub: Hub, read: (signal: AbortSignal) => Promise<T>, deps: unknown[]): [Load<T>, () => void] {
  const [load, setLoad] = useState<Load<T>>({ state: "loading" });
  const [attempt, setAttempt] = useState(0);
  const reader = useRef(read); reader.current = read;
  useEffect(() => {
    const controller = new AbortController();
    setLoad(current => current.state === "loaded" ? current : { state: "loading" });
    reader.current(controller.signal).then(value => {
      if (!controller.signal.aborted) setLoad({ state: "loaded", value });
    }).catch(error => {
      if (controller.signal.aborted) return;
      if (error instanceof HubApiError && error.status === 401) { hub.clearPrivateWorkspace(); return; }
      if (error instanceof HubApiError && [403, 404, 503].includes(error.status) && error.code !== "device_not_found") {
        setLoad({ state: "unavailable" }); return;
      }
      setLoad({ state: "failed", message: error instanceof HubApiError && error.code === "device_not_found"
        ? "This device isn't in the Hub's inventory." : "The Hub's infrastructure list couldn't be read. Use Refresh to try again." });
    });
    return () => controller.abort();
  }, [...deps, attempt]);
  return [load, () => setAttempt(value => value + 1)];
}

export function InfrastructurePage({ hub, actions, deviceId }: { hub: Hub; actions: React.ReactNode; deviceId?: string }) {
  return deviceId ? <DevicePage hub={hub} actions={actions} id={deviceId} /> : <DeviceListPage hub={hub} actions={actions} />;
}

function Unavailable({ actions }: { actions: React.ReactNode }) {
  return <>
    <PageHeader eyebrow="INFRASTRUCTURE" title="Infrastructure">{actions}</PageHeader>
    <section className="panel"><p>{UNAVAILABLE}</p></section>
  </>;
}

function DeviceListPage({ hub, actions }: { hub: Hub; actions: React.ReactNode }) {
  const client = useClient(hub);
  const now = useNow();
  const [retired, setRetired] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [creating, setCreating] = useState(false);
  const [load, refresh] = useRead<DeviceList>(hub, signal => client.list(signal, retired), [client, retired, hub.data]);
  if (load.state === "unavailable") return <Unavailable actions={actions} />;
  const list = load.state === "loaded" ? load.value : null;
  const shown = (list?.items ?? []).filter(device => filter === "all" ? true
    : filter === "maintenance" ? device.open_maintenance !== null : needsAttention(device, now));
  const count = (kind: Filter) => (list?.items ?? []).filter(device =>
    kind === "maintenance" ? device.open_maintenance !== null : needsAttention(device, now)).length;
  const canEdit = Boolean(list?.capabilities.device_write) && hub.writable && !hub.stale && !hub.demo;
  return <>
    <PageHeader eyebrow="INFRASTRUCTURE" title="Infrastructure"
      subtitle="The devices the Hub knows about, what its checks last saw, and any maintenance in progress. Nothing here powers or changes a device.">
      {actions}
    </PageHeader>
    <section className="panel infra" aria-label="Devices">
      <PanelTitle title="Devices" count={list ? shown.length : undefined}>
        {canEdit && <button type="button" className="secondary" onClick={() => setCreating(true)}>Add device</button>}
        <button type="button" className="secondary" disabled={load.state === "loading"} onClick={refresh}>
          {load.state === "loading" ? "Reading…" : "Refresh"}</button>
      </PanelTitle>
      {hub.demo && <p role="status">Demo: illustrative devices with example addresses. Nothing is read from a real network.</p>}
      <div className="filter-bar">
        <div className="chip-row" role="group" aria-label="Show">
          {(["all", "attention", "maintenance"] as const).map(kind => <button key={kind} type="button" aria-pressed={filter === kind}
            className={filter === kind ? "chip selected" : "chip"} onClick={() => setFilter(kind)}>
            {kind === "all" ? "All" : kind === "attention" ? "Needs attention" : "In maintenance"}
            {list && kind !== "all" && <span>{count(kind)}</span>}</button>)}
        </div>
        <label className="infra-toggle"><input type="checkbox" checked={retired} onChange={event => setRetired(event.target.checked)} /> Show retired</label>
      </div>
      {creating && canEdit && <DeviceEditor client={client} csrf={hub.session?.csrf_token ?? ""} device={null}
        onDone={saved => { setCreating(false); if (saved) { refresh(); navigate({ page: "device", id: saved.id }); } }} />}
      {load.state === "loading" && !list && <p role="status">Reading the device list…</p>}
      {load.state === "failed" && <p className="notice error" role="alert">{load.message}</p>}
      {list && list.items.length === 0 && <p className="infra-empty">No devices are recorded yet.</p>}
      {list && list.items.length > 0 && shown.length === 0 && <p className="infra-empty">No devices match this filter.</p>}
      {shown.length > 0 && <ul className="infra-list">
        {shown.map(device => <DeviceRow key={device.id} device={device} now={now} self={hub.session?.actor ?? null} />)}
      </ul>}
    </section>
  </>;
}

function DeviceRow({ device, now, self }: { device: Device; now: Date; self: string | null }) {
  const maintenance = device.open_maintenance ? maintenanceView(device.open_maintenance, now, self) : null;
  return <li className={device.retired ? "infra-row retired" : "infra-row"}>
    <a className="infra-row-main" href={href({ page: "device", id: device.id })}>
      <strong>{device.name}</strong>
      <small>{[device.hostname, device.addresses[0]].filter(Boolean).join(" · ") || "No address recorded"}</small>
    </a>
    <div className="infra-row-facts">
      {(device.vlan !== null || device.network) && <span className="infra-chip">{device.vlan !== null ? `VLAN ${device.vlan}` : device.network}</span>}
      {device.retired && <span className="infra-chip">Retired</span>}
    </div>
    <div className="infra-row-status">
      {device.observations.length === 0 && <Badge tone="neutral">Unknown: no checks set up</Badge>}
      {device.observations.map(item => { const view = observationView(item, now);
        return <Badge key={item.probe_id} tone={view.tone}>{view.label}: {view.line}</Badge>; })}
      {maintenance && <span className={maintenance.overdue ? "infra-maintenance overdue" : "infra-maintenance"}>Maintenance: {maintenance.line}</span>}
    </div>
  </li>;
}

function DevicePage({ hub, actions, id }: { hub: Hub; actions: React.ReactNode; id: string }) {
  const client = useClient(hub);
  const now = useNow();
  const self = hub.session?.actor ?? null;
  const [editing, setEditing] = useState(false);
  const [load, refresh] = useRead<DeviceDetail>(hub, signal => client.detail(id, signal), [client, id, hub.data]);
  if (load.state === "unavailable") return <Unavailable actions={actions} />;
  const detail = load.state === "loaded" ? load.value : null;
  const device = detail?.device ?? null;
  const writable = hub.writable && !hub.stale && !hub.demo;
  const csrf = hub.session?.csrf_token ?? "";
  return <>
    <PageHeader eyebrow="INFRASTRUCTURE" title={device?.name ?? "Device"}
      subtitle={device ? device.role : undefined}>{actions}</PageHeader>
    <p className="infra-back"><a href={href({ page: "infrastructure" })}>← All devices</a></p>
    {load.state === "loading" && !detail && <p role="status">Reading this device…</p>}
    {load.state === "failed" && <p className="notice error" role="alert">{load.message}</p>}
    {device && detail && <div className="infra-detail">
      <section className="panel" aria-label="Declared details">
        <PanelTitle title="Declared details">
          {detail.capabilities.device_write && writable && !editing && <button type="button" className="secondary" onClick={() => setEditing(true)}>Edit</button>}
          <button type="button" className="secondary" disabled={load.state === "loading"} onClick={refresh}>Refresh</button>
        </PanelTitle>
        <p className="panel-description">What's been recorded about this device. Recorded details aren't checked against the device itself.</p>
        {device.retired && <p className="notice" role="status">This device is retired. Its history is kept.</p>}
        {editing ? <DeviceEditor client={client} csrf={csrf} device={device} onDone={saved => { setEditing(false); if (saved) refresh(); }} />
          : <dl className="infra-facts">
            <dt>Hostname</dt><dd>{device.hostname ?? "Not recorded"}</dd>
            <dt>Addresses</dt><dd>{device.addresses.length ? device.addresses.map(item => <code key={item}>{item}</code>) : "Not recorded"}</dd>
            <dt>Network</dt><dd>{device.network ?? "Not recorded"}</dd>
            <dt>VLAN</dt><dd>{device.vlan ?? "Not recorded"}</dd>
            <dt>Role</dt><dd>{device.role}</dd>
            <dt>Device ID</dt><dd><code>{device.id}</code></dd>
            <dt>Last changed</dt><dd><time dateTime={device.updated_at}>{ago(device.updated_at, now)}</time></dd>
          </dl>}
      </section>
      <section className="panel" aria-label="Checks">
        <PanelTitle title="Checks" />
        <p className="panel-description">A passed check means only that the check succeeded. It doesn't prove the device is powered on or that agents can use it.</p>
        {device.observations.length === 0 ? <p className="infra-empty">No checks are set up for this device, so its state is unknown.</p>
          : <ul className="infra-checks">{device.observations.map(item => { const view = observationView(item, now);
            return <li key={item.probe_id}><div><strong>{view.label}</strong><small><code>{item.probe_id}</code></small></div>
              <Badge tone={view.tone}>{view.line}</Badge></li>; })}</ul>}
      </section>
      <MaintenanceSection client={client} csrf={csrf} detail={detail} now={now} self={self}
        canWrite={detail.capabilities.maintenance_write && writable && !device.retired} onChanged={refresh} />
    </div>}
  </>;
}

function useLeaveGuard(dirty: boolean, message: string) {
  const ref = useRef(dirty); ref.current = dirty;
  useEffect(() => registerRouteLeaveGuard(() => !ref.current || window.confirm(message)), []);
}
const when = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
const newRequestId = () => (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`);
/** datetime-local value (the browser's zone) as an absolute ISO time, or null when empty. */
function toIso(local: string): string | null {
  if (!local) return null;
  const parsed = new Date(local);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function MaintenanceSection({ client, csrf, detail, now, self, canWrite, onChanged }: {
  client: InfrastructureClient; csrf: string; detail: DeviceDetail; now: Date; self: string | null; canWrite: boolean; onChanged: () => void;
}) {
  const open = detail.device.open_maintenance;
  return <section className="panel" aria-label="Maintenance">
    <PanelTitle title="Maintenance" />
    <p className="panel-description">Notes on planned or ongoing work, so an outage has a reason attached. A note records intent; it isn't proof the work happened.</p>
    {open ? <OpenMaintenance client={client} csrf={csrf} entry={open} now={now} self={self} canWrite={canWrite} onChanged={onChanged} />
      : canWrite ? <MaintenanceForm client={client} csrf={csrf} deviceId={detail.device.id} onChanged={onChanged} />
        : <p className="infra-empty">No maintenance is open.</p>}
    <h3 className="infra-history">History</h3>
    {detail.maintenance.items.length === 0 ? <p className="infra-empty">No maintenance has been recorded.</p>
      : <ol className="infra-journal">{detail.maintenance.items.map(item => <li key={item.id}>
        <div className="infra-journal-head"><strong>{actorLabel(item.opened_by, self)}</strong>
          <span>opened <time dateTime={item.opened_at}>{ago(item.opened_at, now)}</time></span>
          <Badge tone={item.state === "open" ? "amber" : "neutral"}>{item.state === "open" ? "Open" : "Resolved"}</Badge></div>
        <p className="infra-text">{item.reason}</p>
        {item.expected_end && <small>Expected end: {when(item.expected_end)}</small>}
        {item.state === "resolved" && item.resolved_by && item.resolved_at && <div className="infra-resolution">
          <small>Resolved by {actorLabel(item.resolved_by, self)} <time dateTime={item.resolved_at}>{ago(item.resolved_at, now)}</time></small>
          <p className="infra-text">{item.resolution}</p></div>}
      </li>)}</ol>}
    {detail.maintenance.truncated && <p className="panel-footnote">Showing the 50 most recent notes.</p>}
  </section>;
}

function OpenMaintenance({ client, csrf, entry, now, self, canWrite, onChanged }: {
  client: InfrastructureClient; csrf: string; entry: Maintenance; now: Date; self: string | null; canWrite: boolean; onChanged: () => void;
}) {
  const view = maintenanceView(entry, now, self);
  const [resolving, setResolving] = useState(false);
  const [resolution, setResolution] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useLeaveGuard(resolving && resolution.trim() !== "", "Discard the unsaved resolution note?");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !resolution.trim()) return;
    setBusy(true); setError("");
    try {
      await client.resolveMaintenance(entry.id, { expected_revision: entry.revision, resolution }, csrf);
      setResolving(false); setResolution(""); onChanged();
    } catch (failure) { setError(writeFailureText(failure)); }
    finally { setBusy(false); }
  }
  return <div className={view.overdue ? "infra-open overdue" : "infra-open"}>
    <Badge tone="amber">{view.overdue ? "Open · overdue" : "Open"}</Badge>
    <p><strong>{actorLabel(entry.opened_by, self)}</strong> opened this <time dateTime={entry.opened_at}>{ago(entry.opened_at, now)}</time>.</p>
    <p className="infra-text">{entry.reason}</p>
    {entry.expected_end && <p className={view.overdue ? "amber" : undefined}>{view.overdue
      ? `Expected to end ${when(entry.expected_end)}; that's passed, but it stays open until someone resolves it.`
      : `Expected to end ${when(entry.expected_end)}.`}</p>}
    {canWrite && !resolving && <button type="button" onClick={() => setResolving(true)}>Resolve…</button>}
    {resolving && <form onSubmit={submit} aria-label="Resolve maintenance">
      <label className="field"><span className="field-label">What happened?</span>
        <textarea required maxLength={TEXT_MAX} value={resolution} onChange={event => setResolution(event.target.value)}
          placeholder="e.g. Disk replaced; checks passing again." /></label>
      {error && <p className="notice error" role="alert">{error}</p>}
      <div className="button-row">
        <button type="button" className="secondary" disabled={busy} onClick={() => { setResolving(false); setError(""); }}>Cancel</button>
        {error && <button type="button" className="secondary" disabled={busy} onClick={onChanged}>Reload</button>}
        <button type="submit" disabled={busy || !resolution.trim()}>{busy ? "Saving…" : "Resolve maintenance"}</button>
      </div>
    </form>}
  </div>;
}

function MaintenanceForm({ client, csrf, deviceId, onChanged }: { client: InfrastructureClient; csrf: string; deviceId: string; onChanged: () => void }) {
  const [reason, setReason] = useState("");
  const [end, setEnd] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // One request id per note, kept across retries, so a retried save can't open two notes.
  const requestId = useRef(newRequestId());
  useLeaveGuard(reason.trim() !== "", "Discard the unsaved maintenance note?");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !reason.trim()) return;
    if (end && toIso(end) === null) { setError("The expected end isn't a valid date and time."); return; }
    setBusy(true); setError("");
    try {
      await client.openMaintenance(deviceId, { reason, expected_end: toIso(end), client_request_id: requestId.current }, csrf);
      requestId.current = newRequestId(); setReason(""); setEnd(""); onChanged();
    } catch (failure) {
      if (failure instanceof HubApiError && failure.code === "idempotency_conflict") requestId.current = newRequestId();
      setError(writeFailureText(failure));
    } finally { setBusy(false); }
  }
  return <form className="infra-form" onSubmit={submit} aria-label="Record maintenance">
    <label className="field"><span className="field-label">Reason</span>
      <textarea required maxLength={TEXT_MAX} value={reason} onChange={event => setReason(event.target.value)}
        placeholder="e.g. Planned shutdown to replace the disk." />
      <small>{reason.length}/{TEXT_MAX}. Shown to everyone who can see infrastructure.</small></label>
    <label className="field compact"><span className="field-label">Expected end (optional)</span>
      <input type="datetime-local" value={end} onChange={event => setEnd(event.target.value)} /></label>
    {error && <p className="notice error" role="alert">{error}</p>}
    <div className="button-row">
      {error && <button type="button" className="secondary" disabled={busy} onClick={onChanged}>Reload</button>}
      <button type="submit" disabled={busy || !reason.trim()}>{busy ? "Saving…" : "Record maintenance"}</button>
    </div>
  </form>;
}

interface EditorDraft { id: string; name: string; hostname: string; addresses: string; network: string; vlan: string; role: string }
function editorProblems(draft: EditorDraft, creating: boolean): string[] {
  const problems: string[] = [];
  const addresses = draft.addresses.split(/\s+/).filter(Boolean);
  if (creating && !isSlug(draft.id)) problems.push("The device ID must be lowercase letters, numbers and dashes.");
  if (!draft.name.trim()) problems.push("Give the device a name.");
  if (!draft.role.trim()) problems.push("Say what the device is for.");
  if (draft.hostname && !isHostname(draft.hostname)) problems.push("The hostname isn't valid.");
  if (addresses.length > 16) problems.push("At most 16 addresses.");
  const bad = addresses.filter(item => !isIpAddress(item));
  if (bad.length) problems.push(`Not an IP address: ${bad.join(", ")}.`);
  if (new Set(addresses).size !== addresses.length) problems.push("An address is listed twice.");
  if (draft.network && !isNetwork(draft.network)) problems.push("The network must be in CIDR form with no host bits, e.g. 192.0.2.0/24.");
  else if (draft.network && addresses.some(item => inNetwork(item, draft.network) === false)) problems.push("Every address must be inside the network.");
  if (draft.vlan && !(/^\d{1,4}$/.test(draft.vlan) && Number(draft.vlan) >= 1 && Number(draft.vlan) <= 4094)) problems.push("The VLAN must be a number from 1 to 4094.");
  return problems;
}
function toDraft(draft: EditorDraft, retired: boolean): DeviceDraft {
  return { id: draft.id, name: draft.name.trim(), hostname: draft.hostname.trim() || null, addresses: draft.addresses.split(/\s+/).filter(Boolean),
    network: draft.network.trim() || null, vlan: draft.vlan ? Number(draft.vlan) : null, role: draft.role.trim(), retired };
}

function DeviceEditor({ client, csrf, device, onDone }: { client: InfrastructureClient; csrf: string; device: Device | null; onDone: (saved: Device | null) => void }) {
  const creating = device === null;
  const initial: EditorDraft = { id: device?.id ?? "", name: device?.name ?? "", hostname: device?.hostname ?? "",
    addresses: (device?.addresses ?? []).join("\n"), network: device?.network ?? "", vlan: device?.vlan?.toString() ?? "", role: device?.role ?? "" };
  const [draft, setDraft] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [shown, setShown] = useState(false);
  const dirty = JSON.stringify(draft) !== JSON.stringify(initial);
  useLeaveGuard(dirty, "Discard unsaved device changes?");
  const problems = editorProblems(draft, creating);
  const set = (key: keyof EditorDraft) => (event: { target: { value: string } }) => setDraft(current => ({ ...current, [key]: event.target.value }));
  async function save(retired: boolean) {
    setShown(true);
    if (busy || problems.length) return;
    setBusy(true); setError("");
    try { onDone(await client.saveDevice(toDraft(draft, retired), device?.revision ?? 0, csrf)); }
    catch (failure) { setError(writeFailureText(failure)); }
    finally { setBusy(false); }
  }
  function retire() {
    if (device && window.confirm(device.retired ? `Bring ${device.name} back into service?` : `Retire ${device.name}? It's hidden from the list but its history is kept.`))
      void save(!device.retired);
  }
  return <form className="infra-form" aria-label={creating ? "Add device" : "Edit device"} onSubmit={event => { event.preventDefault(); void save(device?.retired ?? false); }}>
    {creating && <label className="field compact"><span className="field-label">Device ID</span>
      <input required maxLength={64} value={draft.id} onChange={set("id")} placeholder="e.g. build-pc" />
      <small>Lowercase letters, numbers and dashes. It can't be changed later and doesn't depend on the address.</small></label>}
    <label className="field"><span className="field-label">Name</span><input required maxLength={100} value={draft.name} onChange={set("name")} /></label>
    <label className="field"><span className="field-label">Role</span><input required maxLength={100} value={draft.role} onChange={set("role")} placeholder="e.g. Staging builds" /></label>
    <label className="field"><span className="field-label">Hostname (optional)</span><input maxLength={253} value={draft.hostname} onChange={set("hostname")} /></label>
    <label className="field"><span className="field-label">IP addresses (optional, one per line)</span>
      <textarea value={draft.addresses} onChange={set("addresses")} /></label>
    <div className="infra-form-row">
      <label className="field compact"><span className="field-label">Network (optional)</span><input value={draft.network} onChange={set("network")} placeholder="e.g. 192.0.2.0/24" /></label>
      <label className="field compact"><span className="field-label">VLAN (optional)</span><input inputMode="numeric" value={draft.vlan} onChange={set("vlan")} /></label>
    </div>
    <small>Leave anything you don't know empty. The VLAN is never guessed from the address.</small>
    {shown && problems.length > 0 && <ul className="notice error" role="alert">{problems.map(item => <li key={item}>{item}</li>)}</ul>}
    {error && <p className="notice error" role="alert">{error}</p>}
    <div className="button-row">
      <button type="button" className="secondary" disabled={busy} onClick={() => { if (!dirty || window.confirm("Discard unsaved device changes?")) onDone(null); }}>Cancel</button>
      {!creating && <button type="button" className="secondary" disabled={busy} onClick={retire}>{device!.retired ? "Return to service" : "Retire device"}</button>}
      <button type="submit" disabled={busy}>{busy ? "Saving…" : creating ? "Add device" : "Save changes"}</button>
    </div>
  </form>;
}
