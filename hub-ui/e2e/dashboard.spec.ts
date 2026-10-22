import { expect, test } from "@playwright/test";
import type { Page, Request } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import type { AgentSummary, TopicSnapshot } from "../src/types";

const NOW = new Date("2026-10-17T18:00:00.000Z");
let ORIGIN: string;
test.beforeEach(({}, testInfo) => {
  const baseURL = testInfo.project.use.baseURL;
  if (!baseURL) throw new Error("Browser tests require an explicit baseURL");
  ORIGIN = new URL(baseURL).origin;
});
const CSRF = "fixture-human-only-csrf";
/** Same-document page change (no reload), as when clicking the sidebar. */
async function go(page: Page, route: string) {
  await page.evaluate((hash) => { location.hash = hash; }, route);
}

const hubConnection = (state: "connected" | "not_observed" = "connected"): NonNullable<AgentSummary["connection"]> => ({
  state, source: "embedded_bus", scope: "this_server_process", observed_at: NOW.toISOString(),
  valid_until: new Date(NOW.getTime() + 30_000).toISOString(), active_streams: state === "connected" ? 1 : 0,
  inbox_subscriptions: 0, subscribed: false, last_stream_write_at: NOW.toISOString(), native_push_available: true,
});

test("Hub connection snapshots remain distinct from unconfigured topic readiness", async ({ page }) => {
  await page.clock.install({ time: NOW });
  const mock = await mockLive(page, { tracking: "not_configured" });
  mock.overview.agents[0].connection = hubConnection();
  mock.overview.agents[1].connection = hubConnection("not_observed");
  await page.goto("/#/agents");
  const rows = page.locator(".hub-connection-row");
  await expect(rows.filter({ hasText: "Codex" })).toContainText("Hub stream observed");
  await expect(rows.filter({ hasText: "Codex" })).toContainText("0 inbox subscription(s)");
  await expect(rows.filter({ hasText: "Hermes" })).toContainText("No active push stream observed");
  await expect(rows.filter({ hasText: "Claude" })).toContainText("Hub connection unknown");
  await expect(page.getByText("Topic readiness: Not tracked yet", { exact: true })).toBeVisible();
  await expect(page.locator(".peer-row")).toHaveCount(0);
  await expect(page.getByText("Online · verified", { exact: true })).toHaveCount(0);
  await expect(page.getByText("may still accept writes after the agent stops listening.", { exact: false })).toBeVisible();
});

