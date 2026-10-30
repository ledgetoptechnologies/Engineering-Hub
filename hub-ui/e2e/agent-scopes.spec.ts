import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import type { Installation, InstallationScope } from "../src/agentConnectionsApi";
import { audit, blocking } from "./audit";

const human = "human:cf:" + "e".repeat(64);
const iso = (offset: number) => new Date(Date.now() + offset).toISOString();
function installation(fields: Partial<Installation> & Pick<Installation, "id" | "family" | "name">): Installation {
  return { host_label: "desk-pc", state: "active", reason: null, created_at: iso(-60_000), approved_at: iso(-50_000), approved_by: human,
    revoked_at: null, revoked_by: null, expires_at: null, revision: 1, pairing_code: null, key_fingerprint: "3f9a1c7e5b2d8f40", ...fields };
}
const unset = (id: string): InstallationScope => ({ installation_id: id, kind: "unclassified", revision: 0, updated_by: null, updated_at: null, internal_effective: false });

async function fixture(page: Page, options: { scopesStatus?: number; writeCode?: string; truncated?: boolean } = {}) {
  const state = {
    installations: [
      installation({ id: "inst_laptop", family: "claude", name: "Claude on the laptop", host_label: "beau-laptop" }),
      installation({ id: "inst_codex", family: "codex", name: "Codex on BEAST", host_label: "beast" }),
      installation({ id: "inst_guest", family: "claude", name: "Thomas's Claude", host_label: "thomas-pc" }),
      installation({ id: "inst_old", family: "hermes", name: "Old Hermes", host_label: "nas", state: "revoked", revoked_at: iso(-10_000), revoked_by: human }),
    ],
    scopes: [unset("inst_laptop"), unset("inst_codex"), unset("inst_guest"), unset("inst_old")],
    puts: [] as { body: { kind: string; installations: { installation_id: string; expected_revision: number }[] }; csrf?: string }[],
    scopeReads: 0, writeCode: options.writeCode ?? "",
    holdNextRead: false, release: null as (() => void) | null,
  };
  await page.route("**/api/hub/v1/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(createDemoOverview());
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    const rest = path.replace("/api/hub/v1/agent-connections", "");
    if (rest === "/capabilities") return respond({ enabled: true, environment: "production", owner: true });
    if (rest === "/installations") return respond({ installations: state.installations });
    if (rest === "/audit") return respond({ events: [] });
    if (rest === "/installation-scopes" && request.method() === "GET") {
      state.scopeReads++;
      // A held read answers later with what the server had when it was asked.
      if (state.holdNextRead) {
        state.holdNextRead = false;
        const then = structuredClone(state.scopes);
        await new Promise<void>(resolve => { state.release = resolve; });
        return respond({ installations: then, truncated: false });
      }
      return options.scopesStatus ? respond({ detail: { code: "not_found" } }, options.scopesStatus) : respond({ installations: state.scopes, truncated: options.truncated ?? false });
    }
    if (rest === "/installation-scopes" && request.method() === "PUT") {
      const body = request.postDataJSON();
      state.puts.push({ body, csrf: request.headers()["x-hub-csrf"] });
      if (state.writeCode) { const code = state.writeCode; state.writeCode = ""; return respond({ detail: { code } }, 409); }
      const saved = body.installations.map((row: { installation_id: string; expected_revision: number }) => {
        const next: InstallationScope = { installation_id: row.installation_id, kind: body.kind, revision: row.expected_revision + 1,
          updated_by: human, updated_at: iso(0), internal_effective: body.kind === "internal" };
        state.scopes = state.scopes.map(item => item.installation_id === row.installation_id ? next : item);
        return next;
      });
      return respond({ installations: saved });
    }
    return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
  });
  return state;
}
const section = (page: Page) => page.getByRole("region", { name: "Our own agents" });

