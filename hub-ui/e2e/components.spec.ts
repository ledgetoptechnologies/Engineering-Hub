import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";
import { DemoComponentStore } from "../src/componentsDemo";
import { audit, blocking } from "./audit";

const human = "human:cf:" + "e".repeat(64);
type Overview = ReturnType<typeof createDemoOverview>;

/** The demo store plays the component API behind the real gateway paths, so the live code path is exercised. */
async function fixture(page: Page, options: { actor?: string; unsupported?: boolean; parent?: boolean } = {}) {
  const overview = createDemoOverview(new Date()) as Overview & { projects: Record<string, unknown>[] };
  overview.projects = overview.projects.map((project) => ({ ...project, description: "", repository_url: null, lead_agent: null,
    status: "on_track", parent_project: options.parent && project.id !== "ledge-top-ops" && project.id !== "engineering-hub" ? "ledge-top-ops" : null,
    archived: false, revision: 1 }));
  const store = new DemoComponentStore();
  const state = { writes: [] as { method: string; path: string; body: Record<string, unknown>; csrf?: string }[], conflictNext: false, forbidNext: false };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method();
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    const fail = (error: unknown) => {
      const e = error as { status: number; code: string };
      return respond({ detail: { code: e.code === "component_conflict" ? "revision_conflict" : e.code === "component_not_found" ? "not_found" : e.code } }, e.status);
    };
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: options.actor ?? human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(overview);
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    const project = /^\/api\/hub\/v1\/projects\/([a-z0-9-]+)$/.exec(path);
    if (project && method === "GET") return respond(overview.projects.find((item) => item.id === project[1]));
    const list = /^\/api\/hub\/v1\/projects\/([a-z0-9-]+)\/components$/.exec(path);
    const one = /^\/api\/hub\/v1\/components\/([0-9a-f-]{36})(\/retire)?$/.exec(path);
    if ((list || one) && options.unsupported) return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
    if (list && method === "GET") {
      expect(url.searchParams.get("include_retired")).toBe("true");
      return respond(store.list(list[1]));
    }
    if (method !== "GET" && (list || one)) {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.writes.push({ method, path, body, csrf: request.headers()["x-hub-csrf"] });
      if (state.forbidNext) { state.forbidNext = false; return respond({ detail: { code: "human_session_required" } }, 403); }
      try {
        if (list) return respond(store.create(list[1], body as never), 201);
        const id = one![1];
        if (state.conflictNext) { state.conflictNext = false; return respond({ detail: { code: "revision_conflict" } }, 409); }
        if (one![2]) return respond(store.retire(id, body.expected_revision as number));
        return respond(store.update(id, body as never));
      } catch (error) { return fail(error); }
    }
    if (one && method === "GET") {
      for (const id of ["ledge-top-ops", "treedar", "project-alpha", "engineering-hub", "3d-viewer"]) {
        const row = store.list(id).items.find((item) => item.id === one[1]);
        if (row) return respond(row);
      }
      return respond({ detail: { code: "not_found" } }, 404);
    }
    return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
  });
  return { state, store };
}
const panel = (page: Page) => page.getByRole("region", { name: "Components" });
const cards = (page: Page) => panel(page).getByRole("list", { name: /components$/ }).getByRole("listitem").filter({ has: page.locator("h3") });

test("a project shows its components as readable cards, active first, retired tucked away", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/projects/project-alpha");
  await expect(cards(page).locator("h3")).toHaveText(["Community", "Enterprise", "iOS app"]);
  await expect(panel(page)).toContainText("3 components · 1 planned · 1 paused");
  await expect(cards(page).first()).toContainText("Web app · community");
  await page.goto("/#/projects/treedar");
  await expect(cards(page).locator("h3")).toHaveText(["TreeDAR processing"]);
  await panel(page).getByRole("button", { name: "Show retired (1)" }).click();
  await expect(cards(page).locator("h3")).toHaveText(["TreeDAR processing", "Field scanner"]);
  // A retired component can be edited (to restore it) but not retired again.
  await expect(panel(page).getByRole("button", { name: "Retire Field scanner" })).toHaveCount(0);
});

test("a person adds a component; the address follows the name and the request carries CSRF and a request key", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto("/#/projects/ledge-top-ops");
  await panel(page).getByRole("button", { name: "Add component" }).click();
  const form = page.getByRole("form", { name: "New component" });
  await form.getByLabel("Name", { exact: true }).fill("Client Portal");
  await expect(form.getByLabel("Address")).toHaveValue("client-portal");
  await form.getByLabel("Kind").selectOption("web_app");
  await form.getByLabel("Status").selectOption("planned");
  await form.getByRole("button", { name: "Add component" }).click();
  await expect(panel(page).getByRole("status")).toHaveText("Added Client Portal.");
  await expect(cards(page).locator("h3")).toContainText(["Client Portal"]);
  const write = state.writes[0];
  expect(write).toMatchObject({ method: "POST", path: "/api/hub/v1/projects/ledge-top-ops/components", csrf: "fixture-csrf" });
  expect(write.body).toMatchObject({ slug: "client-portal", name: "Client Portal", kind: "web_app", status: "planned", repository_url: null });
  expect(typeof write.body.client_request_id).toBe("string");
});

