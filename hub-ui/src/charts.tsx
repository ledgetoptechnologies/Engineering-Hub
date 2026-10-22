/**
 * Small dependency-free charts for the home overview. Stage is ordinal, so it uses one
 * blue ramp light→dark; counts are always shown as text too (legend, labels, sr table).
 */
import { useState } from "react";
import type { Task } from "./types";

export const STAGES: { key: Task["status"]; label: string; color: string }[] = [
  { key: "assigned", label: "Assigned", color: "#bcd4f5" },
  { key: "claimed", label: "In progress", color: "#6fa3e8" },
  { key: "in_review", label: "In review", color: "#2f6fd1" },
  { key: "verified", label: "Review passed", color: "#173d86" },
];

export function stageCounts(tasks: Task[]): Record<Task["status"], number> {
  const counts = { assigned: 0, claimed: 0, in_review: 0, verified: 0 };
  for (const task of tasks) counts[task.status]++;
  return counts;
}

/** One horizontal 100% bar; segments separated by a 2px surface gap. */
export function StageBar({ tasks, label, height = 14 }: { tasks: Task[]; label: string; height?: number }) {
  const counts = stageCounts(tasks);
  const total = tasks.length;
  const [hover, setHover] = useState<string | null>(null);
  if (!total) return <div className="stage-bar empty" role="img" aria-label={`${label}: no tasks`} style={{ height }} />;
  const summary = STAGES.filter((s) => counts[s.key]).map((s) => `${counts[s.key]} ${s.label.toLowerCase()}`).join(", ");
  return (
    <div className="stage-bar-wrap">
      <div className="stage-bar" role="img" aria-label={`${label}: ${summary}`} style={{ height }}>
        {STAGES.filter((s) => counts[s.key]).map((s) => (
          <span key={s.key} className="stage-seg" style={{ flexGrow: counts[s.key], background: s.color }}
            onMouseEnter={() => setHover(s.key)} onMouseLeave={() => setHover(null)}
            title={`${s.label}: ${counts[s.key]} of ${total}`} />
        ))}
      </div>
      {hover && (
        <span className="chart-tip" role="status">
          {STAGES.find((s) => s.key === hover)!.label}: <strong>{counts[hover as Task["status"]]}</strong> of {total}
        </span>
      )}
    </div>
  );
}

export function StageLegend({ tasks }: { tasks: Task[] }) {
  const counts = stageCounts(tasks);
  return (
    <ul className="chart-legend" aria-label="Stages">
      {STAGES.map((s) => (
        <li key={s.key}><span className="swatch" style={{ background: s.color }} aria-hidden="true" />{s.label}
          <strong>{counts[s.key]}</strong></li>
      ))}
    </ul>
  );
}

/** Changes per day for the last `days` days (local time), from record update times. */
export function dailyCounts(times: string[], days = 14, now = new Date()): { day: Date; count: number }[] {
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate() - (days - 1));
  const buckets = Array.from({ length: days }, (_, i) => ({ day: new Date(start.getFullYear(), start.getMonth(), start.getDate() + i), count: 0 }));
  for (const value of times) {
    const at = Date.parse(value);
    if (!Number.isFinite(at)) continue;
    const index = Math.floor((at - start.getTime()) / 86_400_000);
    if (index >= 0 && index < days) buckets[index].count++;
  }
  return buckets;
}

export function DayBars({ series, label }: { series: { day: Date; count: number }[]; label: string }) {
  const [hover, setHover] = useState<number | null>(null);
  const max = Math.max(1, ...series.map((point) => point.count));
  const width = 600, height = 110, gap = 6, bar = (width - gap * (series.length - 1)) / series.length;
  const fmt = (day: Date) => day.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return (
    <figure className="day-bars">
      <svg viewBox={`0 0 ${width} ${height + 18}`} role="img"
        aria-label={`${label}: ${series.map((p) => `${fmt(p.day)} ${p.count}`).join(", ")}`}>
        <line x1="0" x2={width} y1={height} y2={height} className="axis" />
        {series.map((point, i) => {
          const h = point.count ? Math.max(3, (point.count / max) * (height - 8)) : 0;
          const x = i * (bar + gap);
          return (
            <g key={i} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
              <rect x={x} y={0} width={bar} height={height} fill="transparent" />
              {h > 0 && <rect x={x} y={height - h} width={bar} height={h} rx={2}
                className={hover === i ? "bar active" : "bar"} />}
            </g>
          );
        })}
        <text x="0" y={height + 15} className="tick">{fmt(series[0].day)}</text>
        <text x={width} y={height + 15} className="tick" textAnchor="end">Today</text>
      </svg>
      <figcaption className="chart-tip-line" aria-live="polite">
        {hover !== null ? <>{fmt(series[hover].day)}: <strong>{series[hover].count}</strong> {series[hover].count === 1 ? "change" : "changes"}</>
          : <>Busiest day: <strong>{max === 1 && !series.some((p) => p.count) ? 0 : max}</strong> changes</>}
      </figcaption>
    </figure>
  );
}
