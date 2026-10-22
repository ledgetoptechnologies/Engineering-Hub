import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";

const human = "human:cf:" + "e".repeat(64);
// 1x1 transparent PNG.
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

type Overview = ReturnType<typeof createDemoOverview>;
const DECISION_IDS = ["11111111-2222-4333-8444-000000000001", "11111111-2222-4333-8444-000000000002"];
const TASK_IDS = ["22222222-2222-4333-8444-000000000001", "22222222-2222-4333-8444-000000000002", "22222222-2222-4333-8444-000000000003", "22222222-2222-4333-8444-000000000004"];
const IMAGE = { id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", kind: "decision", project: "engineering-hub", title: "Plot A1 canopy map",
  media_type: "image/png", bytes: 210000, sha256: "c".repeat(64), by: "codex", created_at: "2026-10-22T12:00:00Z", entity_revision: 1 };

async function fixture(page: Page, edit: (overview: Overview) => void = () => {}) {
  const overview = createDemoOverview(new Date());
  edit(overview);
  // Attachments are addressed by UUID entity ids, as the server issues them.
  overview.decisions.forEach((decision, index) => { decision.id = DECISION_IDS[index]; });
  overview.tasks.forEach((task, index) => { task.id = TASK_IDS[index]; });
  const state = { answers: [] as { body: Record<string, unknown>; csrf?: string }[], imageReads: 0,
    attachments: {} as Record<string, Record<string, unknown>[]> };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(overview);
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    const list = /^\/api\/hub\/v1\/(tasks|decisions)\/([^/]+)\/attachments$/.exec(path);
    if (list) return respond({ project: "engineering-hub", entity_revision: 1,
      items: (state.attachments[list[2]] ?? []).map((item) => ({ ...item, entity_id: list[2] })) });
    if (/^\/api\/hub\/v1\/attachments\/[0-9a-f-]+\/image$/.test(path)) {
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
    details: "Compare the two **crown counts**.\n\n<img src=x onerror=\"window.__pwned=1\">",
    links: [{ label: "Survey notes", url: "/notes" }, { label: "Vendor page", url: "https://example.com/spec" },
      { label: "Bad", url: "javascript:alert(1)" }],
  });
};

