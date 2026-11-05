import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import type { Installation, Session } from "../src/agentConnectionsApi";

const human = "human:cf:" + "e".repeat(64);
const iso = (offset: number) => new Date(Date.now() + offset).toISOString();
function installation(fields: Partial<Installation> & Pick<Installation, "id" | "family" | "name" | "state">): Installation {
  return { host_label: "desk-pc", reason: null, created_at: iso(-60_000), approved_at: null, approved_by: null, revoked_at: null,
    revoked_by: null, expires_at: null, revision: 1, pairing_code: null, key_fingerprint: "3f9a1c7e5b2d8f40", ...fields };
}
async function fixture(page: Page, options: {
  owner?: boolean; capability?: "present" | "missing"; environment?: "staging" | "production";
} = {}) {
  const state = {
    installations: [
      installation({ id: "inst_laptop", family: "claude", name: "Claude on the laptop", host_label: "beau-laptop", state: "active",
        approved_at: iso(-3_600_000), approved_by: "Beau Koltz", expires_at: iso(80_000_000), revision: 2 }),
      installation({ id: "inst_desk", family: "claude", name: "Claude on the desk PC", state: "pending",
        reason: "awaiting_owner_approval", expires_at: iso(9 * 60_000), pairing_code: "482913", key_fingerprint: "a1b2c3d4e5f60718" }),
      installation({ id: "inst_codex", family: "codex", name: "Codex test box", host_label: "test-box", state: "active",
        approved_at: iso(-7_200_000), approved_by: "Beau Koltz", expires_at: iso(70_000_000), revision: 4 }),
    ],
    sessions: [
      { id: "sess_main", installation_id: "inst_laptop", label: "main chat", issued_at: iso(-300_000), expires_at: iso(600_000),
        last_used_at: iso(-20_000), state: "active", revision: 1 },
      { id: "sess_side", installation_id: "inst_laptop", label: "side chat", issued_at: iso(-200_000), expires_at: iso(700_000),
        last_used_at: null, state: "active", revision: 1 },
    ] as Session[],
    posts: [] as { path: string; method: string; body: Record<string, unknown>; csrf?: string }[],
    failSessions: false, emptyInstallations: false, stallInstallations: false,
    failInstallations: false, stallAudit: false, stallCatalog: false, stallSessions: false, lateSessionStatus: 0,
    actor: human, owner: options.owner ?? true,
    reads: [] as string[], auth: true, csrf: "fixture-csrf" as string | null, listStatus: 200, writeStatus: 200 as number, writeCode: "",
    hold: false, release: null as (() => void) | null, holdSessions: false, sessionReleases: [] as (() => void)[],
  };
  await page.route("**/api/hub/v1/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: state.auth, actor: state.auth ? state.actor : null, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: state.auth ? state.csrf : null });
    if (path.endsWith("/overview")) return respond(createDemoOverview());
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    const prefix = "/api/hub/v1/agent-connections";
    if (!path.startsWith(prefix)) return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
    const rest = path.slice(prefix.length);
    state.reads.push(`${request.method()} ${rest}`);
    if (options.capability === "missing") return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
    if (request.method() === "GET") {
      if (rest === "/capabilities") return respond({ enabled: true, environment: options.environment ?? "staging", owner: state.owner });
      if (state.listStatus !== 200) return respond({ detail: { code: "human_session_required" } }, state.listStatus);
      if (rest === "/installations") {
        // A stalled read never answers; the page must give up on it by itself.
        if (state.stallInstallations) { await new Promise(() => {}); }
        if (state.failInstallations) return respond({ detail: { code: "agent_connections_request_failed" } }, 503);
        return respond({ installations: state.emptyInstallations ? [] : state.installations });
      }
      if (rest === "/hub-capabilities" && state.stallCatalog) { await new Promise(() => {}); }
      if (rest === "/audit" && state.stallAudit) { await new Promise(() => {}); }
      if (rest === "/audit") return respond({ events: [{ id: 1, at: iso(-60_000), actor: "claude", action: "enrollment.requested",
        installation_id: "inst_desk", session_id: null, message_id: null }] });
      const sessions = /^\/installations\/([^/]+)\/sessions$/.exec(rest);
      if (sessions && state.stallSessions) { await new Promise(() => {}); }
      if (sessions && state.failSessions) return respond({ detail: { code: "agent_connections_request_failed" } }, 503);
      if (sessions) {
        // The answer is what the server had when asked; a held read returns that older view later.
        const body = { sessions: structuredClone(state.sessions.filter(item => item.installation_id === sessions[1])) };
        if (state.holdSessions) { state.holdSessions = false; await new Promise<void>(resolve => state.sessionReleases.push(resolve)); }
        if (state.lateSessionStatus) return respond({ detail: { code: "human_session_required" } }, state.lateSessionStatus);
        return respond(body);
      }
      return respond({}, 404);
    }
    const body = request.postDataJSON() as Record<string, unknown>;
    state.posts.push({ path: rest, method: request.method(), body, csrf: request.headers()["x-hub-csrf"] });
    if (state.hold) await new Promise<void>(resolve => { state.release = resolve; });
    if (state.writeStatus !== 200) return respond({ detail: { code: state.writeCode } }, state.writeStatus);
    const target = /^\/(installations|sessions)\/([^/]+)(?:\/(approve|keep-approved|revoke))?$/.exec(rest)!;
    if (target[1] === "sessions") {
      const item = state.sessions.find(session => session.id === target[2])!;
      Object.assign(item, { state: "revoked", revision: item.revision + 1 });
      return respond(item);
    }
    const item = state.installations.find(entry => entry.id === target[2])!;
    if (target[3] === "approve") Object.assign(item, { state: "active", reason: null, approved_at: iso(0), approved_by: "Beau Koltz",
      pairing_code: null, expires_at: null });
    else if (target[3] === "keep-approved") Object.assign(item, { expires_at: null });
    else if (target[3] === "revoke") Object.assign(item, { state: "revoked", reason: "owner_revoked", pairing_code: null, revoked_at: iso(0), revoked_by: "Beau Koltz" });
    else Object.assign(item, { name: body.name });
    item.revision++;
    return respond(item);
  });
  return state;
}
const panelOf = (page: Page) => page.getByRole("region", { name: "Installations", exact: true });

