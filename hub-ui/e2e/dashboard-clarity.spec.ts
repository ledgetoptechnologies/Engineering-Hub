import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";

const commit = "a".repeat(40);
const human = "human:cf:" + "e".repeat(64);

function overview() {
  const now = Date.now();
  const value = createDemoOverview(new Date(now)) as ReturnType<typeof createDemoOverview> & Record<string, unknown>;
  value.tasks = [{
    id: "task-benchmark", revision: 3, updated_at: new Date(now - 60_000).toISOString(), project: "treedar",
    title: "Review and merge claude/benchmark", status: "in_review", owner: "claude", candidate_commit: commit,
    review: null, repair_count: 0, scope: "staging", proposed_by: "claude",
    state_since: new Date(now - 13 * 3600_000).toISOString(),
  }, {
    id: "task-unknown-age", revision: 1, updated_at: new Date(now).toISOString(), project: "engineering-hub",
    title: "Legacy task without audit", status: "assigned", owner: null, candidate_commit: null,
    review: null, repair_count: 0, state_since: null,
  }];
  value.decisions = [{
    id: "decision-test-setup", revision: 2, updated_at: new Date(now - 2 * 3600_000).toISOString(),
    project: "engineering-hub", title: "Staging pilot: separate TEST signing key",
    options: ["Approve isolated TEST setup; execution still needs explicit authorization", "Keep source-only"],
    protected: true, status: "answered", answer: "Approve isolated TEST setup; execution still needs explicit authorization",
    execution_authorization: false, answered_by: human,
  }];
  const observed = new Date(now).toISOString();
  value.agents = (["codex", "hermes", "claude"] as const).map(id => ({
    id, transport: "unknown", chat_readiness: "unverified", reason: "live_presence_adapter_not_configured",
    connection: { state: "connected", source: "embedded_bus", scope: "this_server_process", observed_at: observed,
      valid_until: new Date(now + 25_000).toISOString(), active_streams: id === "hermes" ? 17 : 2,
      inbox_subscriptions: id === "claude" ? 2 : 0, subscribed: id === "claude", last_stream_write_at: observed,
      native_push_available: true },
  }));
  return value;
}

async function fixture(page: Page) {
  const state = { writes: 0 };
  await page.route("**/api/hub/v1/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (request.method() !== "GET") { state.writes++; return respond({}, 405); }
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(overview());
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported",
      observer_epoch: "fixture", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    if (path.endsWith("/notes")) return respond({ items: [], truncated: false });
    return respond({}, 404);
  });
  return state;
}

test("answered decisions are history, not a new approval request", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/");
  const decisions = page.locator("#decisions");
  await expect(decisions.getByRole("heading", { name: "Nothing waiting on you." })).toBeVisible();
  const answers = decisions.getByLabel("Recent answers");
  await expect(answers.getByText("Staging pilot: separate TEST signing key", { exact: true })).toBeVisible();
  await expect(answers.getByText("Answered by you", { exact: false })).toBeVisible();
  await expect(answers.getByText("(2h ago)", { exact: false })).toBeVisible();
  await expect(answers.getByText("did not by itself authorize running anything", { exact: false })).toBeVisible();
  await expect(answers.getByRole("button")).toHaveCount(0);
  expect(state.writes).toBe(0);
});

test("unconfigured projects and runner read as not set up, not as faults", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  const staging = page.locator("#staging");
  await expect(staging.getByText("Not set up yet", { exact: true })).toHaveCount(5);
  await expect(staging.getByText("Unknown", { exact: true })).toHaveCount(0);
  await expect(staging.getByText("Emergency pause off", { exact: true })).toBeVisible();
  await expect(staging.getByText("Automatic runner not set up. Agents only act on the tasks shown here.", { exact: true })).toBeVisible();
  await expect(staging.getByRole("button", { name: "Pause new work" })).toBeEnabled();
});

test("work cards show who they wait on and audited age", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  const work = page.locator("#work");
  const card = work.locator("details", { hasText: "Review and merge claude/benchmark" });
  await expect(card.getByText("Waiting for an independent review. No reviewer assigned; any agent except Claude can review.", { exact: true })).toBeVisible();
  await expect(card.getByText("(13h ago)", { exact: false })).toBeVisible();
  await card.locator("summary").click();
  await expect(card.getByText("None assigned yet", { exact: true })).toBeVisible();
  const legacy = work.locator("details", { hasText: "Legacy task without audit" });
  await expect(legacy.getByText("Time in this state not recorded", { exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("dashboard-clarity.png"), fullPage: true });
});

test("connected agents state whether an inbox subscription was observed without claiming readiness", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  const agents = page.locator("#agents");
  await expect(agents.getByText("Inbox subscription observed: new messages can be pushed to this agent.", { exact: true })).toHaveCount(1);
  await expect(agents.getByText("No active inbox subscription observed.", { exact: false })).toHaveCount(2);
  await expect(agents.getByText("17 active stream(s)", { exact: false })).toBeVisible();
  await expect(agents.getByText(/leak/i)).toHaveCount(0);
});
