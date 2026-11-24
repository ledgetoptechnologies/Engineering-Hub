import { expect, test, type Page, type Request } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import type { Task } from "../src/types";

const task = (id: string, title: string, fields: Partial<Task> = {}): Task => ({
  id: `00000000-0000-4000-8000-0000000000${id}`, title, project: "engineering-hub", owner: "hermes",
  status: "claimed", revision: 3, updated_at: "2026-11-22T10:00:00Z", state_since: "2026-11-22T10:00:00Z",
  candidate_commit: null, review: null, repair_count: 0, ...fields,
});

async function fixture(page: Page, tasks: Task[], write?: (request: Request) => { json: unknown; status?: number }) {
  const overview = createDemoOverview(new Date());
  overview.tasks = tasks;
  overview.withdrawn = { tasks: tasks.filter((item) => item.status === "withdrawn"), decisions: [] };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const respond = (json: unknown, status = 200) => route.fulfill({ status, json });
    if (request.method() === "POST" && write) {
      const { json, status } = write(request);
      return respond(json, status);
    }
    if (request.method() !== "GET") return respond({}, 405);
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: `human:cf:${"e".repeat(64)}`,
      github_id: null, auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(overview);
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    return respond({ items: [] });
  });
  return overview;
}

const titles = (page: Page) => page.locator("#work .task-row > summary strong");

test("priority mode ranks unfinished work without losing filters or creation-order ties", async ({ page }) => {
  await fixture(page, [
    task("09", "Older urgent task", { priority: "urgent" }),
    task("01", "Low priority task", { priority: "low" }),
    task("08", "Legacy task"),
    task("02", "Completed urgent task", { priority: "urgent", status: "done" }),
    task("03", "Withdrawn urgent task", { priority: "urgent", status: "withdrawn", withdrawn_at: "2026-11-22T10:00:00Z" }),
    task("04", "High priority task", { priority: "high", project: "3d-viewer", owner: "codex" }),
    task("05", "Newer urgent task", { priority: "urgent", updated_at: "2026-11-20T10:00:00Z", state_since: "2026-11-20T10:00:00Z", owner: null, status: "assigned" }),
    task("06", "Explicit normal task", { priority: "normal" }),
  ]);
  await page.goto("/#/");
  await page.getByRole("link", { name: /^Work(?: \d+)?$/ }).click();
  await expect(titles(page).filter({ hasText: "Completed urgent task" })).toHaveCount(1);
  await expect(page.getByText("Withdrawn tasks (1)")).toBeVisible();
  const priority = page.getByRole("button", { name: "Priority", exact: true });
  await expect(priority).toBeVisible();
  await priority.click();
  await expect(priority).toHaveAttribute("aria-pressed", "true");
  const ranked = ["Older urgent task", "Newer urgent task", "High priority task", "Legacy task", "Explicit normal task", "Low priority task"];
  await expect(titles(page)).toHaveText(ranked);
  await expect(page.locator("#work .task-row > summary").filter({ hasText: "Legacy task" })).toContainText("Normal priority");
  await expect(page.getByText("Withdrawn tasks (1)")).toHaveCount(0);

  const filters = page.getByRole("group", { name: "Filter by state" });
  await filters.getByRole("button", { name: /Claimed/ }).click();
  await expect(titles(page)).toHaveText(ranked.filter((title) => title !== "Newer urgent task"));
  const project = page.getByRole("combobox", { name: "Project" });
  await project.selectOption("engineering-hub");
  await expect(titles(page)).toHaveText(["Older urgent task", "Legacy task", "Explicit normal task", "Low priority task"]);
  await filters.getByRole("button", { name: /^All / }).click();
  const owners = page.getByRole("region", { name: "Who's on what" });
  await owners.getByRole("button", { name: /Unclaimed/ }).click();
  await expect(titles(page)).toHaveText(["Newer urgent task"]);

  await page.reload();
  await expect(priority).toHaveAttribute("aria-pressed", "true");
  await expect(titles(page)).toHaveText(ranked);
  await priority.click();
  await expect(titles(page).filter({ hasText: "Completed urgent task" })).toHaveCount(1);
});

