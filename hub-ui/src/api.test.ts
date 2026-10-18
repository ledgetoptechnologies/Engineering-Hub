import { describe, expect, it, vi } from "vitest";
import { HubApiError, HubClient } from "./api";

const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

describe("live human API boundary", () => {
  it("preserves the exact topic ACL denial code without downgrading other forbidden failures", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ detail: { code: "topic_read_forbidden" } }, 403));
    await expect(new HubClient({ mode: "live", fetchImpl }).getTopic("topic")).rejects.toMatchObject({ status: 403, code: "topic_read_forbidden" });
  });
  it("calls browser fetch with the global receiver, not the HubClient", async () => {
    let receiver: unknown;
    const fetchImpl = function (this: unknown) {
      receiver = this;
      return Promise.resolve(response({ authenticated: false }));
    } as typeof fetch;
    await new HubClient({ mode: "live", fetchImpl }).getSession();
    expect(receiver).toBe(globalThis);
  });
  it("uses same-origin cookies and never sends an agent bearer", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(response({ authenticated: false }));
    const client = new HubClient({ mode: "live", fetchImpl });
    await client.getSession();
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/hub/v1/auth/me",
      expect.objectContaining({
        method: "GET",
        credentials: "same-origin",
        redirect: "error",
      }),
    );
    const headers = fetchImpl.mock.calls[0][1]?.headers as Record<
      string,
      string
    >;
    expect(headers.Authorization).toBeUndefined();
    expect(headers["X-Hub-CSRF"]).toBeUndefined();
  });

  it("sends exact revision-bound decision and task payloads with CSRF", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockImplementation(async () => response({}));
    const client = new HubClient({ mode: "live", fetchImpl });
    await client.answerDecision("decision-1", 7, "Yes", "csrf-in-memory");
    await client.createTask(
      "3d-viewer",
      "Verify candidate",
      "stable-request-key",
      "csrf-in-memory",
    );
    await client.setPause(true, "csrf-in-memory");
    await client.logout("csrf-in-memory");
    expect(fetchImpl.mock.calls.map((call) => call[0])).toEqual([
      "/api/hub/v1/decisions/decision-1/answer",
      "/api/hub/v1/tasks",
      "/api/hub/v1/pause",
      "/api/hub/v1/auth/logout",
    ]);
    expect(JSON.parse(fetchImpl.mock.calls[0][1]?.body as string)).toEqual({
      expected_revision: 7,
      answer: "Yes",
    });
    expect(JSON.parse(fetchImpl.mock.calls[1][1]?.body as string)).toEqual({
      project: "3d-viewer",
      title: "Verify candidate",
      client_request_id: "stable-request-key",
    });
    for (const [, options] of fetchImpl.mock.calls) {
      const headers = options?.headers as Record<string, string>;
      expect(headers["X-Hub-CSRF"]).toBe("csrf-in-memory");
      expect(headers.Origin).toBeUndefined(); // The browser supplies the actual origin.
      expect(headers.Authorization).toBeUndefined();
    }
    await expect(client.setPause(false, "")).rejects.toMatchObject({
      code: "csrf_required",
    });
    expect(fetchImpl).toHaveBeenCalledTimes(4);
  });

  it("encodes exact-topic reads and keeps preflight advisory", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockImplementation(async () =>
        response({ advisory: true, delivery_guaranteed: false }),
      );
    const client = new HubClient({ mode: "live", fetchImpl });
    await client.getTopic("topic/name");
    const result = await client.preflight("topic:name", "hermes");
    await client.getEventsStatus();
    expect(fetchImpl.mock.calls.map((call) => call[0])).toEqual([
      "/api/hub/v1/topics/topic%2Fname/peers",
      "/api/hub/v1/topics/topic%3Aname/preflight?recipient=hermes",
      "/api/hub/v1/events/status",
    ]);
    expect(result.delivery_guaranteed).toBe(false);
  });

  it("sanitizes HTML, unknown error bodies and network exceptions", async () => {
    const secret = "PRIVATE-CREDENTIAL";
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(`<h1>${secret}</h1>`, { status: 502 }),
      )
      .mockResolvedValueOnce(response({ detail: { code: secret } }, 403))
      .mockRejectedValueOnce(new Error(secret));
    const client = new HubClient({ mode: "live", fetchImpl });
    for (const code of ["request_failed", "forbidden", "network_unavailable"]) {
      try {
        await client.getOverview();
        throw new Error("Expected rejection");
      } catch (error) {
        expect(error).toBeInstanceOf(HubApiError);
        expect((error as HubApiError).code).toBe(code);
        expect(String(error)).not.toContain(secret);
      }
    }
  });

  it("preserves safe conflict codes and rejects malformed success JSON", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        response({ detail: { code: "revision_conflict" } }, 409),
      )
      .mockResolvedValueOnce(new Response("not-json", { status: 200 }));
    const client = new HubClient({ mode: "live", fetchImpl });
    await expect(client.getOverview()).rejects.toMatchObject({
      status: 409,
      code: "revision_conflict",
    });
    await expect(client.getOverview()).rejects.toMatchObject({
      code: "invalid_response",
    });
  });
});