test("Hub connection observations expire locally without polling", async ({ page }) => {
  await page.clock.install({ time: NOW });
  const mock = await mockLive(page, { tracking: "not_configured" });
  mock.overview.agents[0].connection = hubConnection();
  await page.goto("/#/agents");
  await expect(page.getByText("Hub stream observed", { exact: true })).toBeVisible();
  const reads = mock.reads();
  await page.clock.runFor(30_100);
  await expect(page.getByText("Hub stream observed", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Hub connection snapshot expired", { exact: true })).toBeVisible();
  expect(mock.reads()).toBe(reads);
});

test("refreshed stale stream snapshots stay neutral and do not imply topic readiness", async ({ page }) => {
  await page.clock.install({ time: NOW });
  const mock = await mockLive(page, { tracking: "not_configured" });
  const connection: NonNullable<AgentSummary["connection"]> = {
    ...hubConnection("not_observed"), state: "stale", stale_streams: 3,
  };
  mock.overview.agents[0].connection = connection;
  await page.goto("/#/agents");
  const row = page.locator(".hub-connection-row").filter({ hasText: "Codex" });
  for (let refresh = 0; refresh < 3; refresh++) {
    const observed = NOW.getTime() + refresh * 10_000;
    connection.observed_at = new Date(observed).toISOString();
    connection.valid_until = new Date(observed + 30_000).toISOString();
    await page.getByRole("button", { name: "Refresh workspace", exact: true }).click();
    await expect(row).toContainText("Connection stale — heartbeat not recently observed");
    await expect(row).toContainText("0 fresh stream(s)");
    await expect(row).toContainText("3 stale stream(s)");
    await expect(row.locator(".presence.on")).toHaveCount(0);
    await expect(page.getByText("Online · verified", { exact: true })).toHaveCount(0);
    await page.clock.runFor(10_000);
  }
});

test("loss of overview event feed invalidates connection and not-configured topic snapshots", async ({ page }) => {
  await page.clock.install({ time: NOW });
  await controllableEvents(page);
  const mock = await mockLive(page, { events: true, tracking: "not_configured" });
  mock.overview.agents[0].connection = hubConnection();
  await page.goto("/#/agents");
  await expect(page.getByText("Snapshot unverified", { exact: true })).toBeVisible();
  await emit(page, "invalidate");
  await expect(page.getByText("Hub stream observed", { exact: true })).toBeVisible();
  await emit(page, "error");
  await expect(page.getByText("Hub stream observed", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Hub connection snapshot unverified", { exact: true })).toHaveCount(3);
  await expect(page.getByText("Topic readiness: Not tracked yet", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Topic readiness: Snapshot unverified", { exact: true })).toBeVisible();
});

test("configured-but-unknown topic remains unverified while Hub transport is connected", async ({ page }) => {
  await page.clock.install({ time: NOW });
  const mock = await mockLive(page, { tracking: "configured" });
  mock.overview.agents[0].connection = hubConnection();
  await page.route("**/api/hub/v1/topics/*/peers", (route) => {
    const snapshot = createDemoTopic("auto-wake-v1-test", NOW);
    snapshot.tracking = "configured";
    snapshot.items = snapshot.items.map((item) => ({ ...item, status: "unknown_stale", reason: "observer_unavailable" }));
    return route.fulfill({ json: snapshot });
  });
  await page.goto("/#/agents");
  await expect(page.getByText("Hub stream observed", { exact: true })).toBeVisible();
  await expect(page.locator(".peer-row").getByText("Unknown", { exact: true })).toHaveCount(3);
  await expect(page.getByText("Topic readiness: Not tracked yet", { exact: true })).toHaveCount(0);
});

test("topic errors do not erase independent Hub connection snapshots", async ({ page }) => {
  await page.clock.install({ time: NOW });
  const mock = await mockLive(page);
  mock.overview.agents[0].connection = hubConnection();
  await page.route("**/api/hub/v1/topics/*/peers", (route) => route.fulfill({ status: 503, json: { detail: "unavailable" } }));
  await page.goto("/#/agents");
  await expect(page.getByText("Hub stream observed", { exact: true })).toBeVisible();
  await expect(page.getByText("Topic status unavailable.", { exact: false })).toBeVisible();
  await expect(page.getByText("Topic readiness: Not tracked yet", { exact: true })).toHaveCount(0);
  await page.route("**/api/hub/v1/overview", (route) => route.fulfill({ status: 503, json: { detail: "unavailable" } }));
  await page.getByRole("button", { name: "Refresh workspace", exact: true }).click();
  await expect(page.getByText("Hub stream observed", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Hub connection snapshot unverified", { exact: true })).toHaveCount(3);
});

test("invalid or future connection timestamps never render current connection evidence or HTML", async ({ page }) => {
  await page.clock.install({ time: NOW });
  const mock = await mockLive(page);
  mock.overview.agents[0].connection = { ...hubConnection(), observed_at: "<img src=x onerror=alert(1)>" };
  mock.overview.agents[1].connection = { ...hubConnection(), observed_at: new Date(NOW.getTime() + 60_000).toISOString() };
  mock.overview.agents[2].connection = { ...hubConnection(), valid_until: "not-a-date", last_stream_write_at: "<script>alert(1)</script>" };
  await page.goto("/#/agents");
  await expect(page.getByText("Hub stream observed", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Hub connection unavailable", { exact: true })).toHaveCount(3);
  await expect(page.locator(".hub-connection-row img, .hub-connection-row script")).toHaveCount(0);
});

test("agent staging proposals show proposer and scope without implying ownership", async ({ page }) => {
  const mock = await mockLive(page);
  mock.overview.tasks[0].scope = "staging";
  mock.overview.tasks[0].proposed_by = "hermes";
  mock.overview.tasks[0].owner = null;
  mock.overview.tasks[0].status = "assigned";
  await page.goto("/#/work");
  const row = page.locator("details.task-row").filter({ hasText: mock.overview.tasks[0].title });
  await expect(row.getByText("Staging proposal · Hermes", { exact: true })).toBeVisible();
  await expect(row.locator(".owner")).toContainText("Unclaimed");
});

test("Cloudflare opens the workspace without an extra sign-in click and respects sign-out", async ({ page }) => {
  const mock = await mockLive(page, { signedIn: false, access: true, bootstrap: true });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Sign out", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in securely" })).toHaveCount(0);
  expect(mock.requests.filter((request) => request.url().endsWith("/auth/bootstrap"))).toHaveLength(1);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByRole("link", { name: "Sign in securely" })).toBeVisible();
  await expect(page.getByText("Cloudflare Access protects this workspace.", { exact: false })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("link", { name: "Sign in securely" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out", exact: true })).toHaveCount(0);
});

test("failed Cloudflare bootstrap stays closed without redirect or retry loops", async ({ page }) => {
  const mock = await mockLive(page, { signedIn: false, access: true, bootstrapDenied: true });
  await page.goto("/");
  await expect(page.getByRole("link", { name: "Sign in securely" })).toBeVisible();
  await expect(page.getByRole("button", { name: "New task" })).toBeDisabled();
  expect(mock.requests.filter((request) => request.url().endsWith("/auth/bootstrap"))).toHaveLength(1);
  expect(mock.reads()).toBe(0);
  expect(new URL(page.url()).pathname).toBe("/");
});

test("held rollback reports why it needs attention without claiming rollback success", async ({ page }) => {
  const mock = await mockLive(page);
  mock.overview.runs.push({
    id: "fixture-held-pilot", revision: 4, updated_at: NOW.toISOString(),
    task_id: "fixture-task", project: "engineering-hub", target: "engineering-hub:staging",
    commit: "a".repeat(40), artifact_digest: "sha256:" + "b".repeat(64), policy_revision: 1,
    review: { actor: "claude", commit: "a".repeat(40), verdict: "approve" },
    status: "held", command_id: "fixture-phase", fencing_generation: 1,
    created_by: "codex", execution_actor: "executor:hub-pilot-v1", execution_manifest_digest: null,
    events: [{ event: "held", actor: "executor:hub-pilot-v1", command_id: "fixture-phase",
      fencing_generation: 1, reason: "rollback_outcome_uncertain" }],
  });
  await page.goto("/#/staging");
  const held = page.locator("details").filter({ hasText: "Needs attention: Rollback outcome uncertain" });
  await expect(held).toHaveCount(1);
  await held.locator("summary").click();
  await expect(held.getByText("Needs attention: Rollback outcome uncertain", { exact: true })).toBeVisible();
  await expect(held.getByText("Held", { exact: true })).toBeVisible();
  await expect(held.getByText("Rolled back", { exact: true })).toHaveCount(0);
});

async function mockLive(
  page: Page,
  { signedIn = true, events = false, expires = false, canonical = false, access = false, bootstrap = false, bootstrapDenied = false, tracking = undefined as TopicSnapshot["tracking"] } = {},
) {
  const overview = createDemoOverview(NOW);
  const requests: Request[] = [];
  let overviewReads = 0;
  await page.context().addCookies([
    {
      name: "hub_session",
      value: "fixture-browser-session",
      url: ORIGIN,
      httpOnly: true,
    },
  ]);
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request();
    requests.push(request);
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    if (path.endsWith("/auth/me"))
      return respond({
        authenticated: signedIn,
        actor: signedIn ? access ? "human:cf:" + "a".repeat(64) : "human:42" : null,
        github_id: signedIn && !access ? 42 : null,
        csrf_token: signedIn ? CSRF : null,
        ...(access ? { auth_provider: "cloudflare_access", email: signedIn ? "operator@first.example" : null } : {}),
      });
    if (path.endsWith("/auth/bootstrap")) {
      expect(request.method()).toBe("POST");
      const headers = await request.allHeaders();
      expect(headers.origin).toBe(ORIGIN);
      expect(headers.authorization).toBeUndefined();
      expect(headers["x-hub-csrf"]).toBeUndefined();
      if (bootstrapDenied) return respond({ detail: "denied" }, 403);
      signedIn = bootstrap;
      return respond({ authenticated: signedIn, auth_provider: "cloudflare_access",
        actor: signedIn ? "human:cf:" + "a".repeat(64) : null,
        csrf_token: signedIn ? CSRF : null, github_id: null,
        email: signedIn ? "operator@first.example" : null });
    }
    if (!signedIn)
      return respond({ detail: { code: "sign_in_required" } }, 401);
    if (request.method() === "POST") {
      const headers = await request.allHeaders();
      expect(headers["origin"]).toBe(ORIGIN);
      expect(headers["x-hub-csrf"]).toBe(CSRF);
      expect(headers["authorization"]).toBeUndefined();
      expect(headers["cookie"]).toContain(
        "hub_session=fixture-browser-session",
      );
      if (path.endsWith("/auth/logout")) {
        signedIn = false;
        bootstrap = false; // Models the server's HttpOnly logout suppression.
        return respond({ authenticated: false });
      }
      if (path.endsWith("/tasks")) {
        const payload = request.postDataJSON();
        expect(payload.client_request_id).toMatch(/^[0-9a-f-]{36}$/);
        const task = {
          ...overview.tasks[1],
          id: "fixture-created-task",
          project: payload.project,
          title: payload.title,
          owner: null,
          status: "assigned" as const,
          revision: 1,
        };
        overview.tasks.push(task);
        return respond(task, 201);
      }
      if (path.endsWith("/pause")) {
        overview.paused = request.postDataJSON().paused;
        return respond({ paused: overview.paused, revision: 1 });
      }
      if (path.includes("/decisions/") && path.endsWith("/answer")) {
        const decision = overview.decisions[0];
        decision.answer = request.postDataJSON().answer;
        decision.status = "answered";
        decision.revision++;
        return respond(decision);
      }
      return respond({ detail: { code: "unexpected_write" } }, 500);
    }
    if (path.endsWith("/overview")) {
      overviewReads++;
      return respond(overview);
    }
    if (path.endsWith("/events/status"))
      return respond({
        available: events,
        reason: events ? "native_source_active" : "native_source_unsupported",
        observer_epoch: "fixture-public-epoch",
        cursor: 0,
      });
    if (path.includes("/topics/") && path.endsWith("/peers")) {
      const topic = decodeURIComponent(
        path.split("/topics/")[1].replace("/peers", ""),
      );
      const snapshot = createDemoTopic(topic, NOW);
      if (tracking) snapshot.tracking = tracking;
      if (canonical) {
        snapshot.event_source = "canonical_registry";
        snapshot.registry_epoch = "12345678-1234-1234-1234-123456789abc";
      }
      if (expires) {
        snapshot.items[0].valid_until = new Date(
          NOW.getTime() + 1000,
        ).toISOString();
        snapshot.items[1].valid_until = new Date(
          NOW.getTime() + 3000,
        ).toISOString();
      }
      return respond(snapshot);
    }
    return respond({ detail: { code: "unexpected_read" } }, 500);
  });
  return { overview, requests, reads: () => overviewReads };
}

async function controllableEvents(page: Page) {
  await page.addInitScript(() => {
    (window as any).__hubStreams = [];
    class FixtureEventSource {
      onopen: ((event: Event) => void) | null = null;
      onmessage: ((event: { data: string }) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      closed = false;
      handlers: Record<string, (event: unknown) => void> = {};
      addEventListener(kind: string, callback: (event: unknown) => void) {
        this.handlers[kind] = callback;
      }
      constructor(
        public url: string,
        public options: unknown,
      ) {
        (window as any).__hubStreams.push(this);
        queueMicrotask(() => this.onopen?.(new Event("open")));
      }
      close() {
        this.closed = true;
      }
    }
    (window as any).EventSource = FixtureEventSource;
  });
}

async function emit(
  page: Page,
  kind: "invalidate" | "source_unavailable" | "error" | "open",
) {
  await page.evaluate((kind) => {
    const stream = (window as any).__hubStreams.at(-1);
    if (!stream || stream.closed)
      throw new Error("fixture event source unavailable");
    if (kind === "open") stream.onopen?.(new Event("open"));
    else if (kind === "error") stream.onerror?.(new Event("error"));
    else
      stream.onmessage?.({
        data: JSON.stringify({
          kind,
          observer_epoch: "fixture-public-epoch",
          cursor: 1,
        }),
      });
  }, kind);
}

async function emitRegistry(page: Page, kind: string, overrides: Record<string, unknown> = {}) {
  await page.evaluate(({ kind, overrides }) => {
    const stream = (window as any).__hubStreams.filter((entry: any) => entry.url.includes("/topics/")).at(-1);
    if (!stream || stream.closed) throw new Error("Topic source unavailable");
    const epoch = "12345678-1234-1234-1234-123456789abc";
    const body = kind === "registry.invalidated"
      ? { topic: "auto-wake-v1-test", registry_epoch: epoch, snapshot_revision: epoch, cursor: 1 }
      : { topic: "auto-wake-v1-test", registry_epoch: epoch, head: 1, reason: "initial_snapshot" };
    if (kind === "error") stream.onerror?.(new Event("error"));
    else stream.handlers[kind]?.({ data: JSON.stringify({ ...body, ...overrides }), lastEventId: `${epoch}:auto-wake-v1-test:1` });
  }, { kind, overrides });
}

test("colon topic passes strict ingress without discarding task or decision records", async ({ page }) => {
  await page.clock.install({ time: NOW });
  const mock = await mockLive(page);
  await page.route("**/api/hub/v1/topics/**", async (route) => {
    if (new URL(route.request().url()).pathname.includes("%"))
      return route.fulfill({ status: 403, contentType: "application/json", body: JSON.stringify({ detail: { code: "ingress_forbidden" } }) });
    return route.fallback();
  });
  await page.goto("/#/inbox");
  await expect(page.getByRole("heading", { name: "Needs Beau", exact: true })).toBeVisible();
  await go(page, "#/agents");
  await page.getByLabel("TOPIC ROOT").fill("ops:alerts");
  const peers = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/hub/v1/topics/ops:alerts/peers");
  await page.getByRole("button", { name: "Check", exact: true }).click();
  expect((await peers).status()).toBe(200);
  await expect(page.locator(".peer-row").filter({ hasText: "Codex" }).getByText("Online · verified", { exact: true })).toBeVisible();
  await go(page, "#/inbox");
  await expect(page.getByRole("heading", { name: "Needs Beau", exact: true })).toBeVisible();
  await expect(page.getByText(mock.overview.decisions[0].title, { exact: true })).toBeVisible();
  await go(page, "#/work");
  await expect(page.getByText(mock.overview.tasks[0].title, { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "New task", exact: true })).toBeEnabled();
  await go(page, "#/agents");
  await expect(page.locator("#agents").getByRole("alert")).toHaveCount(0);
});

test("topic ingress rejection remains panel-local but overview ingress rejection clears private records", async ({ page }) => {
  await mockLive(page);
  await page.goto("/#/inbox");
  await expect(page.getByRole("heading", { name: "Needs Beau", exact: true })).toBeVisible();
  await go(page, "#/agents");
  await page.route("**/api/hub/v1/topics/*/peers", (route) => route.fulfill({ status: 403, contentType: "application/json", body: JSON.stringify({ detail: { code: "ingress_forbidden" } }) }));
  await page.getByLabel("TOPIC ROOT").fill("ops:alerts");
  await page.getByRole("button", { name: "Check", exact: true }).click();
  await expect(page.locator("#agents").getByRole("alert")).toContainText("rejected by ingress");
  await expect(page.locator(".peer-row")).toHaveCount(0);
  await go(page, "#/inbox");
  await expect(page.getByRole("heading", { name: "Needs Beau", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "New task", exact: true })).toBeEnabled();
  await page.route("**/api/hub/v1/overview", (route) => route.fulfill({ status: 403, contentType: "application/json", body: JSON.stringify({ detail: { code: "ingress_forbidden" } }) }));
  await page.getByRole("button", { name: "Refresh workspace", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Sign in to Engineering Hub." })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Needs Beau", exact: true })).toHaveCount(0);
  await expect(page.getByText("Verify the isolated Viewer staging candidate")).toHaveCount(0);
});

for (const typed of [false, true]) {
  test(`topic ACL denial stays panel-local for ${typed ? "typed" : "default"} topic`, async ({ page }) => {
    await page.clock.install({ time: NOW });
    await mockLive(page);
    const deniedTopic = typed ? "restricted-topic" : "auto-wake-v1-test";
    await page.route(`**/api/hub/v1/topics/${deniedTopic}/peers`, (route) => route.fulfill({
      status: 403, contentType: "application/json", body: JSON.stringify({ detail: { code: "topic_read_forbidden" } }),
    }));
    await page.goto("/#/agents");
    if (typed) {
      await expect(page.locator(".peer-row").filter({ hasText: "Codex" }).getByText("Online · verified", { exact: true })).toBeVisible();
      await page.getByLabel("TOPIC ROOT").fill(deniedTopic);
      await page.getByRole("button", { name: "Check", exact: true }).click();
    }
    await expect(page.getByText("This session cannot read the selected topic.", { exact: false })).toBeVisible();
    await expect(page.locator(".peer-row")).toHaveCount(0);
    await go(page, "#/inbox");
    await expect(page.getByRole("heading", { name: "Needs Beau", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "New task", exact: true })).toBeEnabled();
    await expect(page.getByRole("link", { name: "Sign in with GitHub" })).toHaveCount(0);
  });
}

for (const eventSource of ["hub_only", "none"] as const) {
test(`canonical topic ACL denial stays local and same-topic ${eventSource} recovery clears stale state`, async ({ page }) => {
  await page.clock.install({ time: NOW });
  await controllableEvents(page);
  await mockLive(page, { canonical: true });
  await page.goto("/#/agents");
  const codex = page.locator(".peer-row").filter({ hasText: "Codex" });
  await expect(codex.getByText("Online · verified", { exact: true })).toBeVisible();
  await page.route("**/api/hub/v1/topics/*/peers", (route) => route.fulfill({
    status: 403, contentType: "application/json", body: JSON.stringify({ detail: { code: "topic_read_forbidden" } }),
  }));
  await emitRegistry(page, "registry.invalidated");
  await expect(page.getByText("This session cannot read the selected topic.", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "New task", exact: true })).toBeEnabled();
  await expect(page.locator(".peer-row")).toHaveCount(0);
  await page.route("**/api/hub/v1/topics/*/peers", (route) => route.fulfill({
    contentType: "application/json", body: JSON.stringify({ ...createDemoTopic("auto-wake-v1-test", NOW), event_source: eventSource, registry_epoch: null }),
  }));
  await page.getByRole("button", { name: "Check", exact: true }).click();
  await expect(codex.getByText("Online · verified", { exact: true })).toBeVisible();
  await expect(page.getByText("This session cannot read the selected topic.", { exact: false })).toHaveCount(0);
});
}

test("canonical topic invalidation refreshes only topic and source loss leaves workspace writable", async ({ page }) => {
  await page.clock.install({ time: NOW });
  await controllableEvents(page);
  const mock = await mockLive(page, { canonical: true });
  await page.goto("/#/agents");
  const codex = page.locator(".peer-row").filter({ hasText: "Codex" });
  await expect(codex.getByText("Online · verified", { exact: true })).toBeVisible();
  const overviewReads = mock.reads();
  const topicReads = () => mock.requests.filter((request) => new URL(request.url()).pathname.endsWith("/peers")).length;
  const before = topicReads();
  await emitRegistry(page, "registry.invalidated");
  await expect.poll(topicReads).toBe(before + 1);
  await expect(codex.getByText("Online · verified", { exact: true })).toBeVisible();
  expect(mock.reads()).toBe(overviewReads);
  await emitRegistry(page, "error");
  await expect(codex.getByText("Unknown", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "New task", exact: true })).toBeEnabled();
  const streamCount = await page.evaluate(() => (window as any).__hubStreams.length);
  await page.clock.runFor(20_000);
  expect(await page.evaluate(() => (window as any).__hubStreams.length)).toBe(streamCount);
  expect(mock.reads()).toBe(overviewReads);
  expect(topicReads()).toBe(before + 1);
});

test("canonical terminal recovery takes fresh snapshot and resumes with explicit scoped cursor", async ({ page }) => {
  await page.clock.install({ time: NOW });
  await controllableEvents(page);
  await mockLive(page, { canonical: true });
  await page.goto("/#/agents");
  await expect(page.locator(".peer-row").filter({ hasText: "Codex" }).getByText("Online · verified", { exact: true })).toBeVisible();
  await emitRegistry(page, "registry.snapshot_required");
  await expect.poll(() => page.evaluate(() => (window as any).__hubStreams.filter((entry: any) => entry.url.includes("/topics/")).length)).toBe(2);
  const sources = await page.evaluate(() => (window as any).__hubStreams.map((entry: any) => ({ url: entry.url, closed: entry.closed })));
  expect(sources[0].closed).toBe(true);
  expect(sources[1].url).toBe("/api/hub/v1/topics/auto-wake-v1-test/events?after=1&epoch=12345678-1234-1234-1234-123456789abc");
  await emitRegistry(page, "registry.snapshot_required");
  await expect(page.getByText("Topic event source unavailable.", { exact: false })).toBeVisible();
  expect(await page.evaluate(() => (window as any).__hubStreams.length)).toBe(2);
});

for (const status of [503, 403]) {
  test(`obsolete topic ${status} cannot erase a newer canonical snapshot or workspace`, async ({ page }) => {
    await page.clock.install({ time: NOW });
    await controllableEvents(page);
    await mockLive(page, { canonical: true });
    await page.goto("/#/agents");
    const codex = page.locator(".peer-row").filter({ hasText: "Codex" });
    await expect(codex.getByText("Online · verified", { exact: true })).toBeVisible();
    let release!: () => void;
    let entered!: () => void;
    const held = new Promise<void>((resolve) => { release = resolve; });
    const pending = new Promise<void>((resolve) => { entered = resolve; });
    let first = true;
    await page.route("**/api/hub/v1/topics/*/peers", async (route) => {
      if (!first) return route.fallback();
      first = false;
      entered();
      await held;
      await route.fulfill({ status, contentType: "application/json", body: JSON.stringify({ detail: { code: "obsolete_failure" } }) });
    });
    try {
      await page.getByRole("button", { name: "Refresh workspace", exact: true }).click();
      await pending;
      await emitRegistry(page, "registry.invalidated");
      await expect(codex.getByText("Online · verified", { exact: true })).toBeVisible();
      const response = page.waitForResponse((response) => response.status() === status && response.url().endsWith("/peers"));
      release();
      await response;
      // Flush promise/render work through a browser task boundary, not time polling.
      await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      await expect(codex.getByText("Online · verified", { exact: true })).toBeVisible();
      await expect(page.getByRole("button", { name: "New task", exact: true })).toBeEnabled();
      await go(page, "#/inbox");
      await expect(page.getByRole("heading", { name: "Needs Beau", exact: true })).toBeVisible();
      await expect(page.getByText("Topic status unavailable.", { exact: false })).toHaveCount(0);
    } finally { release(); }
  });
}

test("demo is explicitly labelled and task/decision/pause actions never contact an API", async ({
  page,
}, testInfo) => {
  const writes: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/")) writes.push(request.url());
  });
  await page.goto("/?demo=1#/inbox");
  await expect(
    page.getByText("Demo workspace.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Sample records and local-only actions. Not your live infrastructure.",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Needs Beau", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("demo-workspace.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "New task", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("combobox", { name: "Project", exact: true })
    .selectOption("3d-viewer");
  await page
    .getByLabel("What needs to happen?")
    .fill("Fixture demo acceptance task");
  await page.getByRole("button", { name: "Assign task", exact: true }).click();
  await go(page, "#/work");
  await expect(
    page.getByText("Fixture demo acceptance task", { exact: true }),
  ).toBeVisible();
  await expect(page.locator('.notice[role="status"]')).toContainText(
    "Demo only; no server changed.",
  );
  await go(page, "#/inbox");
  await expect(page.getByRole("radio", { checked: true })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Record decision", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("radio", { name: "NAS-backed small fixture", exact: true })
    .check();
  await page
    .getByRole("button", { name: "Record decision", exact: true })
    .click();
  await expect(page.locator('.notice[role="status"]')).toContainText(
    "Decision recorded, not an execution grant.",
  );
  await expect(
    page.getByRole("heading", { name: "Nothing waiting on you." }),
  ).toBeVisible();
  await go(page, "#/staging");
  await page
    .getByRole("button", { name: "Pause new work", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "Active actions reach a safe boundary",
  );
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Pause new work", exact: true })
    .click();
  await expect(
    page.getByText("Emergency pause on: new staging work is blocked", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Resume", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Resume new work", exact: true })
    .click();
  await expect(
    page.getByText("Running normally: nothing is paused", { exact: true }),
  ).toBeVisible();
  expect(writes).toEqual([]);
});

test("signed-out live mode shows no sample or private records and makes no private reads", async ({
  page,
}) => {
  const mock = await mockLive(page, { signedIn: false });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Sign in to Engineering Hub." }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Sign in securely" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "New task", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("heading", { name: "Needs Beau", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Verify the isolated Viewer staging candidate"),
  ).toHaveCount(0);
  await expect(
    page.getByText("Choose the initial scratch-storage benchmark"),
  ).toHaveCount(0);
  expect(
    mock.requests.every((request) =>
      new URL(request.url()).pathname.endsWith("/auth/me"),
    ),
  ).toBe(true);
});

test("live task creation uses browser session, same-origin request and CSRF without agent bearer", async ({
  page,
}) => {
  const mock = await mockLive(page);
  let releaseQueue!: () => void;
  const queueWaiting = new Promise<void>((resolve) => { releaseQueue = resolve; });
  // Keep a second legitimate status visible: success must be identified by
  // its content, not by assuming the entire dashboard has only one live region.
  await page.route((url) => url.pathname === "/api/hub/v1/work/status", async (route) => {
    await queueWaiting;
    await route.fulfill({ status: 423, contentType: "application/json",
      body: JSON.stringify({ detail: { code: "work_queue_not_configured" } }) });
  });
  try {
    await page.goto("/#/staging");
    await expect
      .poll(() =>
        mock.requests.map(
          (request) => `${request.method()} ${new URL(request.url()).pathname}`,
        ),
      )
      .toContain("GET /api/hub/v1/overview");
    await page.getByRole("button", { name: "New task", exact: true }).click();
    await page
      .getByLabel("What needs to happen?")
      .fill("Live fixture human assignment");
    await page.getByRole("button", { name: "Assign task", exact: true }).click();
    await expect(page.getByRole("status").filter({ hasText: /^Reading queue status…$/ })).toBeVisible();
    await expect(page.getByRole("status").filter({ hasText: /^Task assigned\.$/ })).toHaveText("Task assigned.");
    await go(page, "#/work");
    await expect(
      page.getByText("Live fixture human assignment", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Demo workspace.", { exact: true })).toHaveCount(
      0,
    );
  } finally {
    releaseQueue();
  }
});

test("SSE invalidation refetches, loss marks routes stale and disables mutations without polling", async ({
  page,
}) => {
  await page.clock.install({ time: NOW });
  await controllableEvents(page);
  const mock = await mockLive(page, { events: true });
  await page.goto("/#/agents");
  await expect(
    page.getByText("Snapshot unverified", { exact: true }),
  ).toBeVisible();
  await emit(page, "invalidate");
  await expect(
    page.getByText("Event feed connected", { exact: true }),
  ).toBeVisible();
  await expect(
    page
      .locator(".peer-row")
      .filter({ hasText: "Codex" })
      .getByText("Online · verified", { exact: true }),
  ).toBeVisible();
  const reads = mock.reads();
  await page.clock.runFor(20_000);
  expect(mock.reads()).toBe(reads);
  await emit(page, "error");
  await expect(
    page.getByText("Snapshot unverified", { exact: true }),
  ).toBeVisible();
  await expect(
    page
      .locator(".peer-row")
      .filter({ hasText: "Codex" })
      .getByText("Unknown", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "New task", exact: true }),
  ).toBeDisabled();
  await emit(page, "open");
  await emit(page, "invalidate");
  await expect(
    page.getByText("Event feed connected", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "New task", exact: true }),
  ).toBeEnabled();
  await emit(page, "source_unavailable");
  await expect(
    page.getByText("Snapshot unverified", { exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(() => (window as any).__hubStreams.at(-1).closed),
  ).toBe(true);
});

test("each peer expires locally at its own deadline without HTTP refresh", async ({
  page,
}) => {
  await page.clock.install({ time: NOW });
  const mock = await mockLive(page, { expires: true });
  await page.goto("/#/agents");
  const codex = page.locator(".peer-row").filter({ hasText: "Codex" });
  const hermes = page.locator(".peer-row").filter({ hasText: "Hermes" });
  await expect(codex.getByText("Online · verified", { exact: true })).toBeVisible();
  await expect(hermes.getByText("Busy", { exact: true })).toBeVisible();
  const reads = mock.reads();
  await page.clock.runFor(1100);
  await expect(codex.getByText("Unknown", { exact: true })).toBeVisible();
  await expect(hermes.getByText("Busy", { exact: true })).toBeVisible();
  await page.clock.runFor(2100);
  await expect(hermes.getByText("Unknown", { exact: true })).toBeVisible();
  expect(mock.reads()).toBe(reads);
});

test("changing topic clears previous peers before the next snapshot arrives", async ({
  page,
}) => {
  await page.clock.install({ time: NOW });
  await mockLive(page);
  let release!: () => void;
  let entered!: () => void;
  const waiting = new Promise<void>((resolve) => {
    release = resolve;
  });
  const pending = new Promise<void>((resolve) => {
    entered = resolve;
  });
  await page.route(
    "**/api/hub/v1/topics/different-topic/peers",
    async (route) => {
      entered();
      await waiting;
      const snapshot = createDemoTopic("different-topic", NOW);
      snapshot.items.forEach((peer) => {
        peer.status = "unknown_stale";
        peer.valid_until = null;
      });
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(snapshot),
      });
    },
  );
  await page.goto("/#/agents");
  await expect(
    page
      .locator(".peer-row")
      .filter({ hasText: "Codex" })
      .getByText("Online · verified", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("TOPIC ROOT").fill("different-topic");
  await page.getByRole("button", { name: "Check", exact: true }).click();
  await pending;
  await expect(page.locator(".peer-row")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "New task", exact: true }),
  ).toBeEnabled();
  release();
  await expect(page.locator(".peer-row")).toHaveCount(3);
  await expect(
    page
      .locator(".peer-row")
      .filter({ hasText: "Codex" })
      .getByText("Unknown", { exact: true }),
  ).toBeVisible();
});

for (const lost of ["error", "source_unavailable"] as const) {
  test(`late invalidation refresh cannot restore verified state after ${lost}`, async ({
    page,
  }) => {
    await page.clock.install({ time: NOW });
    await controllableEvents(page);
    const mock = await mockLive(page, { events: true });
    await page.goto("/#/agents");
    await expect(
      page.getByText("Snapshot unverified", { exact: true }),
    ).toBeVisible();
    await emit(page, "invalidate");
    await expect(
      page.getByText("Event feed connected", { exact: true }),
    ).toBeVisible();
    let release!: () => void;
    let entered!: () => void;
    const waiting = new Promise<void>((resolve) => {
      release = resolve;
    });
    const pending = new Promise<void>((resolve) => {
      entered = resolve;
    });
    await page.route("**/api/hub/v1/overview", async (route) => {
      entered();
      await waiting;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(mock.overview),
      });
    });
    await emit(page, "invalidate");
    await pending;
    await emit(page, lost);
    release();
    await expect(
      page.getByRole("button", { name: "Refresh workspace" }),
    ).toBeEnabled();
    await expect(
      page.getByText("Snapshot unverified", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("Event feed connected", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "New task", exact: true }),
    ).toBeDisabled();
    // Evidence may be retained as Unknown or cleared entirely. Neither path
    // may restore a verified route from a response that crossed disconnection.
    await expect(
      page.locator(".peer-row").getByText("Online · verified", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.locator(".peer-row").getByText("Busy", { exact: true }),
    ).toHaveCount(0);
  });
}

test("a burst of invalidations coalesces to one in-flight refresh and one catchup", async ({
  page,
}) => {
  await page.clock.install({ time: NOW });
  await controllableEvents(page);
  const mock = await mockLive(page, { events: true });
  await page.goto("/#/agents");
  await expect(
    page.getByText("Snapshot unverified", { exact: true }),
  ).toBeVisible();
  await emit(page, "invalidate");
  await expect(
    page.getByText("Event feed connected", { exact: true }),
  ).toBeVisible();
  let release!: () => void;
  let entered!: () => void;
  const waiting = new Promise<void>((resolve) => {
    release = resolve;
  });
  const pending = new Promise<void>((resolve) => {
    entered = resolve;
  });
  let reads = 0;
  await page.route("**/api/hub/v1/overview", async (route) => {
    reads++;
    if (reads === 1) {
      entered();
      await waiting;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(mock.overview),
    });
  });
  await emit(page, "invalidate");
  await pending;
  await page.evaluate(() => {
    const stream = (window as any).__hubStreams.at(-1);
    for (let cursor = 2; cursor < 102; cursor++)
      stream.onmessage({
        data: JSON.stringify({
          kind: "invalidate",
          observer_epoch: "fixture-public-epoch",
          cursor,
        }),
      });
  });
  expect(reads).toBe(1);
  release();
  await expect.poll(() => reads).toBe(2);
  await expect(
    page.getByText("Event feed connected", { exact: true }),
  ).toBeVisible();
  await page.clock.runFor(20_000);
  expect(reads).toBe(2);
});

test("conflicting decision disables writes until explicit refresh without echoing errors", async ({
  page,
}) => {
  await mockLive(page);
  await page.route("**/api/hub/v1/decisions/*/answer", (route) =>
    route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        detail: {
          code: "revision_conflict",
          private: "private-error-do-not-echo",
        },
      }),
    }),
  );
  await page.goto("/#/inbox");
  await page
    .getByRole("radio", { name: "NAS-backed small fixture", exact: true })
    .check();
  await page
    .getByRole("button", { name: "Record decision", exact: true })
    .click();
  await expect(page.locator('.notice[role="alert"]')).toHaveText(
    "This record changed. Refresh before trying again.",
  );
  await expect(
    page.getByRole("button", { name: "New task", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Record decision", exact: true }),
  ).toBeDisabled();
  await expect(page.getByText("private-error-do-not-echo")).toHaveCount(0);
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect(
    page.getByRole("button", { name: "New task", exact: true }),
  ).toBeEnabled();
});

test("expired human session removes loaded private records and cannot fall back to demo", async ({
  page,
}) => {
  await mockLive(page);
  await page.route("**/api/hub/v1/decisions/*/answer", (route) =>
    route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "sign_in_required" } }),
    }),
  );
  await page.goto("/#/inbox");
  await page
    .getByRole("radio", { name: "NAS-backed small fixture", exact: true })
    .check();
  await page
    .getByRole("button", { name: "Record decision", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Sign in to Engineering Hub." }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Needs Beau", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Verify the isolated Viewer staging candidate"),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "New task", exact: true }),
  ).toBeDisabled();
  await expect(page.getByText("Demo workspace.", { exact: true })).toHaveCount(
    0,
  );
});

