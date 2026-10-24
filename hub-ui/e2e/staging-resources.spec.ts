import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import { DemoResourceStore } from "../src/stagingResourcesDemo";
import type { StagingResource } from "../src/stagingResourcesApi";
import { audit, blocking } from "./audit";

const human = "human:cf:" + "e".repeat(64);

/** The demo store plays the staging API behind the real gateway paths, so the live code path is exercised. */
async function fixture(page: Page, options: { unsupported?: boolean; previewUrl?: string; status?: Record<string, unknown> | "error" } = {}) {
  const store = new DemoResourceStore();
  const state = { posts: [] as { path: string; body: Record<string, unknown>; csrf?: string }[], conflictNext: false, pinOnApply: "",
    lastPlan: null as ReturnType<DemoResourceStore["plan"]> | null };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(createDemoOverview(new Date()));
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    if (path.startsWith("/api/hub/v1/staging/") && options.unsupported) return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
    if (path === "/api/hub/v1/staging/status" && options.status)
      return options.status === "error" ? respond({ detail: { code: "staging_unavailable" } }, 503) : respond(options.status);
    if (path === "/api/hub/v1/staging/resources" && request.method() === "GET")
      return respond(store.list({ project: url.searchParams.get("project") ?? undefined }));
    if (request.method() === "POST" && path.startsWith("/api/hub/v1/staging/")) {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.posts.push({ path, body, csrf: request.headers()["x-hub-csrf"] });
      const keep = /^\/api\/hub\/v1\/staging\/resources\/([^/]+)\/keep$/.exec(path);
      if (keep) {
        const current = store.list({}).items.find((item) => item.id === keep[1])!;
        if (state.conflictNext) { state.conflictNext = false; return respond({ detail: { code: "staging_resource_conflict", current_revision: current.revision + 1 } }, 409); }
        const { expected_revision, ...keepRequest } = body;
        return respond(store.keep({ ...current, revision: expected_revision as number } as StagingResource, keepRequest as never));
      }
      if (path.endsWith("/preview-session")) return options.previewUrl
        ? respond({ url: options.previewUrl, expires_at: new Date(Date.now() + 10 * 60_000).toISOString() })
        : respond({ detail: { code: "preview_not_configured" } }, 503);
      if (path.endsWith("/cleanup/plan")) return respond(state.lastPlan = store.plan((body.project as string | undefined) ?? null));
      if (path.endsWith("/cleanup/apply")) {
        // Someone pins an item between plan and apply: apply must skip it, not delete it.
        if (state.pinOnApply) {
          const target = store.list({}).items.find((item) => item.id === state.pinOnApply)!;
          store.keep(target, { mode: "pinned", reason: "Pinned in another tab" });
        }
        // The panel sends only the reviewed plan's ID and digest; the server applies the plan it issued.
        if (Object.keys(body).sort().join() !== "digest,plan_id" || body.plan_id !== state.lastPlan?.plan_id) return respond({ detail: { code: "staging_plan_changed" } }, 409);
        return respond(store.apply(state.lastPlan!));
      }
    }
    return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
  });
  return { store, state };
}

const panel = (page: Page) => page.getByRole("region", { name: "Staging resources" });

test("lists what staging keeps, with expiry, protection and unknown sizes stated plainly", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/staging");
  const rows = panel(page).getByRole("list", { name: "Kept items" }).getByRole("listitem");
  await expect(rows).toHaveCount(7);
  const rollback = rows.filter({ hasText: "Protected: rollback copy" });
  await expect(rollback).toHaveCount(1);
  // Protected items offer no keep or removal controls.
  await expect(rollback.getByRole("button")).toHaveCount(0);
  await expect(rows.filter({ hasText: "Pinned: Compare against the next release" }).getByRole("button", { name: "Unpin" })).toBeVisible();
  await expect(rows.filter({ hasText: "Test run" })).toContainText("size unknown");
  await expect(rows.filter({ hasText: "Expired, removed at the next cleanup" })).toHaveCount(2);
  expect(blocking(await audit(page, "page", 1440))).toEqual([]);
});

