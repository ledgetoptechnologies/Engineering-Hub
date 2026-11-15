import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { HubApiError } from "./api";
import { SourceIntentPanel, SourceIntentPageView, sourceIntentReducer } from "./SourceIntentPanel";
import { parseSourceIntentPage, readSourceIntentPage } from "./sourceIntentApi";

const id = "11111111-1111-4111-8111-111111111111";
const other = "22222222-2222-4222-8222-222222222222";
const envelope = { scope: "original_intent_only", worker_authority_verified: false, next_cursor: null };
const item = { submission_id: id, state: "deposit_unknown", project: "engineering-hub", recipe: "static", actor: "codex", request_id: other,
  source_id: id, reserved_command_id: other, review_task_id: id, review_task_revision: 2,
  source_commit: "a".repeat(40), source_digest: "b".repeat(64),
  source_binding: { scheme: "git-sha1-tree-v1", commit_oid: "a".repeat(40), tree_oid: "d".repeat(40),
    archive_sha256: "b".repeat(64), commit_object_sha256: "e".repeat(64), expanded_bytes: 10, entries: 1 },
  reviewed_profile: { project: "engineering-hub", recipe: "static", generation: 1, digest: `sha256:${"c".repeat(64)}` },
  source_identity_assurance: "journal_only", worker_ledger_authority: "unverified", source_post_outcome: "unknown",
  review_status: "historical_not_current", execution_authorization: false, automatic_retry_or_release: false };

describe("owner source handoff", () => {
  it("does not fetch or render private data before an owner asks to check", () => {
    const read = vi.fn();
    const html = renderToStaticMarkup(<SourceIntentPanel read={read} onUnauthorized={() => {}} />);
    expect(read).toHaveBeenCalledTimes(0); expect(html.includes(id)).toBe(false);
    expect(html).toContain("Owner-only local records");
  });
  it("renders journal limits, no action controls and a bounded continuation", () => {
    const page = parseSourceIntentPage({ ...envelope, next_cursor: other, items: [item] });
    const html = renderToStaticMarkup(<SourceIntentPageView page={page} onNext={() => {}} />);
    expect(html).toContain("worker identity and deposit outcome are unverified");
    expect(html).toContain("task review is historical, not current");
    expect(html).toContain("Source, from journal only"); expect(html).toContain("not proof of dispatch");
    expect(html).toContain("Git tree"); expect(html).toContain("Commit object digest"); expect(html).toContain("Request");
    expect(html).toContain("Check next page");
    expect(/<button[^>]*>(?:Retry|Release|Start)/i.test(html)).toBe(false);
  });
  it("does not treat an empty page with a cursor as complete", () => {
    const page = parseSourceIntentPage({ ...envelope, items: [], next_cursor: other });
    const html = renderToStaticMarkup(<SourceIntentPageView page={page} onNext={() => {}} />);
    expect(html).toContain("More records remain to check"); expect(html).toContain("Check next page");
  });
  it("redacts unreadable rows and rejects unsafe server claims", () => {
    const page = parseSourceIntentPage({ ...envelope, items: [{ submission_id: id, state: "unreadable", payload: "private secret" }] });
    expect(JSON.stringify(page).includes("private secret")).toBe(false);
    expect(renderToStaticMarkup(<SourceIntentPageView page={page} onNext={() => {}} />)).toContain("cannot be read safely");
    expect(() => parseSourceIntentPage({ ...envelope, items: [{ ...item, execution_authorization: true }] })).toThrow(HubApiError);
    expect(() => parseSourceIntentPage({ ...envelope, items: [{ ...item, worker_ledger_authority: "verified" }] })).toThrow(HubApiError);
    expect(() => parseSourceIntentPage({ ...envelope, items: [{ ...item, source_binding: { ...item.source_binding, commit_oid: "f".repeat(40) } }] })).toThrow(HubApiError);
  });
  it("returns no data to a nonowner and never reflects the error body", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ detail: "private secret" }), { status: 403, headers: { "content-type": "application/json" } }));
    await expect(readSourceIntentPage(null, new AbortController().signal, fetcher as typeof fetch)).rejects.toMatchObject({ status: 403, code: "source_handoff_unavailable" });
    expect(fetcher).toHaveBeenCalledOnce();
    expect(JSON.stringify(fetcher.mock.calls).includes("private secret")).toBe(false);
    const prior = { load: "loaded" as const, page: parseSourceIntentPage({ ...envelope, items: [item] }) };
    expect(sourceIntentReducer(prior, { type: "hidden" })).toEqual({ load: "hidden", page: null });
  });
  it("uses an explicit cursor and rejects malformed pages", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ ...envelope, items: [], next_cursor: null }), { status: 200, headers: { "content-type": "application/json" } }));
    await readSourceIntentPage(other, new AbortController().signal, fetcher as typeof fetch);
    expect(String(fetcher.mock.calls[0][0])).toContain(`after=${other}`);
    expect(() => parseSourceIntentPage({ ...envelope, items: [], next_cursor: "invalid" })).toThrow(HubApiError);
  });
});
