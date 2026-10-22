import { expect, test, type Page } from "@playwright/test";
import { createDemoOverview, createDemoTopic } from "../src/mocks";

const human = "human:cf:" + "e".repeat(64);
type Overview = ReturnType<typeof createDemoOverview>;

async function fixture(page: Page, options: { gateway?: boolean } = {}) {
  const overview = createDemoOverview(new Date()) as Overview & { projects: Record<string, unknown>[] };
  overview.projects = overview.projects.map((project) => ({ ...project, description: "", repository_url: null, lead_agent: null,
    status: "on_track", parent_project: null, archived: false, revision: 1 }));
  const state = { writes: [] as { method: string; path: string; body: Record<string, unknown>; csrf?: string }[], conflict: false };
  await page.route("**/api/hub/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method();
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: human, github_id: null,
      auth_provider: "cloudflare_access", email: "operator@example.test", csrf_token: "fixture-csrf" });
    if (path.endsWith("/overview")) return respond(overview);
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "f", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test"));
    if (path.endsWith("/work/status")) return respond({}, 423);
    const match = /^\/api\/hub\/v1\/projects(?:\/([a-z0-9-]+))?(\/archive)?$/.exec(path);
    if (match) {
      if (options.gateway) return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
      const [, id, archive] = match;
      if (method === "GET" && id) return respond(overview.projects.find((item) => item.id === id));
      const body = request.postDataJSON() as Record<string, unknown>;
      state.writes.push({ method, path, body, csrf: request.headers()["x-hub-csrf"] });
      if (state.conflict) return respond({ detail: { code: "revision_conflict" } }, 409);
      if (method === "POST" && !id) {
        const created = { id: body.project_id, name: body.name, environment: "staging", executor_enabled: false, health: "unknown",
          reason: "metadata_only", description: body.description, repository_url: body.repository_url, lead_agent: body.lead_agent,
          status: body.status, parent_project: body.parent_project, archived: false, revision: 1 };
        overview.projects.push(created);
        return respond(created, 201);
      }
      const project = overview.projects.find((item) => item.id === id)!;
      if (archive) Object.assign(project, { archived: true, revision: (project.revision as number) + 1 });
      else Object.assign(project, { ...body, revision: (project.revision as number) + 1 });
      return respond(project);
    }
    return respond({ detail: { code: "gateway_route_forbidden" } }, 403);
  });
  return { overview, state };
}

test("a person creates a notes-only project; the ID follows the name and nothing else is enabled", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto("/#/projects");
  await page.getByRole("button", { name: "New project" }).click();
  const form = page.getByRole("form", { name: "New project" });
  await form.getByLabel("Name", { exact: true }).fill("Mlsna LandScript");
  await expect(form.getByLabel("Project ID")).toHaveValue("mlsna-landscript");
  await form.getByLabel("Description").fill("Client project, notes only.");
  await form.getByLabel("Status").selectOption("At risk");
  await form.getByRole("button", { name: "Create project" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(state.writes).toHaveLength(1);
  expect(state.writes[0]).toMatchObject({ method: "POST", path: "/api/hub/v1/projects", csrf: "fixture-csrf",
    body: { project_id: "mlsna-landscript", name: "Mlsna LandScript", description: "Client project, notes only.",
      repository_url: null, lead_agent: null, status: "at_risk", parent_project: null } });
  expect(typeof state.writes[0].body.client_request_id).toBe("string");
  await expect(page.getByRole("link", { name: /Mlsna LandScript/ }).first()).toBeVisible();
});

test("editing reads the latest revision, sends it with CAS, and shows the human status and repository", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto("/#/projects/treedar");
  await page.getByRole("button", { name: "Edit project" }).click();
  const form = page.getByRole("form", { name: "Edit project" });
  await expect(form.getByLabel("Project ID")).toHaveAttribute("readonly", "");
  await form.getByLabel("Name", { exact: true }).fill("TreeDar Survey");
  await expect(form.getByLabel("Project ID")).toHaveValue("treedar");
  await form.getByLabel("Status").selectOption("Off track");
  await form.getByLabel("Lead agent").selectOption("Codex");
  await form.getByLabel("Repository (optional)").fill("https://github.com/ledgetop/treedar");
  await form.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(state.writes[0]).toMatchObject({ method: "PATCH", path: "/api/hub/v1/projects/treedar",
    body: { expected_revision: 1, name: "TreeDar Survey", status: "off_track", lead_agent: "codex", repository_url: "https://github.com/ledgetop/treedar" } });
  await expect(page.getByRole("heading", { level: 1 })).toContainText("TreeDar Survey");
  await expect(page.getByRole("navigation", { name: "Projects", exact: true }).getByRole("link", { name: "TreeDar Survey" })).toBeVisible();
  await expect(page.locator(".project-meta .status-pill")).toHaveText("Off track");
  await expect(page.getByRole("link", { name: "ledgetop/treedar" })).toHaveAttribute("rel", "noopener noreferrer");
});

test("a conflict keeps the draft and never overwrites; archive asks first and hides the project", async ({ page }) => {
  const { state } = await fixture(page);
  await page.goto("/#/projects/treedar");
  await page.getByRole("button", { name: "Edit project" }).click();
  const form = page.getByRole("form", { name: "Edit project" });
  await form.getByLabel("Description").fill("My unsaved description");
  state.conflict = true;
  await form.getByRole("button", { name: "Save changes" }).click();
  await expect(form.getByRole("alert")).toContainText("Nothing was overwritten");
  await expect(form.getByLabel("Description")).toHaveValue("My unsaved description");
  await expect(form.getByRole("button", { name: "Save changes" })).toBeDisabled();
  page.once("dialog", (dialog) => void dialog.accept());
  await form.getByRole("button", { name: "Cancel" }).click();
  state.conflict = false;
  await page.getByRole("button", { name: "Archive" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Archive project" }).click();
  await expect(page.getByText("This project is archived.", { exact: false })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Projects", exact: true }).getByRole("link", { name: /TreeDar/ })).toHaveCount(0);
  expect(state.writes.at(-1)).toMatchObject({ method: "POST", path: "/api/hub/v1/projects/treedar/archive", body: { expected_revision: 1 } });
});

test("a server without project routes says it needs an update and does not sign out", async ({ page }) => {
  await fixture(page, { gateway: true });
  await page.goto("/#/projects");
  await page.getByRole("button", { name: "New project" }).click();
  const form = page.getByRole("form", { name: "New project" });
  await form.getByLabel("Name", { exact: true }).fill("Anything");
  await form.getByRole("button", { name: "Create project" }).click();
  await expect(form.getByRole("alert")).toContainText("needs an update");
  await expect(page.getByText("operator@example.test", { exact: true })).toBeVisible();
});
