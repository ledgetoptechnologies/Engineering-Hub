import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";

const human = "human:cf:" + "e".repeat(64);
// 1x1 transparent PNG.
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

type Overview = ReturnType<typeof createDemoOverview>;

async function fixture(page: Page, edit: (overview: Overview) => void = () => {}) {
  const overview = createDemoOverview(new Date());
  edit(overview);
  const state = { answers: [] as { body: Record<string, unknown>; csrf?: string }[], imageReads: 0 };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(overview);
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    if (path.startsWith("/api/hub/v1/attachments/")) {
      state.imageReads++;
      return route.fulfill({ status: 200, contentType: "image/png", body: PNG });
    }
    if (path.endsWith("/answer") && request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.answers.push({ body, csrf: request.headers()["x-hub-csrf"] });
      const decision = overview.decisions[0];
      Object.assign(decision, { answer: body.answer, status: "answered", revision: decision.revision + 1, answered_by: human });
      return respond(decision);
    }
    return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
  });
  return { overview, state };
}

const withDetails = (overview: Overview) => {
  Object.assign(overview.decisions[0], {
    details: {
      summary: "Compare the two **crown counts**.\n\n<img src=x onerror=\"window.__pwned=1\">",
      links: [{ label: "Survey notes", url: "/notes" }, { label: "Vendor page", url: "https://example.com/spec" },
        { label: "Bad", url: "javascript:alert(1)" }],
      attachments: [{ kind: "image", id: "plot-a1-map", title: "Plot A1 canopy map", width: 1600, height: 1200, bytes: 420000 },
        { kind: "svg", id: "vector-map", title: "Vector", width: 10, height: 10, bytes: 10 }],
    },
  });
};

test("decision details show a formatted summary, safe links and a zoomable map image", async ({ page }) => {
  const { state } = await fixture(page, withDetails);
  await page.goto("/#/inbox");
  const card = page.locator("form.decision").first();
  await expect(card.locator(".item-summary strong")).toHaveText("crown counts");
  await expect(card.locator(".item-summary img")).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
  const links = card.getByRole("list", { name: "Links" }).getByRole("link");
  await expect(links).toHaveCount(2);
  await expect(card.getByRole("link", { name: "Vendor page" })).toHaveAttribute("target", "_blank");
  await expect(card.getByRole("link", { name: "Vendor page" })).toHaveAttribute("rel", "noopener noreferrer");
  await expect(card.getByText("Bad", { exact: true })).toHaveCount(0);
  const thumbs = card.getByRole("list", { name: "Attached images" }).getByRole("button");
  await expect(thumbs).toHaveCount(1);
  await thumbs.first().click();
  const dialog = page.getByRole("dialog", { name: "Plot A1 canopy map" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("img", { name: "Plot A1 canopy map" })).toBeVisible();
  await dialog.getByRole("button", { name: "+" }).click();
  await expect(dialog.getByText("150%")).toBeVisible();
  await dialog.getByRole("button", { name: "Fit" }).click();
  await expect(dialog.getByText("100%")).toBeVisible();
  expect(state.imageReads).toBeGreaterThan(0);
  await page.screenshot({ path: test.info().outputPath("decision-details.png"), fullPage: true });
});

test("a typed answer is sent as the answer with CSRF and the current revision", async ({ page }) => {
  const { state } = await fixture(page, (overview) => { overview.decisions[0].allow_free_text = true; });
  await page.goto("/#/inbox");
  const card = page.locator("form.decision").first();
  const record = card.getByRole("button", { name: /Record decision/ });
  await card.getByRole("radio", { name: "Type my own answer" }).check();
  await expect(record).toBeDisabled();
  await card.getByRole("textbox", { name: "Your answer" }).fill("Run the NAS fixture first, then inspect the disk on Friday.");
  await expect(record).toBeEnabled();
  await record.click();
  await expect.poll(() => state.answers.length).toBe(1);
  expect(state.answers[0]).toEqual({ csrf: "fixture-csrf", body: { expected_revision: 1,
    answer: "Run the NAS fixture first, then inspect the disk on Friday." } });
});

test("typed answers are offered only when the server allows them and never on protected requests", async ({ page }) => {
  await fixture(page, (overview) => {
    const base = overview.decisions[0];
    delete (base as { allow_free_text?: boolean }).allow_free_text;
    overview.decisions.push({ ...base, id: "protected-one", title: "Protected request", protected: true, allow_free_text: true });
  });
  await page.goto("/#/inbox");
  await expect(page.locator("form.decision")).toHaveCount(2);
  await expect(page.getByRole("radio", { name: "Type my own answer" })).toHaveCount(0);
});

test("task rows show their details when expanded", async ({ page }) => {
  await fixture(page, (overview) => {
    Object.assign(overview.tasks[0], { details: { summary: "Counts differ by **3** crowns in plot A1.",
      links: [{ label: "Run log", url: "https://example.com/log" }] } });
  });
  await page.goto("/#/work");
  const row = page.locator("details.task-row").first();
  await row.locator("summary").click();
  await expect(row.locator(".item-summary strong")).toHaveText("3");
  await expect(row.getByRole("link", { name: "Run log" })).toBeVisible();
});
