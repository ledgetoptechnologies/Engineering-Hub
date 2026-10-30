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

interface Options { capability?: boolean; list?: Reply[]; detail?: Reply[]; writes?: Reply[]; metrics?: Reply[] }
async function fixture(page: Page, options: Options = {}) {
  const state = { lists: 0, details: 0, metrics: 0, writes: [] as { method: string; path: string; body: unknown; csrf: string | null }[] };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const respond = (body: unknown, code = 200) => route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify(body) });
    const play = (reply: Reply) => reply === "abort" ? route.abort() : respond(reply.body, reply.status);
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
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

test("lists devices with each check and maintenance, worded honestly, and reads only when asked", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#/infrastructure");
  const row = devicesPanel(page).getByRole("listitem").filter({ hasText: "Build PC" });
  await expect(row).toContainText("build.example.test · 198.51.100.20");
  await expect(row).toContainText("VLAN 20");
  await expect(row).toContainText("Exporter scrape: Check passed · 2 min ago");
  await expect(row).toContainText("Maintenance: Hermes: Planned shutdown to replace the disk. · overdue by 20 min");
  await expect(devicesPanel(page).getByRole("listitem").filter({ hasText: "Camera hub" })).toContainText("Unknown: no checks set up");
  await expect(devicesPanel(page)).not.toContainText(/\b(online|offline|powered|is up|is down)\b/i);
  await page.waitForTimeout(500);
  expect(state.lists).toBe(1);
  await devicesPanel(page).getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => state.lists).toBe(2);
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

test("a check goes out of date by this browser's clock without reading again", async ({ page }) => {
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
    { status: 503, body: { detail: { code: "infrastructure_disabled" } } },
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
  const state = await fixture(page, { writes: [{ status: 200, body: devices()[1] }] });
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
  await form.getByRole("button", { name: "Add device" }).click();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0]).toMatchObject({ method: "PUT", path: "/api/hub/v1/infrastructure/devices/office-printer", csrf: "fixture-csrf",
    body: { name: "Office printer", hostname: null, addresses: ["192.0.2.40"], network: "192.0.2.0/24", vlan: null, role: "Printing",
      retired: false, expected_revision: 0 } });
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
  const state = await fixture(page, { metrics: [metrics()] });
  await page.goto("/#/infrastructure");
  const panel = healthPanel(page);
  const gpuBox = panel.getByRole("listitem").filter({ hasText: "gpu-box" });
  await expect(gpuBox).toContainText("Linux");
  await expect(gpuBox).toContainText("GPUs");
  await expect(gpuBox).toContainText("GPU 0 · Synthetic GPU A");
  await expect(gpuBox).toContainText("not reported");
  await expect(gpuBox.getByRole("meter", { name: "CPU" })).toHaveAttribute("aria-valuenow", "50");
  const desk = panel.getByRole("listitem").filter({ hasText: "desk" });
  await expect(desk.getByRole("row", { name: /msedge/ })).toContainText("57");
  await expect(desk).toContainText("329 GB free of 3.6 TB");
  await expect(panel.getByRole("listitem").filter({ hasText: "field-pi" })).toContainText("Out of date");
  await expect(panel.getByRole("listitem").filter({ hasText: "old-box" })).toContainText("Not a recognized exporter");
  expect(state.metrics).toBe(1);
  await panel.getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => state.metrics).toBe(2);
  expect(state.writes).toEqual([]);
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
  await fixture(page, { metrics: [discovered("discovered", value => { value.devices[0].device_id = value.devices[1].device_id = "build-pc"; })] });
  await page.goto("/#/infrastructure");
  const panel = healthPanel(page);
  const cards = panel.getByRole("listitem");
  await expect(cards).toHaveCount(3);
  const workstation = cards.filter({ hasText: "demo-workstation" });
  await expect(workstation).toContainText("Windows");
  await expect(workstation).toContainText("GPUs");
  await expect(workstation.getByRole("link", { name: "demo-workstation" })).toHaveAttribute("href", "/infrastructure/build-pc");
  await expect(cards.filter({ hasText: "192.0.2.11:9000" })).toContainText("Not in devices");
  await expect(cards.filter({ hasText: "192.0.2.11:9000" })).toContainText("Not a recognized exporter");
  await expect(cards.filter({ hasText: "duplicate-demo" })).toContainText("Matches more than one device");
  await expect(cards.filter({ hasText: "duplicate-demo" }).getByRole("link")).toHaveCount(0);
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

test("machine health stays hidden without metrics access or when not set up", async ({ page }) => {
  for (const reply of [{ status: 403, body: { detail: { code: "forbidden" } } }, { status: 404, body: { detail: { code: "infrastructure_metrics_not_configured" } } }]) {
    await page.unrouteAll();
    await fixture(page, { metrics: [reply] });
    await page.goto("/#/infrastructure");
    await expect(devicesPanel(page)).toContainText("Build PC");
    await expect(healthPanel(page)).toHaveCount(0);
  }
});

test("a device page shows only that device's machine health", async ({ page }) => {
  await fixture(page, { metrics: [metrics(value => { value.devices[0].device_id = "build-pc"; })] });
  await page.goto("/#/infrastructure/build-pc");
  const panel = healthPanel(page);
  await expect(panel).toContainText("desk");
  await expect(panel).not.toContainText("gpu-box");
});

test("a current failed scrape says so; an old one is only out of date", async ({ page }) => {
  await fixture(page, { metrics: [metrics(value => {
    Object.assign(value.devices[1], { state: "fresh", last_scrape: new Date(Date.now() - 20_000).toISOString() });
    value.devices[2].gpus = [];
  })] });
  await page.goto("/#/infrastructure");
  const panel = healthPanel(page);
  await expect(panel.getByRole("listitem").filter({ hasText: "field-pi" })).toContainText("Scrape failing");
  await expect(panel.getByRole("listitem").filter({ hasText: "gpu-box" })).toContainText("No GPU readings came back.");
});
