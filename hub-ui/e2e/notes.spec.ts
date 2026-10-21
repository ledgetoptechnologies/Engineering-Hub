import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";

const human = "human:cf:" + "e".repeat(64);
interface Stored { project: string; path: string; title: string; content: string; version: number; updated_by: string }
function meta(note: Stored) {
  return { project: note.project, path: note.path, title: note.title, version: note.version, sha256: "c".repeat(64),
    created_by: "codex", updated_by: note.updated_by, created_at: "2026-10-20T12:00:00+00:00",
    updated_at: new Date(Date.now() - 3 * 3600_000).toISOString() };
}

async function fixture(page: Page) {
  const state = {
    notes: [
      { project: "engineering-hub", path: "runbooks/shared-notes.md", title: "Shared notes runbook",
        content: "<img src=x onerror=\"window.__pwned=1\">\nStep one\n  indented step", version: 1, updated_by: "codex" },
      { project: "treedar", path: "handoffs/benchmark.md", title: "Benchmark handoff", content: "FOR-instance results",
        version: 3, updated_by: "claude" },
    ] as Stored[],
    posts: [] as { body: Record<string, unknown>; csrf: string | undefined }[],
    listStatus: 200, truncated: false, slowProject: null as string | null,
    release: null as (() => void) | null,
  };
  await page.route("**/api/hub/v1/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(createDemoOverview());
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported",
      observer_epoch: "fixture", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    if (path === "/api/hub/v1/notes" && request.method() === "GET") {
      const project = url.searchParams.get("project")!;
      expect(request.headers()["authorization"]).toBeUndefined();
      if (state.slowProject === project) await new Promise<void>(resolve => { state.release = resolve; });
      if (state.listStatus !== 200) return respond({ detail: { code: "fixture" } }, state.listStatus);
      const query = url.searchParams.get("query")?.toLowerCase();
      const items = state.notes.filter(note => note.project === project
        && (!query || `${note.title}\n${note.content}`.toLowerCase().includes(query))).map(meta);
      return respond({ items, truncated: state.truncated });
    }
    if (path === "/api/hub/v1/notes/document") {
      const note = state.notes.find(item => item.project === url.searchParams.get("project") && item.path === url.searchParams.get("path"));
      return note ? respond({ ...meta(note), content: note.content }) : respond({ detail: { code: "note_not_found" } }, 404);
    }
    if (path === "/api/hub/v1/notes" && request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.posts.push({ body, csrf: request.headers()["x-hub-csrf"] });
      const existing = state.notes.find(item => item.project === body.project && item.path === body.path);
      const current = existing?.version ?? 0;
      if (current !== body.expected_version)
        return respond({ detail: { code: "notes_version_conflict", current_version: current } }, 409);
      const saved: Stored = { project: body.project as string, path: body.path as string, title: body.title as string,
        content: body.content as string, version: current + 1, updated_by: human };
      state.notes = [...state.notes.filter(item => item !== existing), saved];
      return respond({ ...meta(saved), content: saved.content });
    }
    return respond({}, 404);
  });
  return state;
}
const panelOf = (page: Page) => page.getByRole("region", { name: "Shared notes" });

test("notes render as inert text with metadata, counts and truncation", async ({ page }) => {
  const state = await fixture(page);
  state.truncated = true;
  await page.goto("/");
  const panel = panelOf(page);
  await expect(panel.getByText("1 note in engineering-hub.", { exact: false })).toBeVisible();
  await expect(panel.getByText("Showing the first 50.", { exact: false })).toBeVisible();
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  const view = panel.getByRole("article", { name: "Shared notes runbook" });
  await expect(view.locator("pre")).toHaveText("<img src=x onerror=\"window.__pwned=1\">\nStep one\n  indented step");
  await expect(view.locator("img")).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
  await expect(view.getByText("Updated by codex", { exact: false })).toBeVisible();
  await expect(view.getByText("(3h ago)", { exact: false }).first()).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("notes-read.png"), fullPage: true });
});

