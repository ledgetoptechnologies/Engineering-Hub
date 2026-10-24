import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import { DemoComponentStore } from "../src/componentsDemo";
import { DEMO_REVIEW_ID, DemoReviewStore } from "../src/reviewsDemo";
import { audit, blocking } from "./audit";

const human = "human:cf:" + "e".repeat(64);
const reviewUrl = `/#/projects/treedar/reviews/${DEMO_REVIEW_ID}`;
type Overview = ReturnType<typeof createDemoOverview>;

// A small rotatable sample: a ring of points with colour, so the canvas has something to draw.
const cloud = JSON.stringify({ schema: "pointcloud.v1", units: "m",
  points: Array.from({ length: 400 }, (_, i) => [Math.cos(i / 20) * 5, Math.sin(i / 20) * 5, i / 40, 60, 200, 120]) });

/** The demo stores play the review and component APIs behind the real gateway paths. */
async function fixture(page: Page, options: { actor?: string; unsupported?: boolean; forbidden?: boolean } = {}) {
  const overview = createDemoOverview(new Date()) as Overview & { projects: Record<string, unknown>[] };
  overview.projects = overview.projects.map((project) => ({ ...project, description: "", repository_url: null, lead_agent: null,
    status: "on_track", parent_project: null, archived: false, revision: 1 }));
  const reviews = new DemoReviewStore(), components = new DemoComponentStore();
  const state = { posts: [] as { body: Record<string, unknown>; csrf?: string }[], conflictNext: false, componentChanged: false, signedOut: false, assetReads: 0 };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method();
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: options.actor ?? human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(overview);
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    const projectComponents = /^\/api\/hub\/v1\/projects\/([a-z0-9-]+)\/components$/.exec(path);
    if (projectComponents) return respond(components.list(projectComponents[1]));
    const component = /^\/api\/hub\/v1\/components\/([0-9a-f-]{36})$/.exec(path);
    if (component) return respond(components.list("treedar").items.find((item) => item.id === component[1]) ?? { detail: { code: "not_found" } },
      components.list("treedar").items.some((item) => item.id === component[1]) ? 200 : 404);
    if (path.startsWith("/api/hub/v1/interactive-reviews")) {
      if (options.unsupported) return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
      if (options.forbidden) return respond({ detail: { code: "interactive_review_forbidden" } }, 403);
      if (path === "/api/hub/v1/interactive-reviews") return respond(reviews.list(url.searchParams.get("project") ?? ""));
      if (/\/assets\/[0-9a-f-]{36}$/.test(path)) {
        state.assetReads++;
        return route.fulfill({ status: 200, contentType: "application/json", headers: { "content-length": String(cloud.length) }, body: cloud });
      }
      const answers = /^\/api\/hub\/v1\/interactive-reviews\/([0-9a-f-]{36})\/answers$/.exec(path);
      if (answers && method === "POST") {
        const body = request.postDataJSON() as Record<string, unknown>;
        state.posts.push({ body, csrf: request.headers()["x-hub-csrf"] });
        if (state.signedOut) return respond({ detail: { code: "sign_in_required" } }, 401);
        if (state.conflictNext) { state.conflictNext = false; return respond({ detail: { code: "revision_conflict" } }, 409); }
        if (state.componentChanged) return respond({ detail: { code: "review_component_changed" } }, 409);
        try { return respond(reviews.answer(answers[1], body as never, human)); }
        catch (error) { const e = error as { status: number; code: string }; return respond({ detail: { code: e.code === "review_conflict" ? "revision_conflict" : e.code } }, e.status); }
      }
      const one = /^\/api\/hub\/v1\/interactive-reviews\/([0-9a-f-]{36})$/.exec(path);
      if (one) { try { return respond(reviews.read(one[1])); } catch { return respond({ detail: { code: "not_found" } }, 404); } }
    }
    return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
  });
  return { state, reviews };
}
const question = (page: Page, title: string) => page.locator(".review-item").filter({ has: page.getByRole("group", { name: title }) });

test("an open review is listed on the project with its progress and component", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/projects/treedar");
  const panel = page.getByRole("region", { name: "Reviews" });
  await expect(panel.getByRole("link", { name: /Check sample tree crowns/ })).toContainText("0 of 3 answered · TreeDAR processing");
  await panel.getByRole("link", { name: /Check sample tree crowns/ }).click();
  await expect(page.getByRole("heading", { name: /Check sample tree crowns/ })).toBeVisible();
  await expect(page.getByText("REVIEW · TREEDAR PROCESSING")).toBeVisible();
});

test("answers save with CSRF, the seen revision and a request key; finishing makes them final", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto(reviewUrl);
  await expect(page.getByRole("button", { name: "Finish review" })).toBeDisabled();
  await question(page, "Tree 1").getByLabel("Yes, one tree").check();
  await question(page, "Tree 2").getByLabel("Can't tell").check();
  await expect(page.locator(".review-bar")).toContainText("2 unsaved answers");
  await page.getByRole("button", { name: "Save answers" }).click();
  await expect(page.locator(".review-bar")).toContainText("2 answers saved.");
  expect(state.posts[0]).toMatchObject({ csrf: "fixture-csrf", body: { expected_revision: 1, complete: false,
    answers: [{ item_id: "crown-1", value: "yes" }, { item_id: "crown-2", value: "unsure" }] } });
  expect(typeof state.posts[0].body.client_request_id).toBe("string");
  await question(page, "Anything else").getByLabel("Your answer").fill("Tree 2 overlaps a fence.");
  await page.getByRole("button", { name: "Finish review" }).click();
  await expect(page.getByText("This review is finished. The answers are final.")).toBeVisible();
  expect(state.posts.slice(1).map((post) => post.body)).toMatchObject([
    { expected_revision: 2, answers: [{ item_id: "notes", value: "Tree 2 overlaps a fence." }], complete: false },
    { expected_revision: 3, answers: [], complete: true }]);
  await expect(question(page, "Tree 1").getByLabel("Yes, one tree")).toBeDisabled();
  await expect(page.getByRole("button", { name: "Save answers" })).toHaveCount(0);
});