describe("explicit demo mode", () => {
  it("contains honest fixtures and never performs HTTP reads or writes", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new Error("Must not call network"));
    const client = new HubClient({ mode: "demo", fetchImpl });
    const session = await client.getSession();
    const overview = await client.getOverview();
    expect(overview.tasks).toHaveLength(2);
    expect(overview.decisions[0].status).toBe("open");
    expect(
      overview.projects.every(
        (item) => item.health === "unknown" && !item.executor_enabled,
      ),
    ).toBe(true);
    expect(overview.capabilities.host_dispatch).toBe(false);
    expect((await client.getEventsStatus()).available).toBe(false);
    const peers = await client.getTopic("demo-topic");
    expect(peers.items.map((peer) => peer.status)).toEqual([
      "reachable",
      "busy_queued",
      "unknown_stale",
    ]);
    expect(peers.items[0].valid_until).not.toBeNull();
    expect(peers.items[2].valid_until).toBeNull();
    expect(
      (await client.preflight("demo-topic", "hermes")).delivery_guaranteed,
    ).toBe(false);
    const decision = overview.decisions[0];
    expect(
      (
        await client.answerDecision(
          decision.id,
          decision.revision,
          decision.options[0],
          session.csrf_token!,
        )
      ).execution_authorization,
    ).toBe(false);
    await client.createTask(
      "3d-viewer",
      "Demo assignment",
      "demo-key",
      session.csrf_token!,
    );
    await client.setPause(true, session.csrf_token!);
    await client.logout(session.csrf_token!);
    expect((await client.getSession()).authenticated).toBe(false);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("isolates fixtures by client and enforces revision/idempotency within demo", async () => {
    const first = new HubClient({ mode: "demo" });
    const second = new HubClient({ mode: "demo" });
    const csrf = (await first.getSession()).csrf_token!;
    const task = await first.createTask(
      "3d-viewer",
      "Example",
      "same-key",
      csrf,
    );
    expect(
      (await first.createTask("3d-viewer", "Example", "same-key", csrf)).id,
    ).toBe(task.id);
    await expect(
      first.createTask("3d-viewer", "Changed", "same-key", csrf),
    ).rejects.toMatchObject({ code: "idempotency_conflict" });
    expect((await second.getOverview()).tasks).toHaveLength(2);
    const overview = await first.getOverview();
    overview.tasks.length = 0;
    expect((await first.getOverview()).tasks).toHaveLength(3);
    await expect(
      first.answerDecision("demo-decision-storage", 99, "invalid", csrf),
    ).rejects.toMatchObject({ code: "revision_conflict" });
  });
});
