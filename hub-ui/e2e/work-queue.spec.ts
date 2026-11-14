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
  const state = { reads: 0, writes: 0, actor: "human:42", queueStatus: 200, overviewStatus: 200, queueDelay: 0, body: snapshot() };
  await page.route("**/api/hub/v1/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status,
      contentType: "application/json", body: JSON.stringify(body) });
    if (request.method() !== "GET") { state.writes++; return respond({}, 405); }
    if (path.endsWith("/auth/me")) return respond({ authenticated: true,
      actor: state.actor, github_id: 42, csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(createDemoOverview(), state.overviewStatus);
    if (path.endsWith("/events/status")) return respond({ available: false,
      reason: "native_source_unsupported", observer_epoch: "fixture", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) {
      state.reads++;
      const status = state.queueStatus;
      if (state.queueDelay) await new Promise(resolve => setTimeout(resolve, state.queueDelay));
      expect(request.headers()["authorization"]).toBeUndefined();
      expect(new URL(request.url()).searchParams.get("limit")).toBe("50");
      return respond(status === 200 ? state.body : { detail: { code: "fixture-denial" } }, status).catch(() => {});
    }
    return respond({}, 404);
  });
  return state;
}

test("inbox shows held queue status separately from decisions and only reads", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/inbox");
  const panel = page.getByRole("region", { name: "Held software queue jobs" });
  await expect(panel.getByText("Held at this check: 1.")).toBeVisible();
  await expect(panel.getByText(`Job ${jobId} · staging-pc · held`)).toBeVisible();
  await expect(panel.getByRole("link", { name: "Open software queue" })).toHaveAttribute("href", /staging/);
  expect(state.writes).toBe(0);
});

test("inbox reports a partial held list without guessing the missing job", async ({ page }) => {
  const state = await fixture(page);
  state.body.jobs[0].state = "queued";
  state.body.jobs[0].requires_reconciliation = false;
  state.body.jobs[0].blocked_reason = null;
  await page.goto("/#/inbox");
  const panel = page.getByRole("region", { name: "Held software queue jobs" });
  await expect(panel.getByText("Held at this check: 1.")).toBeVisible();
  await expect(panel.getByText("Some held jobs are outside the displayed recent jobs.")).toBeVisible();
  await expect(panel.getByText(`Job ${jobId}`, { exact: false })).toHaveCount(0);
  expect(state.writes).toBe(0);
});

test("inbox does not treat unavailable queue status as empty", async ({ page }) => {
  const state = await fixture(page);
  state.queueStatus = 503;
  await page.goto("/#/inbox");
  const panel = page.getByRole("region", { name: "Held software queue jobs" });
  await expect(panel.getByText("Queue status is unavailable.")).toBeVisible();
  await expect(panel.getByText("Held at this check:", { exact: false })).toHaveCount(0);
  expect(state.writes).toBe(0);
});

test("inbox clears the private workspace on queue authentication loss", async ({ page }) => {
  const state = await fixture(page);
  state.queueStatus = 401;
  await page.goto("/#/inbox");
  await expect.poll(() => state.reads).toBeGreaterThan(0);
  await expect(page.getByRole("region", { name: "Held software queue jobs" })).toHaveCount(0);
  expect(state.writes).toBe(0);
});

test("inbox hides zero and unconfigured queue states", async ({ page }) => {
  const state = await fixture(page);
  state.body.counts.held = 0;
  state.body.jobs = [];
  await page.goto("/#/inbox");
  await expect.poll(() => state.reads).toBeGreaterThan(0);
  await expect(page.getByRole("region", { name: "Held software queue jobs" })).toHaveCount(0);
  state.queueStatus = 423;
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect.poll(() => state.reads).toBeGreaterThan(1);
  await expect(page.getByRole("region", { name: "Held software queue jobs" })).toHaveCount(0);
});

test("inbox clears the previous session's snapshot while a renewed read is pending and fails", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/inbox");
  const panel = page.getByRole("region", { name: "Held software queue jobs" });
  await expect(panel.getByText("Held at this check: 1.")).toBeVisible();
  state.queueDelay = 500;
  state.queueStatus = 503;
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect.poll(() => state.reads).toBeGreaterThan(1);
  await expect(panel.getByText(`Job ${jobId}`, { exact: false })).toHaveCount(0);
  await expect(panel.getByText("Queue status is unavailable.")).toBeVisible();
  await expect(panel.getByText(`Job ${jobId}`, { exact: false })).toHaveCount(0);
});

