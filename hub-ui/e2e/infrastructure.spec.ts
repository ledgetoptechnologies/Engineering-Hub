import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import { audit, blocking } from "./audit";
import metricsFixture from "../src/fixtures/infrastructure-metrics-v1.json" with { type: "json" };
import discoveredFixture from "../src/fixtures/infrastructure-metrics-v2.json" with { type: "json" };

const human = "human:cf:" + "e".repeat(64);
type Reply = { status: number; body: unknown } | "abort";
const at = (minutes: number, base = Date.now()) => new Date(base - minutes * 60_000).toISOString();

function devices(base = Date.now()) {
  const maintenance = { id: "6f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b", revision: 1, device_id: "build-pc",
    reason: "Planned shutdown to replace the disk.\n<img src=x onerror=alert(1)>", expected_end: at(20.5, base), state: "open",
    opened_at: at(90, base), opened_by: { kind: "agent", identity: "hermes" }, resolved_at: null, resolved_by: null, resolution: null };
  return [
    { id: "build-pc", name: "Build PC", hostname: "build.example.test", addresses: ["198.51.100.20"], network: "198.51.100.0/24", vlan: 20,
      role: "Staging builds", retired: false, revision: 3, updated_at: at(600, base),
      observations: [{ probe_id: "exporter", source_label: "Exporter scrape", state: "ok", observed_at: at(2.5, base), valid_until: at(-1, base) }],
      open_maintenance: maintenance },
    { id: "camera-hub", name: "Camera hub", hostname: null, addresses: [], network: null, vlan: null, role: "Security cameras",
      retired: false, revision: 1, updated_at: at(600, base), observations: [], open_maintenance: null },
  ];
}
const list = (caps = { device_write: true, maintenance_write: true }, base = Date.now()) => ({ status: 200, body: { items: devices(base), capabilities: caps } });

interface Options { actor?: string; capability?: boolean; list?: Reply[]; detail?: Reply[]; writes?: Reply[]; metrics?: Reply[] }
async function fixture(page: Page, options: Options = {}) {
  const state = { lists: 0, details: 0, metrics: 0, writes: [] as { method: string; path: string; body: unknown; csrf: string | null }[] };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const respond = (body: unknown, code = 200) => route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify(body) });
    const play = (reply: Reply) => reply === "abort" ? route.abort() : respond(reply.body, reply.status);
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: options.actor ?? human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) {
      const overview = createDemoOverview(new Date());
      if (options.capability !== false) (overview.capabilities as Record<string, unknown>).infrastructure = true;
      return respond(overview);
    }
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    if (path.startsWith("/api/hub/v1/infrastructure/")) {
      if (request.method() !== "GET") {
        state.writes.push({ method: request.method(), path, body: JSON.parse(request.postData() ?? "null"), csrf: request.headers()["x-hub-csrf"] ?? null });
        const replies = options.writes ?? [{ status: 200, body: {} }];
        return play(replies[Math.min(state.writes.length - 1, replies.length - 1)]);
      }
      if (path === "/api/hub/v1/infrastructure/metrics") {
        const replies = options.metrics ?? [{ status: 404, body: { detail: { code: "infrastructure_metrics_not_configured" } } }];
        return play(replies[Math.min(state.metrics++, replies.length - 1)]);
      }
      if (path === "/api/hub/v1/infrastructure/devices") {
        const replies = options.list ?? [list()];
        return play(replies[Math.min(state.lists++, replies.length - 1)]);
      }
      const replies = options.detail ?? [{ status: 200, body: { device: devices()[0], capabilities: { device_write: true, maintenance_write: true },
        maintenance: { items: [devices()[0].open_maintenance], truncated: false } } }];
      return play(replies[Math.min(state.details++, replies.length - 1)]);
    }
    return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
  });
  return state;
}
const devicesPanel = (page: Page) => page.getByRole("region", { name: "Devices" });

test("appears in the menu only when the Hub grants infrastructure access", async ({ page }) => {
  await fixture(page, { capability: false });
  await page.goto("/#/");
  await expect(page.getByRole("navigation", { name: "Workspace navigation" })).toContainText("Staging");
  await expect(page.getByRole("link", { name: "Infrastructure" })).toHaveCount(0);
  await page.unrouteAll();
  await fixture(page);
  await page.reload();
  await page.getByRole("link", { name: "Infrastructure" }).click();
  await expect(devicesPanel(page)).toContainText("Build PC");
});

test("lists devices with each check and maintenance, and supports manual and 30-second refresh", async ({ page }) => {
  await page.clock.install();
  const state = await fixture(page);
  await page.goto("/#/infrastructure");
  const row = devicesPanel(page).getByRole("listitem").filter({ hasText: "Build PC" });
  await expect(row).toContainText("build.example.test · 198.51.100.20");
  await expect(row).toContainText("VLAN 20");
  await expect(row).toContainText("Exporter scrape: Check passed · 2 min ago");
  await expect(row).toContainText("Maintenance: Hermes: Planned shutdown to replace the disk. · overdue by 20 min");
  await expect(devicesPanel(page).getByRole("listitem").filter({ hasText: "Camera hub" })).toContainText("Unknown: no checks set up");
  await expect(devicesPanel(page)).not.toContainText(/\b(online|offline|powered|is up|is down)\b/i);
  await page.clock.runFor(20_000);
  expect(state.lists).toBe(1);
  await devicesPanel(page).getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => state.lists).toBe(2);
  await page.clock.runFor(10_000);
  await expect.poll(() => state.lists).toBe(3);
  expect(blocking(await audit(page, "page", 1440))).toEqual([]);
});

test("filters to devices needing attention or in maintenance", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/infrastructure");
  await expect(devicesPanel(page).getByRole("listitem")).toHaveCount(2);
  await devicesPanel(page).getByRole("button", { name: /Needs attention/ }).click();
  await expect(devicesPanel(page).getByRole("listitem")).toHaveText([/Build PC/]);
  await devicesPanel(page).getByRole("button", { name: /In maintenance/ }).click();
  await expect(devicesPanel(page).getByRole("listitem")).toHaveText([/Build PC/]);
});

test("a check goes out of date by this browser's clock even while hidden and not polling", async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(document, "visibilityState", { get: () => "hidden" }));
  const start = Date.now();
  await page.clock.install({ time: start });
  const state = await fixture(page, { list: [list(undefined, start)] });
  await page.goto("/#/infrastructure");
  const row = devicesPanel(page).getByRole("listitem").filter({ hasText: "Build PC" });
  await expect(row).toContainText("Check passed");
  await page.clock.runFor(2 * 60_000);
  await expect(row).toContainText("Exporter scrape: Out of date · last seen 4 min ago");
  expect(state.lists).toBe(1);
});

test("not allowed, not set up and an older Hub all show the same plain message", async ({ page }) => {
  for (const reply of [{ status: 403, body: { detail: { code: "infrastructure_forbidden" } } },
    { status: 503, body: { detail: { code: "infrastructure_not_configured" } } },
    { status: 403, body: { detail: { code: "gateway_route_forbidden" } } }]) {
    await page.unrouteAll();
    await fixture(page, { list: [reply] });
    await page.goto("/#/infrastructure");
    await expect(page.getByText("Infrastructure isn't available to you on this Hub.")).toBeVisible();
    await expect(devicesPanel(page)).toHaveCount(0);
  }
});

test("a lost sign-in clears the workspace", async ({ page }) => {
  await fixture(page, { list: [{ status: 401, body: { detail: { code: "human_session_required" } } }] });
  await page.goto("/#/infrastructure");
  await expect(page.getByText("Sign in to Engineering Hub.")).toBeVisible();
});

