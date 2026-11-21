import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import { audit, blocking } from "./audit";
import wire from "../src/fixtures/staging-runtime-wire-v1.json" with { type: "json" };

const human = "human:cf:" + "e".repeat(64);
type Reply = { status: number; body: unknown } | "abort";

/** Plays the Hub behind the real gateway paths; `runtime` answers the runtime route, in order, repeating the last. */
async function fixture(page: Page, runtime: Reply[]) {
  const state = { reads: 0, release: null as null | (() => void) };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(createDemoOverview(new Date()));
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    if (path === "/api/hub/v1/staging/resources") return respond({ items: [], next_after: null });
    if (path === "/api/hub/v1/staging/runtime") {
      if (request.method() !== "GET") return respond({ detail: { code: "method_not_allowed" } }, 405);
      const reply = runtime[Math.min(state.reads++, runtime.length - 1)];
      if (reply === "abort") return route.abort();
      return respond(reply.body, reply.status);
    }
    return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
  });
  return state;
}
const panel = (page: Page) => page.getByRole("region", { name: "Running on the staging PC" });
const fresh = (base = Date.now()) => {
  const body = structuredClone(wire);
  const at = new Date(base - 20_000).toISOString();
  body.snapshot.observed_at = at;
  body.snapshot.sources[0].observed_at = at;
  body.snapshot.sources[1].observed_at = at;
  return body;
};