test("the panel stays hidden when this Hub has no agent-connection service", async ({ page }) => {
  await fixture(page, { capability: "missing" });
  await page.goto("/#/agents/installations");
  await expect(page.getByRole("heading", { level: 1, name: "Installations" })).toBeVisible();
  await expect(panelOf(page)).toHaveCount(0);
});

test("the owner sees installations by agent, a staging badge and the pairing check, never a credential", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/agents/installations");
  const panel = panelOf(page);
  await expect(panel.getByText("STAGING", { exact: true })).toBeVisible();
  const claude = panel.getByRole("region", { name: "Claude installations" });
  await expect(claude.locator(".installation-row strong")).toHaveText(["Claude on the laptop", "Claude on the desk PC"]);
  await expect(claude.getByLabel("Pairing check")).toContainText("482913");
  await expect(claude.getByLabel("Pairing check")).toContainText("a1b2c3d4e5f60718");
  await expect(panel.getByRole("region", { name: "Hermes installations" })).toContainText("No Hermes installations yet.");
  await expect(panel).not.toContainText(/eh[is]_/);
});

test("approving needs an explicit match check and shows only what the server confirmed", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents/installations");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: "Check and approve…" }).click();
  const dialog = page.getByRole("dialog", { name: "Approve Claude on the desk PC?" });
  await expect(dialog).toContainText("482913");
  await expect(dialog.getByRole("button", { name: "Approve", exact: true })).toBeDisabled();
  await dialog.getByLabel("I checked that computer and both match").check();
  await dialog.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(panel.getByRole("status")).toContainText("Approved Claude on the desk PC.");
  expect(state.posts).toEqual([{ path: "/installations/inst_desk/approve", method: "POST",
    body: { expected_revision: 1, until_revoked: true }, csrf: "fixture-csrf" }]);
  const row = panel.locator(".installation-row", { hasText: "Claude on the desk PC" });
  await expect(row.getByText("Active", { exact: true })).toBeVisible();
  await expect(row.getByLabel("Pairing check")).toHaveCount(0);
});

