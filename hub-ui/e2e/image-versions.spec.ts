import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";

function report(now: number, ttl = 600_000) {
  const stamp = (offset = 0) => new Date(now + offset).toISOString();
  return { configured: true, source: "agent_observation_report", reason: "available",
    reported_by: "hermes", report_updated_at: stamp(), advisory_only: true,
    items: [{ project: "engineering-hub", host: "nas", service: "hub", environment: "production",
      repository: "ghcr.io/ledgetoptechnologies/engineering-hub-control", reference: "latest",
      state: "up_to_date", reason: "same_image", checked_at: stamp(), running_observed_at: stamp(),
      published_observed_at: stamp(), valid_until: stamp(ttl), digest_kind: "config",
      current_digest: `sha256:${"a".repeat(64)}`, available_digest: `sha256:${"a".repeat(64)}` }] };
}
async function fixture(page: Page, ttl = 600_000) {
  const now = Date.now();
  await page.clock.install({ time: new Date(now) });
  const state = { image: report(now, ttl) as unknown, imageStatus: 200, reads: 0, hold: false,
    release: null as (() => void) | null };
  await page.route("**/api/hub/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: "human:cf:" + "e".repeat(64),
      auth_provider: "cloudflare_access", csrf_token: "fixture-csrf", email: "operator@example.test", github_id: null });
    if (path.endsWith("/overview")) return respond({ ...createDemoOverview(), image_versions: state.image });
    if (path.endsWith("/image-versions")) {
      state.reads++;
      expect(route.request().method()).toBe("GET");
      expect(route.request().headers()["authorization"]).toBeUndefined();
      const response = state.image, status = state.imageStatus;
      if (state.hold) await new Promise<void>(resolve => { state.release = resolve; });
      return respond(response, status);
    }
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "test", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    return respond({}, 404);
  });
  return state;
}
const panel = (page: Page) => page.getByRole("region", { name: "Production image versions" });

test("production image observations expire locally without a metadata request", async ({ page }) => {
  const state = await fixture(page, 3000);
  await page.goto("/#/");
  await expect(panel(page).getByText("Up to date", { exact: true }).first()).toBeVisible();
  await page.clock.fastForward(3500);
  await expect(panel(page).getByText("Up to date", { exact: true })).toHaveCount(0);
  await expect(panel(page).getByText("Not verified", { exact: true }).first()).toBeVisible();
  expect(state.reads).toBe(0);
});
test("visible metadata refresh is read-only and an older-server response clears only image evidence", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/");
  await expect(panel(page).getByText("Up to date", { exact: true }).first()).toBeVisible();
  state.imageStatus = 404;
  await page.clock.fastForward(300_001);
  await expect.poll(() => state.reads).toBe(1);
  await expect(panel(page).getByText("Up to date", { exact: true })).toHaveCount(0);
  await expect(panel(page).getByText("Not verified", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "New task", exact: true })).toBeEnabled();
});
test("image metadata authorization loss removes the private workspace", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/");
  await expect(panel(page)).toBeVisible();
  state.imageStatus = 401;
  await page.clock.fastForward(300_001);
  await expect(page.getByRole("link", { name: /Sign in securely/ })).toBeVisible();
  await expect(panel(page)).toHaveCount(0);
});
test("late image metadata cannot overwrite a newer workspace refresh", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/");
  await expect(panel(page).getByText("Up to date", { exact: true }).first()).toBeVisible();
  state.hold = true;
  await page.clock.fastForward(300_001);
  await expect.poll(() => state.release !== null).toBe(true);
  state.image = null;
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect(panel(page).getByText("Not verified", { exact: true }).first()).toBeVisible();
  state.release?.();
  await page.waitForTimeout(100);
  await expect(panel(page).getByText("Up to date", { exact: true })).toHaveCount(0);
});

test("a different published image is neutral information, not a health alert or update action", async ({ page }) => {
  const state = await fixture(page);
  const images = state.image as ReturnType<typeof report>;
  Object.assign(images.items[0], { state: "update_available", reason: "different_image",
    available_digest: `sha256:${"b".repeat(64)}` });
  await page.goto("/#/");
  await expect(panel(page).getByText("Update available", { exact: true }).first()).toBeVisible();
  await expect(panel(page).getByRole("alert")).toHaveCount(0);
  await expect(panel(page).getByRole("button")).toHaveCount(0);
  await expect(panel(page).getByText(/Agent report by hermes/)).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("neutral-image-update.png"), fullPage: true, animations: "disabled" });
  expect(state.reads).toBe(0);
});

test("hidden tabs do not refresh image metadata, and visibility resumes one bounded read", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/");
  await expect(panel(page)).toBeVisible();
  await page.evaluate(() => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" }); });
  await page.clock.fastForward(600_001);
  expect(state.reads).toBe(0);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => state.reads).toBe(1);
});
