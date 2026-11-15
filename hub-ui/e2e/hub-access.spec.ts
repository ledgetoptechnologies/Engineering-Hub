import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import wire from "../src/fixtures/agent-hub-bridge-wire-v1.json" with { type: "json" };

const human = "human:cf:" + "e".repeat(64);
const iso = (offset: number) => new Date(Date.now() + offset).toISOString();
const INSTALL = wire.active_grant.installation_id;
const OLD_SESSION = wire.recoverable_claims.items[0].claim_session_id;
const NEW_SESSION = wire.recoverable_claims.items[0].recovery.target_session_ids[0];
const TASK = wire.recovery.task.id;

async function fixture(page: Page, options: { catalog?: boolean } = {}) {
  const state = {
    grant: structuredClone(wire.absent_grant) as Record<string, unknown>,
    claims: structuredClone(wire.recoverable_claims),
    posts: [] as { path: string; method: string; body: Record<string, unknown> }[],
    failNext: null as null | { status: number; code: string } | "abort",
    recovered: false,
    extraTasks: [] as Record<string, unknown>[],
    tracking: "missing" as "missing" | "ok" | "unauthenticated" | "forbidden",
    extraProjects: 0, holdGrant: false, grantReleases: [] as (() => void)[],
  };
  await page.route("**/api/hub/v1/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null, auth_provider: "cloudflare_access",
      email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) {
      const overview = createDemoOverview();
      overview.tasks.push(...(state.extraTasks as never[]));
      for (let index = 1; index <= state.extraProjects; index++)
        overview.projects.push({ ...overview.projects[0], id: `extra-project-${index}`, name: `Extra project ${index}` });
      return respond(overview);
    }
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    if (path.endsWith("/projects/engineering-hub/tracking-tasks") && state.tracking !== "missing") {
      if (state.tracking === "unauthenticated") return respond({ detail: { code: "human_session_required" } }, 401);
      if (state.tracking === "forbidden") return respond({ detail: { code: "forbidden" } }, 403);
      return respond({ project: "engineering-hub", tasks: [{ id: "33333333-4444-4555-8666-777777777777",
        project: "engineering-hub", title: "Private tracked progress", status: "open", revision: 1,
        created_by: "codex", execution_authorization: false, updates: [] }], truncated: false, next_after: null });
    }
    const prefix = "/api/hub/v1/agent-connections";
    if (!path.startsWith(prefix)) return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
    const rest = path.slice(prefix.length);
    if (request.method() === "GET") {
      if (rest === "/capabilities") return respond({ enabled: true, environment: "staging", owner: true });
      if (rest === "/hub-capabilities") return options.catalog === false ? respond({ detail: { code: "gateway_route_forbidden" } }, 403) : respond(wire.catalog);
      if (rest === "/installations") return respond({ installations: [{ id: INSTALL, family: "codex", name: "Codex on the desk PC", host_label: "desk-pc",
        state: "active", reason: null, created_at: iso(-3_600_000), approved_at: iso(-3_000_000), approved_by: human, revoked_at: null, revoked_by: null,
        expires_at: iso(80_000_000), revision: 2, pairing_code: null, key_fingerprint: "18fab9cd3f43991e" }] });
      if (rest === "/audit") return respond({ events: [] });
      if (rest === `/installations/${INSTALL}/sessions`) return respond({ sessions: [
        { id: OLD_SESSION, installation_id: INSTALL, label: "old chat", issued_at: iso(-900_000), expires_at: iso(-60_000), last_used_at: null, state: "revoked", revision: 2 },
        { id: NEW_SESSION, installation_id: INSTALL, label: "new chat", issued_at: iso(-60_000), expires_at: iso(600_000), last_used_at: null, state: "active", revision: 1 }] });
      if (rest === `/installations/${INSTALL}/hub-grant`) {
        // What the server had when asked; a held read answers later with that older view.
        const body = structuredClone(state.grant);
        if (state.holdGrant) { state.holdGrant = false; await new Promise<void>(resolve => state.grantReleases.push(resolve)); }
        return respond(body);
      }
      if (rest === `/installations/${INSTALL}/task-claims`) return respond(state.claims);
      if (rest === `/tasks/${TASK}/claim-recoveries`) return respond(state.recovered ? wire.recovery_history : { task: wire.recoverable_claims.items[0].task, recoveries: [] });
      return respond({}, 404);
    }
    const body = request.postDataJSON() as Record<string, unknown>;
    state.posts.push({ path: rest, method: request.method(), body });
    if (state.failNext) {
      const failure = state.failNext; state.failNext = null;
      if (failure === "abort") return route.abort();
      return respond({ detail: { code: failure.code } }, failure.status);
    }
    if (rest === `/installations/${INSTALL}/hub-grant`) {
      if (body.expected_revision !== state.grant.revision) return respond({ detail: { code: "grant_revision_conflict" } }, 409);
      const expires = iso(Number(body.lifetime_seconds) * 1000);
      state.grant = { ...structuredClone(wire.active_grant), revision: Number(state.grant.revision) + 1, projects: body.projects, capabilities: body.capabilities, expires_at: expires,
        effective: { ...wire.active_grant.effective, projects: body.projects, capabilities: body.capabilities, expires_at: expires } };
      return respond(state.grant);
    }
    if (rest === `/installations/${INSTALL}/hub-grant/revoke`) { state.grant = structuredClone(wire.revoked_grant); return respond(state.grant); }
    if (rest === `/tasks/${TASK}/recover-claim`) {
      const replay = state.recovered; state.recovered = true;
      return respond(replay ? wire.recovery_replay : wire.recovery);
    }
    return respond({}, 404);
  });
  return state;
}
const panel = (page: Page) => page.getByRole("region", { name: "Installations", exact: true });
const access = (page: Page) => page.getByRole("group", { name: "Hub access for Codex on the desk PC" });
async function openAccess(page: Page) {
  await page.goto("/#/agents/installations");
  const row = panel(page).locator(".installation-row", { hasText: "desk-pc" });
  // Session labels help name the claim's sessions.
  await row.getByRole("button", { name: "Show sessions" }).click();
  await expect(row.getByRole("table")).toBeVisible();
  await row.getByRole("button", { name: "Hub access" }).click();
  await expect(access(page)).toBeVisible();
  return row;
}

