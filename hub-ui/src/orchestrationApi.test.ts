import { describe, expect, it, vi } from "vitest";
import { HubApiError } from "./api";
import fixture from "./fixtures/orchestration-wire-v1.json";
import {
  checklistPercent, DISPATCH_STATES, formatEta, isAgentCreated, isStale, listOrchestrationRequests, orchestrationEnabled,
  parseRequestDetail, parseRequestList, readOrchestrationRequest,
} from "./orchestrationApi";
import { demoOrchestrationReader } from "./orchestrationDemo";

const clone = <T,>(value: T): T => structuredClone(value);
const [viewer, billing] = fixture.details;
const viewerId = viewer.request.request_id;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const signal = () => new AbortController().signal;

describe("orchestration gate", () => {
  it("is shown in demo mode only", () => {
    expect(orchestrationEnabled("demo")).toBe(true);
    expect(orchestrationEnabled("live")).toBe(false);
  });
});

describe("wire fixture", () => {
  it("parses the list and both details", () => {
    const list = parseRequestList(clone(fixture.list));
    expect(list.items.map(item => item.request_id)).toEqual(fixture.details.map(item => item.request.request_id));
    for (const detail of fixture.details) expect(parseRequestDetail(clone(detail), detail.request.request_id).request.title).toBeTruthy();
  });
  it("covers every dispatch state, a stale task and agent-created work", () => {
    const dispatches = fixture.details.flatMap(item => parseRequestDetail(clone(item), item.request.request_id).dispatches);
    expect(new Set(dispatches.map(item => item.state))).toEqual(new Set(DISPATCH_STATES));
    expect(dispatches.some(item => item.task.stale)).toBe(true);
    expect(dispatches.some(item => isAgentCreated(item.task.created_by))).toBe(true);
    const projects = fixture.details.flatMap(item => parseRequestDetail(clone(item), item.request.request_id).projects);
    expect(projects.some(item => isAgentCreated(item.created_by))).toBe(true);
  });
  it("keeps agent-written markup as inert text", () => {
    const detail = parseRequestDetail(clone(viewer), viewerId);
    expect(detail.dispatches.some(item => item.task.status_line.includes("<script>"))).toBe(true);
  });
  it("serves the demo through the same parsers", async () => {
    expect((await demoOrchestrationReader.list(signal())).items).toHaveLength(2);
    expect((await demoOrchestrationReader.read(billing.request.request_id, signal())).dispatches).toHaveLength(3);
    await expect(demoOrchestrationReader.read("nope", signal())).rejects.toMatchObject({ status: 404 });
  });
});

describe("strict parsing", () => {
  const list = () => clone(fixture.list) as Record<string, any>;
  const detail = () => clone(viewer) as Record<string, any>;
  it.each([
    ["unknown top-level field", () => ({ ...list(), extra: 1 })],
    ["wrong schema", () => ({ ...list(), schema: "hub-orchestration.v2" })],
    ["unknown item field", () => { const v = list(); v.items[0].authorized = true; return v; }],
    ["missing item field", () => { const v = list(); delete v.items[0].stale_count; return v; }],
    ["done above total", () => { const v = list(); v.items[0].done_count = 99; return v; }],
    ["duplicate request", () => { const v = list(); v.items.push(v.items[0]); return v; }],
    ["control character", () => { const v = list(); v.items[0].title = "a\u0007b"; return v; }],
    ["bad id", () => { const v = list(); v.items[0].request_id = "../x"; return v; }],
    ["bad time", () => { const v = list(); v.items[0].updated_at = "later"; return v; }],
    ["not an object", () => null],
  ])("refuses a list with %s", (_name, make) => {
    expect(() => parseRequestList(make())).toThrow(HubApiError);
  });
  it.each([
    ["a different request", (v: Record<string, any>) => { v.request.request_id = "req-other"; }],
    ["unknown dispatch state", (v: Record<string, any>) => { v.dispatches[0].state = "approved"; }],
    ["dispatch for another request", (v: Record<string, any>) => { v.dispatches[0].request_id = "req-other"; }],
    ["task id mismatch", (v: Record<string, any>) => { v.dispatches[0].task_id = "task-other"; }],
    ["unknown task field", (v: Record<string, any>) => { v.dispatches[0].task.percent = 40; }],
    ["unknown checklist field", (v: Record<string, any>) => { v.dispatches[0].task.checklist[0].html = "<b>"; }],
    ["non-boolean done", (v: Record<string, any>) => { v.dispatches[0].task.checklist[0].done = "yes"; }],
    ["negative ETA", (v: Record<string, any>) => { v.dispatches[0].task.eta_seconds = -1; }],
    ["fractional ETA", (v: Record<string, any>) => { v.dispatches[0].task.eta_seconds = 1.5; }],
    ["absurd agent guess", (v: Record<string, any>) => { v.dispatches[0].task.agent_eta_guess_seconds = 1e12; }],
    ["role with no lease record", (v: Record<string, any>) => { v.roles = v.roles.filter((r: { role: string }) => r.role !== "ui"); }],
    ["holder without expiry", (v: Record<string, any>) => { v.roles[3].lease_holder = "session-x"; }],
    ["unknown project", (v: Record<string, any>) => { v.projects = []; }],
    ["duplicate dispatch", (v: Record<string, any>) => { v.dispatches.push(v.dispatches[0]); }],
    ["unknown decision field", (v: Record<string, any>) => { v.decisions[0].approve_url = "https://example.test"; }],
    ["decision from an unknown role", (v: Record<string, any>) => { v.decisions[0].asked_by_role = "finance"; }],
    ["uppercase role", (v: Record<string, any>) => { v.dispatches[0].role = "UI"; }],
  ])("refuses a detail with %s", (_name, change) => {
    const value = detail();
    change(value);
    expect(() => parseRequestDetail(value, viewerId)).toThrow(HubApiError);
  });
});

