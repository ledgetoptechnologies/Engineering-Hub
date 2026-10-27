import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import { planningPath, type PlanningDraft, type PlanningItem, type PlanningSelection } from "../src/planningApi";

const human = "human:cf:" + "e".repeat(64);
const topic: PlanningSelection = { scope: "topic", project: "engineering-hub", topic: "auto-wake-v1-test" };
const hub: PlanningSelection = { scope: "hub_improvements", project: "engineering-hub", topic: null };
const milestone: PlanningSelection = { scope: "topic", project: "treedar", topic: "treedar-milestone-1" };
const chat: PlanningSelection = { scope: "topic", project: "treedar", topic: "agent-link-5b9b51f9-d5af-45a8-88dd-9f1835029e9e" };
function saved(selection: PlanningSelection, title: string, id: string, updated = "2026-10-22T12:00:00Z"): PlanningItem {
  const key = { ...selection, kind: "idea" as const, item_id: id };
  return { ...key, title, content: "<script>inert proposal</script>", status: "proposed", path: planningPath(key), version: 1,
    sha256: "c".repeat(64), created_by: "codex", updated_by: "codex", created_at: "2026-10-22T12:00:00Z", updated_at: updated };
}
async function fixture(page: Page) {
  const state = { items: [saved(topic, "Topic feature idea", "topic-idea"), saved(hub, "Improve Hub search", "hub-search"),
    saved(milestone, "Checked tree-type map", "tree-map", "2026-10-26T12:00:00Z"),
    saved(chat, "Gaussian splat processing", "splats", "2026-10-27T12:00:00Z")],
    posts: [] as { body: PlanningDraft; csrf?: string }[], listStatus: 200, auth: true, planningReads: [] as string[] };
  await page.route("**/api/hub/v1/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: state.auth, actor: state.auth ? human : null,
      github_id: null, auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: state.auth ? "fixture-csrf" : null });
    if (path.endsWith("/overview")) return respond(createDemoOverview());
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "fixture", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    // Topic discovery reads note paths only: planning records live in the project's note store.
    if (path === "/api/hub/v1/notes" && request.method() === "GET") {
      if (state.listStatus !== 200) return respond({ detail: { code: "notes_unavailable" } }, state.listStatus);
      const project = url.searchParams.get("project"), prefix = url.searchParams.get("path_prefix") ?? "";
      return respond({ items: state.items.filter(item => item.project === project && item.path.startsWith(prefix))
        .map(({ project, path, title, version, sha256, created_by, updated_by, created_at, updated_at }) =>
          ({ project, path, title, version, sha256, created_by, updated_by, created_at, updated_at })), truncated: false });
    }
    const matches = (item: PlanningItem) => item.scope === url.searchParams.get("scope") && item.project === url.searchParams.get("project")
      && item.topic === url.searchParams.get("topic");
    if (path === "/api/hub/v1/planning" && request.method() === "GET") {
      state.planningReads.push(`${url.searchParams.get("project")}/${url.searchParams.get("topic")}`);
      if (state.listStatus !== 200) return respond({ detail: { code: "planning_unavailable" } }, state.listStatus);
      const kind = url.searchParams.get("kind");
      return respond({ items: state.items.filter(item => matches(item) && (!kind || item.kind === kind))
        .map(({ content: _content, ...meta }) => meta), truncated: false });
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
const backlog = (page: Page) => page.getByRole("group", { name: "Backlog" });

test("topic planning and EH improvements stay separate; content is inert", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/planning");
  const panel = panelOf(page);
  await expect(panel.getByRole("button", { name: /Topic feature idea/ })).toBeVisible();
  await expect(panel.getByRole("button", { name: /Improve Hub search/ })).toHaveCount(0);
  await panel.getByRole("button", { name: /Topic feature idea/ }).click();
  await expect(panel.locator("article .formatted")).toContainText("<script>inert proposal</script>");
  await panel.locator("article").getByRole("button", { name: "Plain text" }).click();
  await expect(panel.locator("article pre")).toHaveText("<script>inert proposal</script>");
  await expect(panel.locator("article script")).toHaveCount(0);
  await backlog(page).getByRole("button", { name: "Hub improvements" }).click();
  const hubPanel = page.getByRole("region", { name: "Plans and ideas" });
  await expect(hubPanel.getByRole("button", { name: /Improve Hub search/ })).toBeVisible();
  await expect(hubPanel.getByRole("button", { name: /Topic feature idea/ })).toHaveCount(0);
  await expect(hubPanel.getByText("not a separate permission boundary", { exact: false })).toBeVisible();
});

test("the cross-project page can still browse another project on purpose", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/planning");
  const panel = panelOf(page);
  await panel.getByLabel("Project").selectOption("treedar");
  await expect(panel.getByRole("button", { name: /Checked tree-type map/ })).toBeVisible();
  await expect(panel.getByRole("button", { name: /Topic feature idea/ })).toHaveCount(0);
  // The Hub's own backlog is only offered for Engineering Hub.
  await expect(backlog(page)).toHaveCount(0);
});

