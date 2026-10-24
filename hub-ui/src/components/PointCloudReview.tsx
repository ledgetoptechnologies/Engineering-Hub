import { useEffect, useRef, useState } from "react";

const UUID = "[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}";
const URL_PATTERN = new RegExp(`^/api/hub/v1/interactive-reviews/${UUID}/assets/${UUID}$`);
export const MAX_POINT_BYTES = 8 * 1024 * 1024;
export type Point = [number, number, number] | [number, number, number, number, number, number];
export type PointCloud = { schema: "pointcloud.v1"; points: Point[]; units?: string };
const unsupported = () => new Error("This asset is not a supported small pointcloud.v1 JSON preview.");

/** Normalize before subtraction: finite extremes must never overflow the canvas. */
export function projectPointCloud(points: Point[], view: { yaw: number; pitch: number; zoom: number },
                                  width: number, height: number): [number, number][] {
  let magnitude = 0;
  for (const p of points) magnitude = Math.max(magnitude, Math.abs(p[0]), Math.abs(p[1]), Math.abs(p[2]));
  magnitude ||= 1;
  const low = [Infinity, Infinity, Infinity], high = [-Infinity, -Infinity, -Infinity];
  for (const p of points) for (let i = 0; i < 3; i++) {
    const normalized = p[i] / magnitude;
    low[i] = Math.min(low[i], normalized); high[i] = Math.max(high[i], normalized);
  }
  const center = low.map((v, i) => (v + high[i]) / 2);
  const span = Math.max(...low.map((v, i) => high[i] - v)) || 1;
  const zoom = Number.isFinite(view.zoom) ? Math.max(0.25, Math.min(4, view.zoom)) : 1;
  const yaw = Number.isFinite(view.yaw) ? view.yaw : 0;
  const pitch = Number.isFinite(view.pitch) ? view.pitch : 0;
  const scale = Math.min(width, height) * 0.75 * zoom;
  const sy = Math.sin(yaw), cy = Math.cos(yaw), sp = Math.sin(pitch), cp = Math.cos(pitch);
  return points.map(p => {
    const x = (p[0] / magnitude - center[0]) / span;
    const y = (p[1] / magnitude - center[1]) / span;
    const z = (p[2] / magnitude - center[2]) / span;
    const rx = cy * x - sy * y, depth = sy * x + cy * y;
    const ry = cp * z - sp * depth;
    return [Math.max(-width * 4, Math.min(width * 5, width / 2 + rx * scale)),
      Math.max(-height * 4, Math.min(height * 5, height / 2 - ry * scale))];
  });
}

export function parsePointCloud(value: unknown): PointCloud {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw unsupported();
  const item = value as Record<string, unknown>;
  if (Object.keys(item).some(key => !["schema", "points", "units"].includes(key)) ||
      item.schema !== "pointcloud.v1" || !Array.isArray(item.points) ||
      item.points.length < 1 || item.points.length > 100_000 ||
      (item.units !== undefined && (typeof item.units !== "string" || !item.units.trim() ||
        item.units.length > 32 || /[\u0000-\u001f\u007f]/.test(item.units)))) throw unsupported();
  for (const point of item.points) {
    if (!Array.isArray(point) || ![3, 6].includes(point.length) ||
        point.some(value => typeof value !== "number" || !Number.isFinite(value)) ||
        point.slice(3).some(value => value < 0 || value > 255)) throw unsupported();
  }
  return item as PointCloud;
}