test("create then edit; a concurrent save shows both versions and replaces only on explicit choice", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: "New note" }).click();
  const editor = panel.getByRole("form", { name: "New note" });
  await editor.getByLabel("Path").fill("/bad");
  await expect(editor.getByText("Use a relative path", { exact: false })).toBeVisible();
  await expect(editor.getByRole("button", { name: "Save note" })).toBeDisabled();
  await editor.getByLabel("Path").fill("handoffs/pilot.md");
  await editor.getByLabel("Title").fill("Pilot handoff");
  await editor.getByLabel("Content").fill("Waiting on run authorization.");
  await editor.getByRole("button", { name: "Save note" }).click();
  await expect(panel.getByText('Saved "Pilot handoff" as version 1.', { exact: true })).toBeVisible();
  expect(state.posts[0]).toEqual({ csrf: "fixture-csrf", body: { project: "engineering-hub", path: "handoffs/pilot.md",
    title: "Pilot handoff", content: "Waiting on run authorization.", expected_version: 0 } });
  await expect(panel.getByText("2 notes in engineering-hub.", { exact: false })).toBeVisible();

  await panel.getByRole("article", { name: "Pilot handoff" }).getByRole("button", { name: "Edit" }).click();
  const edit = panel.getByRole("form", { name: "Edit note" });
  await expect(edit.getByLabel("Path")).toHaveAttribute("readonly", "");
  await edit.getByLabel("Content").fill("My edit");
  // An agent saves first.
  const stored = state.notes.find(note => note.path === "handoffs/pilot.md")!;
  Object.assign(stored, { content: "Agent edit", version: 2, updated_by: "codex" });
  await edit.getByRole("button", { name: "Save note" }).click();
  const conflict = edit.getByRole("alert");
  await expect(conflict.getByText("Someone saved version 2 after you opened version 1.", { exact: false })).toBeVisible();
  await expect(conflict.locator("pre")).toHaveText("Agent edit");
  await expect(edit.getByLabel("Content")).toHaveValue("My edit");
  await expect(edit.getByRole("button", { name: "Save note" })).toBeDisabled();
  expect(stored.content).toBe("Agent edit");
  await page.screenshot({ path: test.info().outputPath("notes-conflict.png"), fullPage: true });
  await conflict.getByRole("button", { name: "Replace version 2 with mine" }).click();
  await edit.getByRole("button", { name: "Save note" }).click();
  await expect(panel.getByText('Saved "Pilot handoff" as version 3.', { exact: true })).toBeVisible();
  expect(state.posts.map(post => post.body.expected_version)).toEqual([0, 1, 2]);
  expect(state.notes.find(note => note.path === "handoffs/pilot.md")!.content).toBe("My edit");
  const updated = panel.getByRole("article", { name: "Pilot handoff" }).getByTitle(human);
  await expect(updated).toHaveText("Person eeeeeeee");
});

test("unsaved edits are not dropped by opening another note or switching project", async ({ page }) => {
  const state = await fixture(page);
  state.notes.push({ project: "engineering-hub", path: "b.md", title: "Second note", content: "two", version: 1, updated_by: "codex" });
  await page.goto("/");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  await panel.getByRole("button", { name: "Edit" }).click();
  await panel.getByLabel("Content").fill("draft text");
  const prompts: string[] = [];
  page.once("dialog", dialog => { prompts.push(dialog.message()); void dialog.dismiss(); });
  await panel.getByRole("button", { name: /Second note/ }).click();
  await expect(panel.getByLabel("Content")).toHaveValue("draft text");
  page.once("dialog", dialog => { prompts.push(dialog.message()); void dialog.dismiss(); });
  await panel.getByLabel("Project").fill("treedar");
  await panel.getByRole("button", { name: "Open" }).click();
  await expect(panel.getByLabel("Project")).toHaveValue("engineering-hub");
  await expect(panel.getByLabel("Content")).toHaveValue("draft text");
  expect(prompts).toEqual(["You have unsaved note edits. Discard them?", "You have unsaved note edits. Discard them?"]);
  page.once("dialog", dialog => void dialog.accept());
  await panel.getByRole("button", { name: /Second note/ }).click();
  await expect(panel.getByRole("article", { name: "Second note" })).toBeVisible();
  expect(state.posts).toHaveLength(0);
});

test("a slow list for the previous project never replaces the current project's notes", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/");
  const panel = panelOf(page);
  await expect(panel.getByText("1 note in engineering-hub.", { exact: false })).toBeVisible();
  state.slowProject = "engineering-hub";
  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect(panel.getByText("Reading notes…", { exact: true })).toBeVisible();
  await panel.getByLabel("Project").fill("treedar");
  await panel.getByRole("button", { name: "Open" }).click();
  await expect(panel.getByRole("button", { name: /Benchmark handoff/ })).toBeVisible();
  state.release?.();
  await page.waitForTimeout(300);
  await expect(panel.getByRole("button", { name: /Shared notes runbook/ })).toHaveCount(0);
  await expect(panel.getByText("1 note in treedar.", { exact: false })).toBeVisible();
});

test("an older server without notes says so instead of showing an empty list", async ({ page }) => {
  const state = await fixture(page);
  state.listStatus = 404;
  await page.goto("/");
  const panel = panelOf(page);
  await expect(panel.getByText("This server does not have shared notes yet.", { exact: false })).toBeVisible();
  await expect(panel.getByText("No notes in", { exact: false })).toHaveCount(0);
});

test("notes authentication loss removes the private workspace", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/");
  const panel = panelOf(page);
  await expect(panel.getByRole("button", { name: /Shared notes runbook/ })).toBeVisible();
  state.listStatus = 401;
  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect(panel).toHaveCount(0);
  await expect(page.getByText("Shared notes runbook", { exact: false })).toHaveCount(0);
});