describe("percent", () => {
  it("is unknown for an empty checklist, not zero", () => {
    expect(checklistPercent([])).toBeNull();
  });
  it("is 100 when everything is done and 0 when nothing is", () => {
    expect(checklistPercent([{ text: "a", done: true }, { text: "b", done: true }])).toBe(100);
    expect(checklistPercent([{ text: "a", done: false }])).toBe(0);
  });
  it("rounds down so a partial checklist never reads 100", () => {
    const items = Array.from({ length: 200 }, (_, i) => ({ text: String(i), done: i < 199 }));
    expect(checklistPercent(items)).toBe(99);
    expect(checklistPercent([{ text: "a", done: true }, { text: "b", done: false }, { text: "c", done: false }])).toBe(33);
  });
});

describe("ETA wording", () => {
  it.each([
    [null, "unknown"], [0, "under a minute"], [59, "under a minute"], [60, "about 1 min"], [5400, "about 1 h 30 min"],
    [3600, "about 1 h"], [3599, "about 1 h"], [47 * 3600, "about 47 h"], [200000, "about 2 days"], [7 * 86400, "about 7 days"],
  ])("formats %s seconds as %s", (value, text) => {
    expect(formatEta(value)).toBe(text);
  });
});

describe("flags", () => {
  it("treats only human actors as human-created", () => {
    expect(isAgentCreated("human:1")).toBe(false);
    expect(isAgentCreated("orchestrator")).toBe(true);
    expect(isAgentCreated("codex")).toBe(true);
  });
  it("marks stale by state or by the progress flag", () => {
    const detail = parseRequestDetail(clone(billing), billing.request.request_id);
    expect(detail.dispatches.filter(isStale).map(item => item.dispatch_id)).toEqual(["dsp-demo-export-ops"]);
    const flagged = { ...detail.dispatches[0], task: { ...detail.dispatches[0].task, stale: true } };
    expect(isStale(flagged)).toBe(true);
  });
});

describe("provisional reads", () => {
  it("lists and reads with same-origin GETs on the provisional paths", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValueOnce(json(fixture.list)).mockResolvedValueOnce(json(viewer));
    await listOrchestrationRequests(signal(), fetchImpl);
    await readOrchestrationRequest(viewerId, signal(), fetchImpl);
    expect(fetchImpl.mock.calls.map(call => call[0])).toEqual(["/api/orchestration/requests", `/api/orchestration/requests/${viewerId}`]);
    expect(fetchImpl.mock.calls[0][1]).toMatchObject({ method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store" });
  });
  it("refuses a malformed id without a request and maps a missing route to unsupported", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json({ detail: { code: "gateway_route_forbidden" } }, 403));
    await expect(readOrchestrationRequest("../x", signal(), fetchImpl)).rejects.toMatchObject({ status: 422 });
    expect(fetchImpl).not.toHaveBeenCalled();
    await expect(listOrchestrationRequests(signal(), fetchImpl)).rejects.toMatchObject({ status: 404, code: "orchestration_unsupported" });
  });
  it("refuses a non-JSON success", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(new Response("<html>", { status: 200, headers: { "content-type": "text/html" } }));
    await expect(listOrchestrationRequests(signal(), fetchImpl)).rejects.toMatchObject({ code: "invalid_orchestration_response" });
  });
});
