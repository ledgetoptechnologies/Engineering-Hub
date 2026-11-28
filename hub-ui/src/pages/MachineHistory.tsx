/**
 * History for one machine: CPU, memory, storage, network and, where a DCGM exporter reports, each GPU,
 * over a fixed range. One measure per chart so no chart needs two scales. Read when opened or when the
 * range changes; never polled. A missing sample breaks the line, so a gap reads as a gap and never as zero.
 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { HubApiError } from "../api";
import type { Hub } from "../hub";
import { demoMachineHistory } from "../infrastructureDemo";
import {
  formatBytes, formatCelsius, formatHertz, formatRate, formatWatts, niceTop, readHistory,
  type Gpu, type HistoryPoint, type HistoryRange, type HistorySeries, type MachineHistoryData,
} from "../infrastructureMetrics";

type Load = { state: "loading" } | { state: "failed" } | { state: "gone" } | { state: "unavailable" } | { state: "loaded"; value: MachineHistoryData };
interface Line { name: string; slot: number; points: HistoryPoint[] }
type Format = (value: number | null) => string;
/** How one chart reads: exact text for the tooltip, the axis base, and a fixed top when the unit has one. */
interface Scale { format: Format; base: 1000 | 1024; top?: number }

export const RANGE_TEXT: Record<HistoryRange, string> = {
  "1h": "Last hour", "6h": "Last 6 hours", "24h": "Last 24 hours", "7d": "Last 7 days", "30d": "Last 30 days",
};
const SOURCE_TEXT = {
  ok: null, partial: "Some history didn't come back from Prometheus or wasn't charted, so a chart may be missing or incomplete.",
  not_configured: "This Hub isn't connected to Prometheus yet.", unreachable: "Prometheus didn't answer, so there's no history to show.",
} as const;
const MAX_DISKS = 4, HEIGHT = 200, LEFT = 64, RIGHT = 10, TOP = 10, BOTTOM = 24;
const exactPercent: Format = value => value === null ? "not reported" : `${(value * 100).toFixed(1)}%`;
const RATIO: Scale = { format: exactPercent, base: 1000, top: 1 };
const RATE: Scale = { format: formatRate, base: 1024 };

function latest(points: HistoryPoint[]): number | null {
  for (let index = points.length - 1; index >= 0; index--) if (points[index][1] !== null) return points[index][1];
  return null;
}
function peak(points: HistoryPoint[]): number | null {
  const values = points.flatMap(point => point[1] === null ? [] : [point[1]]);
  return values.length ? Math.max(...values) : null;
}
function timeLabel(seconds: number, range: HistoryRange, compact: boolean): string {
  const date = new Date(seconds * 1000);
  return range === "7d" || range === "30d" ? date.toLocaleDateString(undefined, { month: compact ? "numeric" : "short", day: "numeric" })
    : date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}
