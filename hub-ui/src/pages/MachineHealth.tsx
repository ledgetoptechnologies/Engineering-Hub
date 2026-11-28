/**
 * Machine health from the Hub's Prometheus: CPU, memory, disks, GPUs and the busiest processes for
 * every machine it scrapes, recorded as a device or not. Read on open, on Refresh, and once a minute
 * while the page is showing. Hidden entirely when this person can't read metrics; says so when
 * Prometheus isn't set up.
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent, ReactNode, RefObject } from "react";
import { HubApiError } from "../api";
import type { Hub } from "../hub";
import { demoMachineMetrics } from "../infrastructureDemo";
import {
  HISTORY_RANGES, diskAlert, formatBytes, formatRate, formatUptime, groupMachines, isHistoryHostKey, percent, quickStats, readMetrics, type MachineGroup, type MachineMetrics, type MachineTarget,
} from "../infrastructureMetrics";
import { href, navigate, type MachineDetailRoute } from "../router";
import { Badge, PageHeader, PanelTitle } from "../ui";
import { DEFAULT_MACHINE_ORDER, machineOrderStorageKey, moveMachine, readMachineOrder, sortMachineGroups, writeMachineOrder, type MachineOrderScope } from "../machineOrder";
import { MachineHistory, RANGE_TEXT } from "./MachineHistory";

type Load = { state: "loading" } | { state: "hidden" } | { state: "failed" } | { state: "unavailable" } | { state: "loaded"; value: MachineMetrics };

const SOURCE_TEXT = {
  ok: null,
  not_configured: "This Hub isn't connected to Prometheus yet, so there's nothing to show.",
  partial: "Some readings didn't come back from Prometheus, so a few values are missing.",
  unreachable: "Prometheus didn't answer, so there are no current readings. Machines aren't known to be down.",
} as const;
function omittedText(metrics: MachineMetrics): string | null {
  const { truncated, omitted_count: omitted, warnings } = metrics.source;
  const parts: string[] = [];
  if (truncated) parts.push(omitted ? `${omitted} more ${omitted === 1 ? "target isn't" : "targets aren't"} shown.` : "Some targets aren't shown.");
  if (warnings.includes("invalid_target")) parts.push("Prometheus reported targets the Hub couldn't read; they're left out.");
  if (warnings.includes("series_truncated")) parts.push("Some machines reported more disks, GPUs or programs than are listed.");
  return parts.length ? parts.join(" ") : null;
}
const EXPORTER_TEXT = { node: "Linux", windows: "Windows", nvidia: "GPUs" } as const;

function statusView(item: MachineTarget, now: Date): { text: string; tone: "forest" | "amber" | "neutral" } {
  const seen = item.last_scrape ? ` · ${ago(item.last_scrape, now)}` : "";
  if (item.state === "unknown" || item.up === null) return { text: "Unknown", tone: "neutral" };
  // An old result, failed or not, says nothing about now.
  if (item.state === "stale") return { text: `Out of date${seen}`, tone: "amber" };
  if (!item.up) return { text: `Scrape failing${seen}`, tone: "amber" };
  return { text: `Reporting${seen}`, tone: "forest" };
}
function statusBadge(item: MachineTarget, now: Date) {
  const view = statusView(item, now);
  return <Badge tone={view.tone}>{view.text}</Badge>;
}
function ago(value: string, now: Date): string {
  const seconds = Math.max(0, Math.round((now.getTime() - Date.parse(value)) / 1000));
  return seconds < 90 ? `${seconds}s ago` : seconds < 5400 ? `${Math.round(seconds / 60)}m ago` : `${Math.round(seconds / 3600)}h ago`;
}

function Meter({ label, ratio, detail }: { label: string; ratio: number | null; detail: string }) {
  const tone = ratio === null ? "" : ratio >= 0.9 ? " high" : ratio >= 0.75 ? " warm" : "";
  return <div className="machine-meter">
    <div className="machine-meter-label"><span>{label}</span><span>{detail}</span></div>
    <div className={"machine-meter-track" + tone} role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={ratio === null ? undefined : Math.round(ratio * 100)} aria-valuetext={detail}>
      {ratio !== null && <span style={{ width: `${Math.round(ratio * 100)}%` }} />}
    </div>
  </div>;
}

function TargetBody({ item }: { item: MachineTarget }) {
  const used = item.mem_total_bytes !== null && item.mem_available_bytes !== null && item.mem_available_bytes <= item.mem_total_bytes
    ? item.mem_total_bytes - item.mem_available_bytes : null;
  return <div className="machine-target">
    {item.exporter !== "nvidia" && item.exporter !== null && <>
      <Meter label="CPU" ratio={item.cpu_busy_ratio} detail={percent(item.cpu_busy_ratio)} />
      <Meter label="Memory" ratio={used !== null && item.mem_total_bytes ? used / item.mem_total_bytes : null}
        detail={used !== null ? `${formatBytes(used)} of ${formatBytes(item.mem_total_bytes)}` : formatBytes(null)} />
      {item.disks.map(disk => { const usedDisk = disk.free_bytes === null ? null : disk.size_bytes - disk.free_bytes;
        return <Meter key={disk.mount} label={`Disk ${disk.mount}`} ratio={usedDisk === null ? null : usedDisk / disk.size_bytes}
          detail={disk.free_bytes === null ? `${formatBytes(disk.size_bytes)}, free space not reported` : `${formatBytes(disk.free_bytes)} free of ${formatBytes(disk.size_bytes)}`} />; })}
      <p className="machine-footnote">Up {formatUptime(item.uptime_seconds)}</p>
    </>}
    {item.gpus.map(gpu => <div key={gpu.index} className="machine-gpu">
      <strong>GPU {gpu.index}{gpu.model ? ` · ${gpu.model}` : ""}</strong>
      <Meter label={`GPU ${gpu.index} load`} ratio={gpu.util_ratio} detail={percent(gpu.util_ratio)} />
      <Meter label={`GPU ${gpu.index} memory`} ratio={gpu.mem_used_bytes !== null && gpu.mem_total_bytes ? gpu.mem_used_bytes / gpu.mem_total_bytes : null}
        detail={gpu.mem_used_bytes !== null && gpu.mem_total_bytes !== null ? `${formatBytes(gpu.mem_used_bytes)} of ${formatBytes(gpu.mem_total_bytes)}` : formatBytes(null)} />
      <p className="machine-footnote">Temperature {gpu.temperature_c === null ? "not reported" : `${Math.round(gpu.temperature_c)} °C`}</p>
    </div>)}
    {item.exporter === "nvidia" && item.gpus.length === 0 && <p className="infra-empty">No GPU readings came back.</p>}
    {item.top_processes.length > 0 && <table className="machine-processes">
      <caption>Most memory by program</caption>
      <thead><tr><th scope="col">Program</th><th scope="col">Copies</th><th scope="col">Memory</th></tr></thead>
      <tbody>{item.top_processes.map(row => <tr key={row.name}>
        <td>{row.name}</td><td>{row.count ?? "?"}</td><td>{formatBytes(row.working_set_bytes)}</td></tr>)}</tbody>
    </table>}
    {item.exporter === null && <p className="infra-empty">Not a recognized exporter, so only whether it's being scraped is shown.</p>}
  </div>;
}

const POLL_MS = 60_000;

export interface MachineReading { load: Load; now: Date; refresh: () => void }
export function useMachineMetrics(hub: Hub): MachineReading {
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") setAttempt(value => value + 1); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    if (hub.demo) { setNow(new Date()); setLoad({ state: "loaded", value: demoMachineMetrics() }); return; }
    const controller = new AbortController();
    setLoad(current => current.state === "loaded" ? current : { state: "loading" });
    readMetrics(controller.signal).then(value => {
      if (controller.signal.aborted) return;
      setNow(new Date());
      setLoad(value === null ? { state: "hidden" } : value === "unavailable" ? { state: "unavailable" } : { state: "loaded", value });
    }).catch(error => {
      if (controller.signal.aborted) return;
      if (error instanceof HubApiError && error.status === 401) { hub.clearPrivateWorkspace(); return; }
      setLoad({ state: "failed" });
    });
    return () => controller.abort();
  }, [hub.demo, attempt]);
  return { load, now, refresh: () => setAttempt(value => value + 1) };
}

function GroupStatus({ group }: { group: MachineGroup }) {
  const reporting = group.targets.filter(item => item.state === "fresh" && item.up === true).length;
  const failures = group.targets.some(item => item.state === "fresh" && item.up === false);
  const stale = group.targets.some(item => item.state === "stale");
  if (reporting) return <Badge tone={failures || stale ? "amber" : "forest"}>
    {reporting === group.targets.length ? `${reporting} ${reporting === 1 ? "exporter" : "exporters"} reporting`
      : `${reporting} of ${group.targets.length} exporters reporting`}
  </Badge>;
  if (stale) return <Badge tone="amber">Out of date</Badge>;
  if (failures) return <Badge tone="amber">Scrape failing</Badge>;
  return <Badge tone="neutral">Unknown</Badge>;
}

function CardStat({ label, value, ratio, wide = false }: { label: string; value: string; ratio?: number | null; wide?: boolean }) {
  const tone = ratio === null || ratio === undefined ? "" : ratio >= 0.9 ? " high" : ratio >= 0.75 ? " warm" : "";
  return <div className={wide ? "machine-card-stat wide" : "machine-card-stat"}>
    <span>{label}</span><strong>{value}</strong>
    {ratio !== undefined && <span className={`machine-card-meter${tone}`} aria-hidden="true">
      {ratio !== null && <i style={{ width: `${Math.round(ratio * 100)}%` }} />}
    </span>}
  </div>;
}

function machineRoute(group: MachineGroup, groups: MachineGroup[], inventoryReadAvailable: boolean): MachineDetailRoute {
  if (!inventoryReadAvailable || !group.device_id) return { page: "machine", key: group.key };
  // If two opaque hosts match one inventory record, keep the chosen host in the URL.
  const shared = isHistoryHostKey(group.key) && groups.some(other => other.device_id === group.device_id && other.key !== group.key);
  return { page: "device", id: group.device_id, ...(shared ? { host: group.key } : {}) };
}

interface Reorder {
  dragging: boolean; onPointerDown: (event: PointerEvent<HTMLButtonElement>) => void;
  onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => void;
}
function MachineCard({ group, destination, reorder }: { group: MachineGroup; destination: MachineDetailRoute; reorder: Reorder }) {
  const stats = quickStats(group);
  const exporters = [...new Set(group.targets.map(item => item.exporter ? EXPORTER_TEXT[item.exporter] : `Unknown · ${item.job}`))];
  const network = stats.down === null && stats.up === null ? "not reported"
    : `↓ ${formatRate(stats.down)} · ↑ ${formatRate(stats.up)}`;
  return <li className={reorder.dragging ? "machine-card dragging" : "machine-card"} data-machine-key={group.key}>
    <button type="button" className="machine-card-handle" data-handle={group.key} aria-label={`Reorder ${group.title}`}
      title="Drag, or use the arrow keys, to reorder" onPointerDown={reorder.onPointerDown} onKeyDown={reorder.onKeyDown}>
      <svg viewBox="0 0 12 18" width="12" height="18" aria-hidden="true">{[3, 9].flatMap(cx => [3, 9, 15].map(cy => <circle key={`${cx}${cy}`} cx={cx} cy={cy} r={1.6} />))}</svg>
    </button>
    <a className="machine-card-link" draggable={false} href={href(destination)} aria-label={`View ${group.title} machine details`}>
      <div className="machine-card-head">
        <strong className="machine-card-title">{group.title}</strong>
        <GroupStatus group={group} />
      </div>
      <div className="machine-card-meta">
        {group.labels.role && <span className="infra-chip">{group.labels.role}</span>}
        {group.labels.env && <span className="infra-chip">{group.labels.env}</span>}
        {group.unregistered && <Badge tone="neutral">Not in devices</Badge>}
        {group.ambiguous && <Badge tone="amber">Matches more than one device</Badge>}
        {group.targets.some(item => item.exporter === null) && <span className="machine-card-warning">Not a recognized exporter</span>}
        {diskAlert(stats) && <Badge tone="amber">{diskAlert(stats)}</Badge>}
      </div>
      <div className="machine-card-exporters">{exporters.join(" · ")}</div>
      <div className="machine-card-stats">
        <CardStat label="CPU" value={percent(stats.cpu)} ratio={stats.cpu} />
        <CardStat label="Memory" value={stats.memory === null ? "not reported" : percent(stats.memory)} ratio={stats.memory} />
        <CardStat label="Storage" value={stats.diskRatio === null ? "not reported" : percent(stats.diskRatio)} ratio={stats.diskRatio} />
        <CardStat label="Network" value={network} wide />
      </div>
      <span className="machine-card-cta">View details <span aria-hidden="true">→</span></span>
    </a>
  </li>;
}

const MOVE_KEYS: Record<string, (index: number, last: number) => number> = {
  ArrowUp: index => index - 1, ArrowLeft: index => index - 1, ArrowDown: index => index + 1, ArrowRight: index => index + 1,
  Home: () => 0, End: (_, last) => last,
};

/** Slides cards from where they were to where they are after a reorder (FLIP), unless motion is reduced. */
function useCardSlide(list: RefObject<HTMLUListElement | null>, order: string) {
  const previous = useRef(new Map<string, DOMRect>());
  useLayoutEffect(() => {
    const cards = [...list.current?.querySelectorAll<HTMLElement>("[data-machine-key]") ?? []];
    const still = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    for (const card of cards) {
      const before = previous.current.get(card.dataset.machineKey!), after = card.getBoundingClientRect();
      if (!before || still || typeof card.animate !== "function" || (before.left === after.left && before.top === after.top)) continue;
      card.animate([{ transform: `translate(${before.left - after.left}px, ${before.top - after.top}px)` }, { transform: "none" }],
        { duration: 180, easing: "cubic-bezier(0.2, 0, 0, 1)" });
    }
    previous.current = new Map(cards.map(card => [card.dataset.machineKey!, card.getBoundingClientRect()]));
  }, [order]);
}