test("decision details show a formatted summary, safe links and a zoomable map image", async ({ page }) => {
  const { state } = await fixture(page, withDetails);
  state.attachments[DECISION_IDS[0]] = [IMAGE, { ...IMAGE, id: "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee", media_type: "image/svg+xml", title: "Vector" }];
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
  const { state } = await fixture(page, (overview) => { overview.decisions[0].allow_text = true; });
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

test("closing an attached image never records a selected decision", async ({ page }) => {
  const { state } = await fixture(page, withDetails);
  state.attachments[DECISION_IDS[0]] = [IMAGE];
  await page.goto("/#/inbox");
  const card = page.locator("form.decision").first();
  await card.getByRole("radio").first().check();
  await expect(card.getByRole("button", { name: /Record decision/ })).toBeEnabled();
  await card.getByRole("list", { name: "Attached images" }).getByRole("button").click();
  const dialog = page.getByRole("dialog", { name: "Plot A1 canopy map" });
  await dialog.getByRole("button", { name: "Close dialog" }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.answers).toHaveLength(0);
  await expect(card.getByRole("button", { name: /Record decision/ })).toBeEnabled();
  await card.getByRole("button", { name: /Record decision/ }).click();
  await expect.poll(() => state.answers.length).toBe(1);
});

test("custom answer typing survives matching a preset and same-revision refresh", async ({ page }) => {
  const { overview, state } = await fixture(page, (data) => {
    Object.assign(data.decisions[0], { allow_text: true, options: ["No", "Yes"] });
  });
  await page.goto("/#/inbox");
  const card = page.locator("form.decision").first();
  await card.getByRole("radio", { name: "Type my own answer" }).check();
  const input = card.getByRole("textbox", { name: "Your answer" });
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect(input).toHaveValue("");
  await expect(card.getByRole("radio", { name: "Type my own answer" })).toBeChecked();
  await expect(card.getByRole("button", { name: /Record decision/ })).toBeDisabled();
  await input.pressSequentially("Not until Friday");
  await expect(input).toHaveValue("Not until Friday");
  await expect(card.getByRole("radio", { name: "No", exact: true })).not.toBeChecked();
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect(input).toHaveValue("Not until Friday");
  await expect(card.getByRole("radio", { name: "Type my own answer" })).toBeChecked();
  await expect(card.getByRole("button", { name: /Record decision/ })).toBeEnabled();
  expect(state.answers).toHaveLength(0);
  overview.decisions[0].revision++;
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect(card.getByRole("button", { name: /Record decision/ })).toBeDisabled();
  await expect(input).toHaveCount(0);
  expect(state.answers).toHaveLength(0);
});

test("typed answers are offered only when the server allows them and never on protected requests", async ({ page }) => {
  await fixture(page, (overview) => {
    const base = overview.decisions[0];
    delete (base as { allow_text?: boolean }).allow_text;
    overview.decisions.push({ ...base, id: DECISION_IDS[1], title: "Protected request", protected: true, allow_text: true });
  });
  await page.goto("/#/inbox");
  await expect(page.locator("form.decision")).toHaveCount(2);
  await expect(page.getByRole("radio", { name: "Type my own answer" })).toHaveCount(0);
});

test("task rows show their details when expanded", async ({ page }) => {
  await fixture(page, (overview) => {
    Object.assign(overview.tasks[0], { details: "Counts differ by **3** crowns in plot A1.",
      links: [{ label: "Run log", url: "https://example.com/log" }] });
  });
  await page.goto("/#/work");
  const row = page.locator("details.task-row").first();
  await row.locator("summary").click();
  await expect(row.locator(".item-summary strong")).toHaveText("3");
  await expect(row.getByRole("link", { name: "Run log" })).toBeVisible();
});

test("the Activity tab shows the Hub's recorded history with paging, newest first", async ({ page }) => {
  const { overview } = await fixture(page);
  const project = overview.tasks[0].project;
  const event = (sequence: number, action: string, title: string) => ({ sequence, action, entity_id: TASK_IDS[0], revision: sequence,
    created_at: new Date(Date.now() - sequence * 60000).toISOString(), kind: action.split(".")[0], actor: "codex", current_title: title });
  await page.route("**/api/hub/v1/activity?*", (route) => {
    const before = new URL(route.request().url()).searchParams.get("before");
    const body = before ? { project, items: [event(1, "task.created", "Older item")], next_before: null }
      : { project, items: [event(3, "task.claimed", "Newest item"), event(2, "decision.answered", "Middle item")], next_before: 2 };
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto(`/#/projects/${project}/activity`);
  const history = page.getByRole("list", { name: "Activity history" });
  await expect(history.getByRole("listitem")).toHaveCount(2);
  await expect(history.getByRole("listitem").first()).toContainText("Codex claimed it");
  await page.getByRole("button", { name: "Show older" }).click();
  await expect(history.getByRole("listitem")).toHaveCount(3);
  await expect(page.getByRole("button", { name: "Show older" })).toHaveCount(0);
});

for (const [name, size] of [["desktop", { width: 1280, height: 800 }], ["phone", { width: 390, height: 844 }]] as const) {
  test(`a wide map opens fitted inside the viewer with no sideways scrolling (${name})`, async ({ page }) => {
    // Draw a 1600x900 "map" so the viewer has a realistic wide image to fit.
    await page.goto("about:blank");
    const wide = await page.evaluate(() => {
      const canvas = document.createElement("canvas"); canvas.width = 1600; canvas.height = 900;
      const g = canvas.getContext("2d")!; g.fillStyle = "#e8f5e9"; g.fillRect(0, 0, 1600, 900);
      g.strokeStyle = "#c62828"; g.lineWidth = 8; g.strokeRect(300, 200, 700, 400); g.fillStyle = "#1b5e20"; g.font = "64px sans-serif"; g.fillText("A1", 600, 420);
      return canvas.toDataURL("image/png").split(",")[1];
    });
    const { state } = await fixture(page, withDetails);
    await page.route(/\/api\/hub\/v1\/attachments\/[0-9a-f-]+\/image$/, (route) =>
      route.fulfill({ status: 200, contentType: "image/png", body: Buffer.from(wide, "base64") }));
    state.attachments[DECISION_IDS[0]] = [{ ...IMAGE, title: "Area map: this question is about area A1 (red outline, labelled A1)" }];
    await page.setViewportSize(size);
    await page.goto("/#/inbox");
    await page.locator("form.decision").first().getByRole("list", { name: "Attached images" }).getByRole("button").click();
    const dialog = page.getByRole("dialog");
    const img = dialog.getByRole("img", { name: /Area map/ });
    await expect(img).toBeVisible();
    await expect.poll(() => img.evaluate((el) => (el as HTMLImageElement).complete && (el as HTMLImageElement).naturalWidth)).toBe(1600);
    const box = await dialog.evaluate((el) => ({ scroll: el.scrollWidth, client: el.clientWidth, right: el.getBoundingClientRect().right }));
    expect(box.scroll).toBeLessThanOrEqual(box.client);
    expect(box.right).toBeLessThanOrEqual(size.width);
    const fit = await img.evaluate((el) => { const i = el.getBoundingClientRect(), v = el.parentElement!.getBoundingClientRect();
      return { inside: i.left >= v.left - 1 && i.right <= v.right + 1 && i.top >= v.top - 1 && i.bottom <= v.bottom + 1 }; });
    expect(fit.inside).toBe(true);
    await page.screenshot({ path: `C:/Users/fstor/AppData/Local/Temp/claude/C--Projects-Engineering-Hub/5f63d63c-e920-43cb-8a68-f6801e2c50be/scratchpad/shots/viewer-${name}.png` });
  });
}