function fullTime(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
/** Unbroken runs of samples; each is its own line and filled area, so a gap stays a gap. */
function runs(points: HistoryPoint[]): number[][] {
  const output: number[][] = [];
  points.forEach((point, index) => {
    if (point[1] === null) return;
    if (points[index - 1]?.[1] == null) output.push([]);
    output[output.length - 1].push(index);
  });
  return output;
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

function Chart({ title, lines, range, scale, area = false, legend = lines.length > 1 }: {
  title: string; lines: Line[]; range: HistoryRange; scale: Scale; area?: boolean; legend?: boolean;
}) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const { format } = scale;
  const times = [...new Set(lines.flatMap(line => line.points.map(point => point[0])))].sort((a, b) => a - b);
  const count = times.length, span = times[count - 1] - times[0];
  const samples = lines.map(line => new Map(line.points));
  const top = scale.top ?? niceTop(Math.max(0, ...lines.map(line => peak(line.points) ?? 0)), scale.base);
  const plot = Math.max(1, width - LEFT - RIGHT), inner = HEIGHT - TOP - BOTTOM;
  const x = (moment: number) => LEFT + (span ? (moment - times[0]) / span * plot : plot / 2);
  const y = (value: number) => TOP + (1 - Math.min(value, top) / top) * inner;
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  const labels = !span ? [0.5] : plot < 330 ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1];
  const axis = (value: number) => scale === RATIO ? `${Math.round(value * 100)}%` : format(value).replace(/\.0+(?= )/, "");
  const pick = (event: PointerEvent<SVGSVGElement>) => {
    const at = event.clientX - event.currentTarget.getBoundingClientRect().left;
    setHover(times.reduce((best, moment, index) => Math.abs(x(moment) - at) < Math.abs(x(times[best]) - at) ? index : best, 0));
  };
  const keys = (event: KeyboardEvent<SVGSVGElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    setHover(current => Math.min(count - 1, Math.max(0, (current ?? count - 1) + (event.key === "ArrowLeft" ? -1 : 1))));
  };
  const summary = lines.map(line => `${legend ? line.name + " " : ""}latest ${format(latest(line.points))}, peak ${format(peak(line.points))}`).join("; ");
  return <figure className="history-chart">
    <figcaption>
      <strong>{title}</strong>
      {!legend && <span><b>{format(latest(lines[0].points))}</b> latest · {format(peak(lines[0].points))} peak</span>}
    </figcaption>
    <div className="history-plot" ref={ref}>
      <svg width="100%" height={HEIGHT} role="img" aria-label={`${title}: ${summary}`} tabIndex={0}
        onPointerDown={pick} onPointerMove={pick} onPointerLeave={() => setHover(null)} onKeyDown={keys} onBlur={() => setHover(null)}>
        {ticks.map(tick => <g key={tick}>
          <line className={tick ? "history-grid" : "history-baseline"} x1={LEFT} x2={LEFT + plot} y1={y(tick * top)} y2={y(tick * top)} />
          <text className="history-axis" x={LEFT - 7} y={y(tick * top) + 4} textAnchor="end">{axis(tick * top)}</text>
        </g>)}
        {labels.filter(share => share > 0 && share < 1).map(share =>
          <line key={`grid-${share}`} className="history-grid" x1={x(times[0] + span * share)} x2={x(times[0] + span * share)} y1={TOP} y2={TOP + inner} />)}
        {labels.map(share => {
          const at = times[0] + span * share;
          return <text key={share} className="history-axis" x={x(at)} y={HEIGHT - 6} textAnchor={share === 0 ? "start" : share === 1 ? "end" : "middle"}>{timeLabel(at, range, plot < 240)}</text>;
        })}
        {lines.map((line, slot) => {
          const points = times.map(moment => [moment, samples[slot].get(moment) ?? null] satisfies HistoryPoint);
          return <g key={line.name} className={`history-series-${line.slot}`}>
            {runs(points).map(run => {
              const d = run.map((index, step) => `${step ? "L" : "M"}${x(times[index]).toFixed(1)} ${y(points[index][1]!).toFixed(1)}`).join("");
              const first = x(times[run[0]]).toFixed(1), last = x(times[run[run.length - 1]]).toFixed(1);
              return <g key={run[0]}>
                {area && run.length > 1 && <path className="history-area" d={`${d}L${last} ${y(0)}L${first} ${y(0)}Z`} />}
                {run.length > 1 ? <path className="history-line" d={d} />
                  : <circle className="history-dot" cx={x(times[run[0]])} cy={y(points[run[0]][1]!)} r={2.5} />}
              </g>;
            })}
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
          return <span key={line.name} className={`history-series-${line.slot}`}>
            {legend && <i className="history-swatch" aria-hidden="true" />}
            <strong>{value == null ? "No data" : format(value)}</strong>{legend ? ` ${line.name}` : ""}</span>;
        })}
      </div>}
    </div>
    {legend && <ul className="history-legend" aria-label="Latest samples">{lines.map(line => <li key={line.name}>
      <span className={`history-swatch history-series-${line.slot}`} aria-hidden="true" /><span>{line.name} <strong>{format(latest(line.points))}</strong></span></li>)}</ul>}
  </figure>;
}

