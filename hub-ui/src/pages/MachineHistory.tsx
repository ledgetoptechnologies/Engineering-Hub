/**
 * History for one machine: CPU, memory, storage and network over a fixed range, one measure per
 * chart so no chart needs two scales. Read when opened or when the range changes; never polled.
 * A missing sample breaks the line, so a gap reads as a gap and never as zero.
 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { HubApiError } from "../api";
import type { Hub } from "../hub";
import { demoMachineHistory } from "../infrastructureDemo";
import { formatRate, percent, readHistory, type HistoryPoint, type HistoryRange, type HistorySeries, type MachineHistoryData } from "../infrastructureMetrics";

type Load = { state: "loading" } | { state: "failed" } | { state: "gone" } | { state: "unavailable" } | { state: "loaded"; value: MachineHistoryData };
interface Line { name: string; slot: number; points: HistoryPoint[] }

export const RANGE_TEXT: Record<HistoryRange, string> = {
  "1h": "Last hour", "6h": "Last 6 hours", "24h": "Last 24 hours", "7d": "Last 7 days", "30d": "Last 30 days",
};
const SOURCE_TEXT = {
  ok: null, partial: "Some history didn't come back from Prometheus or wasn't charted, so a chart may be missing or incomplete.",
  not_configured: "This Hub isn't connected to Prometheus yet.", unreachable: "Prometheus didn't answer, so there's no history to show.",
} as const;
const MAX_DISKS = 4, HEIGHT = 156, RIGHT = 8, TOP = 12, BOTTOM = 24;

function latest(points: HistoryPoint[]): number | null {
  for (let index = points.length - 1; index >= 0; index--) if (points[index][1] !== null) return points[index][1];
  return null;
}
function peak(points: HistoryPoint[]): number | null {
  const values = points.flatMap(point => point[1] === null ? [] : [point[1]]);
  return values.length ? Math.max(...values) : null;
}
function niceMax(value: number): number {
  if (value <= 0) return 1024;
  const scale = 1024 ** Math.floor(Math.log(value) / Math.log(1024));
  return [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024].map(step => step * scale).find(step => step >= value) ?? value;
}
function timeLabel(seconds: number, range: HistoryRange, compact: boolean): string {
  const date = new Date(seconds * 1000);
  return range === "7d" || range === "30d" ? date.toLocaleDateString(undefined, { month: compact ? "numeric" : "short", day: "numeric" })
    : date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}
function fullTime(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
function path(points: HistoryPoint[], x: (index: number) => number, y: (value: number) => number): string {
  let text = "", open = false;
  points.forEach((point, index) => {
    if (point[1] === null) { open = false; return; }
    text += `${open ? "L" : "M"}${x(index).toFixed(1)} ${y(point[1]).toFixed(1)}`;
    open = true;
  });
  return text;
}
/** A lone sample between gaps has no line to sit on, so it gets a dot. */
function loneSamples(points: HistoryPoint[]): number[] {
  return points.flatMap((point, index) => point[1] !== null && points[index - 1]?.[1] == null && points[index + 1]?.[1] == null ? [index] : []);
}

