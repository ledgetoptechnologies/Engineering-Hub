import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import { audit, blocking } from "./audit";

const human = "human:cf:" + "e".repeat(64);
const inst = "installation:11111111-2222-4333-8444-555555555555";

async function fixture(page: Page) {
  const overview = createDemoOverview(new Date());
  const [verified, claimed] = overview.tasks;
  const recent = new Date(Date.now() - 2 * 3600_000).toISOString();
  overview.tasks = [
    { ...verified, state_since: recent },
    { ...claimed, state_since: recent },
    { ...claimed, id: "demo-task-paired", title: "Paired Codex task", owner: inst, owner_family: "codex", status: "in_review",
      candidate_commit: "c".repeat(40), state_since: null },
    { ...claimed, id: "demo-task-open", title: "Nobody has this yet", owner: null, status: "assigned", state_since: recent },
  ];
  const state = { writes: 0, overview };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (request.method() !== "GET") { state.writes++; return respond({}, 405); }
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(state.overview);
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
  });
  return state;
}

test("who's on what groups work by recorded owner and filters the table", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/work");
  const summary = page.getByRole("region", { name: "Who's on what" });
  const rows = summary.getByRole("listitem");
  await expect(rows).toHaveCount(4);
  await expect(rows.last()).toContainText("Unclaimed");
  await expect(rows.last()).toContainText("1 not started");
  const paired = rows.filter({ hasText: "1 in review" });
  await expect(paired).toHaveCount(1);
  await expect(paired.locator("strong")).toHaveText("Codex on install 11111111");
  await expect(paired).toContainText("Time in state not available");
  await expect(summary.getByRole("button", { name: /Codex.*passed review in the last 7 days/ })).toBeVisible();

  const table = page.locator("#work .task-table");
  await expect(table.locator(".task-row")).toHaveCount(4);
  const hermes = summary.getByRole("button", { name: /Hermes/ });
  await hermes.click();
  await expect(hermes).toHaveAttribute("aria-pressed", "true");
  await expect(table.locator(".task-row")).toHaveCount(1);
  await expect(table).toContainText("Document staging storage acceptance checks");
  await summary.getByRole("button", { name: "Show everyone" }).click();
  await expect(table.locator(".task-row")).toHaveCount(4);
  await summary.getByRole("button", { name: /Unclaimed/ }).click();
  await expect(table.locator(".task-row")).toHaveCount(1);
  await expect(table).toContainText("Nobody has this yet");
  await summary.getByRole("button", { name: /Unclaimed/ }).click();
  await expect(table.locator(".task-row")).toHaveCount(4);
  expect(state.writes).toBe(0);
});

test("an owner filter whose last card goes away on refresh falls back to everyone", async ({ page }) => {
  const state = await fixture(page);
  const tasks = state.overview.tasks;
  await page.goto("/#/work");
  const summary = page.getByRole("region", { name: "Who's on what" });
  const table = page.locator("#work .task-row");
  const refresh = () => page.getByRole("button", { name: "Refresh workspace" }).click();

  // Hermes's only task is withdrawn: the card goes, and the list shows everyone again.
  await summary.getByRole("button", { name: /Hermes/ }).click();
  await expect(table).toHaveCount(1);
  state.overview = { ...state.overview, tasks: tasks.map((task) => task.owner === "hermes"
    ? { ...task, status: "withdrawn" as const, withdrawn_reason: "Superseded", withdrawn_at: new Date().toISOString() } : task) };
  await refresh();
  await expect(summary.getByRole("button", { name: /Hermes/ })).toHaveCount(0);
  await expect(table).toHaveCount(3);
  await expect(summary.getByRole("button", { name: "Show everyone" })).toHaveCount(0);

  // Codex's only task ages past the 7-day window and every other card goes too: the summary
  // disappears, and the list isn't left filtered to Codex with no way back.
  await summary.getByRole("button", { name: /Codex.*passed review/ }).click();
  await expect(table).toHaveCount(1);
  const old = new Date(Date.now() - 8 * 24 * 3600_000).toISOString();
  state.overview = { ...state.overview, tasks: [{ ...tasks[0], state_since: old }] };
  await refresh();
  await expect(summary).toHaveCount(0);
  await expect(table).toHaveCount(1);

  // When Codex has fresh work again, the old choice doesn't silently come back.
  state.overview = { ...state.overview, tasks: [{ ...tasks[0], status: "claimed", state_since: new Date().toISOString() },
    { ...tasks[1], state_since: new Date().toISOString() }] };
  await refresh();
  await expect(summary.getByRole("button", { name: /Codex/ })).toHaveAttribute("aria-pressed", "false");
  await expect(table).toHaveCount(2);
  expect(state.writes).toBe(0);
});

for (const width of [375, 1440]) {
  test(`who's on what fits at ${width}px`, async ({ page }) => {
    await fixture(page);
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/#/work");
    await expect(page.getByRole("region", { name: "Who's on what" })).toBeVisible();
    expect(blocking(await audit(page, "page", width))).toEqual([]);
  });
}
