import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import { planningPath, type PlanningDraft, type PlanningItem, type PlanningSelection } from "../src/planningApi";

const human = "human:cf:" + "e".repeat(64);
const topic: PlanningSelection = { scope: "topic", project: "engineering-hub", topic: "auto-wake-v1-test" };
const hub: PlanningSelection = { scope: "hub_improvements", project: "engineering-hub", topic: null };
function saved(selection: PlanningSelection, title: string, id: string): PlanningItem {
  const key = { ...selection, kind: "idea" as const, item_id: id };
  return { ...key, title, content: "<script>inert proposal</script>", status: "proposed", path: planningPath(key), version: 1,
    sha256: "c".repeat(64), created_by: "codex", updated_by: "codex", created_at: "2026-10-22T12:00:00Z", updated_at: "2026-10-22T12:00:00Z" };
}
async function fixture(page: Page) {
  const state = { items: [saved(topic, "Topic feature idea", "topic-idea"), saved(hub, "Improve Hub search", "hub-search")],
    posts: [] as { body: PlanningDraft; csrf?: string }[], listStatus: 200, auth: true };
  await page.route("**/api/hub/v1/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: state.auth, actor: state.auth ? human : null,
      github_id: null, auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: state.auth ? "fixture-csrf" : null });
    if (path.endsWith("/overview")) return respond(createDemoOverview());
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "fixture", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    const matches = (item: PlanningItem) => item.scope === url.searchParams.get("scope") && item.project === url.searchParams.get("project")
      && item.topic === url.searchParams.get("topic");
    if (path === "/api/hub/v1/planning" && request.method() === "GET") {
      if (state.listStatus !== 200) return respond({ detail: { code: "planning_unavailable" } }, state.listStatus);
      return respond({ items: state.items.filter(matches).map(({ content: _content, ...meta }) => meta), truncated: false });
    }
    if (path === "/api/hub/v1/planning/document") {
      const item = state.items.find(item => matches(item) && item.kind === url.searchParams.get("kind") && item.item_id === url.searchParams.get("item_id"));
      return item ? respond(item) : respond({ detail: { code: "planning_item_not_found" } }, 404);
    }
    if (path === "/api/hub/v1/planning" && request.method() === "POST") {
      const body = request.postDataJSON() as PlanningDraft;
      state.posts.push({ body, csrf: request.headers()["x-hub-csrf"] });
      const existing = state.items.find(item => item.scope === body.scope && item.project === body.project && item.topic === body.topic
        && item.kind === body.kind && item.item_id === body.item_id);
      if ((existing?.version ?? 0) !== body.expected_version)
        return respond({ detail: { code: "planning_version_conflict", current_version: existing?.version ?? 0 } }, 409);
      const { expected_version, ...values } = body;
      const item: PlanningItem = { ...saved(body, body.title, body.item_id), ...values, path: planningPath(body), version: expected_version + 1, updated_by: human };
      state.items = [...state.items.filter(item => item !== existing), item];
      return respond(item);
    }
    return respond({}, 404);
  });
  return state;
}
const panelOf = (page: Page) => page.getByRole("region", { name: "Plans and ideas" });

test("topic planning and EH improvements stay separate; content is inert", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/planning");
  const panel = panelOf(page);
  await expect(panel.getByRole("button", { name: /Topic feature idea/ })).toBeVisible();
  await expect(panel.getByRole("button", { name: /Improve Hub search/ })).toHaveCount(0);
  await panel.getByRole("button", { name: /Topic feature idea/ }).click();
  await expect(panel.locator("article pre")).toHaveText("<script>inert proposal</script>");
  await expect(panel.locator("article script")).toHaveCount(0);
  await panel.getByLabel("Backlog").selectOption("hub_improvements");
  await expect(panel.getByRole("button", { name: /Improve Hub search/ })).toBeVisible();
  await expect(panel.getByRole("button", { name: /Topic feature idea/ })).toHaveCount(0);
  await expect(panel.getByText("This is organization, not a separate permission boundary.", { exact: false })).toBeVisible();
});