test("an address already used in the project is caught before saving", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto("/#/projects/ledge-top-ops");
  await panel(page).getByRole("button", { name: "Add component" }).click();
  const form = page.getByRole("form", { name: "New component" });
  await form.getByLabel("Name", { exact: true }).fill("Windows app");
  await expect(form).toContainText("Another component here already uses this address.");
  await expect(form.getByRole("button", { name: "Add component" })).toBeDisabled();
  expect(state.writes).toEqual([]);
});

test("editing sends only what changed with the seen revision; a conflict is explained, not overwritten", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto("/#/projects/ledge-top-ops");
  await panel(page).getByRole("button", { name: "Edit Windows app" }).click();
  const form = page.getByRole("form", { name: "Edit component" });
  await expect(form.getByRole("button", { name: "Save changes" })).toBeDisabled();
  await form.getByLabel("Status").selectOption("active");
  state.conflictNext = true;
  await form.getByRole("button", { name: "Save changes" }).click();
  await expect(form.getByRole("alert")).toContainText("Someone changed this component after you opened it. Nothing was overwritten.");
  await expect(form.getByRole("button", { name: "Save changes" })).toBeDisabled();
  expect(state.writes[0]).toMatchObject({ method: "PATCH", body: { expected_revision: 1, status: "active" }, csrf: "fixture-csrf" });
  await form.getByRole("button", { name: "Close" }).click();
  await panel(page).getByRole("button", { name: "Edit Windows app" }).click();
  await page.getByRole("form", { name: "Edit component" }).getByLabel("Status").selectOption("active");
  await page.getByRole("form", { name: "Edit component" }).getByRole("button", { name: "Save changes" }).click();
  await expect(panel(page).getByRole("status")).toHaveText("Saved Windows app.");
  await expect(cards(page).filter({ hasText: "Windows app" })).toContainText("Active");
});

test("retiring asks first, keeps the component for reference and sends the revision", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto("/#/projects/ledge-top-ops");
  await panel(page).getByRole("button", { name: "Retire Thumbnail package" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
  expect(state.writes).toEqual([]);
  await panel(page).getByRole("button", { name: "Retire Thumbnail package" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Retire component" }).click();
  await expect(panel(page).getByRole("status")).toContainText("Retired Thumbnail package. It's kept for reference.");
  await expect(cards(page).locator("h3")).toHaveText(["Operations Viewer", "Windows app"]);
  expect(state.writes[0]).toMatchObject({ method: "POST", body: { expected_revision: 1 } });
  expect(state.writes[0].path).toMatch(/\/retire$/);
});

test("a refused change is explained and doesn't sign you out", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto("/#/projects/ledge-top-ops");
  await panel(page).getByRole("button", { name: "Retire Windows app" }).click();
  state.forbidNext = true;
  await page.getByRole("dialog").getByRole("button", { name: "Retire component" }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("Only a signed-in person can change components.");
  await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
  await expect(cards(page)).toHaveCount(3);
});

test("an agent session reads components with no edit controls", async ({ page }) => {
  await fixture(page, { actor: "codex" });
  await page.goto("/#/projects/project-alpha");
  await expect(cards(page)).toHaveCount(3);
  await expect(panel(page).getByRole("button", { name: /Add component|Edit|Retire/ })).toHaveCount(0);
});

test("a Hub without components says so and offers no editing", async ({ page }) => {
  await fixture(page, { unsupported: true });
  await page.goto("/#/projects/project-alpha");
  await expect(panel(page)).toContainText("This Hub doesn't list components yet.");
  await expect(panel(page).getByRole("button")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: /Project Alpha/ })).toBeVisible();
});

test("an umbrella project lists the projects grouped under it with their component counts", async ({ page }) => {
  await fixture(page, { parent: true });
  await page.goto("/#/projects/ledge-top-ops");
  const group = page.getByRole("region", { name: "Projects in this group" });
  await expect(group.getByRole("link", { name: /Project Alpha/ })).toContainText("3 components · 1 planned · 1 paused");
  await expect(group.getByRole("link", { name: /TreeDar/ })).toContainText("1 component");
});

test("demo mode shows placeholder components without any live address", async ({ page }) => {
  await page.goto("/?demo=1#/projects/ledge-top-ops");
  await expect(cards(page)).toHaveCount(3);
  await expect(panel(page).getByRole("link", { name: /Repository|Package/ })).toHaveCount(0);
});

test("fits a phone screen", async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/#/projects/project-alpha");
  await expect(cards(page)).toHaveCount(3);
  expect(blocking(await audit(page, "page", 375))).toEqual([]);
  await panel(page).getByRole("button", { name: "Edit Community" }).click();
  await expect(page.getByRole("form", { name: "Edit component" }).getByLabel("Name", { exact: true })).toHaveValue("Community");
  expect(blocking(await audit(page, "dialog", 375))).toEqual([]);
});
