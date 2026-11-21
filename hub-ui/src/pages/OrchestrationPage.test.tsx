import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import fixture from "../fixtures/orchestration-wire-v1.json";
import { parseRequestDetail, parseRequestList } from "../orchestrationApi";
import { OrchestrationPage, OrchestrationView, ProgressBar, RequestDetailView, RequestListView } from "./OrchestrationPage";

const now = Date.parse("2026-11-03T11:00:00Z");
const viewer = parseRequestDetail(structuredClone(fixture.details[0]), fixture.details[0].request.request_id);
const billing = parseRequestDetail(structuredClone(fixture.details[1]), fixture.details[1].request.request_id);

describe("orchestration gate", () => {
  it("says there are no orchestrations and fetches nothing while the backend is missing", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    try {
      const html = renderToStaticMarkup(<OrchestrationView requestId="req-demo-viewer-refresh" />);
      expect(html).toContain("Orchestration is not available yet");
      expect(html).not.toContain("progressbar");
      for (const item of fixture.list.items) expect(html).not.toContain(item.title);
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally { fetchSpy.mockRestore(); }
  });
  it("renders the loader when given a reader", () => {
    const reader = { list: vi.fn(() => new Promise<never>(() => {})), read: vi.fn() };
    expect(renderToStaticMarkup(<OrchestrationView requestId={null} reader={reader} />)).toContain("Loading requests");
  });
  it("titles the page without a demo label or blurb", () => {
    const html = renderToStaticMarkup(<OrchestrationPage actions={null} />);
    expect(html).toContain(">Orchestration<");
    expect(html).not.toMatch(/demo|sample/i);
  });
});

describe("progress bar", () => {
  it("exposes progressbar semantics with aria-valuenow", () => {
    const html = renderToStaticMarkup(<ProgressBar label="ui progress" percent={40} />);
    expect(html).toContain('role="progressbar"');
    expect(html).toContain('aria-valuenow="40"');
    expect(html).toContain('aria-valuemin="0"');
    expect(html).toContain('aria-valuemax="100"');
  });
  it("omits aria-valuenow when progress is unknown", () => {
    const html = renderToStaticMarkup(<ProgressBar label="ops progress" percent={null} />);
    expect(html).not.toContain("aria-valuenow");
    expect(html).toContain("No checklist yet");
  });
});

describe("request list", () => {
  it("has a title-only empty state", () => {
    const html = renderToStaticMarkup(<RequestListView items={[]} truncated={false} selected={null} />);
    expect(html).toContain("No orchestrations yet");
    expect(html).not.toMatch(/<p[ >]/);
  });
  it("shows each request with waiting and stale counts", () => {
    const list = parseRequestList(structuredClone(fixture.list));
    const html = renderToStaticMarkup(<RequestListView items={list.items} truncated={false} selected="req-demo-billing-export" now={now} />);
    expect(html).toContain("Refresh the operations viewer landing page");
    expect(html).toContain("1 waiting on you");
    expect(html).toContain("1 stale");
    expect(html).toContain('aria-current="page"');
  });
});

describe("request detail", () => {
  const html = renderToStaticMarkup(<RequestDetailView detail={viewer} now={now} />);
  it("pins decisions above the dispatches", () => {
    expect(html.indexOf("Waiting on you")).toBeGreaterThan(-1);
    expect(html.indexOf("Waiting on you")).toBeLessThan(html.indexOf("orchestration-dispatches"));
  });
  it("shows role, state, percent, human ETA, agent guess, status, blocker and lease", () => {
    expect(html).toContain("Working");
    expect(html).toContain('aria-valuenow="40"');
    expect(html).toContain("ETA about 1 h 30 min");
    expect(html).toContain("agent guess: about 1 h");
    expect(html).toContain("Header done; working on the deliveries list.");
    expect(html).toContain("Blocked on: ui role: summary endpoint branch not pushed yet");
    expect(html).toContain("Held by session-demo-ui-01");
    expect(html).toContain("Unstaffed");
    expect(html).toContain("Asked Beau");
  });
  it("shows no ETA for finished work and 100% for an all-done checklist", () => {
    expect(html).toContain('aria-valuenow="100"');
    expect(html.match(/ETA /g)).toHaveLength(4);
  });
  it("renders agent-written markup as text only", () => {
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("<b>draft</b>");
    expect(html).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
    expect(html).toContain("&lt;b&gt;draft&lt;/b&gt;");
  });
  it("badges agent-created work and stale dispatches", () => {
    const other = renderToStaticMarkup(<RequestDetailView detail={billing} now={now} />);
    expect(other).toContain("Agent-created project");
    expect(other).toContain("Agent-created");
    expect(other).toContain("Stale");
    expect(other).toContain("(may be out of date)");
    expect(other).toContain("Failed");
    expect(other).not.toContain("Waiting on you");
  });
});