test("saving task priority uses both revisions and CSRF without advancing the task revision", async ({ page }) => {
  const editable = task("08", "Editable legacy task");
  const high = task("01", "Existing high priority", { priority: "high" });
  const tasks = [editable, high];
  const writes: { path: string; csrf: string; body: Record<string, unknown> }[] = [];
  await fixture(page, tasks, (request) => {
    const path = new URL(request.url()).pathname;
    const body = request.postDataJSON();
    const csrf = request.headers()["x-hub-csrf"];
    writes.push({ path, csrf, body });
    if (path !== `/api/hub/v1/tasks/${editable.id}/update` || csrf !== "fixture-csrf") return { json: {}, status: 403 };
    const priorityRevision = tasks[0].priority_revision ?? 0;
    if (body.expected_revision !== tasks[0].revision || body.expected_priority_revision !== priorityRevision)
      return { json: { detail: { code: "revision_conflict" } }, status: 409 };
    tasks[0] = { ...tasks[0], priority: body.priority, priority_revision: priorityRevision + 1 };
    return { json: tasks[0] };
  });
  await page.goto("/work");
  await page.getByRole("button", { name: "Priority", exact: true }).click();
  await expect(titles(page)).toHaveText([high.title, editable.title]);
  const row = page.locator("#work .task-row").filter({ hasText: editable.title });
  await row.locator("summary").click();
  const select = row.getByRole("combobox", { name: "Priority" });
  const save = row.getByRole("button", { name: "Save priority" });
  await expect(select).toHaveValue("normal");
  await expect(save).toBeDisabled();
  await select.selectOption("urgent");
  expect(writes).toEqual([]);
  const [request] = await Promise.all([page.waitForRequest((request) => request.method() === "POST" && request.url().endsWith("/update")), save.click()]);
  expect(request.postDataJSON()).toMatchObject({ expected_revision: 3, expected_priority_revision: 0 });
  await expect(titles(page)).toHaveText([editable.title, high.title]);
  await expect(row.locator("summary")).toContainText("Urgent priority");
  await expect(page.getByText("Task priority updated.", { exact: true })).toBeVisible();
  await expect(select).toHaveValue("urgent");
  await expect(save).toBeDisabled();

  await select.selectOption("normal");
  await save.click();
  await expect(titles(page)).toHaveText([high.title, editable.title]);
  expect(writes).toEqual([0, 1].map((priorityRevision) => ({ path: `/api/hub/v1/tasks/${editable.id}/update`,
    csrf: "fixture-csrf", body: { expected_revision: 3, expected_priority_revision: priorityRevision, priority: priorityRevision === 0 ? "urgent" : "normal",
      client_request_id: expect.stringMatching(/^.{1,128}$/) } })));
  expect(writes[0].body.client_request_id).not.toBe(writes[1].body.client_request_id);
});

for (const { changed, update, savedPriority } of [
  { changed: "task revision", update: { revision: 4 }, savedPriority: "normal" },
  { changed: "priority revision", update: { priority_revision: 1, priority: "high" }, savedPriority: "high" },
  { changed: "priority revision with the same priority label", update: { priority_revision: 2, priority: "normal" }, savedPriority: "normal" },
] as const) {
  test(`refreshing a changed ${changed} clears an unsaved priority edit`, async ({ page }) => {
    const editable = task("08", "Task changed while editing");
    const overview = await fixture(page, [editable]);
    await page.goto("/work");
    const row = page.locator("#work .task-row").filter({ hasText: editable.title });
    await row.locator("summary").click();
    const select = row.getByRole("combobox", { name: "Priority" });
    const save = row.getByRole("button", { name: "Save priority" });
    await expect(select).toHaveValue("normal");
    await select.selectOption("urgent");
    await expect(save).toBeEnabled();
    overview.tasks = [{ ...editable, ...update }];
    await page.getByRole("button", { name: "Refresh workspace" }).click();
    await expect(select).toHaveValue(savedPriority);
    await expect(save).toBeDisabled();
  });
}