test("expired task submission closes its private draft rather than leaving a signed-out modal", async ({
  page,
}) => {
  await mockLive(page);
  await page.route("**/api/hub/v1/tasks", (route) =>
    route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "sign_in_required" } }),
    }),
  );
  await page.goto("/");
  await page.getByRole("button", { name: "New task", exact: true }).click();
  await page
    .getByLabel("What needs to happen?")
    .fill("Private draft from expired session");
  await page.getByRole("button", { name: "Assign task", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Sign in to Engineering Hub." }),
  ).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.locator("textarea")).toHaveCount(0);
});

test("protected decision requires explicit choice and separate confirmation; cancel sends nothing", async ({
  page,
}) => {
  const mock = await mockLive(page);
  const decision = mock.overview.decisions[0];
  decision.protected = true;
  decision.revision = 17;
  decision.title = "Choose the protected fixture review outcome";
  await page.goto("/#/inbox");
  const record = page.getByRole("button", {
    name: "Record decision",
    exact: true,
  });
  await expect(record).toBeDisabled();
  await expect(page.getByRole("radio", { checked: true })).toHaveCount(0);
  await page
    .getByRole("radio", { name: "Wait for local disk inspection", exact: true })
    .check();
  await record.click();
  const dialog = page.getByRole("dialog", {
    name: "Confirm protected decision",
    exact: true,
  });
  await expect(dialog).toContainText(decision.title);
  await expect(dialog).toContainText("Wait for local disk inspection");
  await expect(dialog).toContainText(
    "It does not grant permission to execute or deploy.",
  );
  expect(
    mock.requests.filter((request) => request.method() === "POST"),
  ).toHaveLength(0);
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(
    mock.requests.filter((request) => request.method() === "POST"),
  ).toHaveLength(0);
  await record.click();
  await page
    .getByRole("dialog", { name: "Confirm protected decision", exact: true })
    .getByRole("button", { name: "Confirm decision", exact: true })
    .click();
  // The queue panel may briefly show its own "Reading queue status…" status
  // while the overview refreshes after the write.
  await expect(page.getByRole("status").filter({ hasText: /^Decision recorded, not an execution grant\.$/ }))
    .toHaveText("Decision recorded, not an execution grant.");
  const writes = mock.requests.filter((request) => request.method() === "POST");
  expect(writes).toHaveLength(1);
  expect(new URL(writes[0].url()).pathname).toBe(
    "/api/hub/v1/decisions/demo-decision-storage/answer",
  );
  expect(writes[0].postDataJSON()).toEqual({
    expected_revision: 17,
    answer: "Wait for local disk inspection",
  });
  expect(mock.overview.runs).toEqual([]);
});