test("create, CAS conflict and explicit merged save retain the draft", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/planning");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: "New item" }).click();
  const editor = panel.getByRole("form", { name: "New planning item" });
  await editor.getByLabel("Stable ID").fill("shared-plan");
  await editor.getByLabel("Kind", { exact: true }).selectOption("plan");
  await editor.getByLabel("Title", { exact: true }).fill("Shared plan");
  await editor.getByLabel("Plan or suggestion").fill("My original plan");
  await editor.getByRole("button", { name: "Save item" }).click();
  await expect(panel.getByText("Saved version 1.", { exact: false })).toBeVisible();
  expect(state.posts[0].csrf).toBe("fixture-csrf");
  expect(state.posts[0].body).toMatchObject({ ...topic, item_id: "shared-plan", kind: "plan", expected_version: 0 });
  await panel.locator("article").getByRole("button", { name: "Edit", exact: true }).click();
  const editing = panel.getByRole("form", { name: "Edit planning item" });
  await editing.getByLabel("Plan or suggestion").fill("My retained draft");
  const stored = state.items.find(item => item.item_id === "shared-plan")!;
  Object.assign(stored, { content: "Agent proposal", version: 2, updated_by: "claude" });
  await editing.getByRole("button", { name: "Save item" }).click();
  await expect(editing.getByRole("alert")).toContainText("Nothing was overwritten");
  await expect(editing.getByLabel("Plan or suggestion")).toHaveValue("My retained draft");
  await expect(editing.locator("details pre")).toHaveText("Agent proposal");
  await expect(editing.getByRole("button", { name: "Save item" })).toBeDisabled();
  expect(stored.content).toBe("Agent proposal");
  await editing.getByRole("button", { name: "I reviewed or merged it", exact: false }).click();
  await editing.getByRole("button", { name: "Save item" }).click();
  await expect(panel.getByText("Saved version 3.", { exact: false })).toBeVisible();
  expect(state.posts.map(post => post.body.expected_version)).toEqual([0, 1, 2]);
});

test("auth loss clears private planning drafts without a discard prompt", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/planning");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: /Topic feature idea/ }).click();
  await panel.locator("article").getByRole("button", { name: "Edit", exact: true }).click();
  await panel.getByLabel("Plan or suggestion").fill("Revoked private draft");
  const dialogs: string[] = [];
  page.on("dialog", dialog => { dialogs.push(dialog.type()); void dialog.dismiss(); });
  state.auth = false; state.listStatus = 401;
  await panel.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByRole("link", { name: /Sign in securely/ })).toBeVisible();
  await expect(panel).toHaveCount(0);
  await expect(page.getByText("Revoked private draft", { exact: true })).toHaveCount(0);
  expect(dialogs).toEqual([]);
  expect(state.posts).toHaveLength(0);
});

test("a changed kind on a blank new draft is protected on navigation", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/planning");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: "New item" }).click();
  await panel.getByLabel("Kind", { exact: true }).selectOption("feature");
  page.once("dialog", dialog => { void dialog.dismiss(); });
  await panel.getByLabel("Backlog").selectOption("hub_improvements");
  await expect(panel.getByLabel("Backlog")).toHaveValue("topic");
  await expect(panel.getByLabel("Kind", { exact: true })).toHaveValue("feature");
});

test("a project's Plans tab shows that project's topic plans, not the Hub backlog", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/projects/engineering-hub");
  await page.getByRole("navigation", { name: "Engineering Hub sections" }).getByRole("link", { name: "Notes & plans" }).click();
  await page.getByRole("group", { name: "Notes and plans" }).getByRole("link", { name: "Plans & ideas" }).click();
  await expect(page).toHaveURL(/\/(#\/)?projects\/engineering-hub\/plans$/);
  const panel = panelOf(page);
  await expect(panel.getByRole("button", { name: /Topic feature idea/ })).toBeVisible();
  await expect(panel.getByRole("button", { name: /Improve Hub search/ })).toHaveCount(0);
});
