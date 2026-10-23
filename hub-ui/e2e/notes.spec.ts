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
    listStatus: 200, truncated: false, slowProject: null as string | null, pageSize: 0, afters: [] as (string | null)[],
    release: null as (() => void) | null,
    docStatus: 200, docReads: 0, slowDocPath: null as string | null,
    releaseDoc: null as (() => void) | null,
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
      if (state.pageSize) {
        // Real path-ordered paging, like the server: continue after the given path.
        const after = url.searchParams.get("after"), prefix = url.searchParams.get("path_prefix");
        state.afters.push(after);
        const rest = items.sort((a, b) => a.path.localeCompare(b.path))
          .filter(item => (!after || item.path > after) && (!prefix || item.path.startsWith(prefix + "/")));
        return respond({ items: rest.slice(0, state.pageSize), truncated: rest.length > state.pageSize });
      }
      return respond({ items, truncated: state.truncated });
    }
    if (path === "/api/hub/v1/notes/document") {
      state.docReads++;
      const requested = url.searchParams.get("path");
      if (state.slowDocPath === requested) {
        state.slowDocPath = null;
        await new Promise<void>(resolve => { state.releaseDoc = resolve; });
      }
      if (state.docStatus !== 200) return respond({ detail: { code: "notes_unavailable" } }, state.docStatus);
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
// Today's gateway has no client-route fallback (#/ addresses); the dev server has one (real paths).
const MODES = ["path", "hash"] as const;
async function addressMode(page: Page, mode: (typeof MODES)[number]) {
  if (mode === "hash") await page.route(/\/projects$/, route => route.fulfill({ status: 403, contentType: "application/json",
    body: JSON.stringify({ detail: { code: "gateway_route_forbidden" } }) }));
}
async function expectMode(page: Page, mode: (typeof MODES)[number]) {
  if (mode === "hash") expect(page.url()).toContain("#/");
  else expect(page.url()).not.toContain("#");
}

for (const mode of MODES) test(`dirty note navigation can be cancelled or explicitly discarded (${mode} addresses)`, async ({ page }) => {
  const state = await fixture(page);
  await addressMode(page, mode);
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  await panel.getByRole("button", { name: "Edit", exact: true }).click();
  await panel.getByLabel("Content").fill("unsaved route draft");
  for (const link of [
    page.getByRole("navigation", { name: "Workspace navigation" }).getByRole("link", { name: "Home", exact: true }),
    page.getByRole("navigation", { name: "Projects", exact: true }).getByRole("link", { name: "TreeDar", exact: true }),
  ]) {
    page.once("dialog", dialog => { void dialog.dismiss(); });
    await link.click();
    await expect(page).toHaveURL(/\/(#\/)?notes$/);
    await expect(panel.getByLabel("Content")).toHaveValue("unsaved route draft");
  }
  page.once("dialog", dialog => { void dialog.accept(); });
  await page.getByRole("navigation", { name: "Workspace navigation" }).getByRole("link", { name: "Home", exact: true }).click();
  await expect(page).toHaveURL(/:\d+\/(#\/)?$/);
  await page.getByRole("navigation", { name: "Projects", exact: true }).getByRole("link", { name: "Engineering Hub", exact: true }).click();
  await page.getByRole("navigation", { name: "Engineering Hub sections" }).getByRole("link", { name: "Notes & plans" }).click();
  await expect(panel.getByLabel("Content")).toHaveCount(0);
  await expectMode(page, mode);
  expect(state.posts).toHaveLength(0);
});

for (const mode of MODES) test(`cancelled browser Back and Forward preserve both the dirty draft and history (${mode} addresses)`, async ({ page }) => {
  const state = await fixture(page);
  await addressMode(page, mode);
  await page.goto("/#/");
  // Notes live inside the project: Projects → Engineering Hub → Notes & plans.
  await page.getByRole("navigation", { name: "Projects", exact: true }).getByRole("link", { name: "Engineering Hub", exact: true }).click();
  await page.getByRole("navigation", { name: "Engineering Hub sections" }).getByRole("link", { name: "Notes & plans" }).click();
  const panel = panelOf(page);
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  await panel.getByRole("button", { name: "Edit", exact: true }).click();
  await panel.getByLabel("Content").fill("back history draft");
  page.once("dialog", dialog => { void dialog.dismiss(); });
  await page.goBack();
  await expect(page).toHaveURL(/\/(#\/)?notes$/);
  await expect(panel.getByLabel("Content")).toHaveValue("back history draft");
  page.once("dialog", dialog => { void dialog.accept(); });
  await page.goBack();
  await expect(page).toHaveURL(/\/(#\/)?projects\/engineering-hub$/);
  await page.goForward();
  await expect(page).toHaveURL(/\/(#\/)?notes$/);
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  await panel.getByRole("button", { name: "Edit", exact: true }).click();
  await panel.getByLabel("Content").fill("forward history draft");
  // Add a following page, then return without leaving a dirty editor behind.
  page.once("dialog", dialog => { void dialog.accept(); });
  await page.getByRole("navigation", { name: "Workspace navigation" }).getByRole("link", { name: "Home", exact: true }).click();
  await page.goBack();
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  await panel.getByRole("button", { name: "Edit", exact: true }).click();
  await panel.getByLabel("Content").fill("forward preserved draft");
  page.once("dialog", dialog => { void dialog.dismiss(); });
  await page.goForward();
  await expect(page).toHaveURL(/\/(#\/)?notes$/);
  await expect(panel.getByLabel("Content")).toHaveValue("forward preserved draft");
  page.once("dialog", dialog => { void dialog.accept(); });
  await page.goForward();
  await expect(page).toHaveURL(/:\d+\/(#\/)?$/);
  await expectMode(page, mode);
  expect(state.posts).toHaveLength(0);
});

test("authorization loss clears a dirty note without a discard prompt", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  await panel.getByRole("button", { name: "Edit", exact: true }).click();
  await panel.getByLabel("Content").fill("private revoked draft");
  const dialogs: string[] = [];
  page.on("dialog", dialog => { dialogs.push(dialog.type()); void dialog.dismiss(); });
  state.listStatus = 401;
  await panel.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByRole("link", { name: /Sign in securely/ })).toBeVisible();
  await expect(page.getByText("private revoked draft", { exact: true })).toHaveCount(0);
  await expect(panel).toHaveCount(0);
  expect(dialogs).toEqual([]);
  expect(state.posts).toHaveLength(0);
});

for (const mode of MODES) test(`project-to-project navigation protects the project's note draft (${mode} addresses)`, async ({ page }) => {
  const state = await fixture(page);
  await addressMode(page, mode);
  await page.goto("/#/projects/engineering-hub/notes");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  await panel.getByRole("button", { name: "Edit", exact: true }).click();
  await panel.getByLabel("Content").fill("project scoped unsaved draft");
  const nextProject = page.getByRole("navigation", { name: "Projects", exact: true }).getByRole("link", { name: "TreeDar", exact: true });
  page.once("dialog", dialog => { void dialog.dismiss(); });
  await nextProject.click();
  await expect(page).toHaveURL(/\/(#\/)?projects\/engineering-hub\/notes$/);
  await expect(panel.getByLabel("Content")).toHaveValue("project scoped unsaved draft");
  page.once("dialog", dialog => { void dialog.accept(); });
  await nextProject.click();
  await expect(page).toHaveURL(/\/(#\/)?projects\/treedar$/);
  await page.getByRole("navigation", { name: "TreeDar sections" }).getByRole("link", { name: "Notes & plans" }).click();
  await expect(panel.getByRole("button", { name: /Benchmark handoff/ })).toBeVisible();
  await expect(panel.getByLabel("Content")).toHaveCount(0);
  await expectMode(page, mode);
  expect(state.posts).toHaveLength(0);
});

test("notes render as inert text with metadata, counts and truncation", async ({ page }) => {
  const state = await fixture(page);
  state.truncated = true;
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await expect(panel.getByText("1 note in engineering-hub.", { exact: false })).toBeVisible();
  await expect(panel.getByText("More notes below the list.", { exact: false })).toBeVisible();
  await expect(panel.getByRole("button", { name: "Show more notes" })).toBeVisible();
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  const view = panel.getByRole("article", { name: "Shared notes runbook" });
  // Formatted by default: raw HTML in a note is text, never markup.
  await expect(view.locator(".note-content.formatted")).toContainText("<img src=x onerror=");
  await expect(view.locator("img")).toHaveCount(0);
  await view.getByRole("button", { name: "Plain text" }).click();
  await expect(view.locator("pre")).toHaveText("<img src=x onerror=\"window.__pwned=1\">\nStep one\n  indented step");
  await expect(view.locator("img")).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
  await expect(view.getByText("Updated by codex", { exact: false })).toBeVisible();
  await expect(view.getByText("(3h ago)", { exact: false }).first()).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("notes-read.png"), fullPage: true });
});

test("create then edit; a concurrent save shows both versions and replaces only on explicit choice", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: "New note" }).click();
  const editor = panel.getByRole("form", { name: "New note" });
  // No typed paths: pick a folder and a file name; the name follows the title until edited.
  await editor.getByLabel("Title").fill("Pilot handoff");
  await expect(editor.getByLabel("File name")).toHaveValue("pilot-handoff");
  await expect(editor.getByText("pilot-handoff.md", { exact: true })).toBeVisible();
  await editor.getByLabel("Folder").selectOption("New folder…");
  await editor.getByLabel("New folder name").fill("Handoffs");
  await editor.getByLabel("File name").fill("");
  await expect(editor.getByRole("button", { name: "Save note" })).toBeDisabled();
  await editor.getByLabel("File name").fill("pilot");
  await expect(editor.getByText("handoffs/pilot.md", { exact: true })).toBeVisible();
  await editor.getByLabel("Content").fill("Waiting on run authorization.");
  await editor.getByRole("button", { name: "Save note" }).click();
  await expect(panel.getByText('Saved "Pilot handoff" as version 1.', { exact: true })).toBeVisible();
  expect(state.posts[0]).toEqual({ csrf: "fixture-csrf", body: { project: "engineering-hub", path: "handoffs/pilot.md",
    title: "Pilot handoff", content: "Waiting on run authorization.", expected_version: 0 } });
  await expect(panel.getByText("2 notes in engineering-hub.", { exact: false })).toBeVisible();

  await panel.getByRole("article", { name: "Pilot handoff" }).getByRole("button", { name: "Edit" }).click();
  const edit = panel.getByRole("form", { name: "Edit note" });
  await expect(edit.getByLabel("Location")).toHaveAttribute("readonly", "");
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
  // The fixture viewer is this same Cloudflare human, so their own edit reads "You".
  await expect(updated).toHaveText("You");
});

test("unsaved edits are not dropped by opening another note or switching project", async ({ page }) => {
  const state = await fixture(page);
  state.notes.push({ project: "engineering-hub", path: "b.md", title: "Second note", content: "two", version: 1, updated_by: "codex" });
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  await panel.getByRole("button", { name: "Edit" }).click();
  await panel.getByLabel("Content").fill("draft text");
  const prompts: string[] = [];
  page.once("dialog", dialog => { prompts.push(dialog.message()); void dialog.dismiss(); });
  await panel.getByRole("button", { name: /Second note/ }).click();
  await expect(panel.getByLabel("Content")).toHaveValue("draft text");
  page.once("dialog", dialog => { prompts.push(dialog.message()); void dialog.dismiss(); });
  await panel.getByLabel("Project").selectOption("treedar");
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
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await expect(panel.getByText("1 note in engineering-hub.", { exact: false })).toBeVisible();
  state.slowProject = "engineering-hub";
  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect(panel.getByText("Reading notes…", { exact: true })).toBeVisible();
  await panel.getByLabel("Project").selectOption("treedar");
  await expect(panel.getByRole("button", { name: /Benchmark handoff/ })).toBeVisible();
  state.release?.();
  await page.waitForTimeout(300);
  await expect(panel.getByRole("button", { name: /Shared notes runbook/ })).toHaveCount(0);
  await expect(panel.getByText("1 note in treedar.", { exact: false })).toBeVisible();
});

test("an older server without notes says so instead of showing an empty list", async ({ page }) => {
  const state = await fixture(page);
  state.listStatus = 404;
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await expect(panel.getByText("This server does not have shared notes yet.", { exact: false })).toBeVisible();
  await expect(panel.getByText("No notes in", { exact: false })).toHaveCount(0);
});

test("notes authentication loss removes the private workspace", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await expect(panel.getByRole("button", { name: /Shared notes runbook/ })).toBeVisible();
  state.listStatus = 401;
  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect(panel).toHaveCount(0);
  await expect(page.getByText("Shared notes runbook", { exact: false })).toHaveCount(0);
});

test("a new-note path collision can never be applied to a different existing note", async ({ page }) => {
  const state = await fixture(page);
  state.notes.push({ project: "engineering-hub", path: "b.md", title: "Note B", content: "B unseen", version: 1, updated_by: "codex" });
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: "New note" }).click();
  const editor = panel.getByRole("form", { name: "New note" });
  await editor.getByLabel("Title").fill("Mine");
  await editor.getByLabel("Folder").selectOption("runbooks");
  await editor.getByLabel("File name").fill("shared-notes");
  await editor.getByLabel("Content").fill("my text");
  await editor.getByRole("button", { name: "Save note" }).click();
  const conflict = editor.getByRole("alert");
  await expect(conflict.getByText("A note already exists at this path (version 1).", { exact: false })).toBeVisible();
  // Before Replace: the path cannot be retargeted to B.
  await expect(editor.getByLabel("Location")).toHaveAttribute("readonly", "");
  await conflict.getByRole("button", { name: "Replace version 1 with mine" }).click();
  // After Replace: still locked, and the accepted version names this path only.
  await expect(editor.getByLabel("Location")).toHaveAttribute("readonly", "");
  await expect(editor.getByText("Saving replaces version 1 of", { exact: false })).toBeVisible();
  await editor.getByLabel("Location").press("End");
  await editor.getByLabel("Location").pressSequentially("x");
  await expect(editor.getByLabel("Location")).toHaveValue("runbooks/shared-notes.md");
  page.once("dialog", dialog => void dialog.accept());
  await editor.getByRole("button", { name: "Cancel" }).click();
  // A fresh new note at B starts from version 0 and conflicts instead of overwriting.
  await panel.getByRole("button", { name: "New note" }).click();
  const second = panel.getByRole("form", { name: "New note" });
  await second.getByLabel("Title").fill("Mine");
  await second.getByLabel("File name").fill("b");
  await second.getByRole("button", { name: "Save note" }).click();
  await expect(second.getByRole("alert").getByText("A note already exists at this path (version 1).", { exact: false })).toBeVisible();
  expect(state.posts.map(post => [post.body.path, post.body.expected_version])).toEqual([["runbooks/shared-notes.md", 0], ["b.md", 0]]);
  expect(state.notes.find(note => note.path === "b.md")!.content).toBe("B unseen");
  expect(state.notes.find(note => note.path === "runbooks/shared-notes.md")!.version).toBe(1);
});

test("Replace then save writes only the accepted path at the accepted version", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: "New note" }).click();
  const editor = panel.getByRole("form", { name: "New note" });
  await editor.getByLabel("Title").fill("Mine");
  await editor.getByLabel("Folder").selectOption("runbooks");
  await editor.getByLabel("File name").fill("shared-notes");
  await editor.getByLabel("Content").fill("my text");
  await editor.getByRole("button", { name: "Save note" }).click();
  await editor.getByRole("alert").getByRole("button", { name: "Replace version 1 with mine" }).click();
  await editor.getByRole("button", { name: "Save note" }).click();
  await expect(panel.getByText('Saved "Mine" as version 2.', { exact: true })).toBeVisible();
  expect(state.posts.map(post => [post.body.path, post.body.expected_version])).toEqual([["runbooks/shared-notes.md", 0], ["runbooks/shared-notes.md", 1]]);
});

test("notes over the gateway request budget are blocked locally and a 413 keeps the draft", async ({ page }) => {
  const state = await fixture(page);
  await page.route("**/api/hub/v1/notes", async route => route.request().method() === "POST"
    ? route.fulfill({ status: 413, contentType: "application/json", body: JSON.stringify({ detail: { code: "gateway_body_too_large" } }) })
    : route.fallback());
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: "New note" }).click();
  const editor = panel.getByRole("form", { name: "New note" });
  await editor.getByLabel("Title").fill("Big");
  await expect(editor.getByText("big.md", { exact: true })).toBeVisible();
  // 40,000 quote marks are 40 KB of content but 80 KB once JSON-escaped.
  await editor.getByLabel("Content").fill('"'.repeat(40000));
  await expect(editor.getByText("Too large to save.", { exact: false })).toBeVisible();
  await expect(editor.getByRole("button", { name: "Save note" })).toBeDisabled();
  await editor.getByLabel("Content").fill("small enough");
  await editor.getByRole("button", { name: "Save note" }).click();
  await expect(panel.getByText("This note is too large for the Hub to accept.", { exact: false })).toBeVisible();
  await expect(editor.getByLabel("Content")).toHaveValue("small enough");
  expect(state.notes.find(note => note.path === "big.md")).toBeUndefined();
});

function bump(state: Awaited<ReturnType<typeof fixture>>, path: string, content: string) {
  const note = state.notes.find(item => item.path === path)!;
  Object.assign(note, { content, version: note.version + 1, updated_by: "codex" });
}

test("Refresh and re-opening the open note both re-read its current version", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  const view = panel.getByRole("article", { name: "Shared notes runbook" });
  await expect(view.locator(".item-overline")).toContainText("v1");
  bump(state, "runbooks/shared-notes.md", "Saved by an agent as v2");
  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect(view.locator(".note-content")).toHaveText("Saved by an agent as v2");
  await expect(view.locator(".item-overline")).toContainText("v2");
  await expect(panel.getByRole("button", { name: /Shared notes runbook/ })).toContainText("v2");
  bump(state, "runbooks/shared-notes.md", "Saved again as v3");
  const reads = state.docReads;
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  await expect(view.locator(".note-content")).toHaveText("Saved again as v3");
  expect(state.docReads).toBe(reads + 1);
  expect(state.posts).toHaveLength(0);
});

test("Refresh while editing keeps the draft and still blocks a silent overwrite", async ({ page }) => {
  const state = await fixture(page);
  const dialogs: string[] = [];
  page.on("dialog", dialog => { dialogs.push(dialog.message()); void dialog.dismiss(); });
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  await panel.getByRole("button", { name: "Edit" }).click();
  const editor = panel.getByRole("form", { name: "Edit note" });
  await editor.getByLabel("Content").fill("my unsaved draft");
  bump(state, "runbooks/shared-notes.md", "Agent v2");
  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => state.docReads).toBeGreaterThan(1);
  await expect(editor.getByLabel("Content")).toHaveValue("my unsaved draft");
  expect(dialogs).toEqual([]);
  await editor.getByRole("button", { name: "Save note" }).click();
  await expect(editor.getByRole("alert").getByText("Someone saved version 2 after you opened version 1.", { exact: false })).toBeVisible();
  expect(state.notes.find(note => note.path === "runbooks/shared-notes.md")!.content).toBe("Agent v2");
});

test("a slow re-read of the previous note never replaces the newly opened note", async ({ page }) => {
  const state = await fixture(page);
  state.notes.push({ project: "engineering-hub", path: "b.md", title: "Second note", content: "B body", version: 1, updated_by: "codex" });
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  await expect(panel.getByRole("article", { name: "Shared notes runbook" })).toBeVisible();
  bump(state, "runbooks/shared-notes.md", "late A body");
  state.slowDocPath = "runbooks/shared-notes.md";
  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => state.releaseDoc !== null).toBe(true);
  await panel.getByRole("button", { name: /Second note/ }).click();
  await expect(panel.getByRole("article", { name: "Second note" }).locator(".note-content")).toHaveText("B body");
  state.releaseDoc!();
  await page.waitForTimeout(300);
  await expect(panel.getByRole("article", { name: "Second note" })).toBeVisible();
  await expect(panel.getByText("late A body", { exact: true })).toHaveCount(0);
});

test("a failed re-read keeps the note but says it may be out of date", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  const view = panel.getByRole("article", { name: "Shared notes runbook" });
  await expect(view.locator(".item-overline")).toContainText("v1");
  state.docStatus = 503;
  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect(panel.getByText("The note shown may be out of date.", { exact: false })).toBeVisible();
  await expect(view.locator(".item-overline")).toContainText("v1");
});