test("a conflicting save keeps the person's answers and offers a reload", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto(reviewUrl);
  await question(page, "Tree 1").getByLabel("No, several trees").check();
  state.conflictNext = true;
  await page.getByRole("button", { name: "Save answers" }).click();
  await expect(page.getByRole("alert")).toContainText("Someone saved answers to this review meanwhile. Nothing was overwritten.");
  await page.getByRole("alert").getByRole("button", { name: "Reload" }).click();
  await expect(question(page, "Tree 1").getByLabel("No, several trees")).toBeChecked();
  await expect(question(page, "Tree 1")).toContainText("Not saved yet");
  await page.getByRole("button", { name: "Save answers" }).click();
  await expect(question(page, "Tree 1")).toContainText("Saved by you");
});

test("a review whose component changed refuses answers honestly", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto(reviewUrl);
  state.componentChanged = true;
  await question(page, "Tree 2").getByLabel("Yes, one tree").check();
  await page.getByRole("button", { name: "Save answers" }).click();
  await expect(page.getByRole("alert")).toContainText("The component changed after this review was made");
  await expect(page.getByRole("heading", { name: /Check sample tree crowns/ })).toBeVisible();
});

test("an agent session can read a review but gets no answer controls", async ({ page }) => {
  await fixture(page, { actor: "codex" });
  await page.goto(reviewUrl);
  await expect(page.getByText("only a signed-in person can answer it")).toBeVisible();
  await expect(question(page, "Tree 1").getByLabel("Yes, one tree")).toBeDisabled();
  await expect(page.getByRole("button", { name: "Save answers" })).toHaveCount(0);
});

test("no access, or a Hub without reviews, is said plainly and never signs out", async ({ page }) => {
  await fixture(page, { forbidden: true });
  await page.goto(reviewUrl);
  await expect(page.getByText("You don't have access to this review.")).toBeVisible();
  await page.goto("/#/projects/treedar");
  await expect(page.getByRole("region", { name: "Reviews" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Components" })).toBeVisible();
});

test("on a phone: the 3D sample loads only on request, draws, rotates, and answers persist after reload", async ({ page }) => {
  const { state } = await fixture(page);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(reviewUrl);
  expect(blocking(await audit(page, "page", 375))).toEqual([]);
  expect(state.assetReads).toBe(0);
  await question(page, "Tree 1").getByRole("button", { name: "Show 3D sample" }).click();
  const canvas = page.locator(".pointcloud-review-canvas");
  await expect(page.getByText("400 points · m")).toBeVisible();
  expect(state.assetReads).toBe(1);
  const box = (await canvas.boundingBox())!;
  expect(box.width).toBeLessThanOrEqual(375);
  // Non-empty: some pixel differs from the background colour.
  const lit = () => canvas.evaluate((element: HTMLCanvasElement) => {
    const data = element.getContext("2d")!.getImageData(0, 0, element.width, element.height).data;
    let count = 0;
    for (let i = 0; i < data.length; i += 4) if (data[i] !== 0x10 || data[i + 1] !== 0x1b || data[i + 2] !== 0x2a) count++;
    return count;
  });
  await expect.poll(lit).toBeGreaterThan(50);
  const before = await canvas.screenshot();
  await page.getByRole("button", { name: "Rotate right" }).click();
  await expect.poll(async () => Buffer.compare(before, await canvas.screenshot())).not.toBe(0);
  await question(page, "Tree 1").getByLabel("Yes, one tree").check();
  await page.getByRole("button", { name: "Save answers" }).click();
  await expect(page.locator(".review-bar")).toContainText("Answer saved.");
  await page.reload();
  await expect(question(page, "Tree 1").getByLabel("Yes, one tree")).toBeChecked();
  await expect(page.getByText("1 of 3 answered")).toBeVisible();
});

test("demo mode shows a placeholder review, and its sample says it can't load", async ({ page }) => {
  const requests: string[] = [];
  page.on("request", (request) => { if (request.url().includes("/api/")) requests.push(request.url()); });
  await page.goto(`/?demo=1${reviewUrl.slice(1)}`);
  await expect(page.getByRole("heading", { name: /Check sample tree crowns/ })).toBeVisible();
  await question(page, "Tree 1").getByRole("button", { name: "Show 3D sample" }).click();
  await expect(page.getByText("Samples don't load in demo mode.")).toBeVisible();
  expect(requests.filter((url) => url.includes("/interactive-reviews/"))).toEqual([]);
});

test("a sign-out while answering clears the review and its unsaved text, without asking", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto(reviewUrl);
  await question(page, "Anything else").getByLabel("Your answer").fill("private unsaved review note");
  const dialogs: string[] = [];
  page.on("dialog", (dialog) => { dialogs.push(dialog.type()); void dialog.dismiss(); });
  state.signedOut = true;
  await page.getByRole("button", { name: "Save answers" }).click();
  await expect(page.getByRole("link", { name: /Sign in securely/ })).toBeVisible();
  await expect(page.getByText("private unsaved review note")).toHaveCount(0);
  await expect(page.getByText("Check sample tree crowns")).toHaveCount(0);
  expect(dialogs).toEqual([]);
});