for (const status of [422, 503]) {
  test(`topic ${status} clears old readiness but leaves fresh workspace operations usable`, async ({
    page,
  }) => {
    await page.clock.install({ time: NOW });
    await mockLive(page);
    await page.route(
      "**/api/hub/v1/topics/unavailable-fixture-topic/peers",
      (route) =>
        route.fulfill({
          status,
          contentType: "application/json",
          body: JSON.stringify({
            detail: {
              code: status === 422 ? "invalid_topic" : "service_unavailable",
              private: "private-topic-error",
            },
          }),
        }),
    );
    await page.goto("/#/agents");
    await expect(
      page
        .locator(".peer-row")
        .filter({ hasText: "Codex" })
        .getByText("Online · verified", { exact: true }),
    ).toBeVisible();
    await page.getByLabel("TOPIC ROOT").fill("unavailable-fixture-topic");
    await page.getByRole("button", { name: "Check", exact: true }).click();
    await expect(page.locator("#agents").getByRole("alert")).toHaveText(
      "Topic status unavailable. Check the topic and retry; workspace records remain available.",
    );
    await expect(page.locator(".peer-row")).toHaveCount(0);
    await expect(page.getByText("private-topic-error")).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "New task", exact: true }),
    ).toBeEnabled();
    await page.getByRole("button", { name: "New task", exact: true }).click();
    await page
      .getByLabel("What needs to happen?")
      .fill(`Workspace survives topic ${status}`);
    await page
      .getByRole("button", { name: "Assign task", exact: true })
      .click();
    await go(page, "#/work");
    await expect(
      page.getByText(`Workspace survives topic ${status}`, { exact: true }),
    ).toBeVisible();
  });
}

