import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";

async function setup(page: Page, operator = true) {
  const writes: unknown[] = [];
  let accepted = false;
  await page.route("**/api/hub/v1/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const reply = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return reply({ authenticated: true, actor: "human:cf:" + "e".repeat(64),
      github_id: null, csrf_token: "synthetic-csrf", auth_provider: "cloudflare_access" });
    if (path.endsWith("/overview")) {
      const value = createDemoOverview();
      value.autonomy = { ...value.autonomy, configured: true, state: "startup_reconciliation_required", running: true };
      return reply(value);
    }
    if (path.endsWith("/session-reconciliation")) {
      if (!operator) return reply({ detail: { code: "operator_required" } }, 403);
      if (request.method() === "POST") {
        expect(request.headers()["x-hub-csrf"]).toBe("synthetic-csrf");
        const intent = request.postDataJSON();
        writes.push(intent); accepted = intent.action === "accept";
        return reply({ request_id: "a".repeat(64), revision: 2,
          state: accepted ? "accepted" : "declined", execution_authorization: false });
      }
      return reply({ request_id: "a".repeat(64), revision: accepted ? 2 : 1, state: accepted ? "accepted" : "pending",
        evidence: "Synthetic browser test: operator must check actual host inventory.",
        report_digest: "sha256:" + "b".repeat(64), expires_in_seconds: 120, execution_authorization: false });
    }
    if (path.endsWith("/peers")) return reply(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/events/status")) return reply({ available: false, reason: "native_source_unsupported", observer_epoch: "fixture", cursor: 0 });
    if (path.endsWith("/work/status")) return reply({}, 423);
    return reply({}, 404);
  });
  return writes;
}

test("operator opens only the retained staging session without submitting a job", async ({ page }) => {
  const writes = await setup(page);
  await page.goto("/#/staging");
  const panel = page.getByRole("region", { name: "Staging session reconciliation" });
  await expect(panel.getByText("Synthetic browser test: operator must check actual host inventory.")).toBeVisible();
  await expect(panel.getByRole("button", { name: "Accept staging session only" })).toBeEnabled();
  expect(writes).toEqual([]);
  await panel.getByRole("button", { name: "Accept staging session only" }).click();
  await expect(panel.getByText("The operator accepted this session.", { exact: false })).toBeVisible();
  expect(writes).toEqual([{ request_id: "a".repeat(64), expected_revision: 1, action: "accept" }]);
  await expect(panel.getByRole("button", { name: "Accept staging session only" })).toBeDisabled();
});

test("nonoperator denial keeps the human signed in and offers no acceptance", async ({ page }) => {
  const writes = await setup(page, false);
  await page.goto("/#/staging");
  const panel = page.getByRole("region", { name: "Staging session reconciliation" });
  await expect(panel.getByRole("alert")).toContainText("designated staging operator");
  await expect(panel.getByRole("button", { name: "Accept staging session only" })).toBeDisabled();
  await expect(page.getByRole("heading", { name: "Staging.", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out", exact: true })).toBeVisible();
  await expect(page.getByText("Sign in to Engineering Hub.", { exact: true })).toHaveCount(0);
  expect(writes).toEqual([]);
});

test("phone review can decline without overflow or another acceptance", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const writes = await setup(page);
  await page.goto("/#/staging");
  const panel = page.getByRole("region", { name: "Staging session reconciliation" });
  await expect(panel.getByRole("button", { name: "Decline session" })).toBeEnabled();
  await panel.getByRole("button", { name: "Decline session" }).click();
  await expect(panel.getByRole("alert")).toContainText("Session declined and closed");
  await expect(panel.getByRole("button", { name: "Accept staging session only" })).toBeDisabled();
  expect(writes).toEqual([{ request_id: "a".repeat(64), expected_revision: 1, action: "decline" }]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
