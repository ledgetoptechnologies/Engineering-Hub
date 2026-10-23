import { describe, expect, it, vi } from "vitest";
import { decideSessionReconciliation, parseSessionReconciliation, readSessionReconciliation } from "./sessionReconciliationApi";

const id = "a".repeat(64);
const snapshot = (changes = {}) => ({ request_id: id, revision: 1, state: "pending", evidence: "Synthetic test evidence. Not live inventory proof.",
  report_digest: `sha256:${"b".repeat(64)}`, expires_in_seconds: 120, execution_authorization: false, ...changes });
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
describe("staging session human API", () => {
  it("reads through cookies, never bearer credentials, with no cache or redirects", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response(snapshot()));
    expect(await readSessionReconciliation(fetchImpl)).toEqual(snapshot());
    expect(fetchImpl.mock.calls[0][0]).toBe("/api/hub/v1/staging/session-reconciliation");
    expect(fetchImpl.mock.calls[0][1]).toMatchObject({ method: "GET", credentials: "same-origin", cache: "no-store", redirect: "error", headers: { Accept: "application/json" } });
    expect(fetchImpl.mock.calls[0][1]!.body).toBeUndefined();
    expect(fetchImpl.mock.calls[0][1]!.signal).toBeInstanceOf(AbortSignal);
  });
  it.each(["accept", "decline"] as const)("sends only exact CAS intent for %s with human CSRF", async action => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ request_id: id, revision: 2,
      state: action === "accept" ? "accepted" : "declined", execution_authorization: false }));
    await decideSessionReconciliation({ request_id: id, expected_revision: 1, action, report: "forged", actor: "human:1",
      inventory_reconciled: true, token: "not-sent" } as Parameters<typeof decideSessionReconciliation>[0], "csrf", fetchImpl);
    expect(JSON.parse(fetchImpl.mock.calls[0][1]!.body as string)).toEqual({ request_id: id, expected_revision: 1, action });
    expect(fetchImpl.mock.calls[0][1]).toMatchObject({ method: "POST", credentials: "same-origin", headers: { "X-Hub-CSRF": "csrf", "Content-Type": "application/json" } });
  });
  it("requires CSRF and well-formed intent before any network request", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    await expect(decideSessionReconciliation({ request_id: id, expected_revision: 1, action: "accept" }, "", fetchImpl)).rejects.toThrow("csrf_required");
    for (const input of [{ request_id: "../x", expected_revision: 1, action: "accept" }, { request_id: id, expected_revision: true, action: "accept" },
      { request_id: id, expected_revision: 1, action: "deploy" }]) {
      await expect(decideSessionReconciliation(input as Parameters<typeof decideSessionReconciliation>[0], "csrf", fetchImpl)).rejects.toThrow("invalid_session_intent");
    }
    expect(fetchImpl).not.toHaveBeenCalled();
  });
  it.each([403, 404, 409, 410, 401, 503])("preserves safe HTTP status %s without reflecting server details", async status => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(new Response("secret <script>alert(1)</script>", { status }));
    await expect(readSessionReconciliation(fetchImpl)).rejects.toMatchObject({ status });
    await expect(readSessionReconciliation(fetchImpl)).rejects.not.toThrow("secret");
  });
  it("maps an older gateway's missing-route denial to not configured, not an operator denial or sign-out", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ detail: { code: "gateway_route_forbidden" } }, 403));
    await expect(readSessionReconciliation(fetchImpl)).rejects.toMatchObject({ status: 404, code: "session_not_configured" });
  });
  it.each([snapshot({ execution_authorization: true }), snapshot({ expires_in_seconds: Infinity }), snapshot({ expires_in_seconds: -1 }),
    snapshot({ revision: true }), snapshot({ state: "ready" }), snapshot({ request_id: id + "\n" }), snapshot({ report_digest: "bad" }),
    snapshot({ report_digest: `sha256:${"b".repeat(64)}\n` }), snapshot({ evidence: "x".repeat(8193) })])("refuses malformed or authority-inflating snapshot %#", value => {
    expect(() => parseSessionReconciliation(value)).toThrow("invalid_session_response");
  });
  it("refuses an answer for a different request, revision or action", async () => {
    for (const changes of [{ request_id: "c".repeat(64) }, { revision: 3 }, { state: "declined" }, { execution_authorization: true }]) {
      const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ request_id: id, revision: 2, state: "accepted", execution_authorization: false, ...changes }));
      await expect(decideSessionReconciliation({ request_id: id, expected_revision: 1, action: "accept" }, "csrf", fetchImpl)).rejects.toThrow("invalid_session_response");
    }
  });
});
