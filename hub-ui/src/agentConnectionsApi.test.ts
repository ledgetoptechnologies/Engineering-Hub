import { describe, expect, it, vi } from "vitest";
import { HubApiError } from "./api";
import {
  demoAgentConnections, httpAgentConnections, parseAudit, parseCapabilities, parseInstallation, parseInstallations, parseSessions, writeFailureText,
} from "./agentConnectionsApi";
import { actorName, untilText } from "./AgentConnectionsPanel";
import backend from "./fixtures/agent-connections-wire-v1.json";

/** UI-side samples in the frozen DTO shapes; the backend-generated wire fixture is added separately. */
const at = (value: string) => value;
const wire = {
  capabilities: { enabled: true, environment: "staging", owner: true },
  installations: { installations: [
    { id: "inst_laptop", family: "claude", name: "Claude on the laptop", host_label: "beau-laptop", state: "active", reason: null,
      created_at: at("2026-10-27T20:00:00Z"), approved_at: at("2026-10-27T20:02:10Z"), approved_by: "owner", revoked_at: null, revoked_by: null,
      expires_at: at("2026-10-28T20:02:10Z"), revision: 2, pairing_code: null, key_fingerprint: "3f9a1c7e5b2d8f40" },
    { id: "inst_desk", family: "claude", name: "Claude on the desk PC", host_label: "desk-pc", state: "pending", reason: "awaiting_owner_approval",
      created_at: at("2026-10-27T22:50:00Z"), approved_at: null, approved_by: null, revoked_at: null, revoked_by: null,
      expires_at: at("2026-10-27T23:00:00Z"), revision: 1, pairing_code: "482913", key_fingerprint: "a1b2c3d4e5f60718" },
    { id: "inst_codex", family: "codex", name: "Codex test box", host_label: "test-box", state: "revoked", reason: "owner_revoked",
      created_at: at("2026-10-26T10:00:00Z"), approved_at: at("2026-10-26T10:05:00Z"), approved_by: "owner", revoked_at: at("2026-10-27T12:00:00Z"),
      revoked_by: "owner", expires_at: at("2026-10-27T10:05:00Z"), revision: 3, pairing_code: null, key_fingerprint: "0f1e2d3c4b5a6978" },
    { id: "inst_hermes", family: "hermes", name: "Hermes spare", host_label: "spare", state: "expired", reason: "installation_expired",
      created_at: at("2026-10-20T09:00:00Z"), approved_at: at("2026-10-20T09:01:00Z"), approved_by: "owner", revoked_at: null, revoked_by: null,
      expires_at: at("2026-10-21T09:01:00Z"), revision: 2, pairing_code: null, key_fingerprint: "9988776655443322" },
  ] },
  sessions: { installation_id: "inst_laptop", body: { sessions: [
    { id: "sess_main", installation_id: "inst_laptop", label: "main chat", issued_at: at("2026-10-27T22:45:00Z"),
      expires_at: at("2026-10-27T23:00:00Z"), last_used_at: at("2026-10-27T22:51:30Z"), state: "active", revision: 1 },
    { id: "sess_old", installation_id: "inst_laptop", label: "", issued_at: at("2026-10-27T22:00:00Z"),
      expires_at: at("2026-10-27T22:15:00Z"), last_used_at: null, state: "revoked", revision: 2 },
  ] } },
  audit: { events: [
    { id: 3, at: at("2026-10-27T22:50:00Z"), actor: "claude", action: "enrollment.requested", installation_id: "inst_desk", session_id: null, message_id: null },
    { id: 2, at: at("2026-10-27T22:46:00Z"), actor: "claude", action: "message.sent", installation_id: "inst_laptop", session_id: "sess_main", message_id: "msg_1" },
    { id: 1, at: at("2026-10-27T12:00:00Z"), actor: "owner", action: "installation.revoked", installation_id: "inst_codex", session_id: null, message_id: null },
  ] },
  owner_required: { detail: { code: "agent_connections_owner_required" } },
};

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const pending = wire.installations.installations[1];