export async function loadPointCloud(url: string, signal: AbortSignal,
                                     fetcher: typeof fetch = fetch): Promise<PointCloud> {
  if (!URL_PATTERN.test(url) || url.trim() !== url) throw unsupported();
  const response = await fetcher(url, { signal, credentials: "same-origin", redirect: "error",
    cache: "no-store", headers: { Accept: "application/json" } });
  if (!response.ok || response.headers.get("content-type")?.split(";")[0].trim() !== "application/json" ||
      response.headers.has("content-range") || !response.body) {
    await response.body?.cancel().catch(() => undefined);
    throw unsupported();
  }
  const declared = response.headers.get("content-length");
  if (declared !== null && (!/^(0|[1-9][0-9]*)$/.test(declared) || Number(declared) > MAX_POINT_BYTES)) {
    await response.body.cancel();
    throw unsupported();
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  signal.addEventListener("abort", cancel, { once: true });
  try {
    signal.throwIfAborted();
    while (true) {
      const part = await reader.read();
      signal.throwIfAborted();
      if (part.done) break;
      total += part.value.byteLength;
      if (total > MAX_POINT_BYTES) throw unsupported();
      chunks.push(part.value);
    }
    if (declared !== null && total !== Number(declared)) throw unsupported();
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    return parsePointCloud(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)));
  } finally {
    signal.removeEventListener("abort", cancel);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export function PointCloudReview({ url }: { url: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const drag = useRef<{ id: number; x: number; y: number } | null>(null);
  const [cloud, setCloud] = useState<PointCloud | null>(null);
  const [error, setError] = useState("");
  const [view, setView] = useState({ yaw: 0, pitch: -0.35, zoom: 1 });
  useEffect(() => {
    const controller = new AbortController();
    drag.current = null;
    setCloud(null); setError(""); setView({ yaw: 0, pitch: -0.35, zoom: 1 });
    loadPointCloud(url, controller.signal).then(value => {
      if (!controller.signal.aborted) setCloud(value);
    }).catch(() => {
      if (!controller.signal.aborted) setError("Preview unavailable. Only small pointcloud.v1 JSON assets are supported; LAS/LAZ files are not downloaded by this viewer.");
    });
    return () => controller.abort();
  }, [url]);

  useEffect(() => {
    const element = canvas.current;
    if (!element || !cloud) return;
    let frame = 0;
    const draw = () => {
      const context = element.getContext("2d");
      if (!context) return;
      const width = Math.max(240, Math.min(element.clientWidth || 640, 1600));
      const height = 360;
      const ratio = Math.min(Math.max(window.devicePixelRatio || 1, 1), 2);
      element.width = Math.round(width * ratio); element.height = Math.round(height * ratio);
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.fillStyle = "#101b2a"; context.fillRect(0, 0, width, height);
      const projected = projectPointCloud(cloud.points, view, width, height);
      for (let i = 0; i < cloud.points.length; i++) {
        const p = cloud.points[i];
        context.fillStyle = p.length === 6 ? `rgb(${p[3]},${p[4]},${p[5]})` : "#77d8bc";
        context.fillRect(projected[i][0], projected[i][1], 2, 2);
      }
    };
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(draw); };
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(schedule) : null;
    observer?.observe(element); window.addEventListener("resize", schedule); schedule();
    return () => { cancelAnimationFrame(frame); observer?.disconnect(); window.removeEventListener("resize", schedule); };
  }, [cloud, view]);

  return <section aria-label="Pointcloud preview">
    <p>Drag with a mouse or finger to rotate. Use the buttons to zoom or reset. This is a visual preview, not a measurement tool.</p>
    {error ? <p role="status">{error}</p> : !cloud ? <p role="status">Loading pointcloud preview…</p> : <>
      <p>{cloud.points.length.toLocaleString()} points{cloud.units ? ` · ${cloud.units}` : ""}</p>
      <canvas ref={canvas} width={640} height={360} className="pointcloud-review-canvas" aria-label="Rotatable pointcloud; use rotation buttons for keyboard access"
        onPointerDown={event => { if (drag.current) return; drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY }; event.currentTarget.setPointerCapture(event.pointerId); }}
        onPointerMove={event => { const p = drag.current; if (!p || p.id !== event.pointerId) return;
          const dx = event.clientX - p.x, dy = event.clientY - p.y;
          drag.current = { id: p.id, x: event.clientX, y: event.clientY };
          setView(v => ({ ...v, yaw: v.yaw + dx * 0.01, pitch: Math.max(-1.5, Math.min(1.5, v.pitch + dy * 0.01)) })); }}
        onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }} onLostPointerCapture={() => { drag.current = null; }} />
      <div role="group" aria-label="Pointcloud controls">
        <button type="button" onClick={() => setView(v => ({ ...v, yaw: v.yaw - 0.2 }))}>Rotate left</button>
        <button type="button" onClick={() => setView(v => ({ ...v, yaw: v.yaw + 0.2 }))}>Rotate right</button>
        <button type="button" onClick={() => setView(v => ({ ...v, zoom: Math.min(4, v.zoom * 1.25) }))}>Zoom in</button>
        <button type="button" onClick={() => setView(v => ({ ...v, zoom: Math.max(0.25, v.zoom / 1.25) }))}>Zoom out</button>
        <button type="button" onClick={() => setView({ yaw: 0, pitch: -0.35, zoom: 1 })}>Reset view</button>
      </div>
    </>}
  </section>;
}