test("the Hub access section is only offered when the backend has it", async ({ page }) => {
  await fixture(page, { catalog: false });
  await page.goto("/#/agents/installations");
  await expect(panel(page).locator(".installation-row")).toHaveCount(1);
  await expect(panel(page).getByRole("button", { name: "Hub access" })).toHaveCount(0);
});

test("an approved connection starts with no Hub access, and granting needs explicit projects and rights", async ({ page }) => {
  const state = await fixture(page);
  await openAccess(page);
  await expect(access(page)).toContainText("No Hub access. Approving a connection doesn't grant any.");
  await access(page).getByRole("button", { name: "Grant access…" }).click();
  const form = access(page).getByRole("form", { name: "Hub access choices" });
  const save = form.getByRole("button", { name: "Save access" });
  await expect(save).toBeDisabled();
  await form.getByLabel("Engineering Hub").check();
  await expect(save).toBeDisabled();
  await form.getByLabel("Claim tasks").check();
  await form.getByLabel("Read tasks").check();
  await form.getByLabel(/^Lasts for/).selectOption({ label: "1 hour" });
  await save.click();
  await expect(access(page).getByRole("status")).toContainText("Hub access saved.");
  expect(state.posts[0]).toEqual({ path: `/installations/${INSTALL}/hub-grant`, method: "PUT",
    body: { expected_revision: 0, projects: ["engineering-hub"], capabilities: ["task.claim", "task.read"], lifetime_seconds: 3600 } });
  await expect(access(page)).toContainText("Hub access in effect.");
  await expect(access(page).locator(".hub-access-rights")).toContainText("Claim tasks");
});

test("removing all access is a confirmed revoke that keeps the earlier choices visible", async ({ page }) => {
  const state = await fixture(page);
  state.grant = structuredClone(wire.active_grant);
  await openAccess(page);
  await access(page).getByRole("button", { name: "Remove all access…" }).click();
  const dialog = page.getByRole("dialog", { name: /Remove all Hub access/ });
  await expect(dialog).toContainText("Its connection stays approved");
  await dialog.getByRole("button", { name: "Remove access" }).click();
  await expect(access(page)).toContainText("No Hub access: you removed it.");
  await expect(access(page)).toContainText("Earlier choices (not in effect): Engineering Hub");
  expect(state.posts).toEqual([{ path: `/installations/${INSTALL}/hub-grant/revoke`, method: "POST", body: { expected_revision: 1 } }]);
});

