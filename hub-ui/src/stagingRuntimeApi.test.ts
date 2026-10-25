import { describe, expect, it } from "vitest";
import { HubApiError } from "./api";
import { ago, containerStateLabel, countLine, FRESH_SECONDS, imageLabel, parseStagingRuntime, readStagingRuntime, sourceViews, type RuntimeSnapshot } from "./stagingRuntimeApi";
import { createDemoRuntime } from "./stagingRuntimeDemo";
import wire from "./fixtures/staging-runtime-wire-v1.json";

const now = new Date("2026-10-25T12:00:30Z");
const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const snap = (over: Partial<RuntimeSnapshot> = {}) => ({ ...structuredClone(wire), snapshot: { ...structuredClone(wire.snapshot), ...over } });
function readWith(response: Response) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetchImpl = (async (url: string, init: RequestInit) => { calls.push({ url, init }); return response; }) as unknown as typeof fetch;
  return { result: readStagingRuntime(new AbortController().signal, fetchImpl), calls };
}

describe("parseStagingRuntime", () => {
  it("accepts the wire fixture and the demo, folding engine state case and unknown states", () => {
    expect(parseStagingRuntime(wire).snapshot?.containers.map(item => item.state)).toEqual(["running", "exited"]);
    expect(parseStagingRuntime(createDemoRuntime(now)).snapshot?.containers).toHaveLength(5);
    const odd = snap(); odd.snapshot.containers[0].state = "hibernating"; odd.snapshot.containers[1].state = "Exited";
    expect(parseStagingRuntime(odd).snapshot?.containers.map(item => item.state)).toEqual(["unknown", "exited"]);
  });
  it("accepts no snapshot for an unconfigured or unreachable PC", () => {
    expect(parseStagingRuntime({ configured: false, state: "not_configured", snapshot: null }).snapshot).toBeNull();
    expect(parseStagingRuntime({ configured: true, state: "unavailable", snapshot: null }).state).toBe("unavailable");
  });
  it("refuses anything that claims EH control, an unknown schema, state or source, or a duplicated source", () => {
    const managed = snap(); (managed.snapshot.containers[0] as { managed: boolean }).managed = true;
    expect(() => parseStagingRuntime(managed)).toThrow(HubApiError);
    expect(() => parseStagingRuntime({ ...wire, snapshot: { ...wire.snapshot, schema: "hub-staging-runtime.v2" } })).toThrow(HubApiError);
    expect(() => parseStagingRuntime(snap({ sources: [{ id: "nas" as never, state: "observed", observed_at: null }] }))).toThrow(HubApiError);
    expect(() => parseStagingRuntime(snap({ sources: [wire.snapshot.sources[0], wire.snapshot.sources[0]] as never }))).toThrow(HubApiError);
    expect(() => parseStagingRuntime({ ...wire, state: "healthy" })).toThrow(HubApiError);
  });
  it("refuses control characters in names, non-hex ids and stopped services", () => {
    const named = snap(); named.snapshot.containers[0].name = "bad\nname";
    expect(() => parseStagingRuntime(named)).toThrow(HubApiError);
    const id = snap(); id.snapshot.containers[0].id = "not-hex";
    expect(() => parseStagingRuntime(id)).toThrow(HubApiError);
    const service = snap(); service.snapshot.services[0].state = "stopped";
    expect(() => parseStagingRuntime(service)).toThrow(HubApiError);
  });
});