test("nothing is pre-selected, and one Confirm marks the chosen agents as ours", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents");
  const review = section(page);
  await expect(review).toContainText("Codex on BEAST");
  for (const box of await review.getByRole("checkbox").all()) await expect(box).not.toBeChecked();
  // A revoked installation can't be marked as ours.
  await expect(review.getByRole("checkbox", { name: /Old Hermes/ })).toBeDisabled();
  await review.getByRole("checkbox", { name: /Claude on the laptop/ }).check();
  await review.getByRole("checkbox", { name: /Codex on BEAST/ }).check();
  await review.getByRole("button", { name: "Mark 2 as our own agents…" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Not set → Our agent");
  await expect(dialog).not.toContainText("Thomas");
  expect(state.puts).toEqual([]);
  await dialog.getByRole("button", { name: "Confirm" }).click();
  await expect(review).toContainText("Marked 2 installations as our own agents.");
  expect(state.puts).toEqual([{ csrf: "fixture-csrf", body: { kind: "internal", installations: [
    { installation_id: "inst_laptop", expected_revision: 0 }, { installation_id: "inst_codex", expected_revision: 0 }] } }]);
  await expect(review.getByRole("listitem").filter({ hasText: "Codex on BEAST" })).toContainText("Our agent");
  await expect(review.getByRole("listitem").filter({ hasText: "Thomas's Claude" })).toContainText("Not set");
  await page.setViewportSize({ width: 375, height: 812 });
  expect(blocking(await audit(page, "page", 375))).toEqual([]);
});

test("select all active skips revoked installations and can be set back to not set", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents");
  const review = section(page);
  await review.getByRole("button", { name: "Select all active" }).click();
  await review.getByRole("button", { name: "Mark 3 as our own agents…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Confirm" }).click();
  await expect(review).toContainText("Marked 3 installations");
  expect(state.puts[0].body.installations.map(row => row.installation_id)).not.toContain("inst_old");
  await review.getByLabel("Mark selected as").selectOption("unclassified");
  await review.getByRole("checkbox", { name: /Thomas's Claude/ }).check();
  await review.getByRole("button", { name: "Mark 1 as not set…" }).click();
  const confirmation = page.getByRole("dialog");
  await confirmation.getByRole("button", { name: "Confirm" }).click();
  await expect(confirmation).toBeHidden();
  await expect(review.getByRole("listitem").filter({ hasText: "Thomas's Claude" })).toContainText("Not set");
  expect(state.puts[1].body).toEqual({ kind: "unclassified", installations: [{ installation_id: "inst_guest", expected_revision: 1 }] });
});

test("a conflict changes nothing, re-reads the list and says so", async ({ page }) => {
  const state = await fixture(page, { writeCode: "scope_revision_conflict" });
  await page.goto("/#/agents");
  const review = section(page);
  await review.getByRole("checkbox", { name: /Codex on BEAST/ }).check();
  await review.getByRole("button", { name: "Mark 1 as our own agents…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Confirm" }).click();
  await expect(review).toContainText("The list has been read again");
  await expect.poll(() => state.scopeReads).toBeGreaterThan(1);
  await expect(review.getByRole("listitem").filter({ hasText: "Codex on BEAST" })).toContainText("Not set");
});

test("is hidden until the Hub offers it", async ({ page }) => {
  await fixture(page, { scopesStatus: 404 });
  await page.goto("/#/agents");
  await expect(page.getByRole("region", { name: "Installations", exact: true })).toContainText("Codex on BEAST");
  await expect(section(page)).toHaveCount(0);
});

test("a list read that was asked before a confirm can't overwrite what the confirm saved", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/agents");
  const review = section(page);
  await expect(review).toContainText("Codex on BEAST");
  state.holdNextRead = true;
  await page.getByRole("region", { name: "Installations", exact: true }).getByRole("button", { name: "Refresh" }).first().click();
  await expect.poll(() => state.release !== null).toBe(true);
  await review.getByRole("checkbox", { name: /Codex on BEAST/ }).check();
  await review.getByRole("button", { name: "Mark 1 as our own agents…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Confirm" }).click();
  await expect(review).toContainText("Marked 1 installation as our own agents.");
  state.release!();
  await page.waitForTimeout(300);
  await expect(review.getByRole("listitem").filter({ hasText: "Codex on BEAST" })).toContainText("Our agent");
});

test("says when older installations were left out of the list, and only then", async ({ page }) => {
  await fixture(page, { truncated: true });
  await page.goto("/#/agents");
  await expect(section(page)).toContainText("Only 4 installations are listed: active ones first, then the newest.");
  await page.unrouteAll();
  await fixture(page);
  await page.reload();
  await expect(section(page)).toContainText("Codex on BEAST");
  await expect(section(page)).not.toContainText("installations are listed");
});
