import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Hub } from "../hub";
import type { Task } from "../types";
import { ownershipRows, WorkOwnership } from "./WorkOwnership";

afterEach(() => vi.useRealTimers());
const hub = { session: null } as unknown as Hub;
const task = (state_since: string | null): Task => ({ id: String(state_since), revision: 1,
  updated_at: new Date().toISOString(), project: "engineering-hub", title: "Example", status: "claimed",
  owner: "codex", candidate_commit: null, review: null, repair_count: 0, state_since });

describe("ownership display", () => {
  it("labels claims and incomplete time coverage without asserting activity", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-14T12:00:00Z"));
    const known = new Date(Date.now() - 120_000).toISOString();
    const rows = ownershipRows([task(known), task(null), task("bad-date"), task(new Date(Date.now() + 60_000).toISOString())]);
    const html = renderToStaticMarkup(<WorkOwnership hub={hub} rows={rows} selected="all" onSelect={() => {}} />);
    expect(html).toContain("4 claimed");
    expect(html).toContain("Oldest known state time: 2m ago · 3 unknown");
    expect(html).toContain("a claim does not confirm current execution or completion");
    expect(html).not.toContain("4 working");
  });
  it("shows unavailable when all open times are unknown", () => {
    const rows = ownershipRows([task(null)]);
    const html = renderToStaticMarkup(<WorkOwnership hub={hub} rows={rows} selected="all" onSelect={() => {}} />);
    expect(html).toContain("State times unavailable");
  });
});
