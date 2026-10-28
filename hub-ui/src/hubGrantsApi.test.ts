import { describe, expect, it, vi } from "vitest";
import { HubApiError } from "./api";
import { actorAvatar, actorFamily, actorName } from "./actors";
import { waitingOn } from "./dashboardText";
import wire from "./fixtures/agent-hub-bridge-wire-v1.json";
import {
  grantFailureText, httpHubGrants, parseBridgeTask, parseCatalog, parseClaims, parseGrant, parseHistory, parseRecoveryResult,
} from "./hubGrantsApi";
import type { Task } from "./types";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const installation = wire.active_grant.installation_id;
const taskId = wire.recovery.task.id;

describe("backend bridge wire fixture (agent-hub-bridge-wire-v1)", () => {
  it("parses the catalog, every grant state and the gateway copies", () => {
    expect(parseCatalog(wire.catalog)).toEqual({ capabilities: wire.catalog.capabilities, taskRecovery: true });
    expect(parseGrant(wire.absent_grant).effective).toMatchObject({ state: "absent", reason: "grant_absent", projects: [] });
    expect(parseGrant(wire.active_grant).effective).toMatchObject({ state: "active", reason: "authorized", projects: ["engineering-hub"] });
    const revoked = parseGrant(wire.revoked_grant);
    // A revoked grant keeps the earlier choices for the owner, with nothing in effect.
    expect(revoked).toMatchObject({ state: "revoked", projects: ["engineering-hub"], effective: { capabilities: [], projects: [] } });
    expect(parseGrant(wire.gateway.active_grant).effective.capabilities).toEqual(["project.read"]);
    expect(parseCatalog(wire.gateway.catalog).taskRecovery).toBe(true);
  });
  it("parses claim pages, the recovery, its replay and history", () => {
    const blocked = parseClaims(wire.active_claims, installation).items[0];
    expect(blocked.recovery).toEqual({ state: "blocked", reason: "claim_session_active", target_session_ids: [] });
    const ready = parseClaims(wire.recoverable_claims, installation).items[0];
    expect(ready).toMatchObject({ claim_state: "revoked", recovery: { state: "recoverable" } });
    expect(parseRecoveryResult(wire.recovery, taskId)).toMatchObject({ replayed: false, recovery: { prior_revision: 4, resulting_revision: 5 } });
    expect(parseRecoveryResult(wire.recovery_replay, taskId).replayed).toBe(true);
    expect(parseHistory(wire.recovery_history, taskId).recoveries).toHaveLength(1);
    expect(parseBridgeTask(wire.candidate).candidate_family).toBe("codex");
    expect(parseBridgeTask(wire.overview.tasks[0]).owner).toBe(`installation:${installation}`);
  });
  it("rejects shapes that would misstate rights or targets", () => {
    expect(() => parseGrant({ ...wire.active_grant, extra: true })).toThrow(HubApiError);
    expect(() => parseGrant({ ...wire.revoked_grant, effective: { ...wire.revoked_grant.effective, projects: ["engineering-hub"] } })).toThrow(HubApiError);
    expect(() => parseGrant({ ...wire.active_grant, state: "approved" })).toThrow(HubApiError);
    const item = wire.recoverable_claims.items[0];
    expect(() => parseClaims({ ...wire.recoverable_claims, items: [{ ...item, recovery: { ...item.recovery, target_session_ids: [] } }] }, installation)).toThrow(HubApiError);
    expect(() => parseClaims({ ...wire.recoverable_claims, items: [{ ...item, recovery: { ...item.recovery, target_session_ids: [item.claim_session_id] } }] }, installation)).toThrow(HubApiError);
    expect(() => parseClaims(wire.recoverable_claims, "00000000-0000-4000-8000-000000000000")).toThrow(HubApiError);
    expect(() => parseRecoveryResult({ ...wire.recovery, recovery: { ...wire.recovery.recovery, resulting_revision: 7 } }, taskId)).toThrow(HubApiError);
  });
  it("carries no credential-shaped value", () => {
    expect(JSON.stringify(wire)).not.toMatch(/eh[isc]_[A-Za-z0-9]|bearer/i);
  });
});