test("a conflict keeps the dialog open and changes nothing", async ({ page }) => {
  const state = await fixture(page);
  state.writeStatus = 409; state.writeCode = "revision_conflict";
  await page.goto("/#/agents/installations");
  await panelOf(page).getByRole("button", { name: "Check and approve…" }).click();
  const dialog = page.getByRole("dialog", { name: /Approve/ });
  await dialog.getByLabel("I checked that computer and both match").check();
  await dialog.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("This changed since you loaded it");
  await expect(panelOf(page).locator(".installation-row", { hasText: "desk PC" }).getByText("Waiting for approval")).toBeVisible();
});

test("an existing timed approval changes only after explicit owner confirmation", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents/installations");
  const row = panelOf(page).locator(".installation-row", { hasText: "Claude on the laptop" });
  await row.getByRole("button", { name: "Keep approved…", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Keep Claude on the laptop approved?" });
  await expect(dialog).toContainText("15-minute sessions");
  expect(state.posts).toEqual([]);
  await expect(dialog.getByRole("button", { name: "Keep approved until revoked" })).toBeDisabled();
  await dialog.getByLabel("I want this device approved until I disconnect it").check();
  await dialog.getByRole("button", { name: "Keep approved until revoked" }).click();
  expect(state.posts).toEqual([{ path: "/installations/inst_laptop/keep-approved", method: "POST",
    body: { expected_revision: 2 }, csrf: "fixture-csrf" }]);
  await expect(row).toContainText("Approved until you disconnect it.");
  await expect(row.getByRole("button", { name: "Keep approved…", exact: true })).toHaveCount(0);
});

test("disconnecting an installation needs its name, waits for the server and leaves other installations working", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents/installations");
  const panel = panelOf(page);
  const laptop = panel.locator(".installation-row", { hasText: "Claude on the laptop" });
  await laptop.getByRole("button", { name: "Disconnect this installation…" }).click();
  const dialog = page.getByRole("dialog", { name: "Disconnect Claude installation “Claude on the laptop”?" });
  await expect(dialog).toContainText("This disconnects only this installation: Claude · Claude on the laptop");
  await expect(dialog).toContainText("including any other agent on the same computer");
  await expect(dialog).toContainText("pair it again from that computer and approve the new request. Its history stays.");
  await expect(dialog).toContainText("doesn't stop a program already running on that computer");
  const confirm = dialog.getByRole("button", { name: "Disconnect installation" });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel(/Type the installation name to confirm/).fill("Claude on the");
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel(/Type the installation name to confirm/).fill("Claude on the laptop");
  state.hold = true;
  await confirm.click();
  await expect(dialog.getByRole("button", { name: "Disconnecting…" })).toBeDisabled();
  // Not shown as revoked until the server answers.
  await expect(laptop.getByText("Active", { exact: true })).toBeVisible();
  state.release?.();
  await expect(laptop.getByText("Revoked", { exact: true })).toBeVisible();
  await expect(panel.locator(".installation-row", { hasText: "Codex test box" }).getByText("Active", { exact: true })).toBeVisible();
  expect(state.posts[0]).toMatchObject({ path: "/installations/inst_laptop/revoke", body: { expected_revision: 2 } });
});

test("ending one session keeps the installation and its other session, and never disconnects the installation", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents/installations");
  const laptop = panelOf(page).locator(".installation-row", { hasText: "Claude on the laptop" });
  await laptop.getByRole("button", { name: "Show sessions" }).click();
  const table = laptop.getByRole("table", { name: "Sessions for Claude on the laptop" });
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await table.locator("tr", { hasText: "main chat" }).getByRole("button", { name: "End session…" }).click();
  const dialog = page.getByRole("dialog", { name: "End session “main chat”?" });
  await expect(dialog).toContainText("Only this session ends. Claude on the laptop stays approved, and its other sessions keep working.");
  await dialog.getByRole("button", { name: "End this session" }).click();
  await expect(table.locator("tr", { hasText: "main chat" })).toContainText("Revoked");
  await expect(table.locator("tr", { hasText: "side chat" })).toContainText("Active");
  await expect(laptop.getByText("Active", { exact: true }).first()).toBeVisible();
  expect(state.posts).toEqual([{ path: "/sessions/sess_main/revoke", method: "POST", body: { expected_revision: 1 }, csrf: "fixture-csrf" }]);
});

