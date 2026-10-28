import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";

const A = "human:cf:" + "a".repeat(64), B = "human:cf:" + "b".repeat(64);
const PRIVATE_A = "Private task of person A", PRIVATE_B = "Private task of person B";

async function fixture(page: Page) {
  const state = {
    actor: A, holdNext: false, held: [] as (() => void)[], overviewStatus: 200, holdPeers: false, peerReleases: [] as (() => void)[],
    overviews: [] as string[],
  };
  await page.route("**/api/hub/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: state.actor, github_id: null, auth_provider: "cloudflare_access",
      email: "person@example.test", csrf_token: state.actor === A ? "csrf-a" : "csrf-b" });
    if (path.endsWith("/overview")) {
      // Built for whoever is signed in when the request arrives, then possibly held: a held answer is that person's view.
      const who = state.actor;
      state.overviews.push(who);
      const overview = createDemoOverview();
      overview.tasks[0] = { ...overview.tasks[0], title: who === A ? PRIVATE_A : PRIVATE_B };
      const status = state.overviewStatus;
      if (state.holdNext) { state.holdNext = false; await new Promise<void>(resolve => state.held.push(resolve)); }
      if (status !== 200) return respond({ detail: { code: status === 401 ? "human_session_required" : "service_unavailable" } }, status);
      return respond(overview);
    }
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) {
      // Topic readiness is read alongside the overview; A sees reachable agents, B sees none.
      const who = state.actor, snapshot = createDemoTopic("auto-wake-v1-test");
      if (state.holdPeers) { state.holdPeers = false; await new Promise<void>(resolve => state.peerReleases.push(resolve)); }
      return respond(who === A ? snapshot : { ...snapshot, items: [] });
    }
    if (path.endsWith("/work/status")) return respond({}, 423);
    return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
  });
  return state;
}
const refresh = (page: Page) => page.getByRole("button", { name: "Refresh workspace" }).click();

test("a different person's session clears the previous workspace before their own overview arrives", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/work");
  await expect(page.getByText(PRIVATE_A)).toBeVisible();
  state.actor = B; state.holdNext = true;
  await refresh(page);
  await expect.poll(() => state.held.length).toBe(1);
  // B's overview is still on its way: nothing of A's remains.
  await expect(page.getByText(PRIVATE_A)).toHaveCount(0);
  await expect(page.getByText("Opening your workspace…")).toBeVisible();
  state.held[0]();
  await expect(page.getByText(PRIVATE_B)).toBeVisible();
  await expect(page.getByText(PRIVATE_A)).toHaveCount(0);
});

test("if the new person's overview fails, the previous person's records stay gone", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/work");
  await expect(page.getByText(PRIVATE_A)).toBeVisible();
  state.actor = B; state.overviewStatus = 503;
  await refresh(page);
  await expect(page.getByRole("alert").first()).toBeVisible();
  await expect(page.getByText(PRIVATE_A)).toHaveCount(0);
});

test("if the new person's overview says the sign-in is gone, the workspace closes", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/work");
  await expect(page.getByText(PRIVATE_A)).toBeVisible();
  state.actor = B; state.overviewStatus = 401;
  await refresh(page);
  await expect(page.getByRole("link", { name: /Sign in securely/ })).toBeVisible();
  await expect(page.getByText(PRIVATE_A)).toHaveCount(0);
});

test("a late topic answer for the previous person never reaches the new person's workspace", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents");
  await expect(page.locator(".peer-row").first()).toBeVisible();
  // A refresh for A finishes its overview while A's topic answer is still on its way.
  state.holdPeers = true;
  await refresh(page);
  await expect.poll(() => state.peerReleases.length).toBe(1);
  await expect(page.getByRole("button", { name: "Refresh workspace" })).toBeEnabled();
  state.actor = B;
  await refresh(page);
  // Move inside the app (no reload), so the held answer still belongs to this page.
  const nav = page.getByRole("navigation", { name: "Workspace" });
  await nav.getByRole("link", { name: /^Work/ }).click();
  await expect(page.getByText(PRIVATE_B)).toBeVisible();
  await nav.getByRole("link", { name: /^Agents/ }).click();
  await expect(page.locator(".peer-row")).toHaveCount(0);
  state.peerReleases[0]();
  await page.waitForTimeout(300);
  await expect(page.locator(".peer-row")).toHaveCount(0);
  await expect(page.getByText(PRIVATE_A)).toHaveCount(0);
});

test("a refresh by the same person keeps their workspace on screen while it reloads", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/work");
  await expect(page.getByText(PRIVATE_A)).toBeVisible();
  state.holdNext = true;
  await refresh(page);
  await expect.poll(() => state.held.length).toBe(1);
  await expect(page.getByText(PRIVATE_A)).toBeVisible();
  await expect(page.getByText("Opening your workspace…")).toHaveCount(0);
  state.held[0]();
  await expect(page.getByText(PRIVATE_A)).toBeVisible();
});
