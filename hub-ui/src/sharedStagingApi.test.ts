import { describe, expect, it } from "vitest";
import { HubApiError } from "./api";
import { parseSharedStagingStatus, readSharedStaging, recoverFailureText, recoverSharedStaging, SHARED_STATES, stateText } from "./sharedStagingApi";
import fixture from "./fixtures/shared-staging-status-v1.json";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
function fetchWith(response: Response | Error) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetchImpl = (async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    if (response instanceof Error) throw response;
    return response;
  }) as unknown as typeof fetch;
  return { fetchImpl, calls };
}

describe("parseSharedStagingStatus", () => {
  it("accepts every state the Hub can send, with its own reason", () => {
    expect(fixture.map(row => parseSharedStagingStatus(row).state)).toEqual([...SHARED_STATES]);
  });
  it("refuses unknown states, mismatched reasons and extra fields", () => {
    expect(() => parseSharedStagingStatus({ state: "ready", reason: null })).toThrow(HubApiError);
    expect(() => parseSharedStagingStatus({ state: "active", reason: "operator_acceptance_required" })).toThrow(HubApiError);
    expect(() => parseSharedStagingStatus({ state: "pending", reason: null })).toThrow(HubApiError);
    expect(() => parseSharedStagingStatus({ state: "active", reason: null, authorized: true })).toThrow(HubApiError);
    expect(() => parseSharedStagingStatus({ state: "active" })).toThrow(HubApiError);
  });
});

describe("reading and recovering", () => {
  it("reads status as a same-origin GET", async () => {
    const { fetchImpl, calls } = fetchWith(json({ state: "active", reason: null }));
    await expect(readSharedStaging(new AbortController().signal, fetchImpl)).resolves.toEqual({ state: "active", reason: null });
    expect(calls[0].url).toBe("/api/hub/v1/staging/shared/status");
    expect(calls[0].init).toMatchObject({ method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store" });
  });
  it("retries recovery as an empty-body POST with the CSRF token, and never without one", async () => {
    const { fetchImpl, calls } = fetchWith(json({ state: "active", reason: null }));
    await recoverSharedStaging("token", fetchImpl);
    expect(calls[0].url).toBe("/api/hub/v1/staging/shared/recover");
    expect(calls[0].init).toMatchObject({ method: "POST", body: "{}", credentials: "same-origin" });
    expect((calls[0].init.headers as Record<string, string>)["X-Hub-CSRF"]).toBe("token");
    const none = fetchWith(json({}));
    await expect(recoverSharedStaging("", none.fetchImpl)).rejects.toMatchObject({ code: "csrf_required" });
    expect(none.calls).toHaveLength(0);
  });
  it("keeps the server's codes, treats an older Hub as unsupported and no answer as no answer", async () => {
    const signal = new AbortController().signal;
    await expect(readSharedStaging(signal, fetchWith(json({ detail: { code: "shared_staging_not_configured" } }, 503)).fetchImpl))
      .rejects.toMatchObject({ status: 503, code: "shared_staging_not_configured" });
    await expect(readSharedStaging(signal, fetchWith(json({ detail: { code: "gateway_route_forbidden" } }, 403)).fetchImpl))
      .rejects.toMatchObject({ status: 404, code: "shared_staging_unsupported" });
    await expect(readSharedStaging(signal, fetchWith(new Response("<html>", { status: 502 })).fetchImpl))
      .rejects.toMatchObject({ status: 502, code: "shared_staging_request_failed" });
    await expect(recoverSharedStaging("t", fetchWith(new TypeError("Failed to fetch")).fetchImpl))
      .rejects.toMatchObject({ status: 0, code: "network_unavailable" });
  });
});

describe("wording", () => {
  it("never words a state as ready, approved or deployed", () => {
    for (const state of SHARED_STATES) {
      const { title, line } = stateText[state];
      expect(`${title} ${line}`).not.toMatch(/\b(ready|approved|authori[sz]ed)\b/i);
    }
    expect(stateText.active.line).toContain("doesn't mean any app is deployed or healthy");
  });
  it("never says a failed or unanswered retry means the work failed", () => {
    for (const error of [new HubApiError(0, "network_unavailable"), new HubApiError(409, "shared_staging_recovery_required"),
      new HubApiError(409, "shared_staging_lifecycle_changed"), new HubApiError(500, "anything"), new DOMException("x", "TimeoutError")]) {
      const text = recoverFailureText(error);
      expect(text).toContain("Refresh");
      expect(text).not.toMatch(/\bfailed\b/i);
    }
    expect(recoverFailureText(new HubApiError(0, "network_unavailable"))).toContain("may still be running");
  });
});