test("someone who isn't the configured Hub owner sees no installations", async ({ page }) => {
  const state = await fixture(page, { owner: false });
  await page.goto("/#/agents/installations");
  await expect(panelOf(page)).toContainText("Only the configured Hub owner can see and manage agent installations.");
  await expect(panelOf(page).locator(".installation-row")).toHaveCount(0);
  expect(state.reads).toEqual(["GET /capabilities"]);
});

test("a lost sign-in clears the workspace", async ({ page }) => {
  const state = await fixture(page);
  state.listStatus = 401;
  await page.goto("/#/agents/installations");
  await expect(page.getByRole("link", { name: /Sign in securely/ })).toBeVisible();
  await expect(panelOf(page)).toHaveCount(0);
});

test("a pending request runs out by this browser's clock without reading again", async ({ page }) => {
  await page.clock.install({ time: new Date() });
  const state = await fixture(page);
  state.installations[1].expires_at = iso(45_000);
  await page.goto("/#/agents/installations");
  const row = panelOf(page).locator(".installation-row", { hasText: "desk PC" });
  await expect(row.getByRole("button", { name: "Check and approve…" })).toBeVisible();
  const reads = state.reads.length;
  await page.clock.runFor(60_000);
  await expect(row.getByText("Request ran out", { exact: true })).toBeVisible();
  await expect(row.getByRole("button", { name: "Check and approve…" })).toHaveCount(0);
  expect(state.reads.length).toBe(reads);
});

test("a pending request can be declined without approving anything", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents/installations");
  const row = panelOf(page).locator(".installation-row", { hasText: "desk PC" });
  await row.getByRole("button", { name: "Decline…" }).click();
  const dialog = page.getByRole("dialog", { name: "Decline Claude on the desk PC?" });
  await expect(dialog).toContainText("can't be approved later");
  await dialog.getByRole("button", { name: "Decline request" }).click();
  await expect(row.getByText("Revoked", { exact: true })).toBeVisible();
  expect(state.posts).toEqual([{ path: "/installations/inst_desk/revoke", method: "POST", body: { expected_revision: 1 }, csrf: "fixture-csrf" }]);
});

test("an unsupported capability environment keeps the panel hidden instead of showing an error", async ({ page }) => {
  await fixture(page);
  await page.route("**/api/hub/v1/agent-connections/capabilities", route =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ enabled: true, environment: "preview", owner: true }) }));
  await page.goto("/#/agents/installations");
  await expect(page.getByRole("heading", { level: 1, name: "Installations" })).toBeVisible();
  await expect(panelOf(page)).toHaveCount(0);
  await expect(page.locator("#agents").getByRole("alert")).toHaveCount(0);
});

test("production capability keeps the owner installation panel available and identifies the environment", async ({ page }) => {
  await fixture(page, { environment: "production" });
  await page.goto("/#/agents/installations");
  const panel = panelOf(page);
  await expect(panel.getByText("PRODUCTION", { exact: true })).toBeVisible();
  await expect(panel.locator(".installation-row")).toHaveCount(3);
});

test("an older session read that answers late never replaces a newer one", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents/installations");
  const laptop = panelOf(page).locator(".installation-row", { hasText: "Claude on the laptop" });
  state.holdSessions = true;
  await laptop.getByRole("button", { name: "Show sessions" }).click();
  await expect.poll(() => state.sessionReleases.length).toBe(1);
  await laptop.getByRole("button", { name: "Hide sessions" }).click();
  // Someone revokes the session elsewhere; the second read sees it.
  Object.assign(state.sessions[0], { state: "revoked", revision: 2 });
  await laptop.getByRole("button", { name: "Show sessions" }).click();
  const table = laptop.getByRole("table", { name: "Sessions for Claude on the laptop" });
  await expect(table.locator("tr", { hasText: "main chat" })).toContainText("Revoked");
  state.sessionReleases[0]();
  await page.waitForTimeout(300);
  await expect(table.locator("tr", { hasText: "main chat" })).toContainText("Revoked");
  await expect(table.locator("tr", { hasText: "main chat" }).getByRole("button", { name: "End session…" })).toHaveCount(0);
});

