import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";

const jobId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
function snapshot() {
  return { paused: false, queue_paused: false, master_paused: false,
    claim_admission_state: "unconfigured", counts: { held: 1 },
    actions: { "engineering-hub.validate": { machine_id: "staging-pc", environments: ["staging"] } }, jobs: [{
      id: jobId, machine_id: "staging-pc", action_key: "engineering-hub.validate",
      environment: "staging", state: "held", blocked_reason: "requires_reconciliation",
      requires_reconciliation: true, eligible: false, eligible_requires_worker_admission: false,
      target: { task_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", source_commit: "a".repeat(40),
        artifact_digest: "sha256:" + "b".repeat(64) },
    }] };
}

async function fixture(page: Page) {
  const state = { reads: 0, writes: 0, queueStatus: 200, reservationStatus: 200,
    reservation: "held" as "held" | "unknown" | "active" | "not_reserved", reservationReads: 0, body: snapshot() };
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
    if (path.endsWith("/work/machines/staging-pc")) {
      state.reservationReads++;
      expect(request.headers()["authorization"]).toBeUndefined();
      return respond(state.reservationStatus === 200 ? { machine_id: "staging-pc",
        queue_reservation: state.reservation, requires_reconciliation: state.reservation === "unknown" ? null : state.reservation === "held" }
        : { detail: { code: "fixture-denial" } }, state.reservationStatus);
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
  await expect(panel.getByText("staging-pc: Held — reconciliation required")).toBeVisible();
  await expect(panel.getByText("Queue records only;", { exact: false })).toBeVisible();
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

test("reservation refresh shows unknown and unavailable without asserting machine is free", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/staging");
  const panel = page.getByRole("region", { name: "Software validation queue" });
  await expect(panel.getByText("staging-pc: Held — reconciliation required")).toBeVisible();
  state.reservation = "unknown";
  await panel.getByRole("button", { name: "Check queue" }).click();
  await expect(panel.getByText("staging-pc: Reservation unknown")).toBeVisible();
  state.reservationStatus = 423;
  await panel.getByRole("button", { name: "Check queue" }).click();
  await expect(panel.getByText("staging-pc: Reservation unavailable")).toBeVisible();
  await expect(panel.getByText("No reservation in this queue", { exact: false })).toHaveCount(0);
  expect(state.writes).toBe(0);
});

test("reservation remains checking until its response arrives", async ({ page }) => {
  const state = await fixture(page);
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/hub/v1/work/machines/staging-pc", async route => {
    await pending;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      machine_id: "staging-pc", queue_reservation: "held", requires_reconciliation: true }) });
  });
  try {
    await page.goto("/#/staging");
    const panel = page.getByRole("region", { name: "Software validation queue" });
    await expect(panel.getByText("staging-pc: Checking reservation…")).toBeVisible();
    await expect(panel.getByText("Reservation unavailable", { exact: false })).toHaveCount(0);
    release();
    await expect(panel.getByText("staging-pc: Held — reconciliation required")).toBeVisible();
    expect(state.writes).toBe(0);
  } finally { release(); }
});

test("a slow reservation does not hide another machine's held state", async ({ page }) => {
  const state = await fixture(page);
  Object.assign(state.body.actions, { "other.validate": { machine_id: "slow-pc", environments: ["staging"] } });
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/hub/v1/work/machines/slow-pc", async route => {
    await pending;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      machine_id: "slow-pc", queue_reservation: "active", requires_reconciliation: false }) });
  });
  try {
    await page.goto("/#/staging");
    const panel = page.getByRole("region", { name: "Software validation queue" });
    await expect(panel.getByText("slow-pc: Checking reservation…")).toBeVisible();
    await expect(panel.getByText("staging-pc: Held — reconciliation required")).toBeVisible();
    release();
    await expect(panel.getByText("slow-pc: Active queue reservation")).toBeVisible();
    expect(state.writes).toBe(0);
  } finally { release(); }
});

test("authorization failure clears the workspace while another reservation is pending", async ({ page }) => {
  const state = await fixture(page);
  Object.assign(state.body.actions, { "other.validate": { machine_id: "slow-pc", environments: ["staging"] } });
  state.reservationStatus = 401;
  let release!: () => void, notifyStarted!: () => void, slowStarted = false;
  const pending = new Promise<void>(resolve => { release = resolve; });
  const started = new Promise<void>(resolve => { notifyStarted = resolve; });
  await page.route("**/api/hub/v1/work/machines/staging-pc", async route => {
    state.reservationReads++;
    await started;
    await route.fulfill({ status: 401, contentType: "application/json", body: "{}" }).catch(() => {});
  });
  await page.route("**/api/hub/v1/work/machines/slow-pc", async route => {
    slowStarted = true;
    notifyStarted();
    await pending;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      machine_id: "slow-pc", queue_reservation: "held", requires_reconciliation: true }) }).catch(() => {});
  });
  try {
    await page.goto("/#/staging");
    await expect.poll(() => slowStarted).toBe(true);
    await expect.poll(() => state.reservationReads).toBe(1);
    await expect(page.getByRole("region", { name: "Software validation queue" })).toHaveCount(0);
    release();
    await expect(page.getByText("slow-pc: Held — reconciliation required")).toHaveCount(0);
    expect(state.writes).toBe(0);
  } finally { notifyStarted(); release(); }
});

test("a late reservation from an older check cannot replace the latest result", async ({ page }) => {
  await fixture(page);
  let release!: () => void, reads = 0, oldFinished = false;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/hub/v1/work/machines/staging-pc", async route => {
    const first = ++reads === 1;
    if (first) await pending;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      machine_id: "staging-pc", queue_reservation: first ? "not_reserved" : "held",
      requires_reconciliation: !first }) }).catch(() => {});
    if (first) oldFinished = true;
  });
  try {
    await page.goto("/#/staging");
    const panel = page.getByRole("region", { name: "Software validation queue" });
    await expect(panel.getByText("staging-pc: Checking reservation…")).toBeVisible();
    await expect.poll(() => reads).toBe(1);
    await panel.getByRole("button", { name: "Check queue" }).click();
    await expect(panel.getByText("staging-pc: Held — reconciliation required")).toBeVisible();
    release();
    await expect.poll(() => oldFinished).toBe(true);
    await expect(panel.getByText("staging-pc: No reservation in this queue")).toHaveCount(0);
  } finally { release(); }
});

test("reservation authentication failure clears the private workspace", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/staging");
  const panel = page.getByRole("region", { name: "Software validation queue" });
  await expect(panel.getByText("staging-pc: Held — reconciliation required")).toBeVisible();
  state.reservationStatus = 401;
  await panel.getByRole("button", { name: "Check queue" }).click();
  await expect(panel).toHaveCount(0);
  expect(state.writes).toBe(0);
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