describe("agent connection parsers", () => {
  it("accepts records in the frozen shapes", () => {
    expect(parseCapabilities(wire.capabilities)).toEqual({ enabled: true, environment: "staging", owner: true });
    expect(parseInstallations(wire.installations).map(item => item.state)).toEqual(["active", "pending", "revoked", "expired"]);
    expect(parseSessions(wire.sessions.body, wire.sessions.installation_id)).toHaveLength(2);
    expect(parseAudit(wire.audit)).toHaveLength(3);
  });
  it("only treats an enabled staging service as available", () => {
    expect(() => parseCapabilities({ enabled: true, environment: "production", owner: true })).toThrow(HubApiError);
    expect(() => parseCapabilities({ enabled: false, environment: "staging", owner: true })).toThrow(HubApiError);
  });
  it("rejects any extra field, so a credential can never ride along", () => {
    expect(() => parseInstallation({ ...pending, installation_secret: "ehi_x" })).toThrow(HubApiError);
    const session = wire.sessions.body.sessions[0];
    expect(() => parseSessions({ sessions: [{ ...session, token: "ehs_x" }] }, session.installation_id)).toThrow(HubApiError);
  });
  it("requires the code, fingerprint and deadline that a pending approval is checked against", () => {
    expect(() => parseInstallation({ ...pending, pairing_code: null })).toThrow(HubApiError);
    expect(() => parseInstallation({ ...pending, pairing_code: "48291" })).toThrow(HubApiError);
    expect(() => parseInstallation({ ...pending, key_fingerprint: "A1B2C3D4E5F60718" })).toThrow(HubApiError);
    // The code is only ever shown while a request is pending.
    expect(() => parseInstallation({ ...wire.installations.installations[0], pairing_code: "482913" })).toThrow(HubApiError);
    expect(() => parseInstallation({ ...pending, expires_at: null })).toThrow(HubApiError);
  });
  it("rejects unknown states, families and control characters", () => {
    expect(() => parseInstallation({ ...pending, state: "approved" })).toThrow(HubApiError);
    expect(() => parseInstallation({ ...pending, family: "gpt" })).toThrow(HubApiError);
    expect(() => parseInstallation({ ...pending, name: "line\nbreak" })).toThrow(HubApiError);
  });
  it("rejects sessions that belong to another installation and duplicate ids", () => {
    const session = wire.sessions.body.sessions[0];
    expect(() => parseSessions(wire.sessions.body, "inst_other")).toThrow(HubApiError);
    expect(() => parseSessions({ sessions: [session, session] }, session.installation_id)).toThrow(HubApiError);
  });
  it("demo records parse and contain no credential-shaped values", () => {
    const demo = demoAgentConnections(Date.parse("2026-10-27T12:00:00Z"));
    expect(parseInstallations({ installations: demo.installations })).toHaveLength(3);
    expect(JSON.stringify(demo)).not.toMatch(/eh[isc]_/);
  });
});

describe("agent connection client", () => {
  it("approves with the expected revision and a CSRF header, and never sends anything secret", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json({ ...pending, state: "active", pairing_code: null,
      reason: null, approved_at: "2026-10-27T22:55:00Z", approved_by: "owner", revision: 2 }));
    const item = await httpAgentConnections(fetchImpl).approve(pending.id, 1, "csrf-token");
    expect(item.state).toBe("active");
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe(`/api/hub/v1/agent-connections/installations/${pending.id}/approve`);
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>)["X-Hub-CSRF"]).toBe("csrf-token");
    expect(JSON.parse(init?.body as string)).toEqual({ expected_revision: 1, lifetime_seconds: 86400 });
  });
  it("refuses to write without CSRF and to address a malformed id", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    await expect(httpAgentConnections(fetchImpl).revokeInstallation(pending.id, 1, "")).rejects.toMatchObject({ code: "csrf_required" });
    await expect(httpAgentConnections(fetchImpl).revokeSession("../x", 1, "csrf")).rejects.toBeInstanceOf(HubApiError);
    expect(fetchImpl).not.toHaveBeenCalled();
  });
  it("maps a missing route to unavailable and keeps server codes", async () => {
    const missing = vi.fn<typeof fetch>().mockResolvedValue(json({ detail: { code: "gateway_route_forbidden" } }, 403));
    await expect(httpAgentConnections(missing).capabilities(new AbortController().signal))
      .rejects.toMatchObject({ status: 404, code: "agent_connections_unavailable" });
    const owner = vi.fn<typeof fetch>().mockResolvedValue(json(wire.owner_required, 403));
    await expect(httpAgentConnections(owner).installations(new AbortController().signal))
      .rejects.toMatchObject({ status: 403, code: "agent_connections_owner_required" });
  });
  it("says plainly when a change may or may not have happened", () => {
    expect(writeFailureText(new HubApiError(0, "network_unavailable"))).toContain("may or may not");
    expect(writeFailureText(new HubApiError(409, "revision_conflict"))).toContain("changed since you loaded it");
    expect(writeFailureText(new HubApiError(409, "pairing_expired"))).toContain("Start pairing again");
    expect(writeFailureText(new HubApiError(409, "enrollment_not_pending"))).toContain("no longer waiting");
    expect(writeFailureText(new HubApiError(429, "session_limit"))).toContain("limit");
  });
});