test("losing the right to change things disables an already-open form and sends nothing", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents/installations");
  // Found by its computer, since the name becomes an input while renaming.
  const row = panelOf(page).locator(".installation-row", { hasText: "beau-laptop" });
  await row.getByRole("button", { name: "Rename" }).click();
  await row.getByLabel("Name").fill("Renamed laptop");
  await expect(row.getByRole("button", { name: "Save" })).toBeEnabled();
  // The workspace re-reads the session and this sign-in no longer carries a CSRF token.
  state.csrf = null;
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect(row.getByRole("button", { name: "Save" })).toBeDisabled();
  await row.getByLabel("Name").press("Enter");
  await page.waitForTimeout(200);
  expect(state.posts).toEqual([]);
});

test("a long Unicode name that the backend accepts is shown and can be saved", async ({ page }) => {
  const state = await fixture(page);
  state.installations[0].name = "🌲".repeat(60);
  await page.goto("/#/agents/installations");
  await expect(panelOf(page)).toContainText("🌲".repeat(60));
  const laptop = panelOf(page).locator(".installation-row", { hasText: "beau-laptop" });
  await laptop.getByRole("button", { name: "Rename" }).click();
  await laptop.getByLabel("Name").fill("🌳".repeat(90));
  await laptop.getByRole("button", { name: "Save" }).click();
  await expect(panelOf(page).getByRole("status")).toContainText("Name saved.");
  expect(state.posts[0].body).toMatchObject({ name: "🌳".repeat(90), expected_revision: 2 });
  await expect(laptop).toContainText("🌳".repeat(90));
});

test("when a different person signs in on the open page, the previous owner's installations and late answers are gone", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents/installations");
  const laptop = panelOf(page).locator(".installation-row", { hasText: "beau-laptop" });
  state.holdSessions = true;
  await laptop.getByRole("button", { name: "Show sessions" }).click();
  await expect.poll(() => state.sessionReleases.length).toBe(1);
  // Someone else signs in; the Hub workspace re-reads its session without a reload.
  state.actor = "human:cf:" + "f".repeat(64); state.owner = false;
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect(panelOf(page)).toContainText("Only the configured Hub owner can see and manage agent installations.");
  await expect(panelOf(page).locator(".installation-row")).toHaveCount(0);
  // The old owner's held session read answers late and must not bring anything back.
  state.sessionReleases[0]();
  await page.waitForTimeout(300);
  await expect(panelOf(page).locator(".installation-row")).toHaveCount(0);
  await expect(panelOf(page).getByRole("table")).toHaveCount(0);
});

test("the disconnect confirmation states the live-session count only when sessions have loaded", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents/installations");
  const laptop = panelOf(page).locator(".installation-row", { hasText: "beau-laptop" });
  // Not loaded yet: no count is claimed.
  await laptop.getByRole("button", { name: "Disconnect this installation…" }).click();
  let dialog = page.getByRole("dialog", { name: /Disconnect Claude installation/ });
  await expect(dialog).toContainText("Its sessions haven't been loaded on this page, so the count isn't shown. All of them end.");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  // Loaded: the two live sessions are counted.
  await laptop.getByRole("button", { name: "Show sessions" }).click();
  await expect(laptop.getByRole("table")).toBeVisible();
  await laptop.getByRole("button", { name: "Disconnect this installation…" }).click();
  dialog = page.getByRole("dialog", { name: /Disconnect Claude installation/ });
  await expect(dialog).toContainText("Its 2 live sessions end.");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  expect(state.posts).toEqual([]);
});

test("a session list that couldn't be read never turns into a count", async ({ page }) => {
  const state = await fixture(page);
  state.failSessions = true;
  await page.goto("/#/agents/installations");
  const laptop = panelOf(page).locator(".installation-row", { hasText: "beau-laptop" });
  await laptop.getByRole("button", { name: "Show sessions" }).click();
  await expect(laptop.getByRole("alert")).toContainText("Sessions couldn't be read");
  await laptop.getByRole("button", { name: "Disconnect this installation…" }).click();
  await expect(page.getByRole("dialog", { name: /Disconnect Claude installation/ }))
    .toContainText("Its sessions couldn't be read, so the count isn't shown. All of them end.");
});