test("keep 7 more days and pin send CSRF, the seen revision and a required reason", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto("/#/staging");
  const rows = panel(page).getByRole("list", { name: "Kept items" }).getByRole("listitem");
  const workspace = rows.filter({ hasText: "Workspace" }).filter({ hasText: "1.2 GB" });
  await workspace.getByRole("button", { name: "Keep 7 more days" }).click();
  await expect(panel(page).getByText("Workspace kept 7 more days.")).toBeVisible();
  await expect(workspace.getByRole("button", { name: "Let it expire" })).toBeVisible();
  expect(state.posts[0]).toMatchObject({ csrf: "fixture-csrf", body: { mode: "until", reason: "Kept 7 more days", expected_revision: 1 } });

  const preview = rows.filter({ hasText: "Preview" }).filter({ hasText: "Engineering Hub" });
  await preview.getByRole("button", { name: "Pin…" }).click();
  const dialog = page.getByRole("dialog", { name: "Pin this preview?" });
  await expect(dialog.getByRole("button", { name: "Pin" })).toBeDisabled();
  await dialog.getByLabel("Why keep it (required)").fill("Beau wants to compare");
  await dialog.getByRole("button", { name: "Pin" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(preview).toContainText("Pinned: Beau wants to compare");
  expect(state.posts[1].body).toEqual({ mode: "pinned", reason: "Beau wants to compare", expected_revision: 1 });
});

test("a conflicting change is explained and the list is re-read, not overwritten", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto("/#/staging");
  state.conflictNext = true;
  const rows = panel(page).getByRole("list", { name: "Kept items" }).getByRole("listitem");
  await rows.filter({ hasText: "Test run" }).getByRole("button", { name: "Keep 7 more days" }).click();
  await expect(panel(page).getByRole("alert")).toHaveText(/Someone changed this item meanwhile/);
  await expect(rows.filter({ hasText: "Test run" })).not.toContainText("Kept until");
});

test("cleanup shows its plan first, removes only what it listed and reports a skip honestly", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto("/#/staging");
  await panel(page).getByRole("button", { name: "Clean up staging…" }).click();
  const dialog = page.getByRole("dialog", { name: "Clean up staging" });
  await expect(dialog).toContainText("Will remove 1 preview and 1 workspace (1.2 GB).");
  await expect(dialog).toContainText("1 rollback copy");
  expect(blocking(await audit(page, "dialog", 1440))).toEqual([]);
  // The treedar preview gets pinned elsewhere after the plan was shown.
  state.pinOnApply = state.lastPlan!.remove.find((item) => item.kind === "preview")!.id;
  await dialog.getByRole("button", { name: "Remove 2 items" }).click();
  await expect(dialog).toContainText("1 removed, 1 skipped because it changed since the plan, 0 failed.");
  await dialog.getByRole("button", { name: "Done" }).click();
  const rows = panel(page).getByRole("list", { name: "Kept items" }).getByRole("listitem");
  await expect(rows).toHaveCount(6);
  await expect(rows.filter({ hasText: "Pinned in another tab" })).toHaveCount(1);
  expect(state.posts.filter((post) => post.path.endsWith("/apply")).map((post) => post.body))
    .toEqual([{ plan_id: state.lastPlan!.plan_id, digest: state.lastPlan!.digest }]);
});

test("cancelling cleanup removes nothing", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto("/#/staging");
  await panel(page).getByRole("button", { name: "Clean up staging…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
  expect(state.posts.filter((post) => post.path.endsWith("/apply"))).toEqual([]);
  await expect(panel(page).getByRole("listitem")).toHaveCount(7);
});

test("a Hub without staging resources says so and offers no cleanup", async ({ page }) => {
  await fixture(page, { unsupported: true });
  await page.goto("/#/staging");
  await expect(panel(page)).toContainText("This Hub doesn't have staging resources yet.");
  await expect(panel(page).getByRole("button", { name: "Clean up staging…" })).toBeDisabled();
});

test("demo mode works without any staging API and says it's illustrative", async ({ page }) => {
  await page.route("**/api/hub/v1/staging/**", (route) => route.abort());
  await page.goto("/?demo=1#/staging");
  await expect(panel(page)).toContainText("Demo: illustrative items only.");
  await expect(panel(page).getByRole("listitem")).toHaveCount(7);
});

test("fits a phone screen", async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/#/staging");
  await expect(panel(page).getByRole("listitem")).toHaveCount(7);
  expect(blocking(await audit(page, "page", 375))).toEqual([]);
});

test("without a preview address, opening a preview says so and shows no frame", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/staging");
  const preview = panel(page).getByRole("listitem").filter({ hasText: "Preview" }).filter({ hasText: "Engineering Hub" });
  await preview.getByRole("button", { name: "Open preview" }).click();
  await expect(panel(page).getByRole("alert")).toHaveText(/the separate preview address hasn't been set up/);
  await expect(page.locator("iframe")).toHaveCount(0);
});

