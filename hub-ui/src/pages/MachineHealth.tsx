/**
 * Machine health from the Hub's configured Prometheus: CPU, memory, disks, GPUs and the busiest
 * processes per machine. Read on open and on Refresh only; never polled. Hidden entirely when this
 * Hub has no metrics or this person can't read them.
 */
import { useEffect, useState } from "react";
import { HubApiError } from "../api";
import type { Hub } from "../hub";
import {
  formatBytes, formatUptime, groupMachines, percent, readMetrics, type MachineMetrics, type MachineTarget,
} from "../infrastructureMetrics";
import { Badge, PanelTitle } from "../ui";

type Load = { state: "loading" } | { state: "hidden" } | { state: "failed" } | { state: "loaded"; value: MachineMetrics };

const SOURCE_TEXT = {
  ok: null,
  partial: "Some readings didn't come back from Prometheus, so a few values are missing.",
  unreachable: "Prometheus didn't answer, so nothing below is current. Machines aren't known to be down.",
} as const;
const EXPORTER_TEXT = { node: "Linux", windows: "Windows", nvidia: "GPUs" } as const;

function statusBadge(item: MachineTarget, now: Date) {
  const seen = item.last_scrape ? ` · ${ago(item.last_scrape, now)}` : "";
  if (item.state === "unknown" || item.up === null) return <Badge tone="neutral">Unknown</Badge>;
  if (!item.up) return <Badge tone="amber">Scrape failing{seen}</Badge>;
  if (item.state === "stale") return <Badge tone="amber">Out of date{seen}</Badge>;
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
  const used = item.mem_total_bytes !== null && item.mem_available_bytes !== null ? item.mem_total_bytes - item.mem_available_bytes : null;
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
    {item.exporter === "nvidia" && item.gpus.length === 0 && <p className="infra-empty">No GPUs reported.</p>}
    {item.top_processes.length > 0 && <table className="machine-processes">
      <caption>Most memory by program</caption>
      <thead><tr><th scope="col">Program</th><th scope="col">Copies</th><th scope="col">Memory</th></tr></thead>
      <tbody>{item.top_processes.map(row => <tr key={row.name}>
        <td>{row.name}</td><td>{row.count ?? "?"}</td><td>{formatBytes(row.working_set_bytes)}</td></tr>)}</tbody>
    </table>}
    {item.exporter === null && <p className="infra-empty">Only scrape status is configured for this target.</p>}
  </div>;
}

export function MachineHealth({ hub, deviceId }: { hub: Hub; deviceId?: string }) {
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    if (hub.demo) { setLoad({ state: "hidden" }); return; }
    const controller = new AbortController();
    setLoad(current => current.state === "loaded" ? current : { state: "loading" });
    readMetrics(controller.signal).then(value => {
      if (controller.signal.aborted) return;
      setNow(new Date());
      setLoad(value === null ? { state: "hidden" } : { state: "loaded", value });
    }).catch(error => {
      if (controller.signal.aborted) return;
      if (error instanceof HubApiError && error.status === 401) { hub.clearPrivateWorkspace(); return; }
      setLoad({ state: "failed" });
    });
    return () => controller.abort();
  }, [hub.demo, attempt]);
  if (load.state === "hidden") return null;
  const metrics = load.state === "loaded" ? load.value : null;
  const targets = metrics ? metrics.targets.filter(item => deviceId === undefined || item.device_id === deviceId) : [];
  if (metrics && deviceId !== undefined && targets.length === 0) return null;
  const groups = groupMachines(targets);
  const notice = metrics ? SOURCE_TEXT[metrics.source] : null;
  return <section className="panel" aria-label="Machine health">
    <PanelTitle title="Machine health" count={metrics && deviceId === undefined ? groups.length : undefined}>
      <button type="button" className="secondary" disabled={load.state === "loading"} onClick={() => setAttempt(value => value + 1)}>
        {load.state === "loading" ? "Reading…" : "Refresh"}</button>
    </PanelTitle>
    <p className="panel-description">Read from Prometheus when you open this page or press Refresh. Values are what the exporters last reported.</p>
    {load.state === "loading" && !metrics && <p role="status">Reading machine health…</p>}
    {load.state === "failed" && <p className="notice error" role="alert">Machine health couldn't be read. Use Refresh to try again.</p>}
    {notice && <p className="notice" role="status">{notice}</p>}
    {metrics && targets.length === 0 && <p className="infra-empty">No Prometheus targets are mapped to devices yet.</p>}
    {groups.length > 0 && <ul className="machine-list">{groups.map(group => <li key={group.key} className="machine-card">
      <div className="machine-card-head">
        <strong>{group.title}</strong>
        {group.labels.role && <span className="infra-chip">{group.labels.role}</span>}
        {group.labels.env && <span className="infra-chip">{group.labels.env}</span>}
      </div>
      {group.targets.map(item => <div key={`${item.job}\n${item.instance}`} className="machine-section">
        <div className="machine-section-head">
          <span>{item.exporter ? EXPORTER_TEXT[item.exporter] : item.job} <small><code>{item.instance}</code></small></span>
          {statusBadge(item, now)}
        </div>
        {metrics?.source !== "unreachable" && <TargetBody item={item} />}
      </div>)}
    </li>)}</ul>}
  </section>;
}