test("session and installation actions have distinct names and places", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/agents/installations");
  const laptop = panelOf(page).locator(".installation-row", { hasText: "beau-laptop" });
  await laptop.getByRole("button", { name: "Show sessions" }).click();
  await expect(laptop.getByRole("button", { name: "End session…" })).toHaveCount(2);
  await expect(laptop.getByRole("button", { name: "Disconnect this installation…" })).toHaveCount(1);
  await expect(laptop.locator(".installation-disconnect")).toContainText("Whole installation");
  await expect(panelOf(page).getByRole("button", { name: "Revoke…" })).toHaveCount(0);
  // A pending request is declined, never "disconnected".
  const desk = panelOf(page).locator(".installation-row", { hasText: "desk-pc" });
  await expect(desk.getByRole("button", { name: "Decline…" })).toBeVisible();
  await expect(desk.getByRole("button", { name: "Disconnect this installation…" })).toHaveCount(0);
});

test("pairing guidance points at this Hub's own helper downloads and opens when nothing is paired", async ({ page }) => {
  const state = await fixture(page);
  state.emptyInstallations = true;
  await page.goto("/#/agents/installations");
  const guide = panelOf(page).locator("details.pairing-guide");
  await expect(guide).toHaveAttribute("open", "");
  await expect(guide.getByRole("link", { name: "helper" })).toHaveAttribute("href", "/api/agent-sessions/v1/client/agent_session_client.py");
  await expect(guide.getByRole("link", { name: "manifest" })).toHaveAttribute("href", "/api/agent-sessions/v1/client/manifest");
  await expect(guide).toContainText("Approval doesn't start anything: run the helper's connect command to start a session.");
  await expect(guide).toContainText("It's never shown or pasted here.");
});

test("production pairing guidance uses private VPN paths without browser-origin download links", async ({ page }) => {
  const state = await fixture(page, { environment: "production" });
  state.emptyInstallations = true;
  await page.goto("/#/agents/installations");
  const guide = panelOf(page).locator("details.pairing-guide");
  await expect(guide).toHaveAttribute("open", "");
  await expect(guide.getByRole("link", { name: "helper" })).toHaveCount(0);
  await expect(guide.getByRole("link", { name: "manifest" })).toHaveCount(0);
  await expect(guide).toContainText("connect the VPN first");
  await expect(guide).toContainText("/api/agent-sessions/v1/client/agent_session_client.py");
  await expect(guide).toContainText("/api/agent-sessions/v1/client/manifest");
  await expect(guide).toContainText("enroll --origin <canonical-HTTPS-Hub-origin> --private-vpn --private-origin <private-agent-origin>");
  await expect(guide).toContainText("connect --session <name> --private-vpn");
  await expect(guide).toContainText("never use the browser origin for agent traffic");
});

test("a read that never answers is cancelled at the deadline and reported, without retrying", async ({ page }) => {
  await page.clock.install({ time: new Date() });
  const state = await fixture(page);
  state.stallInstallations = true;
  const cancelled: string[] = [];
  page.on("requestfailed", request => { if (request.url().endsWith("/agent-connections/installations")) cancelled.push(request.url()); });
  await page.goto("/#/agents/installations");
  await expect(panelOf(page).getByRole("status")).toContainText("Reading installations…");
  const reads = state.reads.filter(read => read === "GET /installations").length;
  await page.clock.runFor(16_000);
  await expect(panelOf(page).getByRole("alert")).toContainText("The Hub didn't answer in time");
  await expect.poll(() => cancelled.length).toBe(1);
  expect(state.reads.filter(read => read === "GET /installations").length).toBe(reads);
});

