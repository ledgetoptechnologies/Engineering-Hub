import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";

const human = "human:cf:" + "e".repeat(64);

function overview() {
  const value = createDemoOverview(new Date());
  value.decisions.push({ ...value.decisions[0], id: "treedar-question", project: "treedar",
    title: "TreeDar labels, area A1: what mostly grows here?" });
  value.tasks.push({ ...value.tasks[1], id: "treedar-task", project: "treedar", title: "Count tree crowns in audit plot A1" });
  return value;
}

async function fixture(page: Page) {
  const state = { writes: 0, reads: 0 };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (request.method() !== "GET") { state.writes++; return respond({}, 405); }
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) { state.reads++; return respond(overview()); }
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "fixture", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    if (path.endsWith("/notes")) return respond({ items: [], truncated: false });
    return respond({}, 404);
  });
  return state;
}
const nav = (page: Page) => page.getByRole("navigation", { name: "Workspace navigation" });

for (const width of [1440, 390]) test(`project decision timeline dot stays within its column (${width}px)`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1100 });
  await fixture(page);
  await page.goto("/#/projects/engineering-hub");
  const dot = page.locator(".timeline-dot.kind-decision").first();
  await expect(dot).toBeVisible();
  const box = await dot.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.width).toBeLessThanOrEqual(14);
  expect(box!.height).toBeLessThanOrEqual(14);
  expect(await dot.evaluate(element => getComputedStyle(element).padding)).toBe("0px");
  const title = await dot.locator("..").locator("strong").boundingBox();
  expect(title).not.toBeNull();
  expect(box!.x + box!.width).toBeLessThanOrEqual(title!.x);
});

test("sidebar pages switch without reloading or extra reads, and mark the current page", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toContainText(/Good (morning|afternoon|evening)/);
  const reads = state.reads;
  for (const [label, heading] of [["Needs you", "Needs you"], ["Work", "Work in motion"], ["Projects", "Projects"],
    ["Agents", "Agents"], ["Staging", "Staging"], ["Home", /Good/]] as const) {
    await nav(page).getByRole("link", { name: new RegExp("^" + label) }).click();
    await expect(page.getByRole("heading", { level: 1 })).toContainText(heading);
    await expect(nav(page).getByRole("link", { name: new RegExp("^" + label) })).toHaveAttribute("aria-current", "page");
  }
  expect(state.reads).toBe(reads);
  expect(state.writes).toBe(0);
  // Notes and plans live inside each project now, not in the sidebar.
  await expect(nav(page).getByRole("link", { name: /^Notes/ })).toHaveCount(0);
  await expect(nav(page).getByRole("link", { name: /^Plans/ })).toHaveCount(0);
});

test("the inbox count and project filter show only open questions", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/inbox");
  await expect(nav(page).getByRole("link", { name: /^Needs you/ })).toContainText("2");
  await expect(page.locator("form.decision")).toHaveCount(2);
  await page.getByRole("group", { name: "Filter by project" }).getByRole("button", { name: /TreeDar/ }).click();
  await expect(page.locator("form.decision")).toHaveCount(1);
  await expect(page.locator("form.decision")).toContainText("TreeDar labels, area A1");
});

test("a project page shows only that project's questions, work and notes", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  await page.getByRole("navigation", { name: "Projects" }).getByRole("link", { name: "TreeDar" }).click();
  // Real page addresses (no #) when the server serves the app on every path.
  await expect(page).toHaveURL(/\/projects\/treedar$/);
  expect(page.url()).not.toContain("#");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("TreeDar");
  const tabs = page.getByRole("navigation", { name: "TreeDar sections" });
  await tabs.getByRole("link", { name: /Needs you/ }).click();
  await expect(page).toHaveURL(/\/projects\/treedar\/decisions$/);
  await expect(page.locator("form.decision")).toHaveCount(1);
  await tabs.getByRole("link", { name: /Work/ }).click();
  await expect(page.locator(".board-card")).toHaveCount(1);
  await page.getByRole("button", { name: "List" }).click();
  await expect(page.locator("details.task-row")).toHaveCount(1);
  await expect(page.getByText("Count tree crowns in audit plot A1", { exact: true })).toBeVisible();
  await tabs.getByRole("link", { name: "Notes & plans" }).click();
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue("treedar");
  // Browser Back returns to the previous tab.
  await page.goBack();
  await expect(page).toHaveURL(/\/projects\/treedar\/work$/);
  await page.getByRole("navigation", { name: "Breadcrumb" }).getByRole("link", { name: "Projects" }).click();
  await expect(page.locator("table.data-table tbody tr")).toHaveCount(5);
});

test("work filters narrow by state and search without touching the server", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/work");
  const rows = page.locator("details.task-row");
  await expect(rows).toHaveCount(3);
  await page.getByRole("group", { name: "Filter by state" }).getByRole("button", { name: /^Review passed/ }).click();
  await expect(rows).toHaveCount(1);
  await page.getByRole("group", { name: "Filter by state" }).getByRole("button", { name: /^All/ }).click();
  await page.getByLabel("Search tasks").fill("crowns");
  await expect(rows).toHaveCount(1);
  await page.getByLabel("Search tasks").fill("no such task");
  await expect(page.getByText("No tasks match these filters.", { exact: true })).toBeVisible();
  expect(state.writes).toBe(0);
});

test("an unknown project route says so instead of showing another project", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/projects/not-a-project");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Project not found");
  await expect(page.locator("form.decision")).toHaveCount(0);
});

test("on a phone the menu opens, navigates and closes", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixture(page);
  await page.goto("/");
  await expect(nav(page)).toBeHidden();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await expect(nav(page)).toBeVisible();
  await nav(page).getByRole("link", { name: /^Agents/ }).click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Agents");
  await expect(nav(page)).toBeHidden();
});
