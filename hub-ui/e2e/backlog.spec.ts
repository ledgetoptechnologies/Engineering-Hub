import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import type { Task } from "../src/types";

const human = "human:cf:" + "e".repeat(64);
const TASK = "11111111-2222-4333-8444-0000000000b1";

const waitingTask = (text: string, at: string, revision: number): Task => ({
  id: TASK, revision, updated_at: at, project: "engineering-hub", title: "Unify the backlog", status: "waiting_on_beau",
  owner: "claude", candidate_commit: null, review: null, repair_count: 0, proposed_by: "claude",
  waiting_on: { type: "decision", text, previous_status: "claimed", by: "claude", at },
});

async function fixture(page: Page, tasks: Task[]) {
  const overview = createDemoOverview(new Date());
  overview.tasks = tasks;
  const state = { posts: [] as { path: string; body: Record<string, unknown> }[] };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(overview);
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    if (path.endsWith("/resolve-waiting") && request.method() === "POST") {
      state.posts.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      const task = overview.tasks.find((item) => path.includes(item.id))!;
      const { waiting_on, ...rest } = task;
      return respond({ ...rest, status: waiting_on!.previous_status, revision: task.revision + 1 });
    }
    if (request.method() === "GET") return respond({ items: [] });
    return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
  });
  return { overview, state };
}

test("Needs you names the original requester, not the task owner", async ({ page }) => {
  const task = waitingTask("Which region?", "2026-11-16T10:00:00Z", 3);
  const { overview, state } = await fixture(page, [{ ...task, owner: "codex" }]);
  const question = overview.decisions.find((item) => item.status === "open")!;
  const requested = { ...question, requested_by: "hermes" };
  overview.decisions = [requested, { ...question, id: "11111111-2222-4333-8444-0000000000c1", title: "Legacy question" }];
  await page.goto("/#/");
  const preview = page.getByRole("region", { name: "Needs you" });
  await expect(preview.getByRole("link", { name: /Which region\?/ })).toContainText("Requested by Claude");
  await expect(preview.getByRole("link", { name: new RegExp(question.title) })).toContainText("Requested by Hermes");
  await page.goto("/#/inbox");
  await expect(page.locator("form.waiting-item")).toContainText("Requested by Claude");
  await expect(page.locator("form.waiting-item")).not.toContainText("Codex");
  const card = page.locator("form").filter({ has: page.getByRole("heading", { name: question.title, exact: true }) });
  await expect(card).toContainText("Requested by Hermes");
  await card.getByRole("button", { name: "Open full view" }).click();
  await expect(page.getByRole("dialog")).toContainText("Requested by Hermes");
  await page.keyboard.press("Escape");
  await expect(page.locator("form").filter({ hasText: "Legacy question" })).toContainText("Requester not recorded");
  expect(state.posts).toEqual([]);
});

test("a draft written for one waiting item is not sent as the answer to the next item on the same task", async ({ page }) => {
  const { overview, state } = await fixture(page, [waitingTask("Item A: which domain?", "2026-11-16T10:00:00+00:00", 3)]);
  await page.goto("/#/inbox");
  const card = page.locator("form.waiting-item");
  await expect(card).toContainText("Item A: which domain?");
  await card.getByRole("textbox").fill("Answer meant for A");
  // Elsewhere, A is answered and the owner raises B on the same task.
  overview.tasks = [waitingTask("Item B: which region?", "2026-11-16T11:00:00+00:00", 5)];
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect(card).toContainText("Item B: which region?");
  await expect(card.getByRole("textbox")).toHaveValue("");
  await expect(card.getByRole("button", { name: "Send answer" })).toBeDisabled();
  expect(state.posts).toEqual([]);
  await card.getByRole("textbox").fill("Answer for B");
  await card.getByRole("button", { name: "Send answer" }).click();
  await expect.poll(() => state.posts).toEqual([{ path: `/api/hub/v1/tasks/${TASK}/resolve-waiting`,
    body: { expected_revision: 5, note: "Answer for B" } }]);
});

test("the project board shows waiting tasks and the project's Needs you count includes them", async ({ page }) => {
  const done: Task = { ...waitingTask("x", "2026-11-16T09:00:00+00:00", 2), id: "11111111-2222-4333-8444-0000000000b2",
    title: "Finished work", status: "done", waiting_on: null, completed_by: human };
  await fixture(page, [waitingTask("Item A: which domain?", "2026-11-16T10:00:00+00:00", 3), done]);
  await page.goto("/#/projects");
  // One open demo decision plus one task item.
  const row = page.getByRole("row", { name: /Engineering Hub/ });
  await expect(row.locator("td.num").first()).toHaveText("2");
  await page.goto("/#/projects/engineering-hub/work");
  const column = page.getByRole("listitem", { name: "Waiting on Beau" });
  await expect(column).toContainText("Unify the backlog");
  await expect(page.getByRole("listitem", { name: "Done" })).toHaveCount(0);
  await expect(page.getByRole("list", { name: "Work board" })).not.toContainText("Finished work");
  await page.goto("/#/projects/engineering-hub");
  await expect(page.locator(".stat-row .stat").first()).toContainText("2");
  await expect(page.locator(".stat-row")).toContainText("Done");
  await page.goto("/#/projects/engineering-hub/decisions");
  await expect(page.locator("form.waiting-item")).toContainText("Item A: which domain?");
});
