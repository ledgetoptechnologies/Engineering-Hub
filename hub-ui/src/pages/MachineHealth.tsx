/**
 * Machine health from the Hub's Prometheus: CPU, memory, disks, GPUs and the busiest processes for
 * every machine it scrapes, recorded as a device or not. Read on open, on Refresh, and once a minute
 * while the page is showing. Hidden entirely when this person can't read metrics; says so when
 * Prometheus isn't set up.
 */
import { useEffect, useState } from "react";
import { HubApiError } from "../api";
import type { Hub } from "../hub";
import { demoMachineMetrics } from "../infrastructureDemo";
import {
  formatBytes, formatRate, formatUptime, grafanaLink, groupMachines, percent, quickStats, readLinks, readMetrics, type MachineGroup, type MachineMetrics, type MachineTarget,
} from "../infrastructureMetrics";

/** Demo shows where the link sits; the address is illustrative and goes nowhere real. */
const DEMO_GRAFANA = "https://grafana.example.test/d/machine?var-host={host}";
/** The owner's Grafana link template, read once. Any failure just means no link. */
export function useGrafanaTemplate(hub: Hub): string | null {
  const [template, setTemplate] = useState<string | null>(null);
  useEffect(() => {
    if (hub.demo) { setTemplate(DEMO_GRAFANA); return; }
    const controller = new AbortController();
    readLinks(controller.signal).then(value => { if (!controller.signal.aborted) setTemplate(value); }).catch(error => {
      if (controller.signal.aborted) return;
      if (error instanceof HubApiError && error.status === 401) hub.clearPrivateWorkspace();
    });
    return () => controller.abort();
  }, [hub.demo]);
  return template;
}
function GrafanaLink({ template, group }: { template: string | null; group: MachineGroup }) {
  const link = grafanaLink(template, group.targets[0]?.host_label ?? null);
  return link ? <a className="machine-grafana" href={link} target="_blank" rel="noopener noreferrer">View in Grafana</a> : null;
}
import { href } from "../router";
import { Badge, PanelTitle } from "../ui";
import { MachineHistory } from "./MachineHistory";

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
function MachineHead({ group, link, grafana }: { group: MachineGroup; link: boolean; grafana: string | null }) {
  return <div className="machine-card-head">
    <strong>{link && group.device_id ? <a href={href({ page: "device", id: group.device_id })}>{group.title}</a> : group.title}</strong>
    {group.labels.role && <span className="infra-chip">{group.labels.role}</span>}
    {group.labels.env && <span className="infra-chip">{group.labels.env}</span>}
    {group.unregistered && <Badge tone="neutral">Not in devices</Badge>}
    {group.ambiguous && <Badge tone="amber">Matches more than one device</Badge>}
    <GrafanaLink template={grafana} group={group} />
  </div>;
}
const EXPORTER_TEXT = { node: "Linux", windows: "Windows", nvidia: "GPUs" } as const;

function statusBadge(item: MachineTarget, now: Date) {
  const seen = item.last_scrape ? ` · ${ago(item.last_scrape, now)}` : "";
  if (item.state === "unknown" || item.up === null) return <Badge tone="neutral">Unknown</Badge>;
  // An old result, failed or not, says nothing about now.
  if (item.state === "stale") return <Badge tone="amber">Out of date{seen}</Badge>;
  if (!item.up) return <Badge tone="amber">Scrape failing{seen}</Badge>;
  return <Badge tone="forest">Reporting{seen}</Badge>;
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

/** Machines keyed by recorded device, for the numbers under each row. */
export function machinesByDevice(reading: MachineReading): Map<string, MachineGroup> {
  const targets = reading.load.state === "loaded" ? reading.load.value.targets : [];
  const found = new Map<string, MachineGroup>();
  for (const group of groupMachines(targets)) if (group.device_id && !found.has(group.device_id)) found.set(group.device_id, group);
  return found;
}

function Stat({ label, ratio, value, detail }: { label: string; ratio?: number | null; value: string; detail?: string }) {
  const tone = ratio === undefined || ratio === null ? "" : ratio >= 0.9 ? " high" : ratio >= 0.75 ? " warm" : "";
  return <div className="machine-stat">
    <span className="machine-stat-label">{label}</span>
    <strong>{value}</strong>
    {detail && <small>{detail}</small>}
    {ratio !== undefined && <span className={"machine-meter-track" + tone} aria-hidden="true">{ratio !== null && <span style={{ width: `${Math.round(ratio * 100)}%` }} />}</span>}
  </div>;
}

/** CPU, memory, storage and network at a glance. The whole strip opens the device. */
export function MachineStrip({ group, deviceId, now }: { group: MachineGroup; deviceId: string; now: Date }) {
  const stats = quickStats(group);
  const link = href({ page: "device", id: deviceId });
  if (!stats.reporting) return <a className="machine-strip quiet" href={link} aria-label="Machine stats">{statusBadge(group.targets[0], now)}</a>;
  return <a className="machine-strip" href={link} aria-label="Machine stats">
    <Stat label="CPU" ratio={stats.cpu} value={stats.cpu === null ? "–" : percent(stats.cpu)} />
    <Stat label="Memory" ratio={stats.memory} value={stats.memory === null ? "–" : percent(stats.memory)} detail={stats.memory === null ? undefined : stats.memoryText} />
    <Stat label="Storage" ratio={stats.diskRatio} value={stats.diskRatio === null ? "–" : percent(stats.diskRatio)} detail={stats.disk ? `${stats.disk.mount} · ${formatBytes(stats.disk.free_bytes)} free` : undefined} />
    <Stat label="Network" value={stats.down === null ? "–" : `↓ ${formatRate(stats.down)}`} detail={stats.up === null ? undefined : `↑ ${formatRate(stats.up)}`} />
  </a>;
}

function HistoryToggle({ hub, hostKey }: { hub: Hub; hostKey: string }) {
  const [open, setOpen] = useState(false);
  return <>
    <button type="button" className="secondary machine-history-toggle" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? "Hide history" : "History"}</button>
    {open && <MachineHistory hub={hub} hostKey={hostKey} />}
  </>;
}