test("create, CAS conflict and explicit merged save retain the draft", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/planning");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: "New item" }).click();
  const editor = panel.getByRole("form", { name: "New planning item" });
  await expect(editor.getByLabel("Stable ID")).toHaveCount(0);
  await editor.getByLabel("Type").selectOption("Plan");
  await editor.getByLabel("Title", { exact: true }).fill("Shared plan");
  await editor.getByLabel("Details").fill("My original plan");
  await editor.getByRole("button", { name: "Save item" }).click();
  await expect(panel.getByText("Saved version 1.", { exact: false })).toBeVisible();
  expect(state.posts[0].csrf).toBe("fixture-csrf");
  expect(state.posts[0].body).toMatchObject({ ...topic, kind: "plan", expected_version: 0 });
  expect(state.posts[0].body.item_id).toMatch(/^shared-plan-[a-f0-9]{6}$/);
  await panel.locator("article").getByRole("button", { name: "Edit", exact: true }).click();
  const editing = panel.getByRole("form", { name: "Edit planning item" });
  await editing.getByLabel("Details").fill("My retained draft");
  const stored = state.items.find(item => item.item_id === state.posts[0].body.item_id)!;
  Object.assign(stored, { content: "Agent proposal", version: 2, updated_by: "claude" });
  await editing.getByRole("button", { name: "Save item" }).click();
  await expect(editing.getByRole("alert")).toContainText("Nothing was overwritten");
  await expect(editing.getByLabel("Details")).toHaveValue("My retained draft");
  await expect(editing.locator("details pre")).toHaveText("Agent proposal");
  await expect(editing.getByRole("button", { name: "Save item" })).toBeDisabled();
  expect(stored.content).toBe("Agent proposal");
  await editing.getByRole("button", { name: "I reviewed or merged it", exact: false }).click();
  await editing.getByRole("button", { name: "Save item" }).click();
  await expect(panel.getByText("Saved version 3.", { exact: false })).toBeVisible();
  expect(state.posts.map(post => post.body.expected_version)).toEqual([0, 1, 2]);
  expect(new Set(state.posts.map(post => post.body.item_id)).size).toBe(1);
});

test("auth loss clears private planning drafts without a discard prompt", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/planning");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: /Topic feature idea/ }).click();
  await panel.locator("article").getByRole("button", { name: "Edit", exact: true }).click();
  await panel.getByLabel("Details").fill("Revoked private draft");
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

test("a changed type on a blank new draft is protected on navigation", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/planning");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: "New item" }).click();
  await panel.getByLabel("Type").selectOption("Feature");
  page.once("dialog", dialog => { void dialog.dismiss(); });
  await backlog(page).getByRole("button", { name: "Hub improvements" }).click();
  await expect(backlog(page).getByRole("button", { name: "Project plans" })).toHaveAttribute("aria-pressed", "true");
  await expect(panel.getByLabel("Type")).toHaveValue("feature");
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
  await expect(panel.getByLabel("Project")).toHaveCount(0);
});

test("another project's Plans tab shows every topic directly, with readable names and no Hub backlog", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/projects/treedar/plans");
  const panel = panelOf(page);
  // Newest topic first; both topics are read without typing a topic id.
  await expect(panel.locator(".note-link strong")).toHaveText(["Gaussian splat processing", "Checked tree-type map"]);
  await expect(panel.getByRole("button", { name: /Gaussian splat processing/ })).toContainText("Agent chat 5b9b51f9");
  await expect(panel.getByRole("button", { name: /Checked tree-type map/ })).toContainText("Treedar milestone 1");
  await expect(panel.getByText("TreeDar, all topics.", { exact: false })).toBeVisible();
  await expect(panel.getByLabel("Project")).toHaveCount(0);
  await expect(backlog(page)).toHaveCount(0);
  await expect(panel.getByLabel("Show").locator("option")).toHaveText(["Everything", "Plans", "Features", "Ideas"]);
  expect(state.planningReads).not.toContain("treedar/auto-wake-v1-test");
  await panel.getByLabel("Topic").selectOption({ label: "Treedar milestone 1" });
  await expect(panel.locator(".note-link strong")).toHaveText(["Checked tree-type map"]);
  // A single topic no longer repeats its name on every row.
  await expect(panel.getByRole("button", { name: /Checked tree-type map/ })).not.toContainText("Treedar milestone 1");
});

test("a new item can start a named topic; a project with no plans says so", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/projects/project-alpha/plans");
  const panel = panelOf(page);
  await expect(panel.getByText("No plans for Project Alpha yet.")).toBeVisible();
  await panel.getByRole("button", { name: "New item" }).click();
  const editor = panel.getByRole("form", { name: "New planning item" });
  await expect(editor.getByLabel("Topic")).toHaveValue("auto-wake-v1-test");
  await editor.getByLabel("Topic").selectOption("New topic…");
  await editor.getByLabel("New topic name").fill("milestone two");
  await editor.getByLabel("Title", { exact: true }).fill("Alpha kickoff");
  await editor.getByRole("button", { name: "Save item" }).click();
  await expect(panel.getByText("Saved version 1.", { exact: false })).toBeVisible();
  expect(state.posts[0].body).toMatchObject({ scope: "topic", project: "project-alpha", topic: "milestone-two", kind: "idea" });
  await expect(panel.getByRole("button", { name: /Alpha kickoff/ })).toBeVisible();
});

test("shipped improvements move to a Done group with a check, below open work", async ({ page }) => {
  const state = await fixture(page);
  state.items.push({ ...saved(hub, "Dynamic projects", "dynamic-projects"), status: "done" },
    { ...saved(hub, "Custom modules", "custom-modules"), status: "in_progress" });
  await page.goto("/#/planning");
  const panel = panelOf(page);
  await backlog(page).getByRole("button", { name: "Hub improvements" }).click();
  const open = panel.getByRole("region", { name: "Open" }), done = panel.getByRole("region", { name: "Done" });
  await expect(open.getByRole("button")).toHaveText([/Custom modules/, /Improve Hub search/]);
  await expect(done.getByRole("button")).toHaveText([/✓ Dynamic projects/]);
  await expect(panel.getByRole("region", { name: "Declined" })).toHaveCount(0);
});