describe("bridge client", () => {
  it("saves a grant with the grant revision, sorted choices and a bounded lifetime", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json(wire.active_grant));
    await httpHubGrants(fetchImpl).saveGrant(installation, { expected_revision: 0, projects: ["engineering-hub"],
      capabilities: ["task.read", "project.read"], lifetime_seconds: 86400 }, "csrf");
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe(`/api/hub/v1/agent-connections/installations/${installation}/hub-grant`);
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(init?.body as string)).toEqual({ expected_revision: 0, projects: ["engineering-hub"], capabilities: ["task.read", "project.read"], lifetime_seconds: 86400 });
  });
  it("never sends an empty grant (removing access is an explicit revoke)", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    await expect(httpHubGrants(fetchImpl).saveGrant(installation, { expected_revision: 1, projects: [], capabilities: ["task.read"], lifetime_seconds: 3600 }, "csrf"))
      .rejects.toMatchObject({ code: "invalid_hub_grant" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
  it("sends the recovery request as given, including its stable request id", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json(wire.recovery));
    const body = { expected_revision: 4, target_session_id: wire.recovery.recovery.target_session_id, reason: "  Old PC died ", client_request_id: "recover-abc" };
    await httpHubGrants(fetchImpl).recover(taskId, body, "csrf");
    expect(fetchImpl.mock.calls[0][0]).toBe(`/api/hub/v1/agent-connections/tasks/${taskId}/recover-claim`);
    expect(JSON.parse(fetchImpl.mock.calls[0][1]?.body as string)).toEqual({ ...body, reason: "Old PC died" });
  });
  it("reads an agent-credential 401 on an owner route as a refusal, but a human 401 as a lost sign-in", async () => {
    const agent = vi.fn<typeof fetch>().mockResolvedValue(json({ detail: { code: "installation_inactive" } }, 401));
    await expect(httpHubGrants(agent).saveGrant(installation, { expected_revision: 1, projects: ["engineering-hub"], capabilities: ["task.read"], lifetime_seconds: 3600 }, "csrf"))
      .rejects.toMatchObject({ status: 409, code: "installation_inactive" });
    const human = vi.fn<typeof fetch>().mockResolvedValue(json({ detail: { code: "human_session_required" } }, 401));
    await expect(httpHubGrants(human).grant(installation, new AbortController().signal)).rejects.toMatchObject({ status: 401 });
    const bare = vi.fn<typeof fetch>().mockResolvedValue(new Response("", { status: 401 }));
    await expect(httpHubGrants(bare).grant(installation, new AbortController().signal)).rejects.toMatchObject({ status: 401 });
  });
  it("words each fixed failure", () => {
    expect(grantFailureText(new HubApiError(409, "task_execution_present"))).toContain("Work has already been started");
    expect(grantFailureText(new HubApiError(409, "grant_revision_conflict"))).toContain("changed since you loaded it");
    expect(grantFailureText(new HubApiError(0, "network_unavailable"))).toContain("may or may not");
  });
});

describe("actor names", () => {
  const owner = `installation:${installation}`;
  it("never shows an installation as its bare family", () => {
    expect(actorName(owner, { family: "codex" })).toBe("Codex on install 88736abd");
    expect(actorName(owner, { family: "codex", installations: { [installation]: "Desk PC" } })).toBe("Codex · Desk PC");
    expect(actorName(owner)).toBe("Install 88736abd");
    expect(actorName("claude")).toBe("Claude");
  });
  it("only calls the viewer 'You'", () => {
    expect(actorName("human:42", { viewer: "human:42" })).toBe("You");
    expect(actorName("human:7", { viewer: "human:42" })).toBe("A person");
    expect(actorName(owner, { viewer: "human:42" })).not.toBe("You");
  });
  it("gives avatars a family only when one is known", () => {
    expect(actorAvatar(owner, "codex")).toEqual({ className: "codex", letter: "C" });
    expect(actorAvatar(owner)).toEqual({ className: "", letter: "?" });
    expect(actorFamily(owner, "hermes")).toBe("hermes");
  });
  it("explains waiting tasks with the installation and its family", () => {
    const task = { ...(wire.overview.tasks[0] as unknown as Task) };
    const name = (id: string, family?: string | null) => actorName(id, { family });
    expect(waitingOn(task, name)).toBe("Codex on install 88736abd is working on it.");
    expect(waitingOn({ ...task, status: "in_review", review: null }, name))
      .toBe("Waiting for an independent review. No reviewer assigned; any agent except Codex (on any computer) can review.");
  });
});

describe("installation reviews and withdrawals (fixture v2 provenance)", () => {
  it("parses a review by another family's installation with its trusted family", () => {
    const task = parseBridgeTask(wire.installation_review);
    expect(task.review).toEqual({ actor: "installation:7a207d88-1538-4f43-a93c-dd17b78d7c2a", commit: "a".repeat(40), verdict: "approve", family: "claude" });
    expect(parseBridgeTask(wire.candidate).review).toBeNull();
  });
  it("names the reviewer and withdrawer by installation and family", () => {
    const task = wire.installation_review as unknown as Task;
    const name = (id: string, family?: string | null) => actorName(id, { family });
    expect(waitingOn(task, name)).toBe("Review passed by Claude on install 7a207d88. Running it is separate and needs its own authorization.");
    const decision = wire.mcp_withdrawn_decision;
    expect(actorName(decision.withdrawn_by, { family: decision.withdrawn_family, viewer: "human:42" })).toBe("Codex on install 88736abd");
  });
});