test("a successful re-read clears only the out-of-date warning", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/notes");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: /Shared notes runbook/ }).click();
  const view = panel.getByRole("article", { name: "Shared notes runbook" });
  await expect(view.locator(".item-overline")).toContainText("v1");
  state.docStatus = 503;
  await panel.getByRole("button", { name: "Refresh" }).click();
  const warning = panel.getByText("The note shown may be out of date.", { exact: false });
  await expect(warning).toBeVisible();
  state.docStatus = 200;
  bump(state, "runbooks/shared-notes.md", "Recovered v2");
  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect(view.locator(".note-content")).toHaveText("Recovered v2");
  await expect(warning).toHaveCount(0);
  // A save notice is not a read warning and survives a later re-read.
  await view.getByRole("button", { name: "Edit" }).click();
  await panel.getByLabel("Content").fill("Mine v3");
  await panel.getByRole("button", { name: "Save note" }).click();
  const saved = panel.getByText('Saved "Shared notes runbook" as version 3.', { exact: true });
  await expect(saved).toBeVisible();
  const reads = state.docReads;
  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => state.docReads).toBe(reads + 1);
  await expect(saved).toBeVisible();
});

test("the note list scrolls as one area even with the pointer over a note", async ({ page }) => {
  const state = await fixture(page);
  for (let i = 0; i < 40; i++) state.notes.push({ project: "engineering-hub", path: `runbooks/item-${String(i).padStart(2, "0")}.md`,
    title: `Runbook item ${i}`, content: "x", version: 1, updated_by: "codex" });
  await page.setViewportSize({ width: 1400, height: 800 });
  await page.goto("/#/notes");
  const list = panelOf(page).locator(".notes-list");
  await panelOf(page).getByRole("button", { name: /Runbook item 3 / }).hover();
  const before = await list.evaluate(element => element.scrollTop);
  await page.mouse.wheel(0, 600);
  await expect.poll(() => list.evaluate(element => element.scrollTop)).toBeGreaterThan(before);
});

