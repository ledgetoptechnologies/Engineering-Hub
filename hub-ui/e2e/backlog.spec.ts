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
      const body = request.postDataJSON() as Record<string, unknown>;
      state.posts.push({ path, body });
      const cleared = (id: string) => {
        const { waiting_on, ...rest } = overview.tasks.find((item) => item.id === id)!;
        overview.tasks = overview.tasks.filter((item) => item.id !== id);
        return { ...rest, status: waiting_on!.previous_status, revision: rest.revision + 1 };
      };
      if (path.endsWith("/tasks/resolve-waiting"))
        return respond({ items: (body.items as { task_id: string }[]).map((item) => cleared(item.task_id)) });
      return respond(cleared(overview.tasks.find((item) => path.includes(item.id))!.id));
    }
    if (request.method() === "GET") return respond({ items: [] });
    return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
  });
  return { overview, state };
}

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
    body: { expected_revision: 5, answer: "Answer for B" } }]);
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

const question = (id: string, text: string, revision = 2): Task => ({
  ...waitingTask(text, "2026-11-17T10:00:00+00:00", revision), id, title: `Task ${id.slice(-2)}`,
  waiting_on: { type: "decision", text, previous_status: "claimed", by: "claude", at: "2026-11-17T10:00:00+00:00",
    options: [{ label: "Keep it" }, { label: "Split it", detail: "Two PRs" }, { label: "Drop it" }],
    recommended: 1, recommended_reason: "Smaller reviews" },
});
const ids = ["11111111-2222-4333-8444-0000000000c1", "11111111-2222-4333-8444-0000000000c2",
  "11111111-2222-4333-8444-0000000000c3", "11111111-2222-4333-8444-0000000000c4", "11111111-2222-4333-8444-0000000000c5"];

test("questions are answered by option or text, dismissed, and cleared in bulk from the Inbox", async ({ page }) => {
  const { state } = await fixture(page, ids.map((id, index) => question(id, `Question ${index + 1}?`)));
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto("/#/inbox");
  const card = (text: string) => page.locator("form.waiting-item", { hasText: text });
  await expect(card("Question 1?").locator(".waiting-option.recommended")).toContainText("Split it");
  await expect(card("Question 1?")).toContainText("Smaller reviews");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await card("Question 1?").getByRole("button", { name: /Keep it/ }).click();
  await expect(card("Question 1?")).toHaveCount(0);
  await card("Question 2?").getByRole("textbox").fill("Ship it behind a flag");
  await card("Question 2?").getByRole("button", { name: "Send answer" }).click();
  await expect(card("Question 2?")).toHaveCount(0);
  await card("Question 3?").getByRole("textbox").fill("Not needed");
  await card("Question 3?").getByRole("button", { name: "Dismiss" }).click();
  await expect(card("Question 3?")).toHaveCount(0);
  await page.getByRole("checkbox", { name: "Select: Question 4?" }).check();
  await page.getByRole("checkbox", { name: "Select: Question 5?" }).check();
  await page.getByRole("group", { name: "Selected items" }).getByRole("button", { name: "Accept recommended" }).click();
  await expect(page.locator("form.waiting-item")).toHaveCount(0);
  const path = (id: string) => `/api/hub/v1/tasks/${id}/resolve-waiting`;
  expect(state.posts).toEqual([
    { path: path(ids[0]), body: { expected_revision: 2, option: 0 } },
    { path: path(ids[1]), body: { expected_revision: 2, answer: "Ship it behind a flag" } },
    { path: path(ids[2]), body: { expected_revision: 2, note: "Not needed", dismiss: true } },
    { path: "/api/hub/v1/tasks/resolve-waiting", body: { items: [
      { task_id: ids[3], expected_revision: 2, option: 1 }, { task_id: ids[4], expected_revision: 2, option: 1 }] } },
  ]);
});

test("later items stay out of the Needs you count until expanded", async ({ page }) => {
  const later = { ...question(ids[1], "Later question?"), waiting_on: { ...question(ids[1], "Later question?").waiting_on!, level: "later" as const } };
  await fixture(page, [question(ids[0], "Now question?"), later]);
  await page.goto("/#/inbox");
  await expect(page.locator("#task-items .count").first()).toHaveText("1");
  await expect(page.locator("form.waiting-item", { hasText: "Later question?" })).toHaveCount(0);
  await page.getByRole("button", { name: "Later 1" }).click();
  await expect(page.locator("form.waiting-item", { hasText: "Later question?" })).toBeVisible();
});
