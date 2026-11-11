import { describe, expect, it } from "vitest";
import { HubApiError } from "./api";
import { httpInfrastructure, writeFailureText } from "./infrastructureApi";
import { demoDevices } from "./infrastructureDemo";
import {
  actorLabel, inNetwork, ipFamily, isHostname, isIpAddress, isNetwork, maintenanceView, needsAttention, observationView,
  parseDevice, parseDeviceDetail, parseDeviceList, type Observation,
} from "./infrastructureModel";
import fixture from "./fixtures/infrastructure-v1.json";

const now = new Date("2026-10-25T12:00:30Z");
const clone = <T,>(value: T): T => structuredClone(value);
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

describe("parsing", () => {
  it("reads the Prometheus host when the Hub sends it, and none from an older Hub", () => {
    const row = { ...clone(fixture.list).items[0] } as Record<string, unknown>;
    expect(parseDevice(row).metrics_host).toBeNull();
    expect(parseDevice({ ...row, metrics_host: "BEAST" }).metrics_host).toBe("BEAST");
    expect(parseDevice({ ...row, metrics_host: null }).metrics_host).toBeNull();
    for (const bad of ["", "two words", "-x", "a".repeat(65), 7]) expect(() => parseDevice({ ...row, metrics_host: bad })).toThrow(HubApiError);
  });
  it("accepts the fixture and the demo", () => {
    expect(parseDeviceList(fixture.list).items.map(item => item.id)).toEqual(["build-pc", "camera-hub"]);
    expect(parseDeviceDetail(fixture.detail).maintenance.items[0].state).toBe("resolved");
    demoDevices(now).devices.forEach(device => expect(parseDevice(device)).toEqual(device));
  });
  it("refuses extra fields, a combined online flag, bad addresses and unknown states", () => {
    const extra = clone(fixture.list); (extra.items[0] as Record<string, unknown>).online = true;
    expect(() => parseDeviceList(extra)).toThrow(HubApiError);
    const address = clone(fixture.list); address.items[0].addresses = ["198.51.100.300"];
    expect(() => parseDeviceList(address)).toThrow(HubApiError);
    const state = clone(fixture.list); state.items[0].observations[0].state = "up";
    expect(() => parseDeviceList(state)).toThrow(HubApiError);
    const vlan = clone(fixture.list); (vlan.items[0] as { vlan: number }).vlan = 5000;
    expect(() => parseDeviceList(vlan)).toThrow(HubApiError);
  });
  it("refuses control characters in names, a resolved entry without a resolver, and a duplicated probe", () => {
    const name = clone(fixture.list); name.items[0].name = "Build\u0007PC";
    expect(() => parseDeviceList(name)).toThrow(HubApiError);
    const detail = clone(fixture.detail); (detail.maintenance.items[0] as { resolved_by: unknown }).resolved_by = null;
    expect(() => parseDeviceDetail(detail)).toThrow(HubApiError);
    const probes = clone(fixture.list); probes.items[0].observations[1].probe_id = "exporter";
    expect(() => parseDeviceList(probes)).toThrow(HubApiError);
  });
});

describe("wording and ageing", () => {
  const probe = (over: Partial<Observation>): Observation => ({ probe_id: "exporter", source_label: "Exporter scrape", state: "ok",
    observed_at: "2026-10-25T11:59:00Z", valid_until: "2026-10-25T12:04:00Z", ...over });
  it("never words a check as up, down, online or powered", () => {
    for (const state of ["ok", "failed", "unknown", "stale"] as const)
      expect(observationView(probe({ state }), now).line).not.toMatch(/\b(up|down|online|offline|powered)\b/i);
    expect(observationView(probe({}), now)).toMatchObject({ line: "Check passed · 1 min ago", tone: "forest", attention: false });
    expect(observationView(probe({ state: "failed" }), now)).toMatchObject({ line: "Last check failed · 1 min ago", attention: true });
  });
  it("ages an expired check to out of date by this browser's clock, whatever it last said", () => {
    const later = new Date("2026-10-25T12:04:00Z");
    expect(observationView(probe({}), later)).toMatchObject({ line: "Out of date · last seen 5 min ago", tone: "amber", attention: true });
    expect(observationView(probe({ state: "stale" }), now).line).toBe("Out of date · last seen 1 min ago");
    expect(observationView(probe({ state: "unknown", observed_at: null, valid_until: null }), now).line).toBe("Unknown: no current check");
  });
  it("marks a passed expected end as overdue, never as resolved or back", () => {
    const device = parseDeviceList(fixture.list).items[0];
    const view = maintenanceView(device.open_maintenance!, now);
    expect(view).toEqual({ line: "Hermes: Planned shutdown to replace the disk. · overdue by 20 min", overdue: true });
    expect(needsAttention(device, now)).toBe(true);
    expect(maintenanceView(device.open_maintenance!, new Date("2026-10-25T11:00:00Z")).line).toContain("expected to end in 40 min");
  });
  it("names agents, calls the signed-in person You, and never shows another person's identity key", () => {
    expect(actorLabel({ kind: "agent", identity: "codex" })).toBe("Codex");
    expect(actorLabel({ kind: "human", identity: "human:cf:abc" }, "human:cf:abc")).toBe("You");
    expect(actorLabel({ kind: "human", identity: "human:cf:abc" }, "human:cf:other")).toBe("Another person");
  });
});

