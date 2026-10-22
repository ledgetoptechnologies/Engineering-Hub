import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";

const human = "human:cf:" + "e".repeat(64);
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

interface Item { id: string; meta: Record<string, string | number>; label: { choice: string; by: string; at: string } | null; revision: number }

function crownSet() {
  const items: Item[] = [];
  for (const [area, count] of [["A1", 3], ["A2", 2], ["B", 2]] as const)
    for (let i = 0; i < count; i++) items.push({ id: `${area}-${i}`, meta: { area, height_m: 10 + i }, label: null, revision: 0 });
  items[0].label = { choice: "conifer", by: human, at: new Date().toISOString() };
  items[0].revision = 1;
  return { id: "crowns-oct", project: "treedar", title: "Crown types, October flight", instructions: "Tap one answer per crown.",
    choices: [{ key: "conifer", label: "Conifer" }, { key: "broadleaf", label: "Broadleaf" }, { key: "unsure", label: "Not sure" }],
    items, updated_at: new Date().toISOString() };
}

async function fixture(page: Page, { supported = true, gateway = false, sets = [crownSet()] } = {}) {
  const state = { puts: [] as { url: string; body: Record<string, unknown>; csrf?: string }[], conflictNext: false, sets };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(createDemoOverview(new Date()));
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    if (path.endsWith("/notes")) return respond({ items: [], truncated: false });
    if (path.startsWith("/api/hub/v1/label-sets")) {
      if (!supported) return respond({ detail: "Not Found" }, 404);
      // The private gateway refuses routes it does not know with 403, not 404.
      if (gateway) return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
      const parts = path.split("/").slice(5);
      if (parts.length === 0) {
        const project = url.searchParams.get("project");
        return respond({ items: state.sets.filter((s) => s.project === project).map((s) => ({ id: s.id, project: s.project, title: s.title,
          item_count: s.items.length, labeled_count: s.items.filter((i) => i.label).length, updated_at: s.updated_at })) });
      }
      const set = state.sets.find((s) => s.id === parts[0]);
      if (!set) return respond({ detail: { code: "label_set_not_found" } }, 404);
      if (parts.length === 1) return respond(set);
      const item = set.items.find((i) => i.id === parts[2]);
      if (!item) return respond({}, 404);
      if (parts[3] === "image") return route.fulfill({ status: 200, contentType: "image/png", body: PNG });
      if (parts[3] === "label" && request.method() === "PUT") {
        const body = request.postDataJSON() as Record<string, unknown>;
        state.puts.push({ url: path, body, csrf: request.headers()["x-hub-csrf"] });
        if (state.conflictNext || body.expected_revision !== item.revision) {
          state.conflictNext = false;
          item.revision++;
          return respond({ detail: { code: "label_conflict" } }, 409);
        }
        item.revision++;
        item.label = body.choice === null ? null : { choice: body.choice as string, by: human, at: new Date().toISOString() };
        return respond(item);
      }
    }
    return respond({}, 404);
  });
  return state;
}

test("a project lists its labeling sets and opens one with dynamic filters", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/projects/treedar/labeling");
  const panel = page.getByRole("region", { name: "Labeling" });
  await expect(panel.getByText("1 of 7 labeled", { exact: false })).toBeVisible();
  await panel.getByRole("link", { name: /Crown types, October flight/ }).click();
  await expect(page).toHaveURL(/\/projects\/treedar\/labels\/crowns-oct$/);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Crown types, October flight");
  const cards = page.locator("article.label-card");
  await expect(cards).toHaveCount(7);
  // Filters come from the items' own text fields; numeric fields are shown, not filtered.
  await page.getByLabel("Filter by area").selectOption("A2");
  await expect(cards).toHaveCount(2);
  await expect(page.getByLabel("Filter by height_m")).toHaveCount(0);
  await expect(cards.first()).toContainText("height_m 10");
});

