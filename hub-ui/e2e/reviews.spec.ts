import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import { DemoComponentStore } from "../src/componentsDemo";
import { DEMO_REVIEW_ID, DemoReviewStore } from "../src/reviewsDemo";
import type { Review } from "../src/reviewsApi";
import { audit, blocking } from "./audit";

const human = "human:cf:" + "e".repeat(64);
const reviewUrl = `/#/projects/treedar/reviews/${DEMO_REVIEW_ID}`;
type Overview = ReturnType<typeof createDemoOverview>;

// A small rotatable sample: a ring of points with colour, so the canvas has something to draw.
const cloud = JSON.stringify({ schema: "pointcloud.v1", units: "m",
  points: Array.from({ length: 400 }, (_, i) => [Math.cos(i / 20) * 5, Math.sin(i / 20) * 5, i / 40, 60, 200, 120]) });

/** The demo stores play the review and component APIs behind the real gateway paths. */
const OTHER_REVIEW = "d0e0a000-0000-4000-8000-000000000002";
async function fixture(page: Page, options: { actor?: string; unsupported?: boolean; forbidden?: boolean; review?: Partial<Review> } = {}) {
  const overview = createDemoOverview(new Date()) as Overview & { projects: Record<string, unknown>[] };
  overview.projects = overview.projects.map((project) => ({ ...project, description: "", repository_url: null, lead_agent: null,
    status: "on_track", parent_project: null, archived: false, revision: 1 }));
  const reviews = new DemoReviewStore(options.review), components = new DemoComponentStore();
  const state = { posts: [] as { body: Record<string, unknown>; csrf?: string }[], conflictNext: false, componentChanged: false, signedOut: false, assetReads: 0,
    // Set to hold the next answers POST (or the other review's read) until released.
    holdPost: null as Promise<void> | null, holdRead: null as Promise<void> | null };
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
        if (state.holdPost) { const hold = state.holdPost; state.holdPost = null; await hold; }
        if (state.signedOut) return respond({ detail: { code: "sign_in_required" } }, 401);
        if (state.conflictNext) { state.conflictNext = false; return respond({ detail: { code: "revision_conflict" } }, 409); }
        if (state.componentChanged) return respond({ detail: { code: "review_component_changed" } }, 409);
        try { return respond(reviews.answer(answers[1], body as never, human)); }
        catch (error) { const e = error as { status: number; code: string }; return respond({ detail: { code: e.code === "review_conflict" ? "revision_conflict" : e.code } }, e.status); }
      }
      const one = /^\/api\/hub\/v1\/interactive-reviews\/([0-9a-f-]{36})$/.exec(path);
      if (one && one[1] === OTHER_REVIEW) { await state.holdRead; return respond({ detail: { code: "interactive_review_forbidden" } }, 403); }
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

test("moving to another review never shows the old one's questions while loading or after a refusal", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto(reviewUrl);
  await question(page, "Anything else").getByLabel("Your answer").fill("draft for the first review");
  let release!: () => void;
  state.holdRead = new Promise((resolve) => { release = resolve; });
  page.on("dialog", (dialog) => void dialog.accept());
  await page.evaluate((id) => { location.hash = `#/projects/ledge-top-ops/reviews/${id}`; }, OTHER_REVIEW);
  await expect(page.getByRole("heading", { name: "Loading review." })).toBeVisible();
  await expect(page.getByText("Check sample tree crowns")).toHaveCount(0);
  await expect(page.getByRole("radio")).toHaveCount(0);
  release();
  await expect(page.getByText("You don't have access to this review.")).toBeVisible();
  await expect(page.getByText("draft for the first review")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Save answers" })).toHaveCount(0);
});

test("leaving while a long save is in flight sends no further batch and shows nothing stale", async ({ page }) => {
  const items = Array.from({ length: 101 }, (_, i) => ({ id: `q${i + 1}`, title: `Question ${i + 1}`, prompt: "Pick one.",
    choices: [{ id: "a", label: `A${i + 1}` }, { id: "b", label: `B${i + 1}` }] }));
  const { state } = await fixture(page, { review: { items } });
  await page.goto(reviewUrl);
  for (let pageNumber = 0; pageNumber < 6; pageNumber++) {
    await expect(page.getByText(`Page ${pageNumber + 1} of 6`)).toBeVisible();
    for (const radio of await page.getByRole("radio", { name: /^A\d+$/ }).all()) await radio.check();
    if (pageNumber < 5) await page.getByRole("button", { name: "Next" }).click();
  }
  await expect(page.locator(".review-bar")).toContainText("101 unsaved answers");
  let release!: () => void;
  state.holdPost = new Promise((resolve) => { release = resolve; });
  await page.getByRole("button", { name: "Save answers" }).click();
  await expect.poll(() => state.posts.length).toBe(1);
  page.on("dialog", (dialog) => void dialog.accept());
  await page.evaluate(() => { location.hash = "#/projects/treedar"; });
  await expect(page.getByRole("region", { name: "Components" })).toBeVisible();
  release();
  await page.waitForTimeout(500);
  expect(state.posts).toHaveLength(1);
  expect((state.posts[0].body.answers as unknown[]).length).toBe(100);
  await expect(page.getByText(/answers saved/)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: /Check sample tree crowns/ })).toHaveCount(0);
});

test("a finished review still lets an authorized reader inspect its 3D sample", async ({ page }) => {
  await fixture(page, { review: { state: "completed", revision: 4, answers: {
    "crown-1": { value: "yes", by: human, at: "2026-10-21T10:00:00.000Z" },
    "crown-2": { value: "no", by: human, at: "2026-10-21T10:00:00.000Z" },
    notes: { value: "Fine.", by: human, at: "2026-10-21T10:00:00.000Z" } } } });
  await page.goto(reviewUrl);
  await expect(page.getByText("This review is finished. The answers are final.")).toBeVisible();
  await expect(question(page, "Tree 1").getByLabel("Yes, one tree")).toBeDisabled();
  await question(page, "Tree 1").getByRole("button", { name: "Show 3D sample" }).click();
  await expect(page.getByText("400 points · m")).toBeVisible();
  await expect(page.getByRole("button", { name: "Rotate right" })).toBeEnabled();
});