test("inbox never carries actor A queue details into actor B's pending or failed read", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/inbox");
  const panel = page.getByRole("region", { name: "Held software queue jobs" });
  await expect(panel.getByText(`Job ${jobId}`, { exact: false })).toBeVisible();
  state.actor = "human:43";
  state.queueDelay = 500;
  state.queueStatus = 503;
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect.poll(() => state.reads).toBeGreaterThan(1);
  await expect(page.getByText(`Job ${jobId}`, { exact: false })).toHaveCount(0);
  await expect(panel.getByText("Queue status is unavailable.")).toBeVisible();
  await expect(page.getByText(`Job ${jobId}`, { exact: false })).toHaveCount(0);
  expect(state.writes).toBe(0);
});

test("a late actor A queue 401 cannot clear actor B's workspace", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/inbox");
  const panel = page.getByRole("region", { name: "Held software queue jobs" });
  await expect(panel.getByText(`Job ${jobId}`, { exact: false })).toBeVisible();
  state.queueDelay = 1000;
  state.queueStatus = 401;
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect.poll(() => state.reads).toBeGreaterThan(1);
  state.actor = "human:43";
  state.queueDelay = 0;
  state.queueStatus = 200;
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect.poll(() => state.reads).toBeGreaterThan(2);
  await expect(panel.getByText(`Job ${jobId}`, { exact: false })).toBeVisible();
  await page.waitForTimeout(1200);
  await expect(panel.getByText(`Job ${jobId}`, { exact: false })).toBeVisible();
  expect(state.writes).toBe(0);
});

test("inbox does not present old queue details when workspace is stale", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/inbox");
  const panel = page.getByRole("region", { name: "Held software queue jobs" });
  await expect(panel.getByText("Held at this check: 1.")).toBeVisible();
  state.overviewStatus = 503;
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect(panel.getByText("Workspace updates are stale.", { exact: false })).toBeVisible();
  await expect(panel.getByText(`Job ${jobId}`, { exact: false })).toHaveCount(0);
});

test("held queue is visible without implying executor readiness; explicit refresh only reads", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/staging/queues");
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

test("shows only an authoritative visible reservation holder", async ({ page }) => {
  const state = await fixture(page);
  const holder = state.body.jobs[0];
  const waitingId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  state.body.jobs.push({ ...holder, id: waitingId, state: "queued", requires_reconciliation: false,
    blocked_reason: "machine_reserved", reservation_holder_job_id: holder.id });
  await page.goto("/#/staging/queues");
  const panel = page.getByRole("region", { name: "Software validation queue" });
  await expect(panel.getByText(`Reservation holder at this check: job ${jobId} (held for reconciliation).`)).toBeVisible();
  expect(state.writes).toBe(0);
  expect(await panel.getByRole("button").count()).toBe(1);
});

test("keeps queue visible when optional reservation reference does not match", async ({ page }) => {
  const state = await fixture(page);
  const holder = state.body.jobs[0];
  const waitingId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  state.body.jobs.push({ ...holder, id: waitingId, state: "queued", requires_reconciliation: false,
    blocked_reason: "machine_reserved", reservation_holder_job_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc" });
  await page.goto("/#/staging/queues");
  const panel = page.getByRole("region", { name: "Software validation queue" });
  await expect(panel.getByText("Reservation holder is not identified in this snapshot.")).toBeVisible();
  await expect(panel.getByText("Job " + waitingId, { exact: false })).toBeVisible();
  expect(state.writes).toBe(0);
});

test("describes a claimed holder without implying execution", async ({ page }) => {
  const state = await fixture(page);
  state.body.jobs[0].state = "claimed";
  state.body.jobs[0].requires_reconciliation = false;
  state.body.jobs[0].blocked_reason = null;
  state.body.jobs.push({ ...state.body.jobs[0], id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    state: "queued", blocked_reason: "machine_reserved", reservation_holder_job_id: jobId });
  await page.goto("/#/staging/queues");
  const panel = page.getByRole("region", { name: "Software validation queue" });
  await expect(panel.getByText(`Reservation holder at this check: job ${jobId} (claimed; execution not confirmed).`)).toBeVisible();
  expect(state.writes).toBe(0);
});

test("reports pause without claiming an unpaused executor is running", async ({ page }) => {
  const state = await fixture(page);
  state.body.master_paused = true;
  state.body.paused = true;
  await page.goto("/#/staging/queues");
  const panel = page.getByRole("region", { name: "Software validation queue" });
  await expect(panel.getByText("Master: Paused · Queue: Not paused · New claims: Paused")).toBeVisible();
  expect(state.writes).toBe(0);
});

test("queue becoming unavailable clears old rows instead of showing an empty healthy queue", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/staging/queues");
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
  await page.goto("/#/staging/queues");
  const panel = page.getByRole("region", { name: "Software validation queue" });
  await expect(panel.getByText("Job " + jobId, { exact: false })).toBeVisible();
  state.queueStatus = 401;
  await panel.getByRole("button", { name: "Check queue" }).click();
  await expect(panel).toHaveCount(0);
  await expect(page.getByText("Job " + jobId, { exact: false })).toHaveCount(0);
  expect(state.writes).toBe(0);
});
