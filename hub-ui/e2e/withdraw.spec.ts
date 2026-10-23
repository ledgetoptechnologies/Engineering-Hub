import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import { audit, blocking } from "./audit";

const human = "human:cf:" + "e".repeat(64);
type Overview = ReturnType<typeof createDemoOverview>;
const OLD = "11111111-2222-4333-8444-0000000000a1", NEW = "11111111-2222-4333-8444-0000000000a2";

async function fixture(page: Page) {
  const overview = createDemoOverview(new Date());
  const base = overview.decisions[0];
  overview.decisions = [
    { ...base, id: OLD, project: "treedar", title: "TreeDar labels, area A1: what mostly grows here?" },
    { ...base, id: NEW, project: "engineering-hub", title: "TreeDar labels, area A1 (see map): what mostly grows here?" },
  ];
  const state = { posts: [] as { path: string; body: Record<string, unknown>; csrf?: string }[] };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(overview);
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    if (path.endsWith("/attachments")) return respond({ project: "treedar", entity_revision: 1, items: [] });
    const match = /^\/api\/hub\/v1\/(tasks|decisions)\/([^/]+)\/withdraw$/.exec(path);
    if (match && request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.posts.push({ path, body, csrf: request.headers()["x-hub-csrf"] });
      const item = [...overview.tasks, ...overview.decisions].find((entry) => entry.id === match[2])!;
      Object.assign(item, { status: "withdrawn", withdrawn_reason: body.reason, superseded_by: body.superseded_by ?? null,
        withdrawn_by: human, withdrawn_at: new Date().toISOString(), revision: item.revision + 1 });
      return respond(item);
    }
    return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
  });
  return { overview, state };
}

test("withdrawing a superseded question records reason and replacement, then moves it to Withdrawn", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto("/#/inbox");
  await expect(page.locator("form.decision")).toHaveCount(2);
  const old = page.locator("form.decision", { hasText: "area A1: what" });
  await old.getByRole("button", { name: "No longer needed? Withdraw" }).click();
  const dialog = page.getByRole("dialog", { name: "Withdraw this question?" });
  await expect(dialog.getByRole("button", { name: "Withdraw question" })).toBeDisabled();
  await dialog.getByLabel("Why (required)").fill("Replaced by the copy with a map");
  await dialog.getByLabel("Replaced by (optional)").selectOption(NEW);
  expect(blocking(await audit(page, "dialog", 1440))).toEqual([]);
  await dialog.getByRole("button", { name: "Withdraw question" }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.posts).toEqual([{ path: `/api/hub/v1/decisions/${OLD}/withdraw`, csrf: "fixture-csrf",
    body: { expected_revision: 1, reason: "Replaced by the copy with a map", superseded_by: NEW } }]);
  await expect(page.locator("form.decision")).toHaveCount(1);
  const withdrawn = page.locator(".withdrawn-list");
  await withdrawn.getByText("Withdrawn questions (1)").click();
  await expect(withdrawn.getByText("Replaced by the copy with a map")).toBeVisible();
  await expect(withdrawn.getByRole("link", { name: /area A1 \(see map\)/ })).toBeVisible();
});

test("cancelling the withdraw pop-up sends nothing and protected questions offer no withdraw", async ({ page }) => {
  const { overview, state } = await fixture(page);
  overview.decisions[1].protected = true;
  await page.goto("/#/inbox");
  const cards = page.locator("form.decision");
  await expect(cards.nth(1).getByRole("button", { name: /Withdraw/ })).toHaveCount(0);
  await cards.first().getByRole("button", { name: "No longer needed? Withdraw" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(state.posts).toEqual([]);
  await expect(cards).toHaveCount(2);
});

test("withdrawn items are left out of open counts and lists", async ({ page }) => {
  const { overview } = await fixture(page);
  Object.assign(overview.decisions[0], { status: "withdrawn", withdrawn_reason: "Moved", superseded_by: NEW, withdrawn_by: "claude" });
  overview.tasks.forEach((task) => Object.assign(task, { status: "withdrawn", withdrawn_reason: "Moved" }));
  await page.goto("/#/inbox");
  await expect(page.locator("form.decision")).toHaveCount(1);
  await expect(page.getByRole("link", { name: /Needs you/ }).first()).toContainText("1");
  await page.goto("/#/work");
  await expect(page.locator(".task-table .task-row")).toHaveCount(0);
  await expect(page.getByText(`Withdrawn tasks (${overview.tasks.length})`)).toBeVisible();
});
