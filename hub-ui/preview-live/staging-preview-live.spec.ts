import { expect, test, request as requests, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import net from "node:net";
import path from "node:path";
import { randomUUID } from "node:crypto";

const image = "sha256:6642cd751e2814d21e367fda47c47e67c584ffe7fe62270427441359fd69efb2";
const owner = randomUUID();
const name = `eh-preview-browser-${owner}`;
const ownerLabel = "engineering-hub.test.owner";
let hub: string, preview: string, attempted = false, primaryFailed = false;
async function freePort(): Promise<number> {
  const server = net.createServer();
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  const port = (server.address() as net.AddressInfo).port;
  await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  return port;
}
function docker(args: string[]) {
  return execFileSync("docker", ["--context", "desktop-linux", ...args], { encoding: "utf8", timeout: 30_000, maxBuffer: 64 * 1024 });
}
test.beforeAll(async () => {
  try {
  const [a, b] = await Promise.all([freePort(), freePort()]);
  hub = `https://localhost:${a}`;
  preview = `https://127.0.0.1:${b}`;
  attempted = true; // An ambiguous create may already have created the owned container.
  docker(["create", "--pull=never", "--name", name, "--label", `${ownerLabel}=${owner}`, "--network", "bridge", "--read-only",
    "--cap-drop", "ALL", "--cpus", "1", "--memory", "512m", "--memory-swap", "512m",
    "--pids-limit", "128", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777",
    "-p", `127.0.0.1:${a}:8443`, "-p", `127.0.0.1:${b}:8444`,
    "--mount", `type=bind,source=${path.resolve("..")},target=/source,readonly`,
    "-e", "PYTHONPATH=/source/src", "-e", `TEST_HUB_ORIGIN=${hub}`,
    "-e", `TEST_PREVIEW_ORIGIN=${preview}`, "--entrypoint", "python", image,
    "-B", "/source/tests/fixtures/staging_preview_browser_server.py"]);
  docker(["start", name]);
  const probe = await requests.newContext({ ignoreHTTPSErrors: true });
  try {
    await expect.poll(async () => {
      try { return (await probe.get(hub, { timeout: 1000 })).status(); } catch { return 0; }
    }, { timeout: 20_000 }).toBe(401);
  } finally { await probe.dispose(); }
  } catch (error) { primaryFailed = true; throw error; }
});
test.afterEach(async ({}, info) => { primaryFailed ||= info.status !== info.expectedStatus; });
test.afterAll(() => {
  if (!attempted) return;
  try {
    const id = docker(["ps", "-aq", "--no-trunc", "--filter", `name=^/${name}$`]).trim();
    if (!id) return;
    if (!/^[a-f0-9]{64}$/.test(id) || docker(["inspect", "--format", `{{index .Config.Labels "${ownerLabel}"}}`, id]).trim() !== owner)
      throw new Error("Preview fixture container ownership mismatch");
    try { docker(["stop", "--time", "5", id]); }
    finally { docker(["rm", "--force", id]); }
    expect(docker(["ps", "-a", "--filter", `name=^/${name}$`, "--format", "{{.Names}}"]).trim()).toBe("");
  } catch (error) {
    if (!primaryFailed) throw error;
    console.error("Preview fixture cleanup failed; original test failure retained");
  }
});
async function login(page: Page) {
  const response = await page.request.post(hub + "/test/login");
  expect(response.status()).toBe(200);
  const data = await response.json();
  await page.goto(hub);
  expect(await control(page, data.csrf, "restore")).toBe(200);
  return data as { csrf: string; resources: string[] };
}
async function control(page: Page, csrf: string, action: string) {
  return page.evaluate(async ({ hub, csrf, action }) => (await fetch(hub + "/test/control/" + action,
    { method: "POST", headers: { "x-hub-csrf": csrf } })).status, { hub, csrf, action });
}
async function issue(page: Page, csrf: string, id: string) {
  const result = await page.evaluate(async ({ hub, csrf, id }) => {
    const response = await fetch(hub + "/api/hub/v1/staging/resources/" + id + "/preview-session",
      { method: "POST", headers: { "x-hub-csrf": csrf, "content-type": "application/json" }, body: "{}" });
    return { status: response.status, body: await response.json() };
  }, { hub, csrf, id });
  expect(result.status).toBe(200);
  expect(new URL(result.body.url).origin).toBe(preview);
  return result.body.url as string;
}
async function open(page: Page, url: string) {
  await page.locator("iframe").evaluate((element, value) => (element as HTMLIFrameElement).src = value, url);
  await expect(page.frameLocator("iframe").locator("#out")).toHaveText("real-gateway 42");
}
for (const viewport of [{ width: 390, height: 844 }, { width: 1440, height: 1100 }]) {
test(`actual HTTPS gateway loads modules, dynamic import, CSS, JSON and decoded font at ${viewport.width}px with opaque isolation`, async ({ page }) => {
  await page.setViewportSize(viewport);
  const user = await login(page);
  const url = await issue(page, user.csrf, user.resources[0]);
  await open(page, url);
  const frame = page.frames().find(item => item.url() === url)!;
  expect(await frame.evaluate(() => (window as any).fixtureFacts)).toEqual({
    opaque: true, parentReadable: false, cookieReadable: false, fontLoaded: true,
  });
  await expect(page.frameLocator("iframe").locator("#out")).toHaveCSS("color", "rgb(17, 34, 51)");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const observations = await (await page.request.get(hub + "/test/observations")).json();
  expect(observations.files).toEqual(expect.arrayContaining(["index.html", "app.js", "lib.js", "dynamic.js", "style.css", "data.json", "fixture.ttf"]));
  expect(observations.cookie).toBe(false);
  expect(observations.authorization).toBe(false);
  expect(observations.referer).toBe(false);
});
}
test("actual gateway denies sibling capabilities in-browser and binds files to their own resource", async ({ page }) => {
  const user = await login(page);
  const first = await issue(page, user.csrf, user.resources[0]);
  const second = await issue(page, user.csrf, user.resources[1]);
  await open(page, first);
  const frame = page.frames().find(item => item.url() === first)!;
  expect(await frame.evaluate(async other => {
    try { await fetch(other); return "allowed"; } catch { return "blocked"; }
  }, second.replace("index.html", "data.json"))).toBe("blocked");
  // This asset exists in A, but B's immutable reader has only index.html.
  const response = await page.request.get(second.replace("index.html", "data.json"));
  expect(response.status()).toBe(404);
  expect((await page.request.get(first.replace("index.html", "data.json"))).status()).toBe(200);
  // No cookie or CSRF: an anonymous client cannot mint a capability.
  const anonymous = await requests.newContext({ ignoreHTTPSErrors: true });
  try {
    expect((await anonymous.post(hub + "/api/hub/v1/staging/resources/" + user.resources[0] + "/preview-session",
      { data: {} })).status()).toBe(401);
  } finally { await anonymous.dispose(); }
});
for (const action of ["logout", "revoke", "expire"]) {
  test(`actual retained capability is revoked by ${action}`, async ({ page }) => {
    const user = await login(page);
    const url = await issue(page, user.csrf, user.resources[0]);
    await open(page, url);
    expect(await control(page, user.csrf, action)).toBe(200);
    expect((await page.request.get(url.replace("index.html", "data.json"))).status()).toBe(404);
    await page.locator("iframe").evaluate((element, value) => (element as HTMLIFrameElement).src = value, url);
    await expect(page.frameLocator("iframe").locator("#out")).toHaveCount(0);
  });
}
