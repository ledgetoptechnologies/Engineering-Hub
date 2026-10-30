import { describe, expect, it, vi } from "vitest";
import fixture from "./fixtures/infrastructure-metrics-v1.json";
import discovered from "./fixtures/infrastructure-metrics-v2.json";
import { formatBytes, formatUptime, groupMachines, parseMetrics, readMetrics } from "./infrastructureMetrics";

const clone = () => JSON.parse(JSON.stringify(fixture)) as typeof fixture & Record<string, unknown>;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

describe("machine metrics", () => {
  it("parses the server-generated fixture exactly", () => {
    const parsed = parseMetrics(fixture);
    expect(parsed.source).toEqual({ status: "ok", truncated: false, omitted_count: 0, warnings: [] });
    expect(parsed.targets.every(item => item.association === "matched" && item.device_id !== null)).toBe(true);
    expect(parsed.targets).toHaveLength(5);
    const desk = parsed.targets[0];
    expect(desk.labels).toEqual({ host: "desk", role: "workstation", env: "personal" });
    expect(desk.top_processes[0]).toEqual({ name: "msedge", count: 57, working_set_bytes: 4000000000 });
    expect(parsed.targets.find(item => item.exporter === null)?.state).toBe("unknown");
  });
  it.each([
    ["an extra top-level key", (v: any) => { v.extra = 1; }],
    ["another schema version", (v: any) => { v.schema_version = 3; }],
    ["schema 2 with schema 1 rows", (v: any) => { v.schema_version = 2; }],
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

describe("discovered machine metrics (schema 2)", () => {
  const cases = discovered.cases;
  const copy = (name: keyof typeof cases) => JSON.parse(JSON.stringify(cases[name])) as any;
  it("parses every contract case", () => {
    expect(parseMetrics(cases.not_configured)).toMatchObject({ source: { status: "not_configured" }, targets: [] });
    expect(parseMetrics(cases.unreachable).targets).toEqual([]);
    const found = parseMetrics(cases.discovered);
    expect(found.source.warnings).toEqual(["ambiguous_inventory", "unknown_exporter"]);
    expect(found.targets.map(item => item.association)).toEqual(["matched", "matched", "unregistered", "ambiguous"]);
    expect(parseMetrics(cases.partial_truncated).source).toEqual({ status: "partial", truncated: true, omitted_count: 3,
      warnings: ["query_failed", "targets_truncated"] });
  });
  it("groups by the server's host key and flags unregistered and ambiguous machines", () => {
    const groups = groupMachines(parseMetrics(cases.discovered).targets);
    expect(groups.map(group => [group.title, group.targets.length, group.device_id, group.unregistered, group.ambiguous])).toEqual([
      ["192.0.2.11:9000", 1, null, true, false],
      ["demo-workstation", 2, "demo-workstation", false, false],
      ["duplicate-demo", 1, null, false, true],
    ]);
    expect(groups[1].targets.map(item => item.exporter)).toEqual(["windows", "nvidia"]);
  });
  it("never merges by label alone: a different key is a different card", () => {
    const value = copy("discovered");
    value.devices[1].host_key = "target:" + "a".repeat(64); value.devices[1].host_label = null;
    expect(groupMachines(parseMetrics(value).targets).filter(group => group.title.startsWith("demo-workstation") || group.title.startsWith("192.0.2.10"))).toHaveLength(2);
  });
  it.each([
    ["a malformed host key", (v: any) => { v.devices[0].host_key = "host:XYZ"; }],
    ["a host key without a label", (v: any) => { v.devices[0].host_label = null; }],
    ["a target key with a label", (v: any) => { v.devices[2].host_label = "custom"; }],
    ["a matched row without a device", (v: any) => { v.devices[0].device_id = null; }],
    ["an unregistered row naming a device", (v: any) => { v.devices[2].device_id = "demo-workstation"; }],
    ["an ambiguous row naming a device", (v: any) => { v.devices[3].device_id = "duplicate-demo"; }],
    ["an unknown association", (v: any) => { v.devices[0].association = "guessed"; }],
    ["an unknown warning", (v: any) => { v.source.warnings = ["raw_error"]; }],
    ["unsorted warnings", (v: any) => { v.source.warnings = ["unknown_exporter", "ambiguous_inventory"]; }],
    ["duplicate warnings", (v: any) => { v.source.warnings = ["unknown_exporter", "unknown_exporter"]; }],
    ["omitted rows without truncation", (v: any) => { v.source.omitted_count = 2; }],
    ["a missing source key", (v: any) => { delete v.source.truncated; }],
    ["a schema 1 row", (v: any) => { delete v.devices[0].host_key; delete v.devices[0].host_label; delete v.devices[0].association; }],
  ])("rejects %s", (_, change) => {
    const value = copy("discovered"); change(value);
    expect(() => parseMetrics(value)).toThrow();
  });
  it("rejects rows when nothing was or could be fetched", () => {
    for (const name of ["not_configured", "unreachable"] as const) {
      const value = copy(name); value.devices = [copy("discovered").devices[0]];
      expect(() => parseMetrics(value)).toThrow();
    }
  });
  it("accepts an unknown omitted count only when truncated", () => {
    const value = copy("partial_truncated"); value.source.omitted_count = null;
    expect(parseMetrics(value).source.omitted_count).toBeNull();
  });
});

describe("text limits count Unicode characters, as the backend does", () => {
  const tree = "\u{1F332}";
  const withLabel = (label: string) => {
    const value = JSON.parse(JSON.stringify(discovered.cases.discovered)) as any;
    value.devices[0].host_label = label; value.devices[0].labels.host = label;
    return value;
  };
  it("accepts 64 characters outside the BMP and 128-character job and instance values", () => {
    expect(parseMetrics(withLabel(tree.repeat(64))).targets[0].host_label).toBe(tree.repeat(64));
    const value = JSON.parse(JSON.stringify(discovered.cases.discovered)) as any;
    value.devices[2].job = "j".repeat(128); value.devices[2].instance = "i".repeat(128);
    expect(parseMetrics(value).targets[2].instance).toHaveLength(128);
  });
  it("still rejects 65 characters and control characters", () => {
    expect(() => parseMetrics(withLabel(tree.repeat(65)))).toThrow();
    expect(() => parseMetrics(withLabel("a\u0000b"))).toThrow();
  });
});
