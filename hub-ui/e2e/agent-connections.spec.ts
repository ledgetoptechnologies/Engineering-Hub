import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import type { Installation, Session } from "../src/agentConnectionsApi";

const human = "human:cf:" + "e".repeat(64);
const iso = (offset: number) => new Date(Date.now() + offset).toISOString();
function installation(fields: Partial<Installation> & Pick<Installation, "id" | "family" | "name" | "state">): Installation {
  return { host_label: "desk-pc", reason: null, created_at: iso(-60_000), approved_at: null, approved_by: null, revoked_at: null,
    revoked_by: null, expires_at: null, revision: 1, pairing_code: null, key_fingerprint: "3f9a1c7e5b2d8f40", ...fields };
}
async function fixture(page: Page, options: { owner?: boolean; capability?: "present" | "missing" } = {}) {
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
    reads: [] as string[], auth: true, listStatus: 200, writeStatus: 200 as number, writeCode: "", hold: false,
    release: null as (() => void) | null,
  };
  await page.route("**/api/hub/v1/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: state.auth, actor: state.auth ? human : null, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: state.auth ? "fixture-csrf" : null });
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
      if (rest === "/capabilities") return respond({ enabled: true, environment: "staging", owner: options.owner ?? true });
      if (state.listStatus !== 200) return respond({ detail: { code: "human_session_required" } }, state.listStatus);
      if (rest === "/installations") return respond({ installations: state.installations });
      if (rest === "/audit") return respond({ events: [{ id: 1, at: iso(-60_000), actor: "claude", action: "enrollment.requested",
        installation_id: "inst_desk", session_id: null, message_id: null }] });
      const sessions = /^\/installations\/([^/]+)\/sessions$/.exec(rest);
      if (sessions) return respond({ sessions: state.sessions.filter(item => item.installation_id === sessions[1]) });
      return respond({}, 404);
    }
    const body = request.postDataJSON() as Record<string, unknown>;
    state.posts.push({ path: rest, method: request.method(), body, csrf: request.headers()["x-hub-csrf"] });
    if (state.hold) await new Promise<void>(resolve => { state.release = resolve; });
    if (state.writeStatus !== 200) return respond({ detail: { code: state.writeCode } }, state.writeStatus);
    const target = /^\/(installations|sessions)\/([^/]+)(?:\/(approve|revoke))?$/.exec(rest)!;
    if (target[1] === "sessions") {
      const item = state.sessions.find(session => session.id === target[2])!;
      Object.assign(item, { state: "revoked", revision: item.revision + 1 });
      return respond(item);
    }
    const item = state.installations.find(entry => entry.id === target[2])!;
    if (target[3] === "approve") Object.assign(item, { state: "active", reason: null, approved_at: iso(0), approved_by: "Beau Koltz",
      pairing_code: null, expires_at: iso(86_400_000) });
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
  await page.goto("/#/agents");
  await expect(page.getByRole("region", { name: "Hub stream observations" })).toBeVisible();
  await expect(panelOf(page)).toHaveCount(0);
});

test("the owner sees installations by agent, a staging badge and the pairing check, never a credential", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/agents");
  const panel = panelOf(page);
  await expect(panel.getByText("STAGING", { exact: true })).toBeVisible();
  const claude = panel.getByRole("region", { name: "Claude installations" });
  await expect(claude.locator(".installation-row strong")).toHaveText(["Claude on the laptop", "Claude on the desk PC"]);
  await expect(claude.getByLabel("Pairing check")).toContainText("482913");
  await expect(claude.getByLabel("Pairing check")).toContainText("a1b2c3d4e5f60718");
  await expect(panel.getByRole("region", { name: "Hermes installations" })).toContainText("No Hermes installations yet.");
  await expect(panel).toContainText("doesn't prove the agent is listening");
  await expect(panel).not.toContainText(/eh[is]_/);
});

test("approving needs an explicit match check and shows only what the server confirmed", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents");
  const panel = panelOf(page);
  await panel.getByRole("button", { name: "Check and approve…" }).click();
  const dialog = page.getByRole("dialog", { name: "Approve Claude on the desk PC?" });
  await expect(dialog).toContainText("482913");
  await expect(dialog.getByRole("button", { name: "Approve", exact: true })).toBeDisabled();
  await dialog.getByLabel("I checked that computer and both match").check();
  await dialog.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(panel.getByRole("status")).toContainText("Approved Claude on the desk PC.");
  expect(state.posts).toEqual([{ path: "/installations/inst_desk/approve", method: "POST",
    body: { expected_revision: 1, lifetime_seconds: 86400 }, csrf: "fixture-csrf" }]);
  const row = panel.locator(".installation-row", { hasText: "Claude on the desk PC" });
  await expect(row.getByText("Active", { exact: true })).toBeVisible();
  await expect(row.getByLabel("Pairing check")).toHaveCount(0);
});