function orderScope(hub: Hub): MachineOrderScope {
  return hub.demo ? { mode: "demo" } : { mode: "live", actor: hub.session?.actor ?? null };
}

export function MachineHealth({ hub, reading, inventoryReadAvailable }: {
  hub: Hub; reading: MachineReading; inventoryReadAvailable: boolean;
}) {
  const { load, refresh } = reading;
  const scope = orderScope(hub);
  const scopeKey = machineOrderStorageKey(scope) ?? "live-anonymous";
  const [savedOrder, setSavedOrder] = useState(() => ({ scopeKey, order: readMachineOrder(scope) }));
  const order = savedOrder.scopeKey === scopeKey ? savedOrder.order : readMachineOrder(scope);
  const [dragging, setDragging] = useState<string | null>(null);
  const [moved, setMoved] = useState("");
  const list = useRef<HTMLUListElement>(null);
  const metrics = load.state === "loaded" ? load.value : null;
  const groups = groupMachines(metrics?.targets ?? []);
  const shown = sortMachineGroups(groups, order);
  useCardSlide(list, shown.map(group => group.key).join("\n"));
  // The dragged card's node moves while dragging, so follow the pointer on the window, not the handle.
  const dragMove = useRef<(event: globalThis.PointerEvent, drop?: boolean) => void>(() => {});
  useEffect(() => {
    if (!dragging) return;
    const follow = (event: globalThis.PointerEvent) => dragMove.current(event);
    const end = (event: Event) => { if (event.type === "pointerup") dragMove.current(event as globalThis.PointerEvent, true); setDragging(null); };
    const events = [["pointermove", follow], ["pointerup", end], ["pointercancel", end], ["blur", end]] as const;
    for (const [name, handler] of events) window.addEventListener(name, handler as EventListener);
    return () => { for (const [name, handler] of events) window.removeEventListener(name, handler as EventListener); };
  }, [dragging]);
  if (load.state === "hidden") return null;
  const save = (next: string[]) => { writeMachineOrder(next, scope); setSavedOrder({ scopeKey, order: next }); };
  const move = (key: string, index: number) => {
    const from = shown.findIndex(group => group.key === key);
    const to = Math.max(0, Math.min(index, shown.length - 1));
    if (from < 0 || from === to) return false;
    save(moveMachine(shown, key, to));
    setMoved(`${shown[from].title} moved to position ${to + 1} of ${shown.length}.`);
    return true;
  };
  dragMove.current = (event, drop = false) => {
    if (!dragging || (!drop && !event.buttons)) { setDragging(null); return; }
    // While cards slide (element.animate, not CSS hover fades) they are drawn away from their slots, and
    // hit-testing would swap them straight back: wait while dragging, and on release land them first.
    const slides = list.current?.getAnimations?.({ subtree: true }).filter(item => !("transitionProperty" in item) && !("animationName" in item)) ?? [];
    if (slides.length && !drop) return;
    for (const slide of slides) slide.finish();
    const over = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>("[data-machine-key]")?.dataset.machineKey;
    if (over && over !== dragging) move(dragging, shown.findIndex(item => item.key === over));
  };
  const reorder = (group: MachineGroup): Reorder => ({
    dragging: dragging === group.key,
    onPointerDown: event => {
      if (event.button) return;
      event.preventDefault();
      event.currentTarget.releasePointerCapture?.(event.pointerId);
      setDragging(group.key);
    },
    onKeyDown: event => {
      const step = MOVE_KEYS[event.key];
      if (!step) return;
      event.preventDefault();
      const index = shown.findIndex(item => item.key === group.key);
      // Reordering moves the focused button's card; keep focus on it.
      if (move(group.key, step(index, shown.length - 1)))
        requestAnimationFrame(() => list.current?.querySelector<HTMLElement>(`[data-handle="${CSS.escape(group.key)}"]`)?.focus());
    },
  });
  const custom = order.join("\n") !== DEFAULT_MACHINE_ORDER.join("\n");
  const notice = metrics && SOURCE_TEXT[metrics.source.status];
  const omitted = metrics && omittedText(metrics);
  const full = shown.filter(group => diskAlert(quickStats(group)));
  return <section className="panel" aria-label="Machine health">
    <PanelTitle title="Machine health" count={metrics ? groups.length : undefined}>
      {custom && groups.length > 1 && <button type="button" className="secondary" onClick={() => { save([...DEFAULT_MACHINE_ORDER]); setMoved("Order reset."); }}>Reset order</button>}
      <button type="button" className="secondary" disabled={load.state === "loading"} onClick={refresh}>
        {load.state === "loading" ? "Reading…" : "Refresh"}</button>
    </PanelTitle>
    {load.state === "loading" && <p role="status">Reading machine health…</p>}
    {load.state === "failed" && <p className="notice error" role="alert">Machine health couldn't be read. Use Refresh to try again.</p>}
    {load.state === "unavailable" && <p className="notice" role="status">Machine stats aren't available on this Hub right now. Use Refresh to try again.</p>}
    {notice && <p className="notice" role="status">{notice}</p>}
    {omitted && <p className="notice" role="status">{omitted}</p>}
    {metrics && groups.length === 0 && !notice && <p className="infra-empty">Prometheus isn't scraping any machines yet.</p>}
    {full.length > 0 && <p className="notice amber" role="status">
      {full.map(group => `${group.title}: ${diskAlert(quickStats(group))}`).join(" · ")}</p>}
    {groups.length > 0 && <>
      <ul className={dragging ? "machine-list reordering" : "machine-list"} ref={list}>
        {shown.map(group => <MachineCard key={group.key} group={group} reorder={reorder(group)}
          destination={machineRoute(group, groups, inventoryReadAvailable)} />)}
      </ul>
      <p className="sr-only" role="status">{moved}</p>
    </>}
  </section>;
}

function CurrentReadings({ groups, now }: { groups: MachineGroup[]; now: Date }) {
  return <section className="panel machine-current" aria-label="Current readings">
    <PanelTitle title="Current readings" />
    {groups.map(group => <div className="machine-reading-group" key={group.key}>
      {groups.length > 1 && <h3>{group.title}</h3>}
      <div className="machine-current-tags">
        {group.labels.role && <span className="infra-chip">{group.labels.role}</span>}
        {group.labels.env && <span className="infra-chip">{group.labels.env}</span>}
        {group.unregistered && <Badge tone="neutral">Not in devices</Badge>}
        {group.ambiguous && <Badge tone="amber">Matches more than one device</Badge>}
      </div>
      {group.targets.map(item => <div key={`${item.job}\n${item.instance}`} className="machine-section">
        <div className="machine-section-head">
          <span>{item.exporter ? EXPORTER_TEXT[item.exporter] : item.job} <small><code>{item.instance}</code></small></span>
          {statusBadge(item, now)}
        </div>
        <TargetBody item={item} />
      </div>)}
    </div>)}
  </section>;
}

function readingGroups(reading: MachineReading): MachineGroup[] {
  const { load } = reading;
  return load.state === "loaded" && load.value.source.status !== "unreachable" ? groupMachines(load.value.targets) : [];
}

export function MachineWorkspace({ hub, route, reading, inventoryReadAvailable, deviceName, groups = readingGroups(reading) }: {
  hub: Hub; route: MachineDetailRoute; reading: MachineReading; inventoryReadAvailable: boolean;
  deviceName?: string | null; groups?: MachineGroup[];
}) {
  const { load, refresh } = reading;
  if (load.state === "hidden" && route.page === "device") return null;
  const ordered = sortMachineGroups(groups, readMachineOrder(orderScope(hub)));
  // Older metrics payloads have display-only group keys. Show their current readings
  // by device, but never put a legacy label in a URL or send it to the history API.
  const legacyDevices = new Set<string>();
  const choices = ordered.filter(item => {
    if (isHistoryHostKey(item.key)) return true;
    if (!inventoryReadAvailable || !item.device_id || legacyDevices.has(item.device_id)) return false;
    legacyDevices.add(item.device_id);
    return true;
  });
  const group = route.page === "machine" ? groups.find(item => item.key === route.key)
    : choices.find(item => item.device_id === route.id && (!route.host || item.key === route.host));
  const canChart = group && isHistoryHostKey(group.key);
  const currentGroups = !group ? [] : canChart ? [group] : ordered.filter(item => item.device_id === group.device_id);
  const range = route.range ?? "24h";
  const source = load.state === "loaded" ? SOURCE_TEXT[load.value.source.status] : null;
  return <>
    <section className="panel machine-history-panel" aria-label="Machine history">
      <div className="machine-workspace-head">
        <div className="machine-history-title">
          <h2>Machine history</h2>
          {canChart && <span className="machine-history-source">{hub.demo ? "Demo data" : "Prometheus"} · Local time{range === "30d" ? " · 6h samples" : ""}</span>}
        </div>
        <div className="machine-toolbar">
          <label className="machine-control machine-picker">Machine
            <select value={group?.key ?? ""} disabled={!choices.length} onChange={event => {
              const selected = choices.find(item => item.key === event.target.value);
              if (selected) navigate({ ...machineRoute(selected, groups, inventoryReadAvailable), range: route.range });
            }}>
              {!group && <option disabled value="">{load.state === "loading" ? "Reading machines…" : "No matching machine"}</option>}
              {choices.map(item => <option value={item.key} key={item.key}>{item.title}</option>)}
            </select>
          </label>
          <div className="machine-control machine-range-control">
            <span id="machine-range-label">Time range</span>
            <div className="machine-range" role="group" aria-labelledby="machine-range-label">
              {HISTORY_RANGES.map(item => <button key={item} type="button" aria-pressed={item === range} title={RANGE_TEXT[item]}
                disabled={!canChart} onClick={() => navigate({ ...route, range: item })}>{item}</button>)}
            </div>
          </div>
        </div>
      </div>
      {canChart ? <MachineHistory key={`${group.key}|${range}`} hub={hub} hostKey={group.key} range={range} gpus={group.targets.flatMap(item => item.gpus)} /> : <div className="machine-history-empty">
        {load.state === "loading" && <p role="status">Reading machine health…</p>}
        {load.state === "hidden" && <p role="status">Machine stats aren't available to you on this Hub.</p>}
        {load.state === "failed" && <p role="alert">Machine health couldn't be read.</p>}
        {load.state === "unavailable" && <p role="status">Machine stats aren't available on this Hub right now.</p>}
        {source && <p role="status">{source}</p>}
        {group && <p role="status">History isn't available for this machine.</p>}
        {!group && load.state === "loaded" && ["ok", "partial"].includes(load.value.source.status) && <p>{route.page === "machine" || route.host
          ? "No current target matches this machine link. Its status is unknown."
          : deviceName ? <>Prometheus isn't reporting a machine named <code>{deviceName}</code>.</>
            : "No stats yet. A Prometheus host can be recorded in the device's declared details."}</p>}
        {(load.state === "failed" || load.state === "unavailable") && <button type="button" className="secondary" onClick={refresh}>Try again</button>}
      </div>}
    </section>
    {currentGroups.length > 0 && <CurrentReadings groups={currentGroups} now={reading.now} />}
  </>;
}

export function MachineDetailPage({ hub, actions, route, reading, inventoryReadAvailable }: {
  hub: Hub; actions: ReactNode; route: Extract<MachineDetailRoute, { page: "machine" }>; reading: MachineReading; inventoryReadAvailable: boolean;
}) {
  const groups = readingGroups(reading);
  const group = groups.find(item => item.key === route.key);
  return <>
    <div className="machine-detail-heading"><PageHeader eyebrow="INFRASTRUCTURE / MACHINE HEALTH" title={group?.title ?? "Machine health"}>{actions}</PageHeader></div>
    <p className="infra-back"><a href={href({ page: "infrastructure" })}>← Machine health</a></p>
    <div className="infra-detail">
      <MachineWorkspace hub={hub} route={route} reading={reading} groups={groups} inventoryReadAvailable={inventoryReadAvailable} />
    </div>
  </>;
}