describe("untilText", () => {
  const now = Date.parse("2026-10-27T22:00:00Z");
  it("reads deadlines in words and never says a past one is still coming", () => {
    expect(untilText("2026-10-27T22:07:10Z", now)).toBe("in 8 min");
    expect(untilText("2026-10-28T20:00:00Z", now)).toBe("in 22 h");
    expect(untilText("2026-10-27T21:59:00Z", now)).toBe("ended");
  });
});

describe("frozen length limits", () => {
  it("accepts names and labels up to 100 characters and no more", () => {
    const item = { id: "inst_x", family: "claude", name: "n".repeat(100), host_label: "h".repeat(100), state: "active", reason: null,
      created_at: "2026-10-27T20:00:00Z", approved_at: null, approved_by: null, revoked_at: null, revoked_by: null,
      expires_at: "2026-10-28T20:00:00Z", revision: 1, pairing_code: null, key_fingerprint: "0123456789abcdef" };
    expect(parseInstallation(item).name).toHaveLength(100);
    expect(() => parseInstallation({ ...item, name: "n".repeat(101) })).toThrow(HubApiError);
    expect(() => parseInstallation({ ...item, host_label: "h".repeat(101) })).toThrow(HubApiError);
  });
});

describe("backend wire fixture (agent-connections-wire-v1, exported by the composed owner API test)", () => {
  it("parses every owner response the backend produced", () => {
    expect(backend.schema).toBe("agent-connections-wire-v1");
    expect(parseCapabilities(backend.capabilities).owner).toBe(true);
    const [pendingItem] = parseInstallations(backend.pending);
    expect(pendingItem).toMatchObject({ state: "pending", pairing_code: "377995", key_fingerprint: "18fab9cd3f43991e" });
    expect(parseInstallation(backend.approved)).toMatchObject({ state: "active", pairing_code: null, key_fingerprint: pendingItem.key_fingerprint });
    expect(parseSessions(backend.sessions, backend.approved.id)).toHaveLength(1);
    expect(parseSessions({ sessions: [backend.revoked_session] }, backend.approved.id)[0].state).toBe("revoked");
    expect(parseAudit(backend.audit).map(event => event.action))
      .toEqual(["session.revoked", "session.issued", "installation.approved", "enrollment.requested"]);
    expect(writeFailureText(new HubApiError(409, backend.revision_conflict.detail.code))).toContain("changed since you loaded it");
  });
  it("carries no credential-shaped value", () => {
    expect(JSON.stringify(backend)).not.toMatch(/eh[isc]_|token|secret|bearer/i);
  });
});

describe("actorName", () => {
  it("names the viewer, installations and agents, and never invents a person's name", () => {
    const items = parseInstallations(backend.pending);
    expect(actorName("human:42", items, "human:42")).toBe("You");
    expect(actorName(backend.approved.id, items)).toBe("Synthetic processing PC");
    expect(actorName("human:7", items, "human:42")).toBe("A person");
    expect(actorName("claude", items)).toBe("Claude");
  });
});
