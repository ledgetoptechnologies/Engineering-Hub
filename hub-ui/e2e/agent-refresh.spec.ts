import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";

const NOW = Date.parse("2026-10-27T12:00:00Z");
const connection = (now: number, state = "connected", ttl = 30_000) => ({
  state, source: "embedded_bus", scope: "this_server_process", observed_at: new Date(now).toISOString(),
  valid_until: new Date(now + ttl).toISOString(), active_streams: state === "connected" ? 1 : 0,
  stale_streams: state === "stale" ? 1 : 0, inbox_subscriptions: 1, subscribed: true,
  last_stream_write_at: new Date(now).toISOString(), native_push_available: true,
});
async function fixture(page: Page, events = false) {
  await page.clock.install({ time: new Date(NOW) });
  const state = { reads: [] as string[], status: 200, hold: false, releases: [] as (() => void)[],
    csrf: "session-one", signedIn: true, connectionState: "connected", ttl: 30_000, overviewConnected: true };
  if (events) await page.addInitScript(() => {
    class Stream {
      onopen: (() => void) | null = null; onmessage: ((event: { data: string }) => void) | null = null;
      onerror: (() => void) | null = null;
      constructor() { (window as any).__overviewStream = this; queueMicrotask(() => this.onopen?.()); }
      close() {} addEventListener() {}
    }
    (window as any).EventSource = Stream;
  });
  await page.route("**/api/hub/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    state.reads.push(path);
    const respond = (body: unknown, status = 200) => route.fulfill({ status, json: body });
    if (path.endsWith("/auth/me")) return respond({ authenticated: state.signedIn, actor: state.signedIn ? "human:42" : null,
      csrf_token: state.csrf, github_id: 42 });
    if (path.endsWith("/auth/logout")) { state.signedIn = false; return respond({ authenticated: false }); }
    const now = await page.evaluate(() => Date.now());
    if (path.endsWith("/overview")) {
      const overview = createDemoOverview(new Date(now));
      overview.agents = overview.agents.map(agent => ({ ...agent, connection: connection(now, state.overviewConnected ? "connected" : "not_observed") as any }));
      return respond(overview);
    }
    if (path.endsWith("/agents")) {
      expect(route.request().method()).toBe("GET");
      expect(route.request().headers()["authorization"]).toBeUndefined();
      expect(route.request().headers()["x-hub-csrf"]).toBeUndefined();
      const status = state.status;
      const body = { items: ["codex", "claude", "hermes"].map(id => ({ id, connection: connection(now, state.connectionState, state.ttl),
        transport: "online", chat_readiness: "ready", reason: "must not promote" })) };
      if (state.hold) await new Promise<void>(resolve => state.releases.push(resolve));
      await respond(body, status).catch(() => {}); return;
    }
    if (path.endsWith("/events/status")) return respond({ available: events, reason: "fixture", observer_epoch: "fixture", cursor: 0 });
    if (path.endsWith("/peers")) return respond({ ...createDemoTopic("auto-wake-v1-test", new Date(now)), tracking: "not_configured" });
    if (path.endsWith("/topics")) return respond({ items: [] });
    return respond({}, 404);
  });
  return state;
}
const count = (s: { reads: string[] }) => s.reads.filter(p => p.endsWith("/agents")).length;
const rows = (p: Page) => p.locator(".hub-connection-row");
async function visibility(page: Page, value: "visible" | "hidden") {
  await page.evaluate(value => { Object.defineProperty(document, "visibilityState", { configurable: true, value });
    document.dispatchEvent(new Event("visibilitychange")); }, value);
}
test("quiet workspace refreshes only connection evidence and stays current across multiple TTLs", async ({ page }) => {
  const s = await fixture(page); await page.goto("/#/agents");
  await expect(rows(page).getByText("Hub stream observed", { exact: true })).toHaveCount(3);
  const baseline = [...s.reads];
  for (let i = 1; i <= 5; i++) {
    await page.clock.runFor(15_000);
    await expect.poll(() => count(s)).toBe(i);
    await expect(rows(page).getByText("Hub stream observed", { exact: true })).toHaveCount(3);
  }
  expect(s.reads.filter(p => !p.endsWith("/agents"))).toEqual(baseline);
  await expect(page.getByText("Topic readiness: Not tracked yet", { exact: true })).toBeVisible();
  await expect(page.getByText("Online · verified", { exact: true })).toHaveCount(0);
  await page.getByRole("link", { name: "Home", exact: true }).click();
  await expect(page.getByRole("link", { name: /Agent streams observed 3\/3/ })).toBeVisible();
});
test("hidden periods and held reads cannot create fresh evidence or duplicate requests", async ({ page }) => {
  const s = await fixture(page); await page.goto("/#/agents"); await expect(rows(page)).toHaveCount(3);
  await visibility(page, "hidden"); await page.clock.runFor(60_000); expect(count(s)).toBe(0);
  await expect(rows(page).getByText("Hub connection snapshot expired", { exact: true })).toHaveCount(3);
  s.hold = true; await visibility(page, "visible"); await expect.poll(() => count(s)).toBe(1);
  await visibility(page, "visible"); expect(count(s)).toBe(1);
  await visibility(page, "hidden"); s.releases[0](); await page.clock.runFor(1);
  await expect(rows(page).getByText("Hub connection snapshot expired", { exact: true })).toHaveCount(3);
  s.hold = false; await page.clock.runFor(15_000); await visibility(page, "visible");
  await expect.poll(() => count(s)).toBe(2);
  await expect(rows(page).getByText("Hub stream observed", { exact: true })).toHaveCount(3);
});
for (const status of [401, 403]) test(`current agent ${status} clears private workspace and stops reads`, async ({ page }) => {
  const s = await fixture(page); await page.goto("/#/agents"); await expect(rows(page)).toHaveCount(3);
  s.status = status; await page.clock.runFor(15_000);
  await expect(page.getByRole("link", { name: /Sign in securely/ })).toBeVisible();
  expect(s.reads.some(p => p.endsWith("/auth/bootstrap"))).toBe(false);
  const total = s.reads.length; await page.clock.runFor(60_000); expect(s.reads.length).toBe(total);
});
test("old same-actor session auth failure cannot erase a newer overview", async ({ page }) => {
  const s = await fixture(page); await page.goto("/#/agents"); await expect(rows(page)).toHaveCount(3);
  s.hold = true; s.status = 401; await page.clock.runFor(15_000); await expect.poll(() => s.releases.length).toBe(1);
  s.csrf = "session-two"; s.overviewConnected = false;
  await page.getByRole("button", { name: "Refresh workspace", exact: true }).click();
  await expect(rows(page).getByText("No active push stream observed", { exact: true })).toHaveCount(3);
  s.releases[0](); await page.clock.runFor(1);
  await expect(page.getByRole("button", { name: "New task", exact: true })).toBeEnabled();
  await expect(rows(page).getByText("No active push stream observed", { exact: true })).toHaveCount(3);
});
test("ordinary failure clears only observations; disconnected workspace cannot be promoted", async ({ page }) => {
  const s = await fixture(page, true); await page.goto("/#/agents");
  await expect.poll(() => page.evaluate(() => !!(window as any).__overviewStream?.onmessage)).toBe(true);
  await page.evaluate(() => (window as any).__overviewStream.onmessage({ data: JSON.stringify({ kind: "invalidate" }) }));
  await expect(page.getByRole("button", { name: "New task", exact: true })).toBeEnabled();
  await page.evaluate(() => (window as any).__overviewStream.onerror());
  await page.clock.runFor(15_000); await expect.poll(() => count(s)).toBe(1);
  await expect(rows(page).getByText("Hub connection snapshot unverified", { exact: true })).toHaveCount(3);
  await expect(page.getByRole("button", { name: "New task", exact: true })).toBeDisabled();
  await expect(page.getByText("Topic readiness: Snapshot unverified", { exact: true })).toBeVisible();
  s.status = 404; await page.clock.runFor(15_000); await expect.poll(() => count(s)).toBe(2);
  await expect(page.getByRole("link", { name: /^Work / })).toBeVisible();
  await expect(page.getByRole("button", { name: "New task", exact: true })).toBeDisabled();
});
test("a short returned TTL expires locally; ordinary failure cannot retain a positive observation", async ({ page }) => {
  const s = await fixture(page); await page.goto("/#/agents"); await expect(rows(page)).toHaveCount(3);
  s.ttl = 1000; await page.clock.runFor(15_000); await expect.poll(() => count(s)).toBe(1);
  await expect(rows(page).getByText("Hub stream observed", { exact: true })).toHaveCount(3);
  await page.clock.runFor(1100);
  await expect(rows(page).getByText("Hub connection snapshot expired", { exact: true })).toHaveCount(3);
  expect(count(s)).toBe(1);
  s.status = 404; await page.clock.runFor(13_900); await expect.poll(() => count(s)).toBe(2);
  await expect(rows(page).getByText("Hub connection unknown", { exact: true })).toHaveCount(3);
  await expect(page.getByRole("button", { name: "New task", exact: true })).toBeEnabled();
});
test("late success cannot overwrite a newer overview and sign-out clears a held read", async ({ page }) => {
  const s = await fixture(page); await page.goto("/#/agents"); await expect(rows(page)).toHaveCount(3);
  s.hold = true; await page.clock.runFor(15_000); await expect.poll(() => s.releases.length).toBe(1);
  s.csrf = "session-two"; s.overviewConnected = false;
  await page.getByRole("button", { name: "Refresh workspace", exact: true }).click();
  await expect(rows(page).getByText("No active push stream observed", { exact: true })).toHaveCount(3);
  s.releases[0](); await page.clock.runFor(1);
  await expect(rows(page).getByText("No active push stream observed", { exact: true })).toHaveCount(3);
  await page.clock.runFor(15_000); await expect.poll(() => s.releases.length).toBe(2);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByRole("link", { name: /Sign in securely/ })).toBeVisible();
  s.releases[1](); await page.clock.runFor(30_000); expect(count(s)).toBe(2);
  await expect(rows(page)).toHaveCount(0);
});
test("agent refresh on staging does not refetch queue, resource or runtime observations", async ({ page }) => {
  const s = await fixture(page); await page.goto("/#/staging");
  await expect(page.getByRole("region", { name: "Staging resources", exact: true }).getByRole("button", { name: "Check", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Check queue", exact: true })).toBeEnabled();
  const baseline = [...s.reads];
  await page.clock.runFor(15_000); await expect.poll(() => count(s)).toBe(1);
  expect(s.reads.filter(p => !p.endsWith("/agents"))).toEqual(baseline);
});
