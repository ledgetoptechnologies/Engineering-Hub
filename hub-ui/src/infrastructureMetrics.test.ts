import { describe, expect, it, vi } from "vitest";
import fixture from "./fixtures/infrastructure-metrics-v1.json";
import { formatBytes, formatUptime, groupMachines, parseMetrics, readMetrics } from "./infrastructureMetrics";

const clone = () => JSON.parse(JSON.stringify(fixture)) as typeof fixture & Record<string, unknown>;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

describe("machine metrics", () => {
  it("parses the server-generated fixture exactly", () => {
    const parsed = parseMetrics(fixture);
    expect(parsed.source).toBe("ok");
    expect(parsed.targets).toHaveLength(5);
    const desk = parsed.targets[0];
    expect(desk.labels).toEqual({ host: "desk", role: "workstation", env: "personal" });
    expect(desk.top_processes[0]).toEqual({ name: "msedge", count: 57, working_set_bytes: 4000000000 });
    expect(parsed.targets.find(item => item.exporter === null)?.state).toBe("unknown");
  });
  it.each([
    ["an extra top-level key", (v: any) => { v.extra = 1; }],
    ["another schema version", (v: any) => { v.schema_version = 2; }],
    ["an unknown label", (v: any) => { v.devices[0].labels.secret = "x"; }],
    ["a non-finite-looking ratio", (v: any) => { v.devices[0].cpu_busy_ratio = 1.5; }],
    ["a fractional byte count", (v: any) => { v.devices[0].mem_total_bytes = 1.5; }],
    ["more memory available than installed", (v: any) => { v.devices[0].mem_available_bytes = v.devices[0].mem_total_bytes + 1; }],
    ["free space larger than the disk", (v: any) => { v.devices[0].disks[0].free_bytes = 5e12; }],
    ["an unknown exporter", (v: any) => { v.devices[0].exporter = "snmp"; }],
    ["a fresh state without a scrape time", (v: any) => { v.devices[0].last_scrape = null; }],
    ["a duplicate target", (v: any) => { v.devices.push(v.devices[0]); }],
    ["a control character in a name", (v: any) => { v.devices[0].top_processes[0].name = "a\u0007"; }],
    ["too many processes", (v: any) => { v.devices[0].top_processes = Array(11).fill(v.devices[0].top_processes[0]); }],
  ])("rejects %s", (_, change) => {
    const value = clone(); change(value);
    expect(() => parseMetrics(value)).toThrow();
  });
  it("hides on 403, 404, 503 and a missing route; signs out on 401", async () => {
    for (const status of [403, 404, 503]) expect(await readMetrics(new AbortController().signal, vi.fn(async () => json({}, status)))).toBeNull();
    await expect(readMetrics(new AbortController().signal, vi.fn(async () => json({}, 401)))).rejects.toMatchObject({ status: 401 });
    await expect(readMetrics(new AbortController().signal, vi.fn(async () => json({}, 500)))).rejects.toMatchObject({ status: 500 });
  });
  it("reads only the fixed same-origin route", async () => {
    const fetchImpl = vi.fn(async () => json(fixture));
    await readMetrics(new AbortController().signal, fetchImpl);
    expect(fetchImpl).toHaveBeenCalledWith("/api/hub/v1/infrastructure/metrics", expect.objectContaining({ method: "GET", credentials: "same-origin" }));
  });
  it("groups a machine's CPU and GPU exporters by host, GPUs last", () => {
    const groups = groupMachines(parseMetrics(fixture).targets);
    expect(groups.map(group => group.title)).toEqual(["desk", "field-pi", "gpu-box", "old-box"]);
    expect(groups.find(group => group.title === "gpu-box")!.targets.map(item => item.exporter)).toEqual(["node", "nvidia"]);
  });
  it("formats missing values as not reported, never zero", () => {
    expect(formatBytes(null)).toBe("not reported");
    expect(formatBytes(34359738368)).toBe("32.0 GB");
    expect(formatUptime(null)).toBe("not reported");
    expect(formatUptime(90061)).toBe("1d 1h");
  });
});