test("Show more notes continues the list after the last path until everything is shown", async ({ page }) => {
  const state = await fixture(page);
  const base = state.notes[0];
  state.notes = Array.from({ length: 5 }, (_, i) => ({ ...base, path: `plans/note-${i}.md`, title: `Plan ${i}` }));
  state.pageSize = 2;
  await page.goto("/#/notes");
  const panel = panelOf(page);
  const titles = panel.locator(".notes-list .note-link strong");
  await expect(titles).toHaveText(["Plan 0", "Plan 1"]);
  await panel.getByRole("button", { name: "Show more notes" }).click();
  await expect(titles).toHaveText(["Plan 0", "Plan 1", "Plan 2", "Plan 3"]);
  await panel.getByRole("button", { name: "Show more notes" }).click();
  await expect(titles).toHaveText(["Plan 0", "Plan 1", "Plan 2", "Plan 3", "Plan 4"]);
  await expect(panel.getByRole("button", { name: "Show more notes" })).toHaveCount(0);
  expect(state.afters).toEqual([null, "plans/note-1.md", "plans/note-3.md"]);
});

test("searching after Show more starts the list over from the first page", async ({ page }) => {
  const state = await fixture(page);
  const base = state.notes[0];
  state.notes = [...Array.from({ length: 3 }, (_, i) => ({ ...base, path: `plans/note-${i}.md`, title: `Plan ${i}` })),
    ...Array.from({ length: 3 }, (_, i) => ({ ...base, path: `runbooks/step-${i}.md`, title: `Step ${i}` }))];
  state.pageSize = 2;
  await page.goto("/#/notes");
  const panel = panelOf(page);
  const titles = panel.locator(".notes-list .note-link strong");
  await panel.getByRole("button", { name: "Show more notes" }).click();
  await expect(titles).toHaveText(["Plan 0", "Plan 1", "Plan 2", "Step 0"]);
  await panel.locator("select").filter({ hasText: "Path starts with" }).selectOption("prefix");
  await panel.getByRole("searchbox", { name: "Search notes" }).fill("runbooks");
  await panel.getByRole("button", { name: "Search", exact: true }).click();
  await expect(titles).toHaveText(["Step 0", "Step 1"]);
  expect(state.afters.at(-1)).toBeNull();
  await panel.getByRole("button", { name: "Show more notes" }).click();
  await expect(titles).toHaveText(["Step 0", "Step 1", "Step 2"]);
  expect(state.afters.at(-1)).toBe("runbooks/step-1.md");
});
