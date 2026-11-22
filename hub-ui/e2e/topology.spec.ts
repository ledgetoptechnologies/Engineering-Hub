import { expect, test } from "@playwright/test";
import { audit, blocking } from "./audit";

test("opens Topology from Infrastructure and returns to the overview", async ({ page }) => {
  await page.goto("/?demo=1#/infrastructure");
  await page.getByRole("link", { name: "Topology", exact: true }).click();
  await expect(page).toHaveURL(/\/infrastructure\/views\/topology\?demo=1$/);
  await expect(page.getByRole("heading", { name: "Topology.", exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Topology.", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Back to Infrastructure", exact: true }).click();
  await expect(page).toHaveURL(/\/infrastructure\?demo=1$/);
});

const registered = () => ({ items: [
  { id: "build-pc", name: "Build PC", hostname: "build.example.test", addresses: ["192.0.2.20"], network: "192.0.2.0/24", vlan: 20,
    metrics_host: null, role: "Staging builds", retired: false, revision: 1, updated_at: "2026-11-01T12:00:00Z",
    observations: [{ probe_id: "ping", source_label: "Ping check", state: "ok", observed_at: "2026-11-01T12:00:00Z", valid_until: "2027-11-01T12:00:00Z" }],
    open_maintenance: null },
  { id: "backup-nas", name: "Backup NAS", hostname: null, addresses: ["192.0.2.33"], network: "192.0.2.0/24", vlan: 20,
    metrics_host: null, role: "Backup storage", retired: true, revision: 2, updated_at: "2026-11-01T12:00:00Z",
    observations: [], open_maintenance: null },
  { id: "camera-hub", name: "Camera hub", hostname: null, addresses: [], network: null, vlan: null,
    metrics_host: null, role: "Security cameras", retired: false, revision: 1, updated_at: "2026-11-01T12:00:00Z",
    observations: [], open_maintenance: null },
], capabilities: { device_write: false, maintenance_write: false } });

async function fixture(page: import("@playwright/test").Page) {
  const state = { listReads: 0, metricsReads: 0, writes: 0, requestedUrl: "" };
  await page.route("**/api/hub/v1/infrastructure/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname.endsWith("/metrics")) {
      state.metricsReads++;
      return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: { code: "not_configured" } }) });
    }
    if (url.pathname.endsWith("/devices")) {
      state.listReads++;
      state.requestedUrl = url.pathname + url.search;
      if (request.method() !== "GET") state.writes++;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(registered()) });
    }
    state.writes++;
    return route.fulfill({ status: 403, contentType: "application/json", body: "{}" });
  });
  return state;
}

test("shows registered subnet groups, including devices without Prometheus, and links existing details", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/e2e/fixtures/topology.html");

  await expect(page.getByRole("heading", { name: "Topology." })).toBeVisible();
  await expect(page.getByRole("region", { name: "192.0.2.0/24 subnet" })).toContainText("Build PC");
  await expect(page.getByRole("region", { name: "192.0.2.0/24 subnet" })).toContainText("Backup NAS");
  await expect(page.getByRole("region", { name: "192.0.2.0/24 subnet" })).toContainText("Retired");
  await expect(page.getByRole("region", { name: "Unassigned devices" })).toContainText("Camera hub");
  await expect(page.getByRole("region", { name: "192.0.2.0/24 subnet" }).getByRole("link", { name: "Build PC" }))
    .toHaveAttribute("href", "#/infrastructure/build-pc");
  await expect(page.getByText(/does not scan the network/i)).toBeVisible();
  await expect(page.getByText(/Prometheus has no data/i)).toBeVisible();
  await expect(page.locator("body")).not.toContainText(/\b(online|offline|powered|is up|is down)\b/i);
  expect(state).toMatchObject({ listReads: 1, metricsReads: 0, writes: 0, requestedUrl: "/api/hub/v1/infrastructure/devices?include_retired=true" });
});

test("filters subnet groups by declared device facts and stays usable on a phone", async ({ page }) => {
  await fixture(page);
  await page.goto("/e2e/fixtures/topology.html");
  const search = page.getByRole("searchbox", { name: "Filter topology" });
  await search.fill("backup storage");
  await expect(page.getByRole("region", { name: "192.0.2.0/24 subnet" })).toContainText("Backup NAS");
  await expect(page.getByRole("region", { name: "192.0.2.0/24 subnet" })).not.toContainText("Build PC");
  await expect(page.getByRole("region", { name: "Unassigned devices" })).toHaveCount(0);
  await search.fill("no matching device");
  await expect(page.getByText("No registered devices match this filter.")).toBeVisible();
  await search.clear();
  await page.setViewportSize({ width: 375, height: 812 });
  await expect(page.getByRole("region", { name: "Unassigned devices" })).toContainText("Camera hub");
  expect(blocking(await audit(page, "page", 375))).toEqual([]);
});