test("a device page shows declared facts, checks and the journal as plain text", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/infrastructure/build-pc");
  const facts = page.getByRole("region", { name: "Declared details" });
  await expect(facts).toContainText("198.51.100.0/24");
  await expect(facts).toContainText("Recorded details aren't checked against the device itself.");
  await expect(page.getByRole("region", { name: "Checks" })).toContainText("doesn't prove the device is powered on or that agents can use it");
  const maintenance = page.getByRole("region", { name: "Maintenance" });
  await expect(maintenance).toContainText("Open · overdue");
  await expect(maintenance).toContainText("that's passed, but it stays open until someone resolves it");
  // Agent-written text is shown, never run or rendered as markup.
  await expect(maintenance).toContainText("<img src=x onerror=alert(1)>");
  await expect(page.locator("img[src=x]")).toHaveCount(0);
});

test("a temporary detail outage preserves the maintenance draft and polling recovers", async ({ page }) => {
  await page.clock.install();
  const device = devices()[1];
  const detail = { device, capabilities: { device_write: false, maintenance_write: true }, maintenance: { items: [], truncated: false } };
  const state = await fixture(page, { detail: [{ status: 200, body: detail },
    { status: 503, body: { detail: { code: "infrastructure_unavailable" } } },
    { status: 200, body: { ...detail, device: { ...device, name: "Camera hub recovered" } } },
    { status: 503, body: { detail: { code: "infrastructure_not_configured" } } }] });
  await page.goto("/#/infrastructure/camera-hub");
  const reason = page.getByRole("form", { name: "Record maintenance" }).getByLabel("Reason");
  await reason.fill("Unsaved maintenance note");
  await page.clock.runFor(30_000);
  const notice = page.getByText("Couldn't refresh device details. Showing the last reading.");
  await expect(notice).toBeVisible();
  await expect(reason).toHaveValue("Unsaved maintenance note");
  await page.clock.runFor(30_000);
  await expect(page.getByRole("heading", { name: /Camera hub recovered/ })).toBeVisible();
  await expect(notice).toHaveCount(0);
  await expect(reason).toHaveValue("Unsaved maintenance note");
  await page.clock.runFor(30_000);
  await expect(reason).toHaveCount(0);
  await expect(page.getByText("Infrastructure isn't available to you on this Hub.")).toBeVisible();
  await page.clock.runFor(30_000);
  expect(state.details).toBe(4);
});

test("records maintenance with CSRF and keeps one request id across a retry", async ({ page }) => {
  const free = { ...devices()[1] };
  const state = await fixture(page, {
    detail: [{ status: 200, body: { device: free, capabilities: { device_write: false, maintenance_write: true }, maintenance: { items: [], truncated: false } } }],
    writes: ["abort", { status: 200, body: {} }] });
  await page.goto("/#/infrastructure/camera-hub");
  const form = page.getByRole("form", { name: "Record maintenance" });
  await form.getByLabel("Reason").fill("Moving to the new rack.");
  await form.getByRole("button", { name: "Record maintenance" }).click();
  await expect(form.getByRole("alert")).toContainText("may or may not have been saved");
  await expect(form.getByLabel("Reason")).toHaveValue("Moving to the new rack.");
  await form.getByRole("button", { name: "Record maintenance" }).click();
  await expect.poll(() => state.writes.length).toBe(2);
  const [first, second] = state.writes;
  expect(first).toMatchObject({ method: "POST", path: "/api/hub/v1/infrastructure/devices/camera-hub/maintenance", csrf: "fixture-csrf" });
  expect(first.body).toMatchObject({ reason: "Moving to the new rack.", expected_end: null });
  expect((second.body as { client_request_id: string }).client_request_id).toBe((first.body as { client_request_id: string }).client_request_id);
  await expect(page.getByRole("region", { name: "Declared details" }).getByRole("button", { name: "Edit" })).toHaveCount(0);
});

test("a resolve that loses a race says so and keeps the text", async ({ page }) => {
  await fixture(page, { writes: [{ status: 409, body: { detail: { code: "revision_conflict" } } }] });
  await page.goto("/#/infrastructure/build-pc");
  const maintenance = page.getByRole("region", { name: "Maintenance" });
  await maintenance.getByRole("button", { name: "Resolve…" }).click();
  const form = page.getByRole("form", { name: "Resolve maintenance" });
  await form.getByLabel("What happened?").fill("Disk replaced.");
  await form.getByRole("button", { name: "Resolve maintenance" }).click();
  await expect(form.getByRole("alert")).toHaveText("Someone changed this first. Reload to see the latest; your text is kept here.");
  await expect(form.getByLabel("What happened?")).toHaveValue("Disk replaced.");
});

test("the owner can add a device; the form catches mistakes before saving", async ({ page }) => {
  const saved = { ...devices()[1], id: "office-printer", name: "Office printer", role: "Printing" };
  const state = await fixture(page, {
    writes: [{ status: 200, body: saved }],
    detail: [{ status: 200, body: { device: saved, capabilities: { device_write: true, maintenance_write: true },
      maintenance: { items: [], truncated: false } } }],
  });
  await page.goto("/#/infrastructure");
  await devicesPanel(page).getByRole("button", { name: "Add device" }).click();
  const form = page.getByRole("form", { name: "Add device" });
  await form.getByLabel("Device ID").fill("Office Printer");
  await form.getByLabel("Name", { exact: true }).fill("Office printer");
  await form.getByLabel("Role").fill("Printing");
  await form.getByLabel(/IP addresses/).fill("192.0.2.40\n198.51.100.300");
  await form.getByLabel(/Network/).fill("192.0.2.0/24");
  await form.getByRole("button", { name: "Add device" }).click();
  await expect(form.getByRole("alert")).toContainText("The device ID must be lowercase");
  await expect(form.getByRole("alert")).toContainText("Not an IP address: 198.51.100.300.");
  expect(state.writes).toHaveLength(0);
  await form.getByLabel("Device ID").fill("office-printer");
  await form.getByLabel(/IP addresses/).fill("192.0.2.40");
  const dialogs: string[] = [];
  page.on("dialog", async dialog => { dialogs.push(dialog.message()); await dialog.dismiss(); });
  await form.getByRole("button", { name: "Add device" }).click();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0]).toMatchObject({ method: "PUT", path: "/api/hub/v1/infrastructure/devices/office-printer", csrf: "fixture-csrf",
    body: { name: "Office printer", hostname: null, addresses: ["192.0.2.40"], network: "192.0.2.0/24", vlan: null, role: "Printing",
      retired: false, expected_revision: 0 } });
  await expect(page).toHaveURL(/\/infrastructure\/office-printer$/);
  await expect(page.getByRole("heading", { level: 1, name: `${saved.name}.`, exact: true })).toBeVisible();
  expect(dialogs).toEqual([]);
});