test("tapping saves with CSRF and revision; tapping again clears; keyboard labels and moves on", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/projects/treedar/labels/crowns-oct");
  const card = page.getByRole("article", { name: /Item A2-0/ });
  await card.getByRole("button", { name: "Broadleaf" }).click();
  await expect(card.getByRole("button", { name: "Broadleaf" })).toHaveAttribute("aria-pressed", "true");
  expect(state.puts[0]).toMatchObject({ url: "/api/hub/v1/label-sets/crowns-oct/items/A2-0/label", csrf: "fixture-csrf",
    body: { choice: "broadleaf", expected_revision: 0 } });
  await expect(page.getByLabel("Progress")).toContainText("2/7 labeled");
  await card.getByRole("button", { name: "Broadleaf" }).click();
  await expect(card.getByRole("button", { name: "Broadleaf" })).toHaveAttribute("aria-pressed", "false");
  expect(state.puts[1].body).toEqual({ choice: null, expected_revision: 1 });
  const keyed = page.getByRole("article", { name: /Item B-0/ });
  await keyed.focus();
  await page.keyboard.press("3");
  await expect(keyed.getByRole("button", { name: "Not sure" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("article", { name: /Item B-1/ })).toBeFocused();
});

test("a conflicting save reloads the set and says so", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/projects/treedar/labels/crowns-oct");
  state.conflictNext = true;
  await page.getByRole("article", { name: /Item A1-1/ }).getByRole("button", { name: "Conifer" }).click();
  await expect(page.getByText("Someone else changed that item.", { exact: false })).toBeVisible();
});

test("any agent-defined choices work, not just tree types", async ({ page }) => {
  const quiz = { id: "stats-ch3", project: "treedar", title: "Chapter 3 practice", instructions: "Pick the distribution.",
    choices: [{ key: "normal", label: "Normal" }, { key: "binomial", label: "Binomial" }, { key: "poisson", label: "Poisson" },
      { key: "uniform", label: "Uniform" }],
    items: [{ id: "q1", meta: { section: "3.1" }, label: null, revision: 0 }, { id: "q2", meta: { section: "3.2" }, label: null, revision: 0 }],
    updated_at: new Date().toISOString() };
  await fixture(page, { sets: [quiz] });
  await page.goto("/#/projects/treedar/labels/stats-ch3");
  await expect(page.getByRole("article", { name: /Item q1/ }).getByRole("button")).toHaveText(["Normal", "Binomial", "Poisson", "Uniform"]);
  await expect(page.getByLabel("Filter by section")).toBeVisible();
});

test("servers without labeling sets show no panel and no error", async ({ page }) => {
  await fixture(page, { supported: false });
  await page.goto("/#/projects/treedar/labeling");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("TreeDar");
  await expect(page.getByText("Labeling sets appear here", { exact: false })).toBeVisible();
  await expect(page.getByRole("region", { name: "Labeling" })).toHaveCount(0);
  await page.goto("/#/projects/treedar/labels/crowns-oct");
  await expect(page.getByText("This labeling set isn't available for this project.", { exact: true })).toBeVisible();
});

test("a set belonging to another project is never shown under this one", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/projects/3d-viewer/labels/crowns-oct");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Labeling set not found");
  await expect(page.locator("article.label-card")).toHaveCount(0);
});

test("a gateway that does not know the labeling route is not treated as a sign-out", async ({ page }) => {
  await fixture(page, { gateway: true });
  await page.goto("/#/projects/treedar/labeling");
  await expect(page.getByText("Labeling sets appear here", { exact: false })).toBeVisible();
  await expect(page.getByRole("region", { name: "Labeling" })).toHaveCount(0);
  // Still signed in, and the private workspace is still loaded.
  await expect(page.getByText("operator@example.test", { exact: true })).toBeVisible();
  await page.getByRole("navigation", { name: "TreeDar sections" }).getByRole("link", { name: /Work/ }).click();
  await expect(page.getByRole("list", { name: "Work board" })).toBeVisible();
});