function bySeries(series: HistorySeries[], key: HistorySeries["key"]) { return series.filter(item => item.key === key); }

/** One GPU's charts, coloured by index so a card keeps its colour across sections and ranges. */
function GpuSection({ index, series, gpu, range }: { index: string; series: HistorySeries[]; gpu: Gpu | undefined; range: HistoryRange }) {
  const slot = Number(index) % 4 + 1;
  const line = (key: HistorySeries["key"]) => series.filter(item => item.key === key).slice(0, 1).map(item => ({ name: `GPU ${index}`, slot, points: item.points }));
  const charts: [string, HistorySeries["key"], Scale, boolean][] = [
    ["Utilization", "gpu_util", RATIO, true],
    ["Memory used", "gpu_memory", { format: formatBytes, base: 1024, top: gpu?.mem_total_bytes || undefined }, true],
    ["Temperature", "gpu_temperature", { format: formatCelsius, base: 1000 }, false],
    ["Power", "gpu_power", { format: formatWatts, base: 1000 }, true],
    ["SM clock", "gpu_clock", { format: formatHertz, base: 1000 }, false],
  ];
  return <section className="history-gpu" aria-label={`GPU ${index}`}>
    <h3>GPU {index}{gpu?.model ? ` · ${gpu.model}` : ""}</h3>
    <div className="history-grid-layout">
      {charts.map(([title, key, scale, area]) => { const lines = line(key);
        return lines.length > 0 && <Chart key={key} title={title} lines={lines} range={range} scale={scale} area={area} legend={false} />; })}
    </div>
  </section>;
}

export function MachineHistory({ hub, hostKey, range, gpus = [] }: { hub: Hub; hostKey: string; range: HistoryRange; gpus?: Gpu[] }) {
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
  const { charts, gpuSeries, hasSamples } = useMemo(() => {
    const series = data?.series.filter(item => item.points.some(([, value]) => value !== null)) ?? [];
    const single = (key: HistorySeries["key"]) => bySeries(series, key).slice(0, 1).map(item => ({ name: key, slot: 1, points: item.points }));
    // The fullest few, coloured by name so a mount keeps its colour when the order changes.
    const disks = bySeries(series, "disk").sort((a, b) => (latest(b.points) ?? 0) - (latest(a.points) ?? 0)).slice(0, MAX_DISKS)
      .sort((a, b) => (a.label ?? "").localeCompare(b.label ?? "")).map((item, index) => ({ name: item.label ?? "Disk", slot: index + 1, points: item.points }));
    const network = [...bySeries(series, "network_rx").slice(0, 1).map(item => ({ name: "Down", slot: 1, points: item.points })),
      ...bySeries(series, "network_tx").slice(0, 1).map(item => ({ name: "Up", slot: 2, points: item.points }))];
    const gpuSeries = new Map<string, HistorySeries[]>();
    for (const item of series) if (item.key.startsWith("gpu_") && item.label !== null) gpuSeries.set(item.label, [...gpuSeries.get(item.label) ?? [], item]);
    return { charts: { cpu: single("cpu"), memory: single("memory"), disks, network },
      gpuSeries: [...gpuSeries.entries()].sort(([a], [b]) => Number(a) - Number(b) || a.localeCompare(b)), hasSamples: series.length > 0 };
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
      {charts.cpu.length > 0 && <Chart title="CPU" lines={charts.cpu} range={range} scale={RATIO} area />}
      {charts.memory.length > 0 && <Chart title="Memory" lines={charts.memory} range={range} scale={RATIO} area />}
      {charts.disks.length > 0 && <Chart title="Storage used" lines={charts.disks} range={range} scale={RATIO} legend />}
      {charts.network.length > 0 && <Chart title="Network" lines={charts.network} range={range} scale={RATE} area legend />}
    </div>}
    {gpuSeries.map(([index, series]) => <GpuSection key={index} index={index} series={series} range={range}
      gpu={gpus.find(item => String(item.index) === index)} />)}
    {hasSamples && <p className="history-note">Gaps indicate missing samples.</p>}
  </div>;
}