describe("readStagingRuntime", () => {
  it("reads the runtime route as a same-origin GET", async () => {
    const { result, calls } = readWith(jsonResponse(wire));
    await expect(result).resolves.toMatchObject({ state: "ready" });
    expect(calls[0].url).toBe("/api/hub/v1/staging/runtime");
    expect(calls[0].init).toMatchObject({ method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store" });
  });
  it("treats an older Hub's missing route as unsupported, and keeps 401, 403 and 503 distinct", async () => {
    await expect(readWith(jsonResponse({ detail: { code: "gateway_route_forbidden" } }, 403)).result)
      .rejects.toMatchObject({ status: 404, code: "staging_runtime_unsupported" });
    await expect(readWith(new Response("", { status: 404 })).result).rejects.toMatchObject({ code: "staging_runtime_unsupported" });
    await expect(readWith(jsonResponse({ detail: { code: "staging_admin_required" } }, 403)).result)
      .rejects.toMatchObject({ status: 403, code: "staging_admin_required" });
    await expect(readWith(new Response("", { status: 401 })).result).rejects.toMatchObject({ status: 401 });
    await expect(readWith(new Response("<html>", { status: 503 })).result).rejects.toMatchObject({ status: 503, code: "staging_unavailable" });
    await expect(readWith(jsonResponse({ detail: { code: "staging_not_configured" } }, 503)).result)
      .rejects.toMatchObject({ status: 503, code: "staging_not_configured" });
  });
  it("refuses a non-JSON success", async () => {
    await expect(readWith(new Response("ok", { status: 200, headers: { "content-type": "text/plain" } })).result).rejects.toMatchObject({ status: 502 });
  });
});

describe("wording", () => {
  it("never words a missing, unreadable or unconfigured source as nothing running", () => {
    const views = sourceViews(parseStagingRuntime(snap({ sources: [{ id: "worker", state: "unavailable", observed_at: "2026-10-25T11:00:00Z" }] })).snapshot!, now);
    expect(views.map(view => view.listed)).toEqual([false, false]);
    expect(views[0].line).toBe("Couldn't be read (last seen 1 h ago), so what's running here is unknown.");
    expect(views[1].line).toBe("Not reported, so what's running here is unknown.");
    const off = sourceViews(parseStagingRuntime(snap({ sources: [{ id: "host", state: "not_configured", observed_at: null }] })).snapshot!, now);
    expect(off[1]).toMatchObject({ listed: false, line: "The host view isn't set up, so other programs on the PC aren't shown." });
  });
  it("splits by scope, and words a stale source the Hub sent without rows as unknown", () => {
    const [worker, host] = sourceViews(parseStagingRuntime(wire).snapshot!, now);
    expect(worker).toMatchObject({ listed: true, tone: "forest", line: "Seen just now.", stale: false });
    expect(worker.containers.map(item => item.name)).toEqual(["eh-build"]);
    expect(host).toMatchObject({ listed: true, tone: "forest", stale: false });
    expect(countLine(host.containers, host.services)).toBe("0 running · 1 not running · 1 service");
    // The Hub drops a stale source's rows, so an empty stale list must never read as "nothing running".
    const dropped = snap({ sources: [wire.snapshot.sources[0], { id: "host", state: "stale", observed_at: "2026-10-25T11:20:00Z" }] as never,
      containers: wire.snapshot.containers.filter(item => item.scope === "worker") as never, services: [] });
    expect(sourceViews(parseStagingRuntime(dropped).snapshot!, now)[1]).toMatchObject({ listed: false, tone: "amber",
      line: "Out of date (last seen 40 min ago), so what's running here is unknown." });
    // Should rows ever arrive with a stale source, they're shown as last seen.
    expect(sourceViews(parseStagingRuntime(snap({ sources: dropped.snapshot.sources as never })).snapshot!, now)[1]).toMatchObject({
      listed: true, stale: true, line: "Out of date: last seen 40 min ago. It may have changed since." });
  });
  it("ages an observed source by this browser's clock, and marks a retained observation out of date", () => {
    const snapshot = parseStagingRuntime(wire).snapshot!;
    const later = (seconds: number) => new Date(Date.parse("2026-10-25T12:00:00Z") + seconds * 1000);
    expect(sourceViews(snapshot, later(FRESH_SECONDS))[0]).toMatchObject({ stale: false, tone: "forest" });
    expect(sourceViews(snapshot, later(FRESH_SECONDS + 1))[0]).toMatchObject({ stale: true, tone: "amber",
      line: "Out of date: last seen 1 min ago. It may have changed since." });
    expect(sourceViews(snapshot, later(5), true)[0]).toMatchObject({ stale: true, tone: "amber" });
    // Unread sources stay unknown either way; ageing never turns them into a list.
    const unread = parseStagingRuntime(snap({ sources: [{ id: "worker", state: "unavailable", observed_at: null }] })).snapshot!;
    expect(sourceViews(unread, later(5), true).map(view => view.listed)).toEqual([false, false]);
    expect(containerStateLabel("running", true)).toBe("Last seen: running");
    expect(containerStateLabel("exited")).toBe("Stopped");
  });
  it("keeps to the backend's bounds: 256 containers and services, 64-hex ids, 256-character names", () => {
    const many = (n: number) => Array.from({ length: n }, (_, i) =>
      ({ ...wire.snapshot.containers[0], id: i.toString(16).padStart(64, "0") }));
    expect(parseStagingRuntime(snap({ containers: many(256) as never })).snapshot?.containers).toHaveLength(256);
    expect(() => parseStagingRuntime(snap({ containers: many(257) as never }))).toThrow(HubApiError);
    const services = (n: number) => Array.from({ length: n }, (_, i) => ({ name: `s${i}`, state: "running", scope: "host" }));
    expect(() => parseStagingRuntime(snap({ services: services(257) as never }))).toThrow(HubApiError);
    const shortId = snap(); shortId.snapshot.containers[0].id = "a".repeat(63);
    expect(() => parseStagingRuntime(shortId)).toThrow(HubApiError);
    const longName = snap(); longName.snapshot.services[0].name = "n".repeat(256);
    expect(parseStagingRuntime(longName).snapshot?.services[0].name).toHaveLength(256);
    longName.snapshot.services[0].name = "n".repeat(257);
    expect(() => parseStagingRuntime(longName)).toThrow(HubApiError);
  });
  it("shows an image without its registry address or digest", () => {
    expect(imageLabel("registry.example.test:5000/eh/build@sha256:aaaa")).toBe("build");
    expect(imageLabel("192.168.1.9:5000/viewer:5")).toBe("viewer:5");
    expect(imageLabel("viewer:5")).toBe("viewer:5");
  });
  it("words ages", () => {
    expect(ago("2026-10-25T12:04:50Z", now)).toBe("just now");
    expect(ago("2026-10-25T09:05:00Z", now)).toBe("2 h ago");
    expect(ago("2026-10-20T12:05:00Z", now)).toBe("4 days ago");
  });
});