/** On a device page: the name Prometheus would need to report for this device to get stats. */
export interface DeviceMatch { id: string; name: string | null }

export function MachineHealth({ hub, device, reading, shownDevices }: { hub: Hub; device?: DeviceMatch; reading: MachineReading; shownDevices?: Set<string> }) {
  const { load, now, refresh } = reading;
  const grafana = useGrafanaTemplate(hub);
  const deviceId = device?.id;
  if (load.state === "hidden") return null;
  const metrics = load.state === "loaded" ? load.value : null;
  // A machine already shown under its device row isn't repeated here.
  const targets = metrics ? metrics.targets.filter(item => deviceId !== undefined ? item.device_id === deviceId
    : !(item.device_id && shownDevices?.has(item.device_id))) : [];
  if (metrics && shownDevices && targets.length === 0 && metrics.source.status === "ok") return null;
  const unmatched = Boolean(metrics && device && targets.length === 0 && (metrics.source.status === "ok" || metrics.source.status === "partial"));
  const groups = groupMachines(targets);
  const notice = metrics ? SOURCE_TEXT[metrics.source.status] : null;
  const omitted = metrics && deviceId === undefined ? omittedText(metrics) : null;
  return <section className="panel" aria-label="Machine health">
    <PanelTitle title="Machine health" count={metrics && deviceId === undefined ? groups.length : undefined}>
      <button type="button" className="secondary" disabled={load.state === "loading"} onClick={refresh}>
        {load.state === "loading" ? "Reading…" : "Refresh"}</button>
    </PanelTitle>
    {load.state === "loading" && !metrics && <p role="status">Reading machine health…</p>}
    {load.state === "failed" && <p className="notice error" role="alert">Machine health couldn't be read. Use Refresh to try again.</p>}
    {load.state === "unavailable" && <p className="notice" role="status">Machine stats aren't available on this Hub right now. Use Refresh to try again.</p>}
    {unmatched && <p className="infra-empty">{device!.name
      ? <>Prometheus isn't reporting a machine named <code>{device!.name}</code>. If it uses a different name for this device, set it under Edit as the Prometheus host.</>
      : <>No stats yet. Record a hostname, or the name Prometheus uses for this device, under Edit.</>}</p>}
    {notice && <p className="notice" role="status">{notice}</p>}
    {omitted && <p className="notice" role="status">{omitted}</p>}
    {metrics && !device && targets.length === 0 && metrics.source.status !== "not_configured" && metrics.source.status !== "unreachable" &&
      <p className="infra-empty">Prometheus isn't scraping any machines yet.</p>}
    {groups.length > 0 && <ul className="machine-list">{groups.map(group => <li key={group.key} className="machine-card">
      <MachineHead group={group} link={deviceId === undefined} grafana={grafana} />
      {group.targets.map(item => <div key={`${item.job}\n${item.instance}`} className="machine-section">
        <div className="machine-section-head">
          <span>{item.exporter ? EXPORTER_TEXT[item.exporter] : item.job} <small><code>{item.instance}</code></small></span>
          {statusBadge(item, now)}
        </div>
        {metrics?.source.status !== "unreachable" && <TargetBody item={item} />}
      </div>)}
      {deviceId === undefined && <HistoryToggle hub={hub} hostKey={group.key} />}
    </li>)}</ul>}
    {deviceId !== undefined && groups.map(group => <MachineHistory key={group.key} hub={hub} hostKey={group.key} />)}
  </section>;
}