test("a preview URL on the dashboard's own origin is refused", async ({ page }) => {
  await fixture(page, { previewUrl: "http://127.0.0.1:5174/preview/x/" });
  await page.goto("/#/staging");
  await panel(page).getByRole("listitem").filter({ hasText: "Engineering Hub" }).getByRole("button", { name: "Open preview" }).first().click();
  await expect(panel(page).getByRole("alert")).toBeVisible();
  await expect(page.locator("iframe")).toHaveCount(0);
});

test("a preview runs in an opaque sandbox on its own origin: ES modules and JSON load, the Hub and cookies stay out of reach", async ({ page, context }) => {
  const previewOrigin = "https://preview.example.test";
  await context.addCookies([{ name: "hub_session", value: "secret", domain: "127.0.0.1", path: "/" }]);
  const seen: { path: string; cookie?: string; referer?: string }[] = [];
  await page.route(`${previewOrigin}/**`, (route) => {
    const request = route.request(), path = new URL(request.url()).pathname;
    seen.push({ path, cookie: request.headers()["cookie"], referer: request.headers()["referer"] });
    const headers = { "Content-Security-Policy": "sandbox allow-scripts", "Access-Control-Allow-Origin": "*" };
    if (path.endsWith("/app.js")) return route.fulfill({ headers, contentType: "text/javascript", body: `
      import { label } from "./lib.js";
      const data = await (await fetch("./data.json")).json();
      let parentReadable = "no"; try { void parent.document.title; parentReadable = "yes"; } catch {}
      let cookie; try { cookie = document.cookie || "empty"; } catch { cookie = "blocked"; }
      document.getElementById("out").textContent = label + " " + data.n + " origin=" + self.origin + " parent=" + parentReadable + " cookie=" + cookie;` });
    if (path.endsWith("/lib.js")) return route.fulfill({ headers, contentType: "text/javascript", body: `export const label = "ok";` });
    if (path.endsWith("/data.json")) return route.fulfill({ headers, contentType: "application/json", body: `{"n": 42}` });
    return route.fulfill({ headers, contentType: "text/html", body: `<!doctype html><p id="out">loading</p><script type="module" src="./app.js"></script>` });
  });
  await fixture(page, { previewUrl: `${previewOrigin}/t/ticket123/` });
  await page.goto("/#/staging");
  await panel(page).getByRole("listitem").filter({ hasText: "Engineering Hub" }).getByRole("button", { name: "Open preview" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Preview for Engineering Hub" });
  const frame = dialog.locator("iframe");
  await expect(frame).toHaveAttribute("sandbox", "allow-scripts");
  await expect(frame).toHaveAttribute("src", `${previewOrigin}/t/ticket123/`);
  await expect(page.frameLocator("iframe.preview-frame").locator("#out")).toHaveText("ok 42 origin=null parent=no cookie=blocked");
  // Neither the Hub's cookie nor its address travels with preview requests.
  expect(seen.map((item) => item.path)).toEqual(expect.arrayContaining(["/t/ticket123/", "/t/ticket123/app.js", "/t/ticket123/lib.js", "/t/ticket123/data.json"]));
  for (const item of seen) { expect(item.cookie).toBeUndefined(); expect(item.referer ?? "").not.toContain("127.0.0.1"); }
  expect(seen[0].referer).toBeUndefined();
  expect(blocking(await audit(page, "dialog", 1440))).toEqual([]);
});

test("the service status line never claims readiness the server didn't report", async ({ page }) => {
  await fixture(page, { status: { configured: true, running: true, reachable: true, profiles: { "engineering-hub": ["vite-build"] }, previews_configured: false } });
  await page.goto("/#/staging");
  const line = panel(page).locator(".staging-status");
  await expect(line).toContainText("Staging PC reachable. 1 project has build or test recipes you can use.");
  await expect(line).toContainText("the separate preview address hasn't been set up");
});

test("an unset-up service says so, and a failed status read says unknown without hiding the list", async ({ page }) => {
  await fixture(page, { status: { configured: false, running: false, reachable: false, profiles: {}, state: "not_configured" } });
  await page.goto("/#/staging");
  await expect(panel(page).locator(".staging-status")).toContainText("Service not ready");
  await expect(panel(page).locator(".staging-status")).toContainText("The staging service isn't set up yet.");
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await fixture(page, { status: "error" });
  await page.reload();
  await expect(panel(page).locator(".staging-status")).toHaveText("Staging service status is unknown right now.");
  await expect(panel(page).getByRole("list", { name: "Kept items" }).getByRole("listitem")).toHaveCount(7);
});
