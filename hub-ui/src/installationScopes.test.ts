import { describe, expect, it, vi } from "vitest";
import { HubApiError } from "./api";
import { httpInstallationScopes, parseInstallationScopes, scopeFailureText } from "./agentConnectionsApi";

const unset = { installation_id: "inst_a", kind: "unclassified", revision: 0, updated_by: null, updated_at: null, internal_effective: false };
const ours = { installation_id: "inst_b", kind: "internal", revision: 2, updated_by: "human:1", updated_at: "2026-10-30T02:00:00Z", internal_effective: true };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

describe("installation scopes", () => {
  it("parses never-set and set rows exactly", () => {
    expect(parseInstallationScopes({ installations: [unset, ours] }).map(row => row.kind)).toEqual(["unclassified", "internal"]);
  });
  it.each([
    ["an extra key", { ...ours, token: "x" }],
    ["an unknown kind", { ...ours, kind: "admin" }],
    ["a set kind at revision 0", { ...unset, kind: "internal" }],
    ["revision 0 with an author", { ...unset, updated_by: "human:1", updated_at: "2026-10-30T02:00:00Z" }],
    ["a set row without an author", { ...ours, updated_by: null, updated_at: null }],
    ["effective without being internal", { ...ours, kind: "project_only" }],
    ["a negative revision", { ...ours, revision: -1 }],
  ])("rejects %s", (_, row) => {
    expect(() => parseInstallationScopes({ installations: [row] })).toThrow();
  });
  it("rejects duplicates and oversized lists", () => {
    expect(() => parseInstallationScopes({ installations: [ours, ours] })).toThrow();
    expect(() => parseInstallationScopes({ installations: Array.from({ length: 257 }, (_, i) => ({ ...unset, installation_id: `inst_${i}` })) })).toThrow();
  });
  it("sends one PUT with the chosen kind, revisions and CSRF", async () => {
    const fetchImpl = vi.fn(async () => json({ installations: [{ ...ours, installation_id: "inst_a", revision: 1 }] }));
    await httpInstallationScopes(fetchImpl).set("internal", [{ installation_id: "inst_a", expected_revision: 0 }], "csrf-1");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [path, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe("/api/hub/v1/agent-connections/installation-scopes");
    expect(init.method).toBe("PUT");
    expect((init.headers as Record<string, string>)["X-Hub-CSRF"]).toBe("csrf-1");
    expect(JSON.parse(init.body as string)).toEqual({ kind: "internal", installations: [{ installation_id: "inst_a", expected_revision: 0 }] });
  });
  it("rejects an answer that doesn't match what was sent", async () => {
    const client = httpInstallationScopes(vi.fn(async () => json({ installations: [{ ...ours, installation_id: "inst_other" }] })));
    await expect(client.set("internal", [{ installation_id: "inst_a", expected_revision: 0 }], "c")).rejects.toThrow();
    const wrongKind = httpInstallationScopes(vi.fn(async () => json({ installations: [{ ...unset, installation_id: "inst_a", kind: "project_only", revision: 1,
      updated_by: "human:1", updated_at: "2026-10-30T02:00:00Z" }] })));
    await expect(wrongKind.set("internal", [{ installation_id: "inst_a", expected_revision: 0 }], "c")).rejects.toThrow();
  });
  it("sends nothing for an empty selection", async () => {
    const fetchImpl = vi.fn();
    await expect(httpInstallationScopes(fetchImpl).set("internal", [], "c")).rejects.toThrow();
    expect(fetchImpl).not.toHaveBeenCalled();
  });
  it("words each pinned failure plainly", () => {
    expect(scopeFailureText(new HubApiError(409, "scope_revision_conflict"))).toContain("read again");
    expect(scopeFailureText(new HubApiError(409, "installation_inactive"))).toContain("isn't active");
    expect(scopeFailureText(new HubApiError(403, "session_owner_required"))).toContain("owner");
    expect(scopeFailureText(new HubApiError(0, "network_unavailable"))).toContain("may or may not");
  });
});