function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(480);
  useEffect(() => {
    if (!ref.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(entries => { const next = entries[0]?.contentRect.width; if (next) setWidth(next); });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

function Chart({ title, lines, range, format, ratio }: { title: string; lines: Line[]; range: HistoryRange; format: (value: number | null) => string; ratio: boolean }) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const times = [...new Set(lines.flatMap(line => line.points.map(point => point[0])))].sort((a, b) => a - b);
  const count = times.length, span = times[count - 1] - times[0];
  const samples = lines.map(line => new Map(line.points));
  const top = ratio ? 1 : niceMax(Math.max(0, ...lines.map(line => peak(line.points) ?? 0)));
  const left = ratio ? 45 : 70;
  const plot = Math.max(1, width - left - RIGHT), inner = HEIGHT - TOP - BOTTOM;
  const x = (moment: number) => left + (span ? (moment - times[0]) / span * plot : plot / 2);
  const y = (value: number) => TOP + (1 - value / top) * inner;
  const ticks = [0, 0.5, 1];
  const labels = !span ? [0.5] : plot < 330 ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1];
  const pick = (event: PointerEvent<SVGSVGElement>) => {
    const at = event.clientX - event.currentTarget.getBoundingClientRect().left;
    setHover(times.reduce((best, moment, index) => Math.abs(x(moment) - at) < Math.abs(x(times[best]) - at) ? index : best, 0));
  };
  const keys = (event: KeyboardEvent<SVGSVGElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    setHover(current => Math.min(count - 1, Math.max(0, (current ?? count - 1) + (event.key === "ArrowLeft" ? -1 : 1))));
  };
  const multiple = lines.length > 1 || title === "Storage used" || title === "Network";
  const summary = lines.map(line => `${multiple ? line.name + " " : ""}latest ${format(latest(line.points))}, peak ${format(peak(line.points))}`).join("; ");
  return <figure className="history-chart">
    <figcaption>
      <strong>{title}</strong>
      {!multiple && <span><b>{format(latest(lines[0].points))}</b> latest · {format(peak(lines[0].points))} peak</span>}
    </figcaption>
    <div className="history-plot" ref={ref}>
      <svg width="100%" height={HEIGHT} role="img" aria-label={`${title}: ${summary}`} tabIndex={0}
        onPointerDown={pick} onPointerMove={pick} onPointerLeave={() => setHover(null)} onKeyDown={keys} onBlur={() => setHover(null)}>
        {ticks.map(tick => <g key={tick}>
          <line className="history-grid" x1={left} x2={left + plot} y1={y(tick * top)} y2={y(tick * top)} />
          <text className="history-axis" x={left - 7} y={y(tick * top) + 4} textAnchor="end">{ratio ? `${tick * 100}%` : formatRate(tick * top)}</text>
        </g>)}
        {labels.map(share => {
          const at = times[0] + span * share;
          return <text key={share} className="history-axis" x={x(at)} y={HEIGHT - 6} textAnchor={share === 0 ? "start" : share === 1 ? "end" : "middle"}>{timeLabel(at, range, plot < 240)}</text>;
        })}
        {lines.map((line, slot) => {
          const points = times.map(moment => [moment, samples[slot].get(moment) ?? null] satisfies HistoryPoint);
          return <g key={line.name} className={`history-series-${line.slot}`}>
            <path className="history-line" d={path(points, index => x(times[index]), y)} />
            {loneSamples(points).map(index => <circle key={index} className="history-dot" cx={x(times[index])} cy={y(points[index][1]!)} r={2.5} />)}
          </g>;
        })}
        {hover !== null && <g>
          <line className="history-cross" x1={x(times[hover])} x2={x(times[hover])} y1={TOP} y2={TOP + inner} />
          {lines.map((line, slot) => { const value = samples[slot].get(times[hover]);
            return value == null ? null : <circle key={line.name} className={`history-mark history-series-${line.slot}`} cx={x(times[hover])} cy={y(value)} r={4} />; })}
        </g>}
      </svg>
      {hover !== null && <div className={x(times[hover]) > width / 2 ? "history-tip left" : "history-tip"} role="status">
        <small>{fullTime(times[hover])}</small>
        {lines.map((line, slot) => { const value = samples[slot].get(times[hover]);
          return <span key={line.name}><strong>{value == null ? "No data" : format(value)}</strong>{multiple ? ` ${line.name}` : ""}</span>;
        })}
      </div>}
    </div>
    {multiple && <ul className="history-legend" aria-label="Latest samples">{lines.map(line => <li key={line.name}>
      <span className={`history-swatch history-series-${line.slot}`} aria-hidden="true" /><span>{line.name} <strong>{format(latest(line.points))}</strong></span></li>)}</ul>}
  </figure>;
}

function bySeries(series: HistorySeries[], key: HistorySeries["key"]) { return series.filter(item => item.key === key); }

export function MachineHistory({ hub, hostKey, range }: { hub: Hub; hostKey: string; range: HistoryRange }) {
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (hub.demo) { setLoad({ state: "loaded", value: demoMachineHistory(hostKey, range) }); return; }
    const controller = new AbortController();
    setLoad({ state: "loading" });
    readHistory(hostKey, range, controller.signal).then(value => {
      if (!controller.signal.aborted) setLoad(value === null ? { state: "gone" } : value === "unavailable" ? { state: "unavailable" } : { state: "loaded", value });
    }).catch(error => {
      if (controller.signal.aborted) return;
      if (error instanceof HubApiError && error.status === 401) { hub.clearPrivateWorkspace(); return; }
      setLoad({ state: "failed" });
    });
    return () => controller.abort();
  }, [hub.demo, hostKey, range, attempt]);
  const data = load.state === "loaded" ? load.value : null;
  const { charts, hasSamples } = useMemo(() => {
    const series = data?.series.filter(item => item.points.some(([, value]) => value !== null)) ?? [];
    const single = (key: HistorySeries["key"]) => bySeries(series, key).slice(0, 1).map(item => ({ name: key, slot: 1, points: item.points }));
    // The fullest few, coloured by name so a mount keeps its colour when the order changes.
    const disks = bySeries(series, "disk").sort((a, b) => (latest(b.points) ?? 0) - (latest(a.points) ?? 0)).slice(0, MAX_DISKS)
      .sort((a, b) => (a.label ?? "").localeCompare(b.label ?? "")).map((item, index) => ({ name: item.label ?? "Disk", slot: index + 1, points: item.points }));
    const network = [...bySeries(series, "network_rx").slice(0, 1).map(item => ({ name: "Down", slot: 1, points: item.points })),
      ...bySeries(series, "network_tx").slice(0, 1).map(item => ({ name: "Up", slot: 2, points: item.points }))];
    return { charts: { cpu: single("cpu"), memory: single("memory"), disks, network }, hasSamples: series.length > 0 };
  }, [data]);
  const message = load.state === "loading" ? "Reading history…"
    : load.state === "gone" ? "History isn't available for this machine."
    : load.state === "failed" ? "History couldn't be read."
    : load.state === "unavailable" ? "History isn't available on this Hub right now."
    : SOURCE_TEXT[load.value.source] ?? (!hasSamples ? "No history for this range." : null);
  const retry = load.state === "failed" || load.state === "unavailable" || data?.source === "unreachable";
  return <div className="history">
    {message && <div className="history-message">
      <p role={load.state === "failed" ? "alert" : "status"}>{message}</p>
      {retry && <button type="button" className="secondary" onClick={() => setAttempt(value => value + 1)}>Try again</button>}
    </div>}
    {hasSamples && <div className="history-grid-layout">
      {charts.cpu.length > 0 && <Chart title="CPU" lines={charts.cpu} range={range} format={percent} ratio />}
      {charts.memory.length > 0 && <Chart title="Memory" lines={charts.memory} range={range} format={percent} ratio />}
      {charts.disks.length > 0 && <Chart title="Storage used" lines={charts.disks} range={range} format={percent} ratio />}
      {charts.network.length > 0 && <Chart title="Network" lines={charts.network} range={range} format={formatRate} ratio={false} />}
    </div>}
    {hasSamples && <p className="history-note">Gaps indicate missing samples.</p>}
  </div>;
}