test("Show retired cannot discard an open Add device draft", async ({ page }) => {
  await fixture(page);
  const active = list().body;
  const retiredDevice = { ...active.items[1], id: "retired-fixture", name: "Retired fixture device", retired: true };
  const queries: Array<string | null> = [];
  await page.route(url => url.pathname === "/api/hub/v1/infrastructure/devices", route => {
    const includeRetired = new URL(route.request().url()).searchParams.get("include_retired");
    queries.push(includeRetired);
    return route.fulfill({ json: includeRetired === "true" ? { ...active, items: [...active.items, retiredDevice] } : active });
  });

  await page.goto("/#/infrastructure");
  const panel = devicesPanel(page);
  const showRetired = panel.getByRole("checkbox", { name: "Show retired" });
  const retiredRow = panel.getByRole("link", { name: /^Retired fixture device/ });
  await panel.getByRole("button", { name: "Add device" }).click();
  const form = page.getByRole("form", { name: "Add device" });
  const name = form.getByLabel("Name", { exact: true });
  await name.fill("Unsaved office printer");

  // Attempt a real click without waiting for Playwright's enabled check.
  await showRetired.scrollIntoViewIfNeeded();
  const box = (await showRetired.boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(name).toHaveValue("Unsaved office printer");
  await expect(showRetired).toBeDisabled();
  await expect(showRetired).not.toBeChecked();
  await expect(retiredRow).toHaveCount(0);
  expect(queries).toEqual([null]);

  page.once("dialog", dialog => dialog.accept());
  await form.getByRole("button", { name: "Cancel" }).click();
  await expect(form).toHaveCount(0);
  await expect(showRetired).toBeEnabled();
  await expect.poll(() => queries).toEqual([null, null]);
  await showRetired.check();
  await expect(retiredRow).toBeVisible();
  expect(queries).toEqual([null, null, "true"]);
});

const editorCases = [
  { title: "Add device", path: "/#/infrastructure", panelName: "Devices", open: "Add device", save: "Add device", count: "lists" },
  { title: "Edit device", path: "/#/infrastructure/build-pc", panelName: "Declared details", open: "Edit", save: "Save changes", count: "details" },
] as const;

for (const editor of editorCases) test(`refreshing the workspace preserves an open ${editor.title} draft and pauses Inventory reads`, async ({ page }) => {
  await page.clock.install();
  const state = await fixture(page);
  await page.goto(editor.path);
  const panel = page.getByRole("region", { name: editor.panelName });
  await panel.getByRole("button", { name: editor.open, exact: true }).click();
  const form = panel.getByRole("form", { name: editor.title });
  const name = form.getByRole("textbox", { name: "Name", exact: true });
  await name.fill("Unsaved workstation");

  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/hub/v1/overview", async route => {
    await held;
    const overview = createDemoOverview(new Date());
    (overview.capabilities as Record<string, unknown>).infrastructure = true;
    await route.fulfill({ json: overview });
  });
  const refreshWorkspace = page.getByRole("button", { name: "Refresh workspace" });
  const refreshInventory = panel.getByRole("button", { name: "Refresh", exact: true });
  const save = form.getByRole("button", { name: editor.save, exact: true });
  try {
    await refreshWorkspace.click();
    await expect(refreshWorkspace).toBeDisabled();
    await expect(name).toHaveValue("Unsaved workstation");
    await expect(save).toBeDisabled();
    await expect(refreshInventory).toBeDisabled();
    release();
    await expect(refreshWorkspace).toBeEnabled();
    await expect(save).toBeEnabled();
    await page.clock.runFor(30_000);
    await expect(name).toHaveValue("Unsaved workstation");
    expect(state[editor.count]).toBe(1);

    page.once("dialog", dialog => dialog.accept());
    await form.getByRole("button", { name: "Cancel" }).click();
    await expect(form).toHaveCount(0);
    await expect(refreshInventory).toBeEnabled();
    await expect.poll(() => state[editor.count]).toBe(2);
  } finally { release(); }
});

test("a device save 401 clears an open Inventory draft when metrics are forbidden", async ({ page }) => {
  const state = await fixture(page, {
    metrics: [{ status: 403, body: { detail: { code: "forbidden" } } }],
    writes: [
      { status: 403, body: { detail: { code: "infrastructure_forbidden" } } },
      { status: 401, body: { detail: { code: "human_session_required" } } },
    ],
  });
  await page.goto("/#/infrastructure");
  const panel = devicesPanel(page);
  await panel.getByRole("button", { name: "Add device" }).click();
  const form = panel.getByRole("form", { name: "Add device" });
  const name = form.getByLabel("Name", { exact: true });
  const submit = form.getByRole("button", { name: "Add device", exact: true });
  await form.getByLabel("Device ID").fill("office-printer");
  await name.fill("Unsaved office printer");
  await form.getByLabel("Role").fill("Printing");

  await submit.click();
  await expect(form.getByRole("alert")).toContainText("You aren't allowed to make this change.");
  await expect(name).toHaveValue("Unsaved office printer");
  await expect(submit).toBeEnabled();

  await submit.click();
  await expect(page.getByText("Sign in to Engineering Hub.")).toBeVisible();
  await expect(form).toHaveCount(0);
  await expect(panel).toHaveCount(0);
  expect(state.writes).toHaveLength(2);
  expect(state.metrics).toBe(1);
});

test("a single network can't hold both IP families; a dual-stack device leaves it empty", async ({ page }) => {
  const state = await fixture(page, { writes: [{ status: 200, body: devices()[0] }] });
  await page.goto("/#/infrastructure");
  await devicesPanel(page).getByRole("button", { name: "Add device" }).click();
  const form = page.getByRole("form", { name: "Add device" });
  await form.getByLabel("Device ID").fill("dual-pc");
  await form.getByLabel("Name", { exact: true }).fill("Dual-stack PC");
  await form.getByLabel("Role").fill("Testing");
  await form.getByLabel(/IP addresses/).fill("198.51.100.20\n2001:db8::20");
  await form.getByLabel(/Network/).fill("198.51.100.0/24");
  await form.getByRole("button", { name: "Add device" }).click();
  await expect(form.getByRole("alert")).toContainText("The network is IPv4, so it can't hold 2001:db8::20.");
  await expect(form.getByRole("alert")).toContainText("Leave the network empty for a device with both IPv4 and IPv6 addresses.");
  expect(state.writes).toHaveLength(0);
  await form.getByLabel(/Network/).fill("");
  await form.getByRole("button", { name: "Add device" }).click();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0].body).toMatchObject({ addresses: ["198.51.100.20", "2001:db8::20"], network: null });
});

test("someone without edit rights sees no editing controls", async ({ page }) => {
  await fixture(page, { list: [list({ device_write: false, maintenance_write: false })] });
  await page.goto("/#/infrastructure");
  await expect(devicesPanel(page)).toContainText("Build PC");
  await expect(devicesPanel(page).getByRole("button", { name: "Add device" })).toHaveCount(0);
});

test("demo mode reads nothing and offers no changes", async ({ page }) => {
  let reads = 0;
  await page.route("**/api/hub/v1/infrastructure/**", (route) => { reads++; return route.abort(); });
  await page.goto("/?demo=1#/infrastructure");
  await expect(devicesPanel(page)).toContainText("Demo: illustrative devices");
  await expect(devicesPanel(page).getByRole("button", { name: "Add device" })).toHaveCount(0);
  await devicesPanel(page).getByRole("link", { name: /Build PC/ }).click();
  await expect(page.getByRole("region", { name: "Maintenance" })).toContainText("Planned shutdown to replace the disk.");
  await expect(page.getByRole("button", { name: "Resolve…" })).toHaveCount(0);
  expect(reads).toBe(0);
});

test("a preference left under the old unscoped key never appears in demo", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("engineering-hub.machine-order.v1", '["lab-node"]'));
  await page.goto("/?demo=1#/infrastructure");
  const panel = healthPanel(page);
  await expect(panel.locator(".machine-card-title")).toHaveText([
    "build", "lab-node", "storage",
  ]);
});

test("fits a phone screen, list and device page", async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/#/infrastructure");
  await expect(devicesPanel(page).getByRole("listitem")).toHaveCount(2);
  expect(blocking(await audit(page, "page", 375))).toEqual([]);
  await page.goto("/#/infrastructure/build-pc");
  await expect(page.getByRole("region", { name: "Maintenance" })).toBeVisible();
  expect(blocking(await audit(page, "page", 375))).toEqual([]);
});

const healthPanel = (page: Page) => page.getByRole("region", { name: "Machine health" });
const metrics = (change: (value: any) => void = () => {}) => {
  const value = JSON.parse(JSON.stringify(metricsFixture)); change(value); return { status: 200, body: value };
};

