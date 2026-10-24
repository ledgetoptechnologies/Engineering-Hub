import { describe, expect, it } from "vitest";
import { HubApiError } from "./api";
import {
  applyCleanup, expiryLabel, extendedUntil, formatBytes, keepResource, keptLabel, listResources, parseCleanupPlan, parseCleanupResult,
  parsePreviewSession, parseResource, parseResourcePage, parseStagingStatus, planCleanup, planSummary, resultSummary, ResourceConflictError, startPreview, statusLine, recheckJob, recheckSummary, parseRecheckResult,
  type CleanupPlan, type StagingResource,
} from "./stagingResourcesApi";
import { createDemoResources, DemoResourceStore } from "./stagingResourcesDemo";
import wire from "./fixtures/staging-wire-v1.json";

const now = new Date("2026-10-24T12:00:00Z");
const id = (n: number) => `5ea9e000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const resource = (over: Partial<StagingResource> = {}): StagingResource => ({ ...createDemoResources(now)[0], ...over });
const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
function recorder(response: Response) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetchImpl = (async (url: string, init: RequestInit) => { calls.push({ url, init }); return response; }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

describe("parseResource", () => {
  it("accepts the demo fixtures", () => {
    for (const item of createDemoResources(now)) expect(parseResource(item)).toEqual(item);
  });
  it("keeps an unknown size as null, never zero, and refuses negative sizes", () => {
    expect(parseResource(resource({ size_bytes: null })).size_bytes).toBeNull();
    expect(() => parseResource(resource({ size_bytes: -1 }))).toThrow(HubApiError);
  });
  it("refuses unknown kinds, states and protection reasons, and a protection without its reference", () => {
    expect(() => parseResource({ ...resource(), kind: "container" })).toThrow(HubApiError);
    expect(() => parseResource({ ...resource(), state: "gone" })).toThrow(HubApiError);
    expect(() => parseResource(resource({ protected: [{ reason: "because" as never, ref: "x" }] }))).toThrow(HubApiError);
    expect(() => parseResource({ ...resource(), protected: [{ reason: "active_job", ref: null }] })).toThrow(HubApiError);
  });
  it("accepts a free-text topic up to the store's 200 characters, and no more", () => {
    expect(parseResource(resource({ topic: "a ".repeat(100) })).topic).toHaveLength(200);
    expect(parseResource(resource({ topic: "🌲".repeat(200) })).topic).toBe("🌲".repeat(200));
    expect(() => parseResource(resource({ topic: "a".repeat(201) }))).toThrow(HubApiError);
    expect(() => parseResource(resource({ topic: "line
break" }))).toThrow(HubApiError);
  });
  it("needs the job that made it and an update time", () => {
    expect(() => parseResource({ ...resource(), job_id: null })).toThrow(HubApiError);
    expect(() => parseResource({ ...resource(), updated_at: undefined })).toThrow(HubApiError);
  });
});

describe("listResources", () => {
  it("sends the filter and refuses a page for another project", async () => {
    const page = { items: [resource({ project: "treedar" })], next_after: null };
    const { calls, fetchImpl } = recorder(jsonResponse(page));
    await expect(listResources({ project: "engineering-hub" }, new AbortController().signal, fetchImpl)).rejects.toThrow(HubApiError);
    expect(calls[0].url).toBe("/api/hub/v1/staging/resources?limit=50&project=engineering-hub");
    expect(calls[0].init.credentials).toBe("same-origin");
  });
  it("reports a gateway that doesn't know the route as unsupported, not a lost sign-in", async () => {
    const { fetchImpl } = recorder(jsonResponse({ detail: { code: "gateway_route_forbidden" } }, 403));
    await expect(listResources({}, new AbortController().signal, fetchImpl)).rejects.toMatchObject({ status: 404, code: "staging_resources_unsupported" });
  });
});

describe("keepResource", () => {
  it("sends CSRF, the reason and the revision it saw", async () => {
    const saved = resource({ revision: 2, keep: { mode: "pinned", until: null, by: "human:demo", reason: "Compare", at: now.toISOString() } });
    const { calls, fetchImpl } = recorder(jsonResponse(saved));
    await keepResource(resource(), { mode: "pinned", reason: "  Compare " }, "token", fetchImpl);
    expect(calls[0].url).toBe(`/api/hub/v1/staging/resources/${id(1)}/keep`);
    expect((calls[0].init.headers as Record<string, string>)["X-Hub-CSRF"]).toBe("token");
    expect(JSON.parse(calls[0].init.body as string)).toEqual({ mode: "pinned", reason: "Compare", expected_revision: 1 });
  });
  it("needs a reason for every change, including letting it expire, and a session token to write", async () => {
    await expect(keepResource(resource(), { mode: "pinned", reason: " " }, "token")).rejects.toMatchObject({ code: "keep_reason_required" });
    await expect(keepResource(resource(), { mode: "none", reason: "" }, "token")).rejects.toMatchObject({ code: "keep_reason_required" });
    await expect(keepResource(resource(), { mode: "none", reason: "Let it expire" }, "")).rejects.toMatchObject({ code: "csrf_required" });
  });
  it("turns a revision conflict into a typed error", async () => {
    const { fetchImpl } = recorder(jsonResponse({ detail: { code: "staging_resource_conflict", current_revision: 4 } }, 409));
    const error = await keepResource(resource(), { mode: "none", reason: "Let it expire" }, "token", fetchImpl).catch(caught => caught);
    expect(error).toBeInstanceOf(ResourceConflictError);
    expect(error.currentRevision).toBe(4);
  });
  it("refuses a reply that didn't advance the revision", async () => {
    const { fetchImpl } = recorder(jsonResponse(resource()));
    await expect(keepResource(resource(), { mode: "none", reason: "Let it expire" }, "token", fetchImpl)).rejects.toMatchObject({ code: "invalid_staging_response" });
  });
});

describe("cleanup", () => {
  const plan = (): CleanupPlan => new DemoResourceStore(() => now).plan(null);
  it("applies with only the plan ID and digest", async () => {
    const reviewed = plan();
    const { calls, fetchImpl } = recorder(jsonResponse({ removed: [reviewed.remove[0].id], failed: [], skipped_changed: [] }));
    await applyCleanup(reviewed, "token", fetchImpl);
    expect(calls[0].url).toBe("/api/hub/v1/staging/cleanup/apply");
    expect(JSON.parse(calls[0].init.body as string)).toEqual({ plan_id: reviewed.plan_id, digest: reviewed.digest });
  });
  it("refuses a result that mentions items the plan didn't remove, or reports one twice", () => {
    const reviewed = plan();
    const kept = reviewed.kept[0].id, removed = reviewed.remove[0].id;
    expect(() => parseCleanupResult({ removed: [kept], failed: [], skipped_changed: [] }, reviewed)).toThrow(HubApiError);
    expect(() => parseCleanupResult({ removed: [removed], failed: [], skipped_changed: [removed] }, reviewed)).toThrow(HubApiError);
  });
  it("refuses a plan that lists an item both to remove and to keep, or a reason other than expiry", () => {
    const reviewed = plan();
    expect(() => parseCleanupPlan({ ...reviewed, kept: [...reviewed.kept, { id: reviewed.remove[0].id, why: "pinned" }] })).toThrow(HubApiError);
    expect(() => parseCleanupPlan({ ...reviewed, remove: [{ ...reviewed.remove[0], why: "old" }] })).toThrow(HubApiError);
  });
  it("never plans to remove protected, pinned or unexpired items", () => {
    const reviewed = plan();
    const all = createDemoResources(now);
    const removable = reviewed.remove.map(item => all.find(other => other.id === item.id)!);
    expect(removable.length).toBeGreaterThan(0);
    for (const item of removable) {
      expect(item.protected).toEqual([]);
      expect(item.keep).toBeNull();
      expect(Date.parse(item.expires_at!)).toBeLessThanOrEqual(now.getTime());
    }
  });
});

describe("preview sessions", () => {
  const dashboard = "https://hub.example.test";
  it("opens only on a different, secure origin", () => {
    expect(parsePreviewSession({ url: "https://preview.example.test/t/abc/", expires_at: now.toISOString() }, dashboard).url)
      .toBe("https://preview.example.test/t/abc/");
    for (const url of ["https://hub.example.test/preview/x/", "http://preview.example.test/", "javascript:alert(1)",
      "https://user:pass@preview.example.test/", "/preview/x/"])
      expect(() => parsePreviewSession({ url, expires_at: now.toISOString() }, dashboard)).toThrow(HubApiError);
  });
  it("allows plain http only for a local development origin", () => {
    expect(() => parsePreviewSession({ url: "http://127.0.0.1:5175/t/x/", expires_at: now.toISOString() }, "http://127.0.0.1:5174")).not.toThrow();
    expect(() => parsePreviewSession({ url: "http://127.0.0.1:5174/t/x/", expires_at: now.toISOString() }, "http://127.0.0.1:5174")).toThrow(HubApiError);
  });
  it("asks for a session with CSRF and an empty body, for previews only", async () => {
    const { calls, fetchImpl } = recorder(jsonResponse({ url: "https://preview.example.test/t/abc/", expires_at: now.toISOString() }));
    await startPreview(resource(), "token", dashboard, fetchImpl);
    expect(calls[0].url).toBe(`/api/hub/v1/staging/resources/${id(1)}/preview-session`);
    expect(calls[0].init.body).toBe("{}");
    await expect(startPreview(resource({ kind: "build" }), "token", dashboard, fetchImpl)).rejects.toMatchObject({ code: "invalid_input" });
  });
  it("passes an unconfigured preview origin through as its own code", async () => {
    const { fetchImpl } = recorder(jsonResponse({ detail: { code: "preview_not_configured" } }, 503));
    await expect(startPreview(resource(), "token", dashboard, fetchImpl)).rejects.toMatchObject({ code: "preview_not_configured" });
  });
});

describe("wording", () => {
  it("summarises a plan by kind, size and why things are kept", () => {
    const summary = planSummary(new DemoResourceStore(() => now).plan(null));
    expect(summary.remove).toBe("Will remove 1 preview and 1 workspace (1.2 GB).");
    expect(summary.keep).toBe("Will keep 2 not expired yet, 1 rollback copy, 1 pinned and 1 in use by a running job.");
  });
  it("names known and prefixed protection reasons, and shows unknown ones rather than dropping them", () => {
    expect(keptLabel("protected:rollback_target")).toBe("rollback copy");
    expect(keptLabel("latest_failure")).toBe("latest failure, kept for diagnosis");
    expect(keptLabel("owner_review")).toBe("owner review");
  });
  it("says when sizes are unknown rather than adding them as zero", () => {
    const reviewed = new DemoResourceStore(() => now).plan(null);
    reviewed.remove[0].size_bytes = null; reviewed.total_bytes = null;
    expect(planSummary(reviewed).remove).toContain("(size unknown)");
  });
  it("reports skipped and failed items honestly", () => {
    expect(resultSummary({ removed: [id(2)], failed: [], skipped_changed: [id(3)] }))
      .toBe("1 removed, 1 skipped because it changed since the plan, 0 failed.");
  });
  it("ranks protection over pins over dates", () => {
    const pinned = { mode: "pinned" as const, until: null, by: "human:demo", reason: "Compare", at: now.toISOString() };
    expect(expiryLabel(resource({ keep: pinned, protected: [{ reason: "rollback_target", ref: "r" }] }), now).text).toBe("Protected: rollback copy");
    expect(expiryLabel(resource({ keep: pinned }), now).text).toBe("Pinned: Compare");
    expect(expiryLabel(resource({ expires_at: "2026-10-24T11:00:00Z" }), now).text).toBe("Expired, removed at the next cleanup");
    expect(expiryLabel(resource({ expires_at: "2026-10-24T18:00:00Z" }), now)).toEqual({ text: "Expires in 6 hours", tone: "amber" });
  });
  it("extends from the later of now and the current expiry", () => {
    expect(extendedUntil(resource({ expires_at: "2026-10-26T12:00:00Z" }), now)).toBe("2026-11-02T12:00:00.000Z");
    expect(extendedUntil(resource({ expires_at: "2026-10-20T12:00:00Z" }), now)).toBe("2026-10-31T12:00:00.000Z");
  });
  it("formats sizes and keeps unknown distinct from zero", () => {
    expect(formatBytes(null)).toBe("size unknown");
    expect(formatBytes(0)).toBe("0 bytes");
    expect(formatBytes(1_200_000_000)).toBe("1.2 GB");
  });
});

describe("service status", () => {
  const ready = { configured: true, running: true, reachable: true, profiles: { "engineering-hub": ["vite-build"] } };
  it("says reachable only when configured, running and reachable", () => {
    expect(statusLine(parseStagingStatus(ready))).toEqual({ text: "Staging PC reachable. 1 project has build or test recipes you can use.", tone: "forest" });
    expect(statusLine(parseStagingStatus({ ...ready, reachable: false })).tone).toBe("amber");
    expect(statusLine(parseStagingStatus({ ...ready, running: false })).text).toContain("isn't running");
  });
  it("puts a stated reason ahead of the flags, and refuses contradictions and unknown states", () => {
    expect(statusLine(parseStagingStatus({ ...ready, state: "existing_executor_owner" })).text).toContain("earlier executor");
    expect(() => parseStagingStatus({ ...ready, configured: false })).toThrow(HubApiError);
    expect(statusLine(parseStagingStatus({ ...ready, state: "warming_up" }))).toEqual({ text: "The staging service isn't ready (warming up).", tone: "amber" });
    expect(() => parseStagingStatus({ ...ready, profiles: { "Bad Project": [] } })).toThrow(HubApiError);
  });
  it("keeps an unstated preview setting distinct from false", () => {
    expect(parseStagingStatus(ready).previews_configured).toBeNull();
    expect(parseStagingStatus({ ...ready, previews_configured: false }).previews_configured).toBe(false);
  });
});

describe("resource store error names", () => {
  it("folds the store's names onto the panel's codes", async () => {
    const cases: [string, number, string][] = [["revision_conflict", 409, "staging_resource_conflict"], ["not_found", 404, "staging_resource_not_found"],
      ["staging_plan_digest_conflict", 409, "staging_plan_changed"], ["staging_preview_not_configured", 503, "preview_not_configured"],
      ["staging_forbidden", 403, "staging_forbidden"], ["staging_cleanup_in_progress", 409, "staging_cleanup_in_progress"],
      ["__proto__", 409, "staging_resource_conflict"]];
    for (const [code, status, expected] of cases) {
      const { fetchImpl } = recorder(jsonResponse({ detail: { code } }, status));
      await expect(planCleanup(null, "token", fetchImpl)).rejects.toMatchObject({ status, code: expected });
    }
  });
  it("treats a store revision conflict with its current revision as a typed conflict", async () => {
    const { fetchImpl } = recorder(jsonResponse({ detail: { code: "revision_conflict", current_revision: 3 } }, 409));
    await expect(keepResource(resource(), { mode: "none", reason: "x" }, "token", fetchImpl)).rejects.toBeInstanceOf(ResourceConflictError);
  });
  it("names the store's kept reasons", () => {
    expect(keptLabel("kept")).toBe("kept by request");
    expect(keptLabel("protected")).toBe("protected");
    expect(keptLabel("active_job")).toBe("in use by a running job");
  });
});

describe("backend reply shapes (PR64)", () => {
  it("reads the unconfigured status exactly as the backend sends it, with no profiles", () => {
    const status = parseStagingStatus({ configured: false, running: false, reachable: false, previews_configured: false });
    expect(statusLine(status).text).toBe("The staging service isn't set up yet.");
  });
  it("accepts plan digests with their sha256: prefix and sends them back unchanged", async () => {
    const reviewed = new DemoResourceStore(() => now).plan(null);
    expect(reviewed.digest.startsWith("sha256:")).toBe(true);
    expect(parseCleanupPlan(reviewed).digest).toBe(reviewed.digest);
    expect(() => parseCleanupPlan({ ...reviewed, digest: "d".repeat(64) })).toThrow(HubApiError);
    const { calls, fetchImpl } = recorder(jsonResponse({ removed: [], failed: [], skipped_changed: [] }));
    await applyCleanup(reviewed, "token", fetchImpl);
    expect(JSON.parse(calls[0].init.body as string).digest).toBe(reviewed.digest);
  });
  it("names the backend's not-set-up and refusal codes instead of guessing", async () => {
    for (const [code, status] of [["staging_not_configured", 503], ["staging_admin_required", 403], ["staging_resource_not_owned", 403]] as const) {
      const { fetchImpl } = recorder(jsonResponse({ detail: { code } }, status));
      await expect(listResources({}, new AbortController().signal, fetchImpl)).rejects.toMatchObject({ status, code });
    }
  });
  it("labels the backend's kept reasons", () => {
    expect(keptLabel("unknown_ownership")).toBe("not recorded as made by an EH job");
    expect(keptLabel("provisioning")).toBe("still being made");
  });
});

describe("held jobs and recheck", () => {
  const ready = { configured: true, running: true, reachable: true, profiles: {} };
  const held = { id: id(990), project: "engineering-hub", recipe: "vite-build", state: "held", phase: "dispatching", source_commit: "497021a" };
  it("reads held jobs and the owner flag, defaulting to none and not allowed", () => {
    expect(parseStagingStatus(ready)).toMatchObject({ can_reconcile: false, held_jobs: [] });
    expect(parseStagingStatus({ ...ready, can_reconcile: true, held_jobs: [held] }).held_jobs).toEqual([held]);
    expect(() => parseStagingStatus({ ...ready, held_jobs: [{ ...held, state: "running" }] })).toThrow(HubApiError);
    expect(() => parseStagingStatus({ ...ready, held_jobs: [held, held] })).toThrow(HubApiError);
  });
  it("posts an empty body with CSRF to the job's reconcile route and checks the reply is for that job", async () => {
    const { calls, fetchImpl } = recorder(jsonResponse({ job: { id: id(990), state: "failed" }, resource_ids: { workspace: id(3), build: id(4) }, phase: "failed" }));
    const result = await recheckJob(id(990), "token", fetchImpl);
    expect(calls[0].url).toBe(`/api/hub/v1/staging/jobs/${id(990)}/reconcile`);
    expect(calls[0].init.body).toBe("{}");
    expect((calls[0].init.headers as Record<string, string>)["X-Hub-CSRF"]).toBe("token");
    expect(result.job.state).toBe("failed");
    expect(result.resource_ids).toEqual({ workspace: id(3), build: id(4) });
    for (const resource_ids of [[id(3)], { container: id(3) }, { workspace: "x" }])
      expect(() => parseRecheckResult({ job: { id: id(990), state: "failed" }, resource_ids, phase: "failed" }, id(990))).toThrow(HubApiError);
    expect(parseRecheckResult({ job: { id: id(990), state: "held" }, resource_ids: {}, phase: "held" }, id(990)).resource_ids).toEqual({});
    expect(() => parseRecheckResult({ job: { id: id(991), state: "failed" }, resource_ids: {}, phase: "failed" }, id(990))).toThrow(HubApiError);
  });
  it("calls only a finished outcome settled", () => {
    const at = (state: string) => recheckSummary({ job: { id: id(990), state }, resource_ids: {}, phase: "x" });
    expect(at("failed")).toEqual({ text: "Confirmed: the original job failed. It's no longer on hold.", settled: true });
    expect(at("succeeded").settled).toBe(true);
    expect(at("held").settled).toBe(false);
    expect(at("held").text).toContain("needs an operator to review");
  });
});

describe("real backend wire fixture (fixtures/staging-wire-v1.json, produced from the HTTP endpoints)", () => {
  it("parses the status exactly as sent, including held jobs and the owner flag", () => {
    const status = parseStagingStatus(wire.status);
    expect(status).toMatchObject({ configured: true, running: false, reachable: false, can_reconcile: true, previews_configured: false });
    expect(status.held_jobs.map(job => job.id)).toEqual([wire.reconcile.job.id]);
    expect(statusLine(status).tone).toBe("amber");
  });
  it("parses the resource page, including free-text topics, pins and unknown sizes", () => {
    const page = parseResourcePage(wire.resources, {});
    expect(page.items).toHaveLength(2);
    expect(page.items[0]).toMatchObject({ kind: "preview", keep: { mode: "pinned" }, size_bytes: null, topic: "Synthetic wire example" });
  });
  it("parses the cleanup plan with its sha256: digest and checks a result against it", () => {
    const plan = parseCleanupPlan(wire.cleanup_plan);
    expect(plan.digest).toBe(wire.cleanup_plan.digest);
    expect(planSummary(plan)).toEqual({ remove: "Will remove 1 build (2.0 KB).", keep: "Will keep 1 protected." });
    expect(parseCleanupResult({ removed: [plan.remove[0].id], failed: [], skipped_changed: [] }, plan).removed).toHaveLength(1);
  });
  it("parses the recheck reply with its kind-keyed resource map", () => {
    const result = parseRecheckResult(wire.reconcile, wire.reconcile.job.id);
    expect(result.resource_ids).toEqual(wire.reconcile.resource_ids);
    expect(recheckSummary(result).settled).toBe(false);
  });
});