test("splits the staging engine from other programs, and offers no controls", async ({ page }) => {
  await fixture(page, [{ status: 200, body: fresh() }]);
  await page.goto("/#/staging");
  const engine = panel(page).getByRole("region", { name: "Staging engine" });
  const host = panel(page).getByRole("region", { name: "Other programs on the PC" });
  await expect(engine).toContainText("Seen just now.");
  await expect(engine.getByRole("listitem")).toHaveText([/eh-build.*Container · build.*Running/]);
  await expect(host).toContainText("Seen just now.");
  await expect(host).toContainText("not run or controlled by EH");
  await expect(host.getByRole("listitem")).toHaveText([/viewer.*Stopped/, /asset-service\.service.*Service.*Running/]);
  // The registry address and digest are never shown, and there is nothing to press but Refresh.
  await expect(panel(page)).not.toContainText("registry.example.test");
  await expect(panel(page)).not.toContainText("sha256");
  await expect(panel(page).getByRole("button")).toHaveText(["Refresh"]);
  // The only mention of readiness is the disclaimer that this isn't one.
  await expect(panel(page).getByText(/\bready\b/i)).toHaveText([/^What the staging PC reported .*doesn't mean the PC is ready for work/]);
  expect(blocking(await audit(page, "page", 1440))).toEqual([]);
});

test("an unreadable source says unknown, not empty", async ({ page }) => {
  const body = fresh();
  body.snapshot.sources = [{ id: "worker", state: "unavailable", observed_at: null }] as never;
  body.snapshot.containers = []; body.snapshot.services = [];
  await fixture(page, [{ status: 200, body }]);
  await page.goto("/#/staging");
  await expect(panel(page).getByRole("region", { name: "Staging engine" })).toContainText("Couldn't be read, so what's running here is unknown.");
  await expect(panel(page).getByRole("region", { name: "Other programs on the PC" })).toContainText("Not reported, so what's running here is unknown.");
  await expect(panel(page)).not.toContainText("Nothing was reported here.");
  await expect(panel(page).getByRole("listitem")).toHaveCount(0);
});

test("a stale source the Hub sent without rows says unknown, not empty", async ({ page }) => {
  const body = fresh();
  body.snapshot.sources[1] = { id: "host", state: "stale", observed_at: new Date(Date.now() - 40 * 60_000).toISOString() };
  body.snapshot.containers = body.snapshot.containers.filter(item => item.scope === "worker"); body.snapshot.services = [];
  await fixture(page, [{ status: 200, body }]);
  await page.goto("/#/staging");
  const host = panel(page).getByRole("region", { name: "Other programs on the PC" });
  await expect(host).toContainText("Out of date (last seen 40 min ago), so what's running here is unknown.");
  await expect(host).not.toContainText("Nothing was reported here.");
  await expect(host.getByRole("listitem")).toHaveCount(0);
  await expect(host.getByText(/running · /)).toHaveCount(0);
});

test("not set up and not answering are said plainly", async ({ page }) => {
  await fixture(page, [{ status: 200, body: { configured: false, state: "not_configured", snapshot: null } },
    { status: 200, body: { configured: true, state: "unavailable", snapshot: null } }]);
  await page.goto("/#/staging");
  await expect(panel(page)).toContainText("Watching the staging PC isn't set up yet, so what runs on it isn't shown.");
  await panel(page).getByRole("button", { name: "Refresh" }).click();
  await expect(panel(page)).toContainText("The staging PC isn't answering, so what's running on it is unknown.");
});

test("a Hub without staging set up says so, not that the PC is down", async ({ page }) => {
  await fixture(page, [{ status: 503, body: { detail: { code: "staging_not_configured" } } }]);
  await page.goto("/#/staging");
  await expect(panel(page)).toContainText("Watching the staging PC isn't set up yet, so what runs on it isn't shown.");
  await expect(panel(page)).not.toContainText("isn't answering");
  await expect(panel(page).getByRole("alert")).toHaveCount(0);
});

test("reads once, refreshes only when asked, and keeps the earlier observation when a refresh fails", async ({ page }) => {
  const state = await fixture(page, [{ status: 200, body: fresh() }, { status: 503, body: { detail: { code: "staging_unavailable" } } }]);
  await page.goto("/#/staging");
  await expect(panel(page).getByRole("region", { name: "Staging engine" })).toContainText("eh-build");
  await page.waitForTimeout(500);
  expect(state.reads).toBe(1);
  await panel(page).getByRole("button", { name: "Refresh" }).click();
  await expect(panel(page).getByRole("alert")).toHaveText(
    "The staging PC isn't answering, so what's running on it is unknown right now. Showing what was seen just now.");
  // The kept observation is marked out of date, row by row.
  const engine = panel(page).getByRole("region", { name: "Staging engine" });
  await expect(engine).toContainText("Out of date: last seen just now.");
  await expect(engine.getByRole("listitem")).toHaveText([/eh-build.*Last seen: running/]);
  await expect(panel(page).getByText("Running", { exact: true })).toHaveCount(0);
  expect(state.reads).toBe(2);
});

test("a page left open goes out of date by its own clock, without reading again", async ({ page }) => {
  const start = Date.now();
  await page.clock.install({ time: start });
  const state = await fixture(page, [{ status: 200, body: fresh(start) }]);
  await page.goto("/#/staging");
  const engine = panel(page).getByRole("region", { name: "Staging engine" });
  await expect(engine).toContainText("Seen just now.");
  await expect(engine.getByRole("listitem")).toHaveText([/eh-build.*Running/]);
  await page.clock.runFor(60_000);
  await expect(engine).toContainText("Seen 1 min ago.");
  await page.clock.runFor(30_000);
  await expect(engine).toContainText("Out of date: last seen 1 min ago. It may have changed since.");
  await expect(engine.getByRole("listitem")).toHaveText([/eh-build.*Last seen: running/]);
  await page.clock.runFor(3 * 60_000);
  await expect(engine).toContainText("Out of date: last seen 4 min ago.");
  expect(state.reads).toBe(1);
});

test("steps aside for people who aren't the staging admin, and on Hubs without it", async ({ page }) => {
  await fixture(page, [{ status: 403, body: { detail: { code: "staging_admin_required" } } }]);
  await page.goto("/#/staging");
  await expect(page.locator("#staging")).toBeVisible();
  await expect(panel(page)).toHaveCount(0);
  await page.unrouteAll();
  await fixture(page, [{ status: 403, body: { detail: { code: "gateway_route_forbidden" } } }]);
  await page.reload();
  await expect(page.locator("#staging")).toBeVisible();
  await expect(panel(page)).toHaveCount(0);
});

test("a lost sign-in clears the panel along with the workspace", async ({ page }) => {
  await fixture(page, [{ status: 401, body: { detail: { code: "human_session_required" } } }]);
  await page.goto("/#/staging");
  await expect(page.getByText("Sign in to Engineering Hub.")).toBeVisible();
  await expect(panel(page)).toHaveCount(0);
});

test("refuses a response that claims EH controls a program, rather than showing it", async ({ page }) => {
  const body = fresh();
  (body.snapshot.containers[1] as { managed: boolean }).managed = true;
  await fixture(page, [{ status: 200, body }]);
  await page.goto("/#/staging");
  await expect(panel(page).getByRole("alert")).toHaveText("What's running on the staging PC couldn't be read. Use Refresh to try again.");
  await expect(panel(page).getByRole("listitem")).toHaveCount(0);
});

test("demo mode reads nothing and says it's illustrative", async ({ page }) => {
  let reads = 0;
  await page.route("**/api/hub/v1/staging/runtime", (route) => { reads++; return route.abort(); });
  await page.goto("/?demo=1#/staging");
  await expect(panel(page)).toContainText("Demo: illustrative programs only.");
  await expect(panel(page).getByRole("region", { name: "Other programs on the PC" }).getByRole("listitem")).toHaveCount(4);
  expect(reads).toBe(0);
});

test("fits a phone screen", async ({ page }) => {
  await fixture(page, [{ status: 200, body: fresh() }]);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/#/staging");
  await expect(panel(page).getByRole("listitem")).toHaveCount(3);
  const box = (await panel(page).boundingBox())!;
  expect(box.x + box.width).toBeLessThanOrEqual(375);
  expect(blocking(await audit(page, "page", 375))).toEqual([]);
});