test("an inactive-connection refusal is explained, not treated as a lost sign-in", async ({ page }) => {
  const state = await fixture(page);
  await openAccess(page);
  await access(page).getByRole("button", { name: "Grant access…" }).click();
  const form = access(page).getByRole("form", { name: "Hub access choices" });
  await form.getByLabel("Engineering Hub").check();
  await form.getByLabel("Read tasks").check();
  state.failNext = { status: 401, code: "installation_inactive" };
  await form.getByRole("button", { name: "Save access" }).click();
  await expect(access(page).getByRole("alert")).toContainText("This connection isn't active");
  await expect(page.getByRole("link", { name: /Sign in securely/ })).toHaveCount(0);
  await expect(panel(page)).toBeVisible();
});

test("a stranded claim is handed over only after reason and confirmation, with one request id per intent", async ({ page }) => {
  const state = await fixture(page);
  await openAccess(page);
  const claims = access(page).getByRole("region", { name: "Tasks claimed by Codex on the desk PC" });
  await expect(claims).toContainText("held by old chat");
  await claims.getByRole("button", { name: "Hand over…" }).click();
  const dialog = page.getByRole("dialog", { name: /Hand over “Transport task”/ });
  await expect(dialog).toContainText("Kept: aaaaaaaaaaaa");
  await expect(dialog).toContainText("The current review (approved by Claude on install 7a207d88) is cleared");
  const submit = dialog.getByRole("button", { name: "Hand over", exact: true });
  await expect(submit).toBeDisabled();
  await dialog.getByLabel("Reason (kept with the task's history)").fill("The old session's PC was rebooted");
  await expect(submit).toBeDisabled();
  await dialog.getByLabel("I understand the review is cleared").check();
  // No answer the first time: the page says it may or may not have happened, and a retry reuses the same request id.
  state.failNext = "abort";
  await submit.click();
  await expect(dialog.getByRole("alert")).toContainText("may or may not have happened");
  await submit.click();
  await expect(access(page).getByRole("status")).toContainText("Handed “Transport task” to new chat");
  expect(state.posts.map(post => post.path)).toEqual([`/tasks/${TASK}/recover-claim`, `/tasks/${TASK}/recover-claim`]);
  expect(state.posts[0].body).toMatchObject({ expected_revision: 4, target_session_id: NEW_SESSION, reason: "The old session's PC was rebooted" });
  expect(state.posts[1].body.client_request_id).toBe(state.posts[0].body.client_request_id);
  await expect(claims).toContainText("held by new chat");
  await expect(claims.getByRole("button", { name: "Hand over…" })).toHaveCount(0);
  state.recovered = true;
  await claims.getByRole("button", { name: "History" }).click();
  await expect(claims.getByRole("list", { name: "Hand-over history" })).toContainText("cleared review: approved by Claude on install 7a207d88");
});

test("a changed reason after a failed attempt is a new request", async ({ page }) => {
  const state = await fixture(page);
  await openAccess(page);
  await access(page).getByRole("button", { name: "Hand over…" }).click();
  const dialog = page.getByRole("dialog", { name: /Hand over/ });
  await dialog.getByLabel("Reason (kept with the task's history)").fill("First reason");
  await dialog.getByLabel("I understand the review is cleared").check();
  state.failNext = { status: 503, code: "hub_bridge_process_owner_required" };
  await dialog.getByRole("button", { name: "Hand over", exact: true }).click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await dialog.getByLabel("Reason (kept with the task's history)").fill("Second reason");
  await dialog.getByRole("button", { name: "Hand over", exact: true }).click();
  await expect(access(page).getByRole("status")).toContainText("Handed");
  expect(state.posts[1].body.client_request_id).not.toBe(state.posts[0].body.client_request_id);
});