describe("form checks", () => {
  it("mirrors the server's address, network and hostname rules", () => {
    expect(["192.0.2.1", "2001:db8::1", "::1"].every(isIpAddress)).toBe(true);
    expect(["192.0.2", "192.0.2.256", "01.2.3.4", "fe80::1%eth0", "host"].some(isIpAddress)).toBe(false);
    expect(isNetwork("192.0.2.0/24")).toBe(true);
    expect(isNetwork("192.0.2.1/24")).toBe(false);
    expect(isNetwork("2001:db8::/32")).toBe(true);
    expect(inNetwork("192.0.2.9", "192.0.2.0/24")).toBe(true);
    expect(inNetwork("198.51.100.9", "192.0.2.0/24")).toBe(false);
    // A single network holds one IP family, as the server requires.
    expect(inNetwork("2001:db8::20", "198.51.100.0/24")).toBe(false);
    expect(inNetwork("198.51.100.20", "2001:db8::/32")).toBe(false);
    expect(inNetwork("2001:db8::20", "2001:db8::/32")).toBeNull();
    expect([ipFamily("192.0.2.1"), ipFamily("2001:db8::/32"), ipFamily("host")]).toEqual([4, 6, null]);
    expect(isHostname("build.example.test")).toBe(true);
    expect(isHostname("-bad.example.test")).toBe(false);
  });
});

describe("provisional HTTP adapter", () => {
  it("sends only declared fields with CSRF and the expected revision", async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    const fetchImpl = (async (url: string, init: RequestInit) => { calls.push({ url, init }); return json(fixture.list.items[1]); }) as unknown as typeof fetch;
    const draft = { id: "camera-hub", name: "Camera hub", hostname: null, metrics_host: "CAMHUB-01", addresses: [], network: null, vlan: null, role: "Security cameras",
      retired: false, sneaky: "x" } as never;
    await httpInfrastructure(fetchImpl).saveDevice(draft, 1, "token");
    expect(calls[0].url).toBe("/api/hub/v1/infrastructure/devices/camera-hub");
    expect(calls[0].init.method).toBe("PUT");
    expect(JSON.parse(calls[0].init.body as string)).toEqual({ name: "Camera hub", hostname: null, metrics_host: "CAMHUB-01", addresses: [], network: null, vlan: null,
      role: "Security cameras", retired: false, expected_revision: 1 });
    expect((calls[0].init.headers as Record<string, string>)["X-Hub-CSRF"]).toBe("token");
  });
  it("treats not allowed and not set up alike, and never claims an unanswered write failed", async () => {
    const fetchWith = (response: Response) => (async () => response) as unknown as typeof fetch;
    await expect(httpInfrastructure(fetchWith(json({ detail: { code: "gateway_route_forbidden" } }, 403))).list(new AbortController().signal, false))
      .rejects.toMatchObject({ status: 404, code: "infrastructure_unavailable" });
    await expect(httpInfrastructure(fetchWith(json({ detail: { code: "infrastructure_forbidden" } }, 403))).list(new AbortController().signal, false))
      .rejects.toMatchObject({ status: 403 });
    expect(writeFailureText(new HubApiError(0, "network_unavailable"))).toContain("may or may not have been saved");
    expect(writeFailureText(new HubApiError(409, "revision_conflict"))).toContain("your text is kept");
  });
});