for (const status of [401, 403]) {
  test(`topic ${status} fails closed globally rather than keeping private workspace records`, async ({
    page,
  }) => {
    await mockLive(page);
    await page.route(
      "**/api/hub/v1/topics/denied-fixture-topic/peers",
      (route) =>
        route.fulfill({
          status,
          contentType: "application/json",
          body: JSON.stringify({ detail: { code: "forbidden" } }),
        }),
    );
    await page.goto("/#/inbox");
    await expect(
      page.getByRole("heading", { name: "Needs Beau", exact: true }),
    ).toBeVisible();
    await go(page, "#/agents");
    await page.getByLabel("TOPIC ROOT").fill("denied-fixture-topic");
    await page.getByRole("button", { name: "Check", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "Sign in to Engineering Hub." }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Needs Beau", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByText("Verify the isolated Viewer staging candidate"),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "New task", exact: true }),
    ).toBeDisabled();
    await expect(page.locator(".peer-row")).toHaveCount(0);
  });
}

test("live profile displays the authenticated principal and GitHub identity, not a hardcoded person", async ({
  page,
}) => {
  await mockLive(page);
  await page.goto("/");
  await expect(
    page.locator(".profile").getByText("human:42", { exact: true }),
  ).toBeVisible();
  await expect(
    page.locator(".profile").getByText("GitHub ID 42", { exact: true }),
  ).toBeVisible();
  await expect(
    page.locator(".profile").getByText("Beau", { exact: true }),
  ).toHaveCount(0);
});