test("a blocked claim says why and offers no hand-over", async ({ page }) => {
  const state = await fixture(page);
  state.claims = structuredClone(wire.active_claims);
  await openAccess(page);
  const claims = access(page).getByRole("region", { name: /Tasks claimed by/ });
  await expect(claims).toContainText("The session holding it is still live");
  await expect(claims.getByRole("button", { name: "Hand over…" })).toHaveCount(0);
});

test("installation-owned tasks are named by installation everywhere, never as the bare family or as You", async ({ page }) => {
  const state = await fixture(page);
  state.extraTasks = [{ ...wire.overview.tasks[0] },
    { ...wire.overview.tasks[0], id: "11111111-2222-4333-8444-555555555555", title: "Withdrawn by an install", status: "withdrawn",
      withdrawn_reason: "Superseded", superseded_by: null, withdrawn_by: `installation:${INSTALL}`, withdrawn_family: "codex", withdrawn_at: iso(-60_000) },
    { ...wire.installation_review, id: "22222222-3333-4444-8555-666666666666", title: "Reviewed transport task" }];
  await page.goto("/#/work");
  const row = page.locator("details.task-row").filter({ has: page.getByText("Transport task", { exact: true }) });
  await expect(row.locator(".owner")).toHaveText(/Codex on install 88736abd/);
  await expect(row).toContainText("Codex on install 88736abd is working on it.");
  await expect(row).toContainText("Staging proposal · Codex on install 88736abd");
  await page.goto("/#/projects/engineering-hub/work");
  await expect(page.locator(".board-card").filter({ has: page.getByText("Transport task", { exact: true }) })).toContainText("Codex on install 88736abd");
  await expect(page.getByRole("region", { name: "Project tracking" }).getByRole("alert")).toContainText("Project tracking could not be loaded");
  await expect(page.getByRole("link", { name: "Sign in securely" })).toHaveCount(0);
  const withdrawn = page.locator(".withdrawn-list");
  await withdrawn.locator("summary").click();
  await expect(withdrawn).toContainText("Withdrawn by an install");
  await expect(withdrawn.locator("li", { hasText: "Withdrawn by an install" })).not.toContainText("You");
  await expect(withdrawn.locator("li", { hasText: "Withdrawn by an install" })).toContainText("Codex on install");
  await page.goto("/#/work");
  const reviewed = page.locator("details.task-row", { hasText: "Review passed by Claude on install 7a207d88" });
  await reviewed.locator("summary").click();
  await expect(reviewed).toContainText("Independent reviewer: Claude on install 7a207d88");
});

for (const denial of ["unauthenticated", "forbidden"] as const) {
  test(`tracking ${denial} clears an already loaded private workspace`, async ({ page }) => {
    const state = await fixture(page);
    state.extraTasks = [{ ...wire.overview.tasks[0] }];
    state.tracking = "ok";
    await page.goto("/#/projects/engineering-hub/work");
    await expect(page.locator(".board-card").filter({ has: page.getByText("Transport task", { exact: true }) })).toBeVisible();
    await expect(page.getByRole("region", { name: "Project tracking" })).toContainText("Private tracked progress");
    state.tracking = denial;
    await page.getByLabel("Tracking records").selectOption("all");
    await expect(page.getByRole("link", { name: "Sign in securely" })).toBeVisible();
    await expect(page.getByText("Transport task", { exact: true })).toHaveCount(0);
    await expect(page.getByText("Private tracked progress", { exact: true })).toHaveCount(0);
  });
}

test("access that has run out by this browser's clock never shows as in effect", async ({ page }) => {
  const state = await fixture(page);
  // The fixture grant's expiry is in the past.
  state.grant = structuredClone(wire.active_grant);
  await openAccess(page);
  await expect(access(page)).toContainText("No Hub access: it ran out. (by this browser's clock; Refresh to confirm)");
  await expect(access(page).locator(".hub-access-rights")).toHaveCount(0);
  await expect(access(page)).toContainText("Earlier choices (not in effect): Engineering Hub");
});

const refresh = (page: Page) => panel(page).getByRole("button", { name: "Refresh", exact: true }).click();
const future = () => ({ ...structuredClone(wire.active_grant), expires_at: iso(3_600_000),
  effective: { ...wire.active_grant.effective, expires_at: iso(3_600_000) } });

