import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";

const jobId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
function snapshot() {
  return { paused: false, queue_paused: false, master_paused: false,
    claim_admission_state: "unconfigured", counts: { held: 1 }, jobs: [{
      id: jobId, machine_id: "staging-pc", action_key: "engineering-hub.validate",
      environment: "staging", state: "held", blocked_reason: "requires_reconciliation",
      requires_reconciliation: true, eligible: false, eligible_requires_worker_admission: false,
      target: { task_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", source_commit: "a".repeat(40),
        artifact_digest: "sha256:" + "b".repeat(64) },
    }] };
}

async function fixture(page: Page) {
  const state = { reads: 0, writes: 0, queueStatus: 200, body: snapshot() };
  await page.route("**/api/hub/v1/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status,
      contentType: "application/json", body: JSON.stringify(body) });
    if (request.method() !== "GET") { state.writes++; return respond({}, 405); }
    if (path.endsWith("/auth/me")) return respond({ authenticated: true,
      actor: "human:42", github_id: 42, csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(createDemoOverview());
    if (path.endsWith("/events/status")) return respond({ available: false,
      reason: "native_source_unsupported", observer_epoch: "fixture", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) {
      state.reads++;
      expect(request.headers()["authorization"]).toBeUndefined();
      expect(new URL(request.url()).searchParams.get("limit")).toBe("50");
      return respond(state.queueStatus === 200 ? state.body : { detail: { code: "fixture-denial" } }, state.queueStatus);
    }
    return respond({}, 404);
  });
  return state;
}

test("held queue is visible without implying executor readiness; explicit refresh only reads", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/staging");
  const panel = page.getByRole("region", { name: "Software validation queue" });
  await expect(panel.getByText("Not configured — claims are closed", { exact: false })).toBeVisible();
  await expect(panel.getByText("Reconciliation required — machine reservation remains held.", { exact: true })).toBeVisible();
  await panel.getByText("Exact task and artifact", { exact: true }).click();
  await expect(panel.getByText("Artifact: sha256:" + "b".repeat(64), { exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("software-queue.png"), fullPage: true });
  const before = state.reads;
  await panel.getByRole("button", { name: "Check queue" }).click();
  await expect.poll(() => state.reads).toBe(before + 1);
  await expect(panel.getByText("Held: 1", { exact: false })).toBeVisible();
  expect(state.writes).toBe(0);
  await expect(panel.getByRole("button")).toHaveCount(1);
});

test("queue becoming unavailable clears old rows instead of showing an empty healthy queue", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/staging");
  const panel = page.getByRole("region", { name: "Software validation queue" });
  await expect(panel.getByText("Job " + jobId, { exact: false })).toBeVisible();
  state.queueStatus = 423;
  await panel.getByRole("button", { name: "Check queue" }).click();
  await expect(panel.getByText("Software queue is not configured.", { exact: false })).toBeVisible();
  await expect(panel.getByText("Job " + jobId, { exact: false })).toHaveCount(0);
  await expect(panel.getByText("No recorded queue jobs.", { exact: true })).toHaveCount(0);
  expect(state.writes).toBe(0);
});

test("queue authentication loss removes the entire private workspace", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/staging");
  const panel = page.getByRole("region", { name: "Software validation queue" });
  await expect(panel.getByText("Job " + jobId, { exact: false })).toBeVisible();
  state.queueStatus = 401;
  await panel.getByRole("button", { name: "Check queue" }).click();
  await expect(panel).toHaveCount(0);
  await expect(page.getByText("Job " + jobId, { exact: false })).toHaveCount(0);
  expect(state.writes).toBe(0);
});