test("Access profile displays the verified email and provider without a fake GitHub identity", async ({ page }) => {
  await mockLive(page, { access: true });
  await page.goto("/");
  await expect(page.locator(".profile").getByText("operator@first.example", { exact: true })).toBeVisible();
  await expect(page.locator(".profile").getByText("Cloudflare Access", { exact: true })).toBeVisible();
  await expect(page.locator(".profile").getByText(/GitHub ID/)).toHaveCount(0);
  await page.getByRole("button", { name: "New task", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
});

test("a pending topic request does not hold fresh workspace records or human actions hostage", async ({
  page,
}) => {
  await mockLive(page);
  let release!: () => void;
  let entered!: () => void;
  const waiting = new Promise<void>((resolve) => {
    release = resolve;
  });
  const pending = new Promise<void>((resolve) => {
    entered = resolve;
  });
  await page.route("**/api/hub/v1/topics/*/peers", async (route) => {
    entered();
    await waiting;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(createDemoTopic("auto-wake-v1-test", NOW)),
    });
  });
  try {
    await page.goto("/#/inbox");
    await pending;
    await expect(
      page.getByRole("heading", { name: "Needs Beau", exact: true }),
    ).toBeVisible();
    await go(page, "#/work");
    await expect(
      page.getByText("Verify the isolated Viewer staging candidate", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "New task", exact: true }),
    ).toBeEnabled();
    await page.getByRole("button", { name: "New task", exact: true }).click();
    await page
      .getByLabel("What needs to happen?")
      .fill("Task created while topic evidence is pending");
    await page
      .getByRole("button", { name: "Assign task", exact: true })
      .click();
    await expect(
      page.getByText("Task created while topic evidence is pending", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);
  } finally {
    release();
  }
});

test("late topic authentication failure closes an already-open private task draft", async ({
  page,
}) => {
  await mockLive(page);
  let release!: () => void;
  let entered!: () => void;
  const waiting = new Promise<void>((resolve) => {
    release = resolve;
  });
  const pending = new Promise<void>((resolve) => {
    entered = resolve;
  });
  await page.route("**/api/hub/v1/topics/*/peers", async (route) => {
    entered();
    await waiting;
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "sign_in_required" } }),
    });
  });
  try {
    await page.goto("/");
    await pending;
    await page.getByRole("button", { name: "New task", exact: true }).click();
    await page
      .getByLabel("What needs to happen?")
      .fill("Private draft before topic-session revocation");
    await expect(
      page.getByRole("dialog", { name: "Assign a task", exact: true }),
    ).toBeVisible();
    release();
    await expect(
      page.getByRole("heading", { name: "Sign in to Engineering Hub." }),
    ).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.locator("textarea")).toHaveCount(0);
    await expect(
      page.getByRole("heading", { name: "Needs Beau", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByText("Verify the isolated Viewer staging candidate"),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "New task", exact: true }),
    ).toBeDisabled();
  } finally {
    release();
  }
});