test("after someone else changes access, Refresh loads the new version and the next save uses it", async ({ page }) => {
  const state = await fixture(page);
  state.grant = future();
  await openAccess(page);
  await expect(access(page)).toContainText("version 1");
  // Changed elsewhere after this page read it.
  state.grant = { ...future(), revision: 2 };
  await access(page).getByRole("button", { name: "Change access…" }).click();
  await access(page).getByRole("button", { name: "Save access" }).click();
  await expect(access(page).getByRole("alert")).toContainText("changed since you loaded it");
  await refresh(page);
  await expect(access(page)).toContainText("version 2");
  await access(page).getByRole("button", { name: "Change access…" }).click();
  await access(page).getByRole("button", { name: "Save access" }).click();
  await expect(access(page).getByRole("status")).toContainText("Hub access saved.");
  expect(state.posts.map(post => post.body.expected_revision)).toEqual([1, 2]);
});

test("Refresh re-reads which claims can be handed over", async ({ page }) => {
  const state = await fixture(page);
  await openAccess(page);
  await expect(access(page).getByRole("button", { name: "Hand over…" })).toBeVisible();
  state.claims = structuredClone(wire.active_claims);
  await refresh(page);
  await expect(access(page)).toContainText("The session holding it is still live");
  await expect(access(page).getByRole("button", { name: "Hand over…" })).toHaveCount(0);
});

test("an older access read that answers after a Refresh never brings back the old state", async ({ page }) => {
  const state = await fixture(page);
  state.grant = future();
  state.holdGrant = true;
  await page.goto("/#/agents/installations");
  const row = panel(page).locator(".installation-row", { hasText: "desk-pc" });
  await row.getByRole("button", { name: "Hub access" }).click();
  await expect.poll(() => state.grantReleases.length).toBe(1);
  state.grant = structuredClone(wire.revoked_grant);
  await refresh(page);
  await expect(access(page)).toContainText("No Hub access: you removed it.");
  state.grantReleases[0]();
  await page.waitForTimeout(300);
  await expect(access(page)).toContainText("No Hub access: you removed it.");
  await expect(access(page).locator(".hub-access-rights")).toHaveCount(0);
});

test("every active project can be chosen, up to 32 per grant", async ({ page }) => {
  const state = await fixture(page);
  state.extraProjects = 40;
  await openAccess(page);
  await access(page).getByRole("button", { name: "Grant access…" }).click();
  const form = access(page).getByRole("form", { name: "Hub access choices" });
  await expect(form.getByLabel("Extra project 40", { exact: true })).toBeEnabled();
  for (let index = 1; index <= 32; index++) await form.getByLabel(`Extra project ${index}`, { exact: true }).check();
  await expect(form).toContainText("At most 32 projects per grant.");
  await expect(form.getByLabel("Extra project 33", { exact: true })).toBeDisabled();
  await expect(form.getByLabel("Extra project 1", { exact: true })).toBeEnabled();
  await form.getByLabel("Read tasks").check();
  await form.getByRole("button", { name: "Save access" }).click();
  await expect(access(page).getByRole("status")).toContainText("Hub access saved.");
  expect((state.posts[0].body.projects as string[]).length).toBe(32);
});

test("the hand-over confirmation shows exact ids, and changing the target asks for confirmation again", async ({ page }) => {
  const state = await fixture(page);
  const second = "9d7a1c2e-0000-4000-8000-00000000abcd";
  state.claims.items[0].recovery.target_session_ids = [NEW_SESSION, second];
  await openAccess(page);
  await access(page).getByRole("button", { name: "Hand over…" }).click();
  const dialog = page.getByRole("dialog", { name: /Hand over/ });
  await expect(dialog.getByLabel("Installation id")).toHaveText(INSTALL);
  await expect(dialog.getByLabel("From session id")).toHaveText(OLD_SESSION);
  await expect(dialog.getByLabel("To session id")).toHaveText(NEW_SESSION);
  await dialog.getByLabel("I understand the review is cleared").check();
  await dialog.getByLabel("Hand over to").selectOption(second);
  await expect(dialog.getByLabel("To session id")).toHaveText(second);
  await expect(dialog.getByLabel("I understand the review is cleared")).not.toBeChecked();
});