test("leaving the page cancels a read that is still open", async ({ page }) => {
  const state = await fixture(page);
  state.stallInstallations = true;
  const cancelled: string[] = [];
  page.on("requestfailed", request => { if (request.url().endsWith("/agent-connections/installations")) cancelled.push(request.url()); });
  await page.goto("/#/agents/installations");
  await expect(panelOf(page).getByRole("status")).toContainText("Reading installations…");
  // In-app navigation unmounts the panel without reloading the page.
  await page.getByRole("navigation", { name: "Workspace" }).getByRole("link", { name: /^Work/ }).click();
  await expect(panelOf(page)).toHaveCount(0);
  await expect.poll(() => cancelled.length).toBe(1);
});

const cancelledReads = (page: Page, suffix: string) => {
  const urls: string[] = [];
  page.on("requestfailed", request => { if (new URL(request.url()).pathname.endsWith(suffix)) urls.push(request.url()); });
  return urls;
};

test("when one of the paired reads fails early, the other one that is still open is cancelled", async ({ page }) => {
  const state = await fixture(page);
  state.failInstallations = true; state.stallAudit = true;
  const audit = cancelledReads(page, "/agent-connections/audit");
  await page.goto("/#/agents/installations");
  await expect(panelOf(page).getByRole("alert")).toContainText("Agent connections couldn't be read");
  await expect.poll(() => audit.length).toBe(1);
});

test("a newer refresh cancels the previous Hub-access catalog check", async ({ page }) => {
  const state = await fixture(page);
  state.stallCatalog = true;
  const catalog = cancelledReads(page, "/agent-connections/hub-capabilities");
  await page.goto("/#/agents/installations");
  await expect(panelOf(page).locator(".installation-row")).toHaveCount(3);
  expect(catalog).toHaveLength(0);
  await panelOf(page).getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => catalog.length).toBe(1);
});

test("leaving the page cancels a session list that is still loading", async ({ page }) => {
  const state = await fixture(page);
  state.stallSessions = true;
  const sessions = cancelledReads(page, "/sessions");
  await page.goto("/#/agents/installations");
  const laptop = panelOf(page).locator(".installation-row", { hasText: "beau-laptop" });
  await laptop.getByRole("button", { name: "Show sessions" }).click();
  await expect(laptop.getByRole("status")).toContainText("Reading sessions…");
  await page.getByRole("navigation", { name: "Workspace" }).getByRole("link", { name: /^Work/ }).click();
  await expect(panelOf(page)).toHaveCount(0);
  await expect.poll(() => sessions.length).toBe(1);
});

test("someone without the owner's Hub sign-in gets a way to sign in, without promising ownership", async ({ page }) => {
  await fixture(page, { owner: false });
  await page.goto("/#/agents/installations");
  const link = panelOf(page).getByRole("link", { name: "sign in to this Hub" });
  await expect(link).toHaveAttribute("href", "/api/hub/v1/auth/login");
  await expect(panelOf(page)).toContainText("Managing installations needs the configured owner's own Hub sign-in on this Hub.");
  await expect(panelOf(page)).toContainText("then come back to Agents");
  await expect(panelOf(page)).not.toContainText("@");
});

for (const variant of ["hangs", "fails"] as const) test(`a Refresh that ${variant} cancels an open session read, and its late answer or 401 never lands`, async ({ page }) => {
  const state = await fixture(page);
  const sessions = cancelledReads(page, "/sessions");
  await page.goto("/#/agents/installations");
  const laptop = panelOf(page).locator(".installation-row", { hasText: "beau-laptop" });
  state.holdSessions = true;
  await laptop.getByRole("button", { name: "Show sessions" }).click();
  await expect.poll(() => state.sessionReleases.length).toBe(1);
  // The next refresh never gets as far as re-reading sessions.
  if (variant === "hangs") state.stallInstallations = true; else state.failInstallations = true;
  await panelOf(page).getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => sessions.length).toBe(1);
  await expect(laptop.getByRole("status").filter({ hasText: "Reading sessions…" })).toHaveCount(0);
  // The cancelled read answers late with a 401; it must neither fill the list nor sign anyone out.
  state.lateSessionStatus = 401;
  state.sessionReleases[0]();
  await page.waitForTimeout(300);
  await expect(page.getByRole("link", { name: /Sign in securely/ })).toHaveCount(0);
  await expect(laptop.getByRole("table")).toHaveCount(0);
  await expect(panelOf(page).locator(".installation-row")).toHaveCount(3);
});
