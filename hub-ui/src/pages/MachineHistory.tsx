/**
 * History for one machine: CPU, memory, storage and network over a fixed range, one measure per
 * chart so no chart needs two scales. Read when opened or when the range changes; never polled.
 * A missing sample breaks the line, so a gap reads as a gap and never as zero.
 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { HubApiError } from "../api";
import type { Hub } from "../hub";
import { demoMachineHistory } from "../infrastructureDemo";
import { HISTORY_RANGES, formatRate, percent, readHistory, type HistoryPoint, type HistoryRange, type HistorySeries, type MachineHistoryData } from "../infrastructureMetrics";

type Load = { state: "loading" } | { state: "failed" } | { state: "gone" } | { state: "unavailable" } | { state: "loaded"; value: MachineHistoryData };
interface Line { name: string; slot: number; points: HistoryPoint[] }

const RANGE_TEXT: Record<HistoryRange, string> = { "1h": "1 hour", "6h": "6 hours", "24h": "24 hours", "7d": "7 days" };
const SOURCE_TEXT = {
  ok: null, partial: "Some history didn't come back from Prometheus or wasn't charted, so a chart may be missing or incomplete.",
  not_configured: "This Hub isn't connected to Prometheus yet.", unreachable: "Prometheus didn't answer, so there's no history to show.",
} as const;
const MAX_DISKS = 4, HEIGHT = 168, LEFT = 44, RIGHT = 8, TOP = 8, BOTTOM = 22;

function latest(points: HistoryPoint[]): number | null {
  return points.at(-1)?.[1] ?? null;
}
/** The series is a snapshot, not a live reading; label its final sample's time. */
function latestText(points: HistoryPoint[], format: (value: number | null) => string): string {
  const point = points.at(-1);
  return point ? `${format(point[1])} at ${new Date(point[0] * 1000).toLocaleString()}` : "not reported";
}
export function selectHistoryDisks(series: HistorySeries[]): HistorySeries[] {
  return series.filter(item => item.key === "disk").sort((a, b) => {
    const left = latest(a.points), right = latest(b.points);
    if (left === null) return right === null ? 0 : 1;
    if (right === null) return -1;
    return right - left;
  }).slice(0, MAX_DISKS);
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
function timeLabel(seconds: number, range: HistoryRange): string {
  const date = new Date(seconds * 1000);
  return range === "7d" ? date.toLocaleDateString(undefined, { weekday: "short", day: "numeric" })
    : date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
function fullTime(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
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

export function Chart({ title, lines, range, format, ratio }: { title: string; lines: Line[]; range: HistoryRange; format: (value: number | null) => string; ratio: boolean }) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const points = lines[0].points, count = points.length;
  const top = ratio ? 1 : niceMax(Math.max(0, ...lines.map(line => peak(line.points) ?? 0)));
  const plot = Math.max(40, width - LEFT - RIGHT), inner = HEIGHT - TOP - BOTTOM;
  const x = (index: number) => LEFT + (count > 1 ? index / (count - 1) * plot : 0);
  const y = (value: number) => TOP + (1 - value / top) * inner;
  const ticks = [0, 0.5, 1];
  const labels = width < 340 ? [0, 1] : [0, 1 / 3, 2 / 3, 1];
  const pick = (event: PointerEvent<SVGSVGElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    setHover(Math.min(count - 1, Math.max(0, Math.round((event.clientX - box.left - LEFT) / plot * (count - 1)))));
  };
  const keys = (event: KeyboardEvent<SVGSVGElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    setHover(current => Math.min(count - 1, Math.max(0, (current ?? count - 1) + (event.key === "ArrowLeft" ? -1 : 1))));
  };
  const summary = lines.map(line => `${lines.length > 1 ? line.name + " " : ""}latest sample ${latestText(line.points, format)}, peak ${format(peak(line.points))}`).join("; ");
  return <figure className="history-chart">
    <figcaption>
      <strong>{title}</strong>
      {lines.length === 1 && <span>Latest sample: {latestText(points, format)} · {format(peak(points))} peak</span>}
    </figcaption>
    <div className="history-plot" ref={ref}>
      <svg width={width} height={HEIGHT} role="img" aria-label={`${title}: ${summary}`} tabIndex={0}
        onPointerMove={pick} onPointerLeave={() => setHover(null)} onKeyDown={keys} onBlur={() => setHover(null)}>
        {ticks.map(tick => <g key={tick}>
          <line className="history-grid" x1={LEFT} x2={LEFT + plot} y1={y(tick * top)} y2={y(tick * top)} />
          <text className="history-axis" x={LEFT - 6} y={y(tick * top) + 4} textAnchor="end">{ratio ? `${tick * 100}%` : formatRate(tick * top).replace("/s", "")}</text>
        </g>)}
        {labels.map(share => { const index = Math.round(share * (count - 1));
          return <text key={share} className="history-axis" x={x(index)} y={HEIGHT - 6} textAnchor={share === 0 ? "start" : share === 1 ? "end" : "middle"}>{timeLabel(points[index][0], range)}</text>; })}
        {lines.map(line => <g key={line.name} className={`history-series-${line.slot}`}>
          <path className="history-line" d={path(line.points, x, y)} />
          {loneSamples(line.points).map(index => <circle key={index} className="history-dot" cx={x(index)} cy={y(line.points[index][1]!)} r={2.5} />)}
        </g>)}
        {hover !== null && <g>
          <line className="history-cross" x1={x(hover)} x2={x(hover)} y1={TOP} y2={TOP + inner} />
          {lines.map(line => { const value = line.points[hover][1];
            return value === null ? null : <circle key={line.name} className={`history-mark history-series-${line.slot}`} cx={x(hover)} cy={y(value)} r={4} />; })}
        </g>}
      </svg>
      {hover !== null && <div className={x(hover) > width / 2 ? "history-tip left" : "history-tip"} style={{ left: x(hover) }} role="status">
        <small>{fullTime(points[hover][0])}</small>
        {lines.map(line => <span key={line.name}><strong>{line.points[hover][1] === null ? "No data" : format(line.points[hover][1])}</strong>{lines.length > 1 ? ` ${line.name}` : ""}</span>)}
      </div>}
    </div>
    {lines.length > 1 && <ul className="history-legend">{lines.map(line => <li key={line.name}>
      <span className={`history-swatch history-series-${line.slot}`} aria-hidden="true" />{line.name} <strong>{latestText(line.points, format)}</strong></li>)}</ul>}
  </figure>;
}

function bySeries(series: HistorySeries[], key: HistorySeries["key"]) { return series.filter(item => item.key === key); }

export function MachineHistory({ hub, hostKey }: { hub: Hub; hostKey: string }) {
  const [range, setRange] = useState<HistoryRange>("24h");
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
  const series = load.state === "loaded" ? load.value.series : [];
  const charts = useMemo(() => {
    const single = (key: HistorySeries["key"]) => bySeries(series, key).slice(0, 1).map(item => ({ name: key, slot: 1, points: item.points }));
    // The fullest few, coloured by name so a mount keeps its colour when the order changes.
    const disks = selectHistoryDisks(series)
      .sort((a, b) => (a.label ?? "").localeCompare(b.label ?? "")).map((item, index) => ({ name: item.label ?? "Disk", slot: index + 1, points: item.points }));
    const network = [...bySeries(series, "network_rx").slice(0, 1).map(item => ({ name: "Down", slot: 1, points: item.points })),
      ...bySeries(series, "network_tx").slice(0, 1).map(item => ({ name: "Up", slot: 2, points: item.points }))];
    return { cpu: single("cpu"), memory: single("memory"), disks, network };
  }, [series]);
  if (load.state === "gone") return null;
  const notice = load.state === "loaded" ? SOURCE_TEXT[load.value.source] : null;
  return <div className="history">
    <div className="history-head">
      <strong>History</strong>
      <div className="history-ranges" role="group" aria-label="History range">
        {HISTORY_RANGES.map(item => <button key={item} type="button" aria-pressed={range === item} aria-label={RANGE_TEXT[item]}
          className={range === item ? "chip selected" : "chip"} onClick={() => setRange(item)}>{item}</button>)}
      </div>
    </div>
    {load.state === "loading" && <p role="status">Reading history…</p>}
    {load.state === "failed" && <p className="notice error" role="alert">History couldn't be read. <button type="button" className="secondary" onClick={() => setAttempt(value => value + 1)}>Try again</button></p>}
    {load.state === "unavailable" && <p className="notice" role="status">History isn't available on this Hub right now. <button type="button" className="secondary" onClick={() => setAttempt(value => value + 1)}>Try again</button></p>}
    {notice && <p className="notice" role="status">{notice}</p>}
    {load.state === "loaded" && load.value.source !== "not_configured" && load.value.source !== "unreachable" && series.length === 0 &&
      <p className="infra-empty">No history for this range.</p>}
    {series.length > 0 && <div className="history-grid-layout">
      {charts.cpu.length > 0 && <Chart title="CPU" lines={charts.cpu} range={range} format={percent} ratio />}
      {charts.memory.length > 0 && <Chart title="Memory" lines={charts.memory} range={range} format={percent} ratio />}
      {charts.disks.length > 0 && <Chart title="Storage used" lines={charts.disks} range={range} format={percent} ratio />}
      {charts.network.length > 0 && <Chart title="Network" lines={charts.network} range={range} format={formatRate} ratio={false} />}
    </div>}
  </div>;
}
