import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import { audit, blocking } from "./audit";

const human = "human:cf:" + "e".repeat(64);
type Reply = { status: number; body: unknown } | "abort";

/** Plays the Hub; `status` and `recover` answer their routes in order, repeating the last. */
async function fixture(page: Page, status: Reply[], recover: Reply[] = []) {
  const state = { reads: 0, posts: 0, csrf: [] as (string | null)[], bodies: [] as (string | null)[] };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const respond = (body: unknown, code = 200) => route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify(body) });
    const play = (reply: Reply) => reply === "abort" ? route.abort() : respond(reply.body, reply.status);
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(createDemoOverview(new Date()));
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    if (path === "/api/hub/v1/staging/resources") return respond({ items: [], next_after: null });
    if (path === "/api/hub/v1/staging/shared/status") {
      if (request.method() !== "GET") return respond({ detail: { code: "method_not_allowed" } }, 405);
      return play(status[Math.min(state.reads++, status.length - 1)]);
    }
    if (path === "/api/hub/v1/staging/shared/recover") {
      if (request.method() !== "POST") return respond({ detail: { code: "method_not_allowed" } }, 405);
      state.csrf.push(request.headers()["x-hub-csrf"] ?? null); state.bodies.push(request.postData());
      return play(recover[Math.min(state.posts++, recover.length - 1)]);
    }
    return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
  });
  return state;
}
const panel = (page: Page) => page.getByRole("region", { name: "Shared staging queue" });
const ok = (state: string, reason: string | null = null) => ({ status: 200, body: { state, reason } });

test("before the session check, it says what's needed and offers nothing to press but Refresh", async ({ page }) => {
  const state = await fixture(page, [ok("pending", "operator_acceptance_required")]);
  await page.goto("/#/staging");
  await expect(panel(page)).toContainText("Waiting for the session check");
  await expect(panel(page)).toContainText("Nothing is taken until you accept the staging session check.");
  await expect(panel(page)).toContainText("“Review staging session” on this page");
  await expect(panel(page).getByRole("button")).toHaveText(["Refresh"]);
  await expect(panel(page).getByText(/\b(ready|approved)\b/i)).toHaveCount(0);
  await page.waitForTimeout(500);
  expect(state.reads).toBe(1);
  expect(blocking(await audit(page, "page", 1440))).toEqual([]);
});

test("an open queue is never worded as deployed or approved", async ({ page }) => {
  await fixture(page, [ok("active")]);
  await page.goto("/#/staging");
  await expect(panel(page)).toContainText("Open");
  await expect(panel(page)).toContainText("doesn't mean any app is deployed or healthy");
  await expect(panel(page)).toContainText("It doesn't approve any job or deployment.");
});

test("retries the check of earlier work only when asked, with CSRF, and shows the result", async ({ page }) => {
  const state = await fixture(page, [ok("recovery_required", "original_operations_unverified")], [ok("active")]);
  await page.goto("/#/staging");
  await expect(panel(page)).toContainText("Earlier work not confirmed");
  await expect(panel(page)).toContainText("it never starts it again");
  expect(state.posts).toBe(0);
  await panel(page).getByRole("button", { name: "Retry checking earlier work" }).click();
  await expect(panel(page)).toContainText("Open");
  expect(state.posts).toBe(1);
  expect(state.csrf).toEqual(["fixture-csrf"]);
  expect(state.bodies).toEqual(["{}"]);
  await expect(panel(page).getByRole("button", { name: "Retry checking earlier work" })).toHaveCount(0);
});

test("a retry with no answer says it may still be running, never that it failed", async ({ page }) => {
  const state = await fixture(page, [ok("recovery_required", "original_operations_unverified"), ok("recovering", "original_operations_pending")], ["abort"]);
  await page.goto("/#/staging");
  await panel(page).getByRole("button", { name: "Retry checking earlier work" }).click();
  await expect(panel(page).getByRole("alert")).toContainText("No answer yet. Recovery may still be running on the Hub.");
  await expect(panel(page).getByRole("alert")).not.toContainText("failed");
  await page.waitForTimeout(500);
  expect(state.posts).toBe(1);  // Never retried on its own.
  await panel(page).getByRole("button", { name: "Refresh" }).click();
  await expect(panel(page)).toContainText("Checking earlier work");
  await expect(panel(page).getByRole("alert")).toHaveCount(0);
  expect(state.posts).toBe(1);
});

test("a retry the Hub refuses says to refresh and keeps the earlier state marked", async ({ page }) => {
  await fixture(page, [ok("recovery_required", "original_operations_unverified")],
    [{ status: 409, body: { detail: { code: "shared_staging_lifecycle_changed" } } }]);
  await page.goto("/#/staging");
  await panel(page).getByRole("button", { name: "Retry checking earlier work" }).click();
  await expect(panel(page).getByRole("alert")).toContainText("setup changed or is stopping, so nothing was retried");
  await expect(panel(page).getByRole("alert")).toContainText("may be out of date");
});

test("steps aside where shared staging isn't set up, for non-owners and on older Hubs", async ({ page }) => {
  for (const reply of [{ status: 503, body: { detail: { code: "shared_staging_not_configured" } } },
    { status: 403, body: { detail: { code: "staging_admin_required" } } },
    { status: 403, body: { detail: { code: "gateway_route_forbidden" } } }]) {
    await page.unrouteAll();
    await fixture(page, [reply]);
    await page.goto("/#/staging");
    await expect(page.getByRole("region", { name: "Staging resources" })).toBeVisible();
    await expect(panel(page)).toHaveCount(0);
  }
});

test("a lost sign-in clears the panel along with the workspace", async ({ page }) => {
  await fixture(page, [{ status: 401, body: { detail: { code: "human_session_required" } } }]);
  await page.goto("/#/staging");
  await expect(page.getByText("Sign in to Engineering Hub.")).toBeVisible();
  await expect(panel(page)).toHaveCount(0);
});

test("an unexpected answer is refused rather than shown", async ({ page }) => {
  await fixture(page, [{ status: 200, body: { state: "active", reason: null, execution_authorized: true } }]);
  await page.goto("/#/staging");
  await expect(panel(page).getByRole("alert")).toHaveText("The shared staging queue's state couldn't be read. Use Refresh to try again.");
  await expect(panel(page)).not.toContainText("Open");
});

test("demo mode reads nothing and offers no retry", async ({ page }) => {
  let reads = 0;
  await page.route("**/api/hub/v1/staging/shared/**", (route) => { reads++; return route.abort(); });
  await page.goto("/?demo=1#/staging");
  await expect(panel(page)).toContainText("Demo: an illustrative state only.");
  await expect(panel(page).getByRole("button", { name: "Retry checking earlier work" })).toHaveCount(0);
  expect(reads).toBe(0);
});

test("fits a phone screen", async ({ page }) => {
  await fixture(page, [ok("recovery_required", "original_operations_unverified")]);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/#/staging");
  await expect(panel(page).getByRole("button", { name: "Retry checking earlier work" })).toBeVisible();
  const box = (await panel(page).boundingBox())!;
  expect(box.x + box.width).toBeLessThanOrEqual(375);
  expect(blocking(await audit(page, "page", 375))).toEqual([]);
});