test("a conflict keeps the dialog open and changes nothing", async ({ page }) => {
  const state = await fixture(page);
  state.writeStatus = 409; state.writeCode = "revision_conflict";
  await page.goto("/#/agents");
  await panelOf(page).getByRole("button", { name: "Check and approve…" }).click();
  const dialog = page.getByRole("dialog", { name: /Approve/ });
  await dialog.getByLabel("I checked that computer and both match").check();
  await dialog.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("This changed since you loaded it");
  await expect(panelOf(page).locator(".installation-row", { hasText: "desk PC" }).getByText("Waiting for approval")).toBeVisible();
});

test("revoking waits for the server and leaves other installations working", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents");
  const panel = panelOf(page);
  const laptop = panel.locator(".installation-row", { hasText: "Claude on the laptop" });
  await laptop.getByRole("button", { name: "Revoke…" }).click();
  const dialog = page.getByRole("dialog", { name: "Revoke Claude on the laptop?" });
  await expect(dialog).toContainText("doesn't stop a program already running there");
  state.hold = true;
  await dialog.getByRole("button", { name: "Revoke installation" }).click();
  await expect(dialog.getByRole("button", { name: "Revoking…" })).toBeDisabled();
  // Not shown as revoked until the server answers.
  await expect(laptop.getByText("Active", { exact: true })).toBeVisible();
  state.release?.();
  await expect(laptop.getByText("Revoked", { exact: true })).toBeVisible();
  await expect(panel.locator(".installation-row", { hasText: "Codex test box" }).getByText("Active", { exact: true })).toBeVisible();
  expect(state.posts[0]).toMatchObject({ path: "/installations/inst_laptop/revoke", body: { expected_revision: 2 } });
});

test("revoking one session keeps the installation and its other session", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents");
  const laptop = panelOf(page).locator(".installation-row", { hasText: "Claude on the laptop" });
  await laptop.getByRole("button", { name: "Sessions" }).click();
  const table = laptop.getByRole("table", { name: "Sessions for Claude on the laptop" });
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await table.locator("tr", { hasText: "main chat" }).getByRole("button", { name: "Revoke…" }).click();
  await page.getByRole("dialog", { name: "Revoke this session?" }).getByRole("button", { name: "Revoke session" }).click();
  await expect(table.locator("tr", { hasText: "main chat" })).toContainText("Revoked");
  await expect(table.locator("tr", { hasText: "side chat" })).toContainText("Active");
  await expect(laptop.getByText("Active", { exact: true }).first()).toBeVisible();
  expect(state.posts).toEqual([{ path: "/sessions/sess_main/revoke", method: "POST", body: { expected_revision: 1 }, csrf: "fixture-csrf" }]);
});

test("someone who isn't the staging owner sees no installations", async ({ page }) => {
  const state = await fixture(page, { owner: false });
  await page.goto("/#/agents");
  await expect(panelOf(page)).toContainText("Only the staging owner can see and manage agent installations.");
  await expect(panelOf(page).locator(".installation-row")).toHaveCount(0);
  expect(state.reads).toEqual(["GET /capabilities"]);
});

test("a lost sign-in clears the workspace", async ({ page }) => {
  const state = await fixture(page);
  state.listStatus = 401;
  await page.goto("/#/agents");
  await expect(page.getByRole("link", { name: /Sign in securely/ })).toBeVisible();
  await expect(panelOf(page)).toHaveCount(0);
});

test("a pending request runs out by this browser's clock without reading again", async ({ page }) => {
  await page.clock.install({ time: new Date() });
  const state = await fixture(page);
  state.installations[1].expires_at = iso(45_000);
  await page.goto("/#/agents");
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
  await page.goto("/#/agents");
  const row = panelOf(page).locator(".installation-row", { hasText: "desk PC" });
  await row.getByRole("button", { name: "Decline…" }).click();
  const dialog = page.getByRole("dialog", { name: "Decline Claude on the desk PC?" });
  await expect(dialog).toContainText("can't be approved later");
  await dialog.getByRole("button", { name: "Decline request" }).click();
  await expect(row.getByText("Revoked", { exact: true })).toBeVisible();
  expect(state.posts).toEqual([{ path: "/installations/inst_desk/revoke", method: "POST", body: { expected_revision: 1 }, csrf: "fixture-csrf" }]);
});

test("an unclear capability answer keeps the panel hidden instead of showing an error", async ({ page }) => {
  await fixture(page);
  await page.route("**/api/hub/v1/agent-connections/capabilities", route =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ enabled: true, environment: "production", owner: true }) }));
  await page.goto("/#/agents");
  await expect(page.getByRole("region", { name: "Hub stream observations" })).toBeVisible();
  await expect(panelOf(page)).toHaveCount(0);
  await expect(page.locator("#agents").getByRole("alert")).toHaveCount(0);
});