test("machine health groups each machine's exporters, says what's missing and reads only when asked", async ({ page }) => {
  const state = await fixture(page, { metrics: [metrics(value => {
    for (const item of value.devices) if (["desk-pc", "gpu-box"].includes(item.device_id)) item.device_id = "build-pc";
  })] });
  await page.goto("/#/infrastructure");
  const panel = healthPanel(page);
  const gpuBox = panel.getByRole("listitem").filter({ hasText: "gpu-box" });
  await expect(gpuBox).toContainText("Linux");
  await expect(gpuBox).toContainText("GPUs");
  await expect(gpuBox).toContainText("not reported");
  await expect(gpuBox).toContainText("50%");
  const desk = panel.getByRole("listitem").filter({ hasText: "desk" });
  await expect(desk).toContainText("25%");
  await expect(panel.getByRole("listitem").filter({ hasText: "field-pi" })).toContainText("Out of date");
  await expect(panel.getByRole("listitem").filter({ hasText: "old-box" })).toContainText("Not a recognized exporter");
  expect(state.metrics).toBe(1);
  await page.waitForTimeout(500);
  expect(state.metrics).toBe(1);
  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => state.metrics).toBe(2);
  expect(state.writes).toEqual([]);
  const historyReads: string[] = [];
  page.on("request", request => { if (request.url().includes("/metrics/history?")) historyReads.push(request.url()); });
  await gpuBox.getByRole("link").click();
  await expect(page).toHaveURL(/\/infrastructure\/build-pc$/);
  const details = page.getByRole("region", { name: "Current readings" });
  await expect(details).toContainText("GPU 0 · Synthetic GPU A");
  await expect(details.getByRole("row", { name: /msedge/ })).toContainText("57");
  await expect(details).toContainText("329 GB free of 3.6 TB");
  await expect(page.getByRole("region", { name: "Machine history" })).toContainText("History isn't available for this machine.");
  expect(historyReads).toEqual([]);
});

test("an unreachable source is unknown, never down, and shows no stale readings", async ({ page }) => {
  await fixture(page, { metrics: [metrics(value => {
    value.source.status = "unreachable";
    for (const item of value.devices) Object.assign(item, { up: null, state: "unknown", last_scrape: null });
  })] });
  await page.goto("/#/infrastructure");
  const panel = healthPanel(page);
  await expect(panel).toContainText("Prometheus didn't answer");
  await expect(panel.getByText("Unknown", { exact: true }).first()).toBeVisible();
  await expect(panel.getByRole("meter")).toHaveCount(0);
});

const discovered = (name: keyof typeof discoveredFixture.cases, change: (value: any) => void = () => {}) => {
  const value = JSON.parse(JSON.stringify(discoveredFixture.cases[name])); change(value); return { status: 200, body: value };
};