test("priority retry keeps the original key and CAS pair until a newer priority revision is seen", async ({ page }) => {
  const editable = task("08", "Retry a priority save");
  const tasks = [editable];
  const writes: Record<string, unknown>[] = [];
  const receipts = new Map<string, { payload: string; result: Task }>();
  await fixture(page, tasks, (request) => {
    const body = request.postDataJSON();
    writes.push(body);
    const payload = JSON.stringify(body);
    const prior = receipts.get(body.client_request_id);
    if (prior) return prior.payload === payload ? { json: prior.result }
      : { status: 409, json: { detail: { code: "idempotency_conflict" } } };
    const current = tasks[0], priorityRevision = current.priority_revision ?? 0;
    if (body.expected_revision !== current.revision || body.expected_priority_revision !== priorityRevision)
      return { status: 409, json: { detail: { code: "revision_conflict" } } };
    tasks[0] = { ...current, priority: body.priority, priority_revision: priorityRevision + 1 };
    receipts.set(body.client_request_id, { payload, result: tasks[0] });
    // The first write commits, but its success response does not reach the browser.
    return receipts.size === 1 ? { status: 503, json: {} } : { json: tasks[0] };
  });
  await page.goto("/work");
  const row = page.locator("#work .task-row").filter({ hasText: editable.title });
  await row.locator("summary").click();
  const select = row.getByRole("combobox", { name: "Priority" });
  const save = row.getByRole("button", { name: "Save priority" });

  await select.selectOption("urgent");
  await save.click();
  await expect(page.getByText("This service is not configured or available yet.")).toBeVisible();
  await expect(select).toHaveValue("urgent");
  await expect(row.locator("summary")).toContainText("Normal priority");
  expect(writes).toEqual([{ expected_revision: 3, expected_priority_revision: 0, priority: "urgent", client_request_id: expect.any(String) }]);
  await save.click();
  await expect(row.locator("summary")).toContainText("Urgent priority");
  expect(writes).toEqual([writes[0], writes[0]]);
  expect(receipts.size).toBe(1);

  tasks[0] = { ...tasks[0], priority: "high", priority_revision: 2 };
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect(row.locator("summary")).toContainText("High priority");
  await select.selectOption("urgent");
  const [request] = await Promise.all([page.waitForRequest((request) => request.method() === "POST" && request.url().endsWith("/update")), save.click()]);
  expect(request.postDataJSON()).toMatchObject({ expected_revision: 3, expected_priority_revision: 2, priority: "urgent" });
  expect(request.postDataJSON().client_request_id).not.toBe(writes[0].client_request_id);
  await expect(row.locator("summary")).toContainText("Urgent priority");
});

test("New task sends selected priority but omits the default Normal", async ({ page }) => {
  const writes: Record<string, unknown>[] = [];
  const tasks: Task[] = [];
  await fixture(page, tasks, (request) => {
    const path = new URL(request.url()).pathname;
    if (path !== "/api/hub/v1/tasks" || request.headers()["x-hub-csrf"] !== "fixture-csrf") return { json: {}, status: 403 };
    const body = request.postDataJSON();
    writes.push(body);
    const created = task(`0${writes.length}`, body.title, { ...(body.priority ? { priority: body.priority } : {}),
      status: "assigned", revision: 1, owner: null });
    tasks.push(created);
    return { json: created, status: 201 };
  });
  await page.goto("/work");
  const dialog = page.getByRole("dialog", { name: "Assign a task" });
  const open = () => page.getByRole("button", { name: "New task" }).click();
  const fill = (title: string) => dialog.getByLabel("What needs to happen?").fill(title);
  const priority = dialog.getByRole("combobox", { name: "Priority" });
  const submit = () => dialog.getByRole("button", { name: "Assign task" }).click();

  await open();
  await expect(priority).toHaveValue("normal");
  await fill("A low priority task");
  await priority.selectOption("low");
  await submit();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator("#work .task-row > summary")).toContainText("Low priority");

  await open();
  await expect(priority).toHaveValue("normal");
  await fill("A default priority task");
  await submit();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator("#work .task-row > summary").filter({ hasText: "A default priority task" })).toContainText("Normal priority");
  expect(writes).toEqual([
    { project: "engineering-hub", title: "A low priority task", priority: "low", client_request_id: expect.any(String) },
    { project: "engineering-hub", title: "A default priority task", client_request_id: expect.any(String) },
  ]);
});

test("demo creates and reprioritizes tasks without Hub requests", async ({ page }) => {
  const hubRequests: string[] = [];
  await page.route("**/api/hub/v1/**", (route) => {
    hubRequests.push(route.request().url());
    return route.abort();
  });
  await page.goto("/work?demo=1");
  await page.getByRole("button", { name: "New task" }).click();
  const dialog = page.getByRole("dialog", { name: "Assign a task" });
  const title = "Demo task stays local";
  await dialog.getByLabel("What needs to happen?").fill(title);
  await dialog.getByRole("combobox", { name: "Priority" }).selectOption("low", { timeout: 5_000 });
  await dialog.getByRole("button", { name: "Assign task" }).click();
  await expect(dialog).toHaveCount(0);
  const row = page.locator("#work .task-row").filter({ hasText: title });
  await expect(row.locator("summary")).toContainText("Low priority");
  await row.locator("summary").click();
  await row.getByRole("combobox", { name: "Priority" }).selectOption("urgent");
  await row.getByRole("button", { name: "Save priority" }).click();
  await expect(row.locator("summary")).toContainText("Urgent priority");
  await expect(page.getByText("Task priority updated. Demo only; no server changed.")).toBeVisible();
  expect(hubRequests).toEqual([]);
});