test("discovered machines show without mappings, flag unregistered and ambiguous ones, and link matched devices", async ({ page }) => {
  await fixture(page, { metrics: [discovered("discovered", value => { value.devices[0].device_id = value.devices[1].device_id = "build-pc";
    Object.assign(value.devices[0], { up: true, state: "fresh", last_scrape: new Date().toISOString(), cpu_busy_ratio: 0.25 }); })] });
  await page.goto("/#/infrastructure");
  const panel = healthPanel(page);
  const cards = panel.getByRole("listitem");
  await expect(cards).toHaveCount(3);
  const matched = cards.filter({ hasText: "demo-workstation" });
  await expect(matched.getByRole("link")).toHaveAttribute("href", "/infrastructure/build-pc");
  await expect(cards.filter({ hasText: "192.0.2.11:9000" })).toContainText("Not in devices");
  await expect(cards.filter({ hasText: "192.0.2.11:9000" })).toContainText("Not a recognized exporter");
  await expect(cards.filter({ hasText: "duplicate-demo" })).toContainText("Matches more than one device");
  await expect(cards.filter({ hasText: "duplicate-demo" }).getByRole("link")).toHaveAttribute("href", /\/infrastructure\/machines\//);
  await page.setViewportSize({ width: 375, height: 812 });
  expect(blocking(await audit(page, "page", 375))).toEqual([]);
});

test("says when Prometheus isn't set up, and when targets were left out", async ({ page }) => {
  await fixture(page, { metrics: [discovered("not_configured"), discovered("partial_truncated")] });
  await page.goto("/#/infrastructure");
  const panel = healthPanel(page);
  await expect(panel).toContainText("isn't connected to Prometheus yet");
  await expect(panel).not.toContainText("isn't scraping any machines");
  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect(panel).toContainText("3 more targets aren't shown.");
  await expect(panel).toContainText("Some readings didn't come back");
  await expect(panel.getByRole("listitem").filter({ hasText: "demo-node" })).toContainText("Not in devices");
});

test("an unreachable discovery source shows no machines and no zeros", async ({ page }) => {
  await fixture(page, { metrics: [discovered("unreachable")] });
  await page.goto("/#/infrastructure");
  const panel = healthPanel(page);
  await expect(panel).toContainText("Prometheus didn't answer");
  await expect(panel.getByRole("listitem")).toHaveCount(0);
  await expect(panel.getByRole("meter")).toHaveCount(0);
});

test("machine health shows even when the device list is refused, and stays hidden when metrics are too", async ({ page }) => {
  const refused = { status: 403, body: { detail: { code: "infrastructure_forbidden" } } };
  await fixture(page, { list: [refused], metrics: [discovered("discovered")] });
  await page.goto("/#/infrastructure");
  await expect(page.getByText("Infrastructure isn't available to you on this Hub.")).toBeVisible();
  await expect(healthPanel(page)).toContainText("demo-workstation");
  await page.unrouteAll();
  await fixture(page, { list: [refused], metrics: [{ status: 403, body: { detail: { code: "forbidden" } } }] });
  await page.reload();
  await expect(page.getByText("Infrastructure isn't available to you on this Hub.")).toBeVisible();
  await expect(healthPanel(page)).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("192.0.2.");
});

test("metrics-only matched and orphaned groups keep opaque details and history without Inventory reads", async ({ page }) => {
  const refused = { status: 403, body: { detail: { code: "infrastructure_forbidden" } } };
  const state = await fixture(page, { list: [refused], detail: [refused], metrics: [discovered("discovered", value => {
    value.devices[0].device_id = value.devices[1].device_id = "build-pc";
    Object.assign(value.devices[0], { up: true, state: "fresh", last_scrape: new Date().toISOString(), cpu_busy_ratio: 0.25 });
  })] });
  const historyReads: string[] = [];
  await page.route("**/api/hub/v1/infrastructure/metrics/history**", route => {
    const url = new URL(route.request().url());
    const host = url.searchParams.get("host") ?? "";
    const range = url.searchParams.get("range") ?? "";
    historyReads.push(`${host}|${range}`);
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: 1,
      generated_at: new Date().toISOString(), host_key: host, range, step_seconds: 30, source: { status: "ok" },
      series: [{ key: "cpu", label: null, unit: "ratio", points: [[1000, 0.2], [1030, 0.6]] }] }) });
  });

  await page.goto("/#/infrastructure");
  const panel = healthPanel(page);
  await expect(page.getByText("Infrastructure isn't available to you on this Hub.")).toBeVisible();
  const matched = panel.getByRole("listitem").filter({ hasText: "demo-workstation" });
  const matchedLink = matched.getByRole("link");
  await expect(matchedLink).toHaveAttribute("href", /\/infrastructure\/machines\//);
  await expect(matchedLink).not.toHaveAttribute("href", "/infrastructure/build-pc");
  await matchedLink.click();
  await expect(page).toHaveURL(/\/infrastructure\/machines\//);
  await expect(page.getByRole("heading", { name: "demo-workstation" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Current readings" })).toContainText("25%");
  await expect(page.getByRole("img", { name: /^CPU:/ })).toBeVisible();
  await expect.poll(() => historyReads.length).toBe(1);

  await page.goBack();
  const orphan = panel.getByRole("listitem").filter({ hasText: "192.0.2.11:9000" });
  await expect(orphan).toContainText("Not in devices");
  const orphanLink = orphan.getByRole("link");
  await expect(orphanLink).toHaveAttribute("href", /\/infrastructure\/machines\//);
  await orphanLink.click();
  await expect(page).toHaveURL(/\/infrastructure\/machines\//);
  await expect(page.getByRole("heading", { name: "192.0.2.11:9000" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Current readings" })).toContainText("Not a recognized exporter");
  await expect(page.getByRole("link", { name: /Machine health/ })).toBeVisible();
  await expect.poll(() => historyReads.length).toBe(2);
  await page.getByRole("combobox", { name: "Time range" }).selectOption("7d");
  await page.getByRole("combobox", { name: "Machine" }).selectOption({ label: "demo-workstation" });
  await expect(page).toHaveURL(/\/infrastructure\/machines\/host:[a-f0-9]{64}\?range=7d$/);
  await expect(page.getByRole("heading", { name: "demo-workstation" })).toBeVisible();
  expect(state.details).toBe(0);
});

test("machine health stays hidden without metrics access, and says so when not set up or unavailable", async ({ page }) => {
  for (const reply of [{ status: 403, body: { detail: { code: "forbidden" } } }, { status: 403, body: { detail: { code: "gateway_route_forbidden" } } }]) {
    await page.unrouteAll();
    await fixture(page, { metrics: [reply] });
    await page.goto("/#/infrastructure");
    await expect(devicesPanel(page)).toContainText("Build PC");
    await expect(healthPanel(page)).toHaveCount(0);
  }
  for (const [reply, text] of [[{ status: 404, body: { detail: { code: "infrastructure_metrics_not_configured" } } }, "isn't connected to Prometheus yet"],
    [{ status: 503, body: { detail: { code: "infrastructure_unavailable" } } }, "aren't available on this Hub right now"]] as const) {
    await page.unrouteAll();
    await fixture(page, { metrics: [reply] });
    await page.goto("/#/infrastructure");
    await expect(devicesPanel(page)).toContainText("Build PC");
    await expect(healthPanel(page)).toContainText(text);
  }
});

test("a device page charts history, keeps gaps, and changes range on request", async ({ page }) => {
  await fixture(page, { metrics: [{ status: 200, body: prioritizedMetrics() }] });
  const ranges: string[] = [];
  await page.route("**/api/hub/v1/infrastructure/metrics/history**", route => {
    const url = new URL(route.request().url());
    ranges.push(url.searchParams.get("range") ?? "");
    const points = [[1000, 0.2], [1030, null], [1060, 0.6], [1090, 0.4]];
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: 1, generated_at: "2026-11-06T12:00:00Z",
      host_key: url.searchParams.get("host"), range: url.searchParams.get("range"), step_seconds: 30, source: { status: "ok" },
      series: [{ key: "cpu", label: null, unit: "ratio", points }, { key: "network_rx", label: null, unit: "bytes_per_second", points: [[1000, 2048], [1030, 4096]] },
        { key: "network_tx", label: null, unit: "bytes_per_second", points: [[1000, 1024], [1030, 512]] }] }) });
  });
  await page.goto("/#/infrastructure/build-pc");
  const cpu = page.getByRole("img", { name: /^CPU:/ });
  await expect(cpu).toHaveAttribute("aria-label", "CPU: latest 40%, peak 60%");
  await expect(page.getByRole("img", { name: /^Network:/ })).toBeVisible();
  await expect(page.getByRole("img", { name: /^Memory:/ })).toHaveCount(0);
  await cpu.focus();
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("ArrowLeft");
  await expect(page.locator(".history-tip")).toContainText("No data");
  await page.getByRole("combobox", { name: "Time range" }).selectOption("7d");
  await expect.poll(() => ranges.at(-1)).toBe("7d");
  expect(ranges[0]).toBe("24h");
  await page.setViewportSize({ width: 375, height: 812 });
  expect(blocking(await audit(page, "page", 375))).toEqual([]);
});

test("history that can't be read says so and offers a retry", async ({ page }) => {
  await fixture(page, { metrics: [{ status: 200, body: prioritizedMetrics() }] });
  let reads = 0;
  await page.route("**/api/hub/v1/infrastructure/metrics/history**", route => {
    if (++reads === 1) return route.fulfill({ status: 500, json: {} });
    const { searchParams } = new URL(route.request().url());
    return route.fulfill({ json: { schema_version: 1, generated_at: new Date().toISOString(),
      host_key: searchParams.get("host"), range: searchParams.get("range"), step_seconds: 720, source: { status: "ok" },
      series: [{ key: "cpu", label: null, unit: "ratio", points: [[3600, null], [4320, null]] }] } });
  });
  await page.goto("/#/infrastructure/build-pc");
  const history = page.getByRole("region", { name: "Machine history" });
  await expect(history.getByRole("alert")).toContainText("History couldn't be read");
  await expect(page.getByRole("region", { name: "Current readings" })).toBeVisible();
  await history.getByRole("button", { name: "Try again" }).click();
  await expect(history).toContainText("No history for this range.");
  await expect(history.getByRole("img")).toHaveCount(0);
  expect(reads).toBe(2);
});

test("a device page shows only that device's machine health", async ({ page }) => {
  await fixture(page, { metrics: [metrics(value => { value.devices[0].device_id = "build-pc"; })] });
  await page.goto("/#/infrastructure/build-pc");
  const panel = page.getByRole("region", { name: "Current readings" });
  await expect(page.getByRole("combobox", { name: "Machine" })).toHaveValue("v1-host:desk");
  await expect(panel.getByRole("row", { name: /msedge/ })).toBeVisible();
  await expect(panel).not.toContainText("gpu-box");
});

test("a current failed scrape says so; an old one is only out of date", async ({ page }) => {
  await fixture(page, { metrics: [metrics(value => {
    Object.assign(value.devices[1], { state: "fresh", last_scrape: new Date(Date.now() - 20_000).toISOString() });
    for (const item of value.devices) if (item.device_id === "gpu-box") item.device_id = "build-pc";
    value.devices[2].gpus = [];
  })] });
  await page.goto("/#/infrastructure");
  const panel = healthPanel(page);
  await expect(panel.getByRole("listitem").filter({ hasText: "field-pi" })).toContainText("Scrape failing");
  await panel.getByRole("listitem").filter({ hasText: "gpu-box" }).getByRole("link").click();
  await expect(page.getByRole("region", { name: "Current readings" })).toContainText("No GPU readings came back.");
});

test("a device page says when Prometheus has no matching machine, and the editor sets the Prometheus host", async ({ page }) => {
  await page.clock.install();
  const state = await fixture(page, { metrics: [metrics()], writes: [{ status: 200, body: devices()[0] }] });
  await page.goto("/#/infrastructure/build-pc");
  await expect(page.getByRole("region", { name: "Machine history" })).toContainText("Prometheus isn't reporting a machine named build.example.test");
  await page.getByRole("region", { name: "Declared details" }).getByRole("button", { name: "Edit" }).click();
  const form = page.getByRole("form", { name: "Edit device" });
  await form.getByLabel(/Prometheus host/).fill("bad host");
  await form.getByRole("button", { name: "Save changes" }).click();
  await expect(form.getByRole("alert")).toContainText("The Prometheus host can use letters");
  expect(state.writes).toHaveLength(0);
  await form.getByLabel(/Prometheus host/).fill("BUILD-PC");
  await page.clock.runFor(30_000);
  await expect(form.getByLabel(/Prometheus host/)).toHaveValue("BUILD-PC");
  expect(state.details).toBe(1);
  await form.getByRole("button", { name: "Save changes" }).click();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0].body).toMatchObject({ hostname: "build.example.test", metrics_host: "BUILD-PC" });
});

function prioritizedMetrics() {
  const value = JSON.parse(JSON.stringify(discoveredFixture.cases.discovered));
  const names = ["Main PC", "Cluster PC", "Hermes", "Staging", "Sync Pi", "DNS", "Zeta"];
  const template = value.devices[0];
  value.devices = names.map((name, index) => {
    const row = JSON.parse(JSON.stringify(template));
    const suffix = (index + 1).toString(16).padStart(64, "0");
    Object.assign(row, {
      host_key: `host:${suffix}`, host_label: name, association: index === 0 ? "matched" : "unregistered",
      device_id: index === 0 ? "build-pc" : null, probe_id: null, job: "node", instance: `machine-${index}:9100`,
      exporter: "node", labels: { host: name }, up: true, last_scrape: new Date().toISOString(), state: "fresh",
      cpu_busy_ratio: 0.25, mem_total_bytes: 1000, mem_available_bytes: 500, disks: [], gpus: [], top_processes: [],
    });
    return row;
  });
  value.source.warnings = [];
  return value;
}

test("new Prometheus targets appear on the next refresh without reloading Infrastructure", async ({ page }) => {
  await page.clock.install();
  const expanded = prioritizedMetrics();
  expanded.devices[1].host_label = "New monitoring host";
  const initial = { ...expanded, devices: [expanded.devices[0]] };
  const state = await fixture(page, { metrics: [{ status: 200, body: initial }, { status: 200, body: expanded }] });
  await page.goto("/#/infrastructure");
  const panel = page.getByRole("region", { name: "Machine health" });
  await expect(panel.getByRole("link", { name: /Main PC/ })).toBeVisible();
  const added = panel.getByRole("link", { name: /New monitoring host/ });
  await expect(added).toHaveCount(0);
  expect(state.metrics).toBe(1);
  await page.clock.runFor(30_000);
  await expect(added).toBeVisible();
  await expect(added).toHaveAttribute("href", /\/infrastructure\/machines\//);
  expect(state.metrics).toBe(2);
});

test("a deferred manual read survives a poll tick, times out and recovers", async ({ page }) => {
  await page.clock.install();
  await fixture(page);
  let reads = 0;
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/hub/v1/infrastructure/devices*", async route => {
    if (++reads === 2) await held;
    await route.fulfill({ json: list().body });
  });
  try {
    await page.goto("/#/infrastructure");
    const panel = devicesPanel(page);
    await expect(panel.getByRole("link", { name: "Build PC" })).toBeVisible();
    await page.clock.runFor(25_000);
    await panel.getByRole("button", { name: "Refresh" }).click();
    await expect.poll(() => reads).toBe(2);
    await page.clock.runFor(6_000);
    expect(reads).toBe(2);
    const notice = panel.getByText("Couldn't refresh device list. Showing the last reading.");
    await expect(notice).toHaveCount(0);
    await page.clock.runFor(10_000);
    await expect(notice).toBeVisible();
    await expect(panel.getByRole("link", { name: "Build PC" })).toBeVisible();
    await page.clock.runFor(20_000);
    await expect(notice).toHaveCount(0);
    expect(reads).toBe(3);
  } finally { release(); }
});

test("Inventory discards the previous filter's reading when the new query fails", async ({ page }) => {
  await fixture(page);
  const queries: Array<string | null> = [];
  const active = list().body;
  const retiredDevice = { ...active.items[1], id: "retired-fixture", name: "Retired fixture device", retired: true };
  await page.route("**/api/hub/v1/infrastructure/devices*", route => {
    const includeRetired = new URL(route.request().url()).searchParams.get("include_retired");
    queries.push(includeRetired);
    if (queries.length === 3) return route.fulfill({ status: 503, json: { detail: { code: "infrastructure_unavailable" } } });
    return route.fulfill({ json: includeRetired === "true" ? { ...active, items: [...active.items, retiredDevice] } : active });
  });
  await page.goto("/#/infrastructure");
  const panel = devicesPanel(page);
  const showRetired = panel.getByRole("checkbox", { name: "Show retired" });
  const retired = panel.getByRole("link", { name: /^Retired fixture device/ });
  const activeRow = panel.getByRole("link", { name: /^Build PC/ });
  await expect(activeRow).toBeVisible();
  await expect(retired).toHaveCount(0);
  await showRetired.check();
  await expect(retired).toBeVisible();

  const failedRead = page.waitForResponse(response => {
    const url = new URL(response.url());
    return url.pathname === "/api/hub/v1/infrastructure/devices" && !url.searchParams.has("include_retired") && response.status() === 503;
  });
  await showRetired.uncheck();
  await failedRead;
  await expect(panel.getByText(/couldn't (refresh device list|be read)/i)).toBeVisible();
  expect(queries).toEqual([null, "true", null]);
  await expect(retired, "the active-only query must not show the retired row from the previous query").toHaveCount(0);
  await expect(panel.getByRole("listitem")).toHaveCount(0);
  await expect(panel.getByRole("alert")).toContainText("Use Refresh to try again");

  await panel.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(activeRow).toBeVisible();
  await expect(retired).toHaveCount(0);
  await expect(showRetired).not.toBeChecked();
  await expect(panel.getByRole("alert")).toHaveCount(0);
  expect(queries).toEqual([null, "true", null, null]);
});

test("a same-query Inventory 429 keeps the last reading and the next poll recovers", async ({ page }) => {
  const now = Date.now();
  await page.clock.install({ time: now });
  const initial = list(undefined, now);
  const recovered = list(undefined, now + 60_000);
  recovered.body.items[0].observations[0].state = "failed";
  const state = await fixture(page, { list: [initial, initial, { status: 429, body: {} }, recovered] });
  const query = "/api/hub/v1/infrastructure/devices?include_retired=true";
  const read = () => page.waitForResponse(response => {
    const url = new URL(response.url());
    return response.request().method() === "GET" && url.pathname + url.search === query;
  });
  await page.goto("/#/infrastructure");
  const panel = devicesPanel(page);
  const reading = panel.getByRole("listitem").filter({ hasText: "Build PC" });
  const showRetired = panel.getByRole("checkbox", { name: "Show retired" });
  await expect(reading).toContainText("Exporter scrape: Check passed");
  const firstRead = read();
  await showRetired.check();
  expect((await firstRead).status()).toBe(200);
  await expect(reading).toContainText("Exporter scrape: Check passed");
  expect(state.lists).toBe(2);

  const rateLimited = read();
  await page.clock.runFor(30_000);
  const failed = await rateLimited;
  expect(failed.status()).toBe(429);
  expect(state.lists).toBe(3);
  const notice = panel.getByRole("status").filter({ hasText: "Couldn't refresh device list. Showing the last reading." });
  const alert = panel.getByRole("alert");
  await expect(notice.or(alert)).toBeVisible();
  // Soft checks still exercise the next poll if retention or its notice regresses.
  expect.soft(await reading.allTextContents(), "retain the last successful reading").toEqual([expect.stringContaining("Exporter scrape: Check passed")]);
  expect.soft(await notice.isVisible(), "show the refresh-failure notice").toBe(true);
  await expect(showRetired).toBeChecked();

  const nextPoll = read();
  await page.clock.runFor(30_000);
  const next = await nextPoll;
  expect(next.status()).toBe(200);
  expect(next.url()).toBe(failed.url());
  expect(state.lists).toBe(4);
  await expect(reading).toContainText("Exporter scrape: Last check failed");
  await expect(notice).toHaveCount(0);
  await expect(alert).toHaveCount(0);
  await expect(showRetired).toBeChecked();
});

test("visible registered device details, current readings and charts refresh every 30 seconds", async ({ page }) => {
  await page.clock.install();
  const metrics = { status: 200, body: prioritizedMetrics() };
  const disabled = { status: 503, body: { detail: { code: "infrastructure_not_configured" } } };
  const state = await fixture(page, { metrics: [metrics, { status: 503, body: {} }, metrics, metrics, disabled] });
  const historyReads: string[] = [];
  await page.route("**/api/hub/v1/infrastructure/metrics/history**", async route => {
    const { searchParams } = new URL(route.request().url());
    const host = searchParams.get("host"), range = searchParams.get("range");
    historyReads.push(`${host}|${range}`);
    if (historyReads.length === 2) return route.fulfill({ status: 503, json: {} });
    if (historyReads.length === 4) return route.fulfill({ status: disabled.status, json: disabled.body });
    await route.fulfill({ json: { schema_version: 1, generated_at: new Date().toISOString(), host_key: host, range,
      step_seconds: 3600, source: { status: "ok" }, series: [{ key: "cpu", label: null, unit: "ratio",
        points: historyReads.length === 1 ? [[3600, 0.2], [7200, 0.6]] : [[10800, 0.8]] }] } });
  });
  await page.goto("/infrastructure/build-pc?range=7d");
  const chart = page.getByRole("img", { name: /^CPU:/ });
  await expect(chart).toHaveAccessibleName(/60%/);
  await chart.press("ArrowRight");
  await expect(page.locator(".history-tip")).toBeVisible();
  await page.clock.runFor(30_000);
  await expect(page.getByText("Couldn't refresh machine health. Showing the last reading.")).toBeVisible();
  await expect(page.getByText("Couldn't refresh history. Showing the last reading.")).toBeVisible();
  await expect(chart).toHaveAccessibleName(/60%/);
  await page.clock.runFor(30_000);
  await expect(chart).toHaveAccessibleName(/80%/);
  await expect(page.locator(".history-tip")).toHaveCount(0);
  await expect(page.getByText(/Couldn't refresh/)).toHaveCount(0);
  expect(state.details).toBe(3);
  expect(state.metrics).toBe(3);
  expect(historyReads).toEqual(Array(3).fill(`host:${"1".padStart(64, "0")}|7d`));
  await expect(page.getByRole("combobox", { name: "Time range" })).toHaveValue("7d");
  await page.clock.runFor(30_000);
  await expect(chart).toHaveCount(0);
  await expect(page.getByText("History isn't available for this machine.")).toBeVisible();
  await page.clock.runFor(30_000);
  await expect(page.getByRole("region", { name: "Machine history" })).toHaveCount(0);
  await page.clock.runFor(30_000);
  expect(historyReads).toHaveLength(4);
  expect(state.metrics).toBe(5);
});

test("registered and discovered pages share graphs-first controls and keep the range through switching, reload and Back", async ({ page }) => {
  const state = await fixture(page, { metrics: [{ status: 200, body: prioritizedMetrics() }] });
  const historyReads: string[] = [];
  await page.route("**/api/hub/v1/infrastructure/metrics/history**", route => {
    const { searchParams } = new URL(route.request().url());
    const host = searchParams.get("host"), range = searchParams.get("range");
    historyReads.push(`${host}|${range}`);
    return route.fulfill({ json: { schema_version: 1, generated_at: new Date().toISOString(), host_key: host, range,
      step_seconds: 3600, source: { status: "ok" }, series: [{ key: "cpu", label: null, unit: "ratio", points: [[3600, 0.2], [7200, 0.6]] }] } });
  });
  await page.goto("/infrastructure/build-pc?range=7d");
  const history = page.getByRole("region", { name: "Machine history" });
  const current = page.getByRole("region", { name: "Current readings" });
  const time = page.getByRole("combobox", { name: "Time range" });
  const machine = page.getByRole("combobox", { name: "Machine" });
  await expect(time).toHaveValue("7d");
  await expect(history.getByRole("img", { name: /^CPU:/ })).toBeVisible();
  expect((await history.boundingBox())!.y).toBeLessThan((await current.boundingBox())!.y);
  expect((await history.boundingBox())!.y).toBeLessThan((await page.getByRole("region", { name: "Declared details" }).boundingBox())!.y);
  const mainKey = `host:${"1".padStart(64, "0")}`, clusterKey = `host:${"2".padStart(64, "0")}`;
  expect(historyReads).toEqual([`${mainKey}|7d`]);
  await time.selectOption("6h");
  await expect(page).toHaveURL(/\/infrastructure\/build-pc\?range=6h$/);
  await machine.selectOption({ label: "Cluster PC" });
  await expect(page).toHaveURL(/\/infrastructure\/machines\/host:[a-f0-9]{64}\?range=6h$/);
  await expect(page.getByRole("heading", { name: "Cluster PC" })).toBeVisible();
  await expect(time).toHaveValue("6h");
  await expect.poll(() => historyReads.at(-1)).toBe(`${clusterKey}|6h`);
  expect(state.details).toBe(1);
  expect(state.metrics).toBe(1);
  await machine.selectOption({ label: "Main PC" });
  await expect(page).toHaveURL(/\/infrastructure\/build-pc\?range=6h$/);
  await expect(time).toHaveValue("6h");
  await page.goBack();
  await expect(page.getByRole("heading", { name: "Cluster PC" })).toBeVisible();
  await expect(time).toHaveValue("6h");
  await page.goBack();
  await expect(time).toHaveValue("6h");
  await page.reload();
  await expect(time).toHaveValue("6h");
  await page.goBack();
  await expect(time).toHaveValue("7d");
});

test("a shared inventory record keeps the selected opaque machine without accepting an unrelated host", async ({ page }) => {
  const body = prioritizedMetrics();
  Object.assign(body.devices[1], { device_id: "build-pc", association: "matched", cpu_busy_ratio: 0.75 });
  await fixture(page, { metrics: [{ status: 200, body }] });
  const reads: string[] = [];
  await page.route("**/api/hub/v1/infrastructure/metrics/history**", route => {
    reads.push(new URL(route.request().url()).searchParams.get("host") ?? "");
    return route.fulfill({ status: 404, json: {} });
  });
  await page.goto("/infrastructure");
  await expect(healthPanel(page).getByRole("link", { name: /Cluster PC/ })).toHaveAttribute("href", /\/infrastructure\/build-pc\?host=/);
  await healthPanel(page).getByRole("link", { name: /Cluster PC/ }).click();
  const cluster = body.devices[1].host_key;
  await expect(page).toHaveURL(`/infrastructure/build-pc?host=${encodeURIComponent(cluster)}`);
  await expect(page.getByRole("combobox", { name: "Machine" })).toHaveValue(cluster);
  await expect(page.getByRole("meter", { name: "CPU" })).toHaveAttribute("aria-valuenow", "75");
  await expect.poll(() => reads).toEqual([cluster]);

  await page.goto(`/infrastructure/build-pc?host=${encodeURIComponent(body.devices[2].host_key)}&range=7d`);
  await expect(page.getByRole("region", { name: "Machine history" })).toContainText("No current target matches this machine link.");
  await expect(page.getByRole("region", { name: "Current readings" })).toHaveCount(0);
  expect(reads).toEqual([cluster]);
});

test("populated charts keep readable, non-overlapping axes and controls at desktop, tablet and phone widths", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const consoleErrors: string[] = [];
  page.on("pageerror", error => consoleErrors.push(error.message));
  await page.goto("/?demo=1#/infrastructure/build-pc?range=30d");
  const history = page.getByRole("region", { name: "Machine history" });
  const charts = history.getByRole("img");
  await expect(charts).toHaveCount(4);
  await expect(page).toHaveURL(/\?demo=1&range=30d$/);
  for (const [width, height, name] of [[1440, 1100, "desktop"], [834, 1112, "tablet"], [375, 900, "mobile"], [320, 780, "small-phone"]] as const) {
    await page.setViewportSize({ width, height });
    await expect.poll(() => charts.evaluateAll(nodes => nodes.every(node => {
      const box = node.getBoundingClientRect();
      const time = [...node.querySelectorAll(":scope > text")];
      const all = [...node.querySelectorAll("text")];
      return time.length >= 3 && all.every(text => {
        const rect = text.getBoundingClientRect();
        return Number.parseFloat(getComputedStyle(text).fontSize) >= 12 && rect.left >= box.left && rect.right <= box.right + 1;
      }) && time.every((text, i) => i === 0 || text.getBoundingClientRect().left >= time[i - 1].getBoundingClientRect().right + 4);
    }))).toBe(true);
    for (const control of await history.getByRole("combobox").all()) {
      const box = (await control.boundingBox())!;
      expect(box.height).toBeGreaterThanOrEqual(44);
      expect(box.x + box.width).toBeLessThanOrEqual(width);
    }
    expect(blocking(await audit(page, "page", width))).toEqual([]);
    if (name === "desktop" || name === "mobile") {
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: test.info().outputPath(`machine-workspace-${name}.png`) });
      if (name === "mobile") await history.screenshot({ path: test.info().outputPath("machine-workspace-mobile-charts.png") });
    }
  }
  expect(consoleErrors).toEqual([]);
});

test("prioritizes compact machine cards and drills into device and discovered-machine charts without overview history reads", async ({ page }) => {
  const state = await fixture(page, { metrics: [{ status: 200, body: prioritizedMetrics() }] });
  const historyReads: string[] = [];
  await page.route("**/api/hub/v1/infrastructure/metrics/history**", route => {
    const url = new URL(route.request().url());
    const host = url.searchParams.get("host") ?? "";
    const range = url.searchParams.get("range") ?? "";
    historyReads.push(`${host}|${range}`);
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: 1,
      generated_at: new Date().toISOString(), host_key: host, range, step_seconds: 30, source: { status: "ok" },
      series: [{ key: "cpu", label: null, unit: "ratio", points: [[1000, 0.2], [1030, null], [1060, 0.6]] }] }) });
  });

  await page.goto("/#/infrastructure");
  const panel = healthPanel(page);
  const cards = panel.getByRole("listitem");
  await expect(cards).toHaveCount(7);
  await expect(panel.locator(".machine-card-title").evaluateAll(nodes => nodes.map(node => node.textContent?.trim()))).resolves.toEqual([
    "Main PC", "Cluster PC", "Hermes", "Staging", "Sync Pi", "DNS", "Zeta",
  ]);
  await expect(cards.first()).toContainText("25%");
  await expect(cards.filter({ hasText: "Cluster PC" }).getByRole("link")).toHaveCount(1);
  await expect.poll(() => state.metrics).toBe(1);
  await page.waitForTimeout(200);
  expect(historyReads).toEqual([]);

  const main = panel.getByRole("link", { name: /Main PC/ });
  await main.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/infrastructure\/build-pc$/);
  await expect(page.getByRole("img", { name: /^CPU:/ })).toBeVisible();
  expect(historyReads).toHaveLength(1);
  await page.goBack();
  await expect(panel.locator(".machine-card-title").first()).toHaveText("Main PC");

  const discovered = panel.getByRole("link", { name: /Cluster PC/ });
  const direct = await discovered.getAttribute("href");
  expect(direct).toContain("/infrastructure/machines/");
  await page.goto(new URL(direct!, page.url()).href);
  await expect(page.getByRole("heading", { name: "Cluster PC" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Current readings" }).getByRole("meter", { name: "Memory" })).toHaveAttribute("aria-valuenow", "50");
  await expect(page.getByRole("img", { name: /^CPU:/ })).toBeVisible();
  await expect(page.getByRole("link", { name: /Machine health/ })).toBeVisible();
  expect(historyReads.at(-1)).toContain("|24h");
  await page.getByRole("link", { name: /Machine health/ }).click();
  await expect(page).toHaveURL(/\/infrastructure$/);

  await page.setViewportSize({ width: 375, height: 812 });
  expect(blocking(await audit(page, "page", 375))).toEqual([]);
});

test("machine order preferences stay browser-local and reorder by exact normalized display alias", async ({ page }) => {
  await fixture(page, { metrics: [{ status: 200, body: prioritizedMetrics() }] });
  await page.goto("/#/infrastructure");
  const panel = healthPanel(page);
  await panel.getByText("Customize order", { exact: true }).click();
  const order = panel.getByLabel("Priority machine names");
  await order.fill("DNS\nmain-pc");
  await panel.getByRole("button", { name: "Save order" }).click();
  await expect(panel.locator(".machine-card-title").evaluateAll(nodes => nodes.map(node => node.textContent?.trim()))).resolves.toEqual([
    "DNS", "Main PC", "Cluster PC", "Hermes", "Staging", "Sync Pi", "Zeta",
  ]);
  const stored = await page.evaluate(actor => localStorage.getItem(`engineering-hub.machine-order.v1.live.${encodeURIComponent(actor)}`), human);
  expect(stored).toBe('["dns","main pc"]');
  expect(stored).not.toContain("host:");
  expect(await page.evaluate(() => localStorage.getItem("engineering-hub.machine-order.v1"))).toBeNull();
  await page.reload();
  await expect(healthPanel(page).locator(".machine-card-title").first()).toHaveText("DNS");
});

test("machine-order preferences persist separately for each live actor", async ({ page }) => {
  const actorA = human;
  const actorB = `human:cf:${"f".repeat(64)}`;
  await fixture(page, { actor: actorA, metrics: [{ status: 200, body: prioritizedMetrics() }] });
  await page.goto("/#/infrastructure");
  let panel = healthPanel(page);
  await panel.getByText("Customize order", { exact: true }).click();
  let order = panel.getByLabel("Priority machine names");
  await order.fill("DNS\nmain-pc");
  await panel.getByRole("button", { name: "Save order" }).click();
  await expect(panel.locator(".machine-card-title").first()).toHaveText("DNS");

  await page.unrouteAll();
  await fixture(page, { actor: actorB, metrics: [{ status: 200, body: prioritizedMetrics() }] });
  await page.reload();
  panel = healthPanel(page);
  await panel.getByText("Customize order", { exact: true }).click();
  order = panel.getByLabel("Priority machine names");
  await expect(order).toHaveValue("main pc\ncluster pc\nhermes\nstaging\nsync pi\ndns");
  await order.fill("Zeta\nCluster PC");
  await panel.getByRole("button", { name: "Save order" }).click();
  await expect(panel.locator(".machine-card-title").first()).toHaveText("Zeta");

  await page.unrouteAll();
  await fixture(page, { actor: actorA, metrics: [{ status: 200, body: prioritizedMetrics() }] });
  await page.reload();
  panel = healthPanel(page);
  await panel.getByText("Customize order", { exact: true }).click();
  order = panel.getByLabel("Priority machine names");
  await expect(order).toHaveValue("dns\nmain pc");
  await expect(panel.locator(".machine-card-title").first()).toHaveText("DNS");
});
