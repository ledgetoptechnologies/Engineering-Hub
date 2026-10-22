import { expect, test, type Page } from "@playwright/test";

// Fake module origin, served with the CSP the module host is expected to send.
const CSP = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'none'";
const PRELUDE = `
let port, nonce, generation, nextId = 1;
const send = (type, payload = {}, id) => port.postMessage({ v: 1, nonce, generation, type, id, payload });
const waiters = {};
const call = (type, payload) => new Promise((resolve) => { const id = nextId++; waiters[id] = resolve; send(type, payload, id); });
window.addEventListener("message", (event) => {
  if (!event.data || event.data.type !== "init" || port) return;
  port = event.ports[0]; nonce = event.data.nonce; generation = event.data.generation;
  port.onmessage = (m) => { const r = m.data; if (waiters[r.id]) { waiters[r.id](r.payload); delete waiters[r.id]; } };
  main();
});
`;
const CASES: Record<string, string> = {
  good: `async function main() {
    send("ready"); send("resize", { height: 640 });
    const data = await call("data.get", { capability: "read:dataset/crowns", key: "plot-a1" });
    const saved = await call("submit", { item_key: "A1", payload: { choice: "conifer" }, expected_revision: 3 });
    send("notice", { level: "info", text: "rows " + data.data.rows.length + ", saved revision " + saved.revision });
  }`,
  escape: `async function main() {
    send("ready");
    const out = [];
    try { await fetch("/api/hub/v1/overview"); out.push("fetch:open"); } catch { out.push("fetch:blocked"); }
    try { void parent.document.title; out.push("dom:open"); } catch { out.push("dom:blocked"); }
    try { void window.localStorage.length; out.push("storage:open"); } catch { out.push("storage:blocked"); }
    try { document.cookie = "x=1"; out.push(document.cookie ? "cookie:open" : "cookie:blocked"); } catch { out.push("cookie:blocked"); }
    try { top.location.href = "https://example.com/"; out.push("nav:open"); } catch { out.push("nav:blocked"); }
    send("notice", { level: "info", text: out.join(" ") });
  }`,
  flood: `function main() { send("ready"); for (let i = 0; i < 200; i++) send("notice", { level: "info", text: "spam " + i }); }`,
  capability: `function main() { send("ready"); send("data.get", { capability: "read:dataset/secret", key: "all" }, 1); }`,
  forged: `function main() { send("ready"); port.postMessage({ v: 1, nonce: "guessed", generation, type: "notice", payload: { level: "info", text: "x" } }); }`,
  oversize: `function main() { send("ready"); send("notice", { level: "info", text: "x".repeat(70000) }); }`,
  silent: `function main() {}`,
  binary: `function main() { send("ready"); send("submit", { item_key: "A1", payload: { blob: new ArrayBuffer(4 * 1024 * 1024) }, expected_revision: 1 }, 9); }`,
  manyreads: `function main() { send("ready"); for (let i = 1; i <= 5; i++) send("data.get", { capability: "read:dataset/crowns", key: "k" + i }, i); }`,
  dupreads: `function main() { send("ready"); send("data.get", { capability: "read:dataset/crowns", key: "a" }, 7); send("data.get", { capability: "read:dataset/crowns", key: "b" }, 7); }`,
  savethenflood: `async function main() { send("ready"); await call("submit", { item_key: "A1", payload: { choice: "kept" }, expected_revision: 1 });
    for (let i = 0; i < 200; i++) send("notice", { level: "info", text: "spam" }); }`,
  submitonly: `function main() { send("ready"); call("submit", { item_key: "A1", payload: { choice: "late" }, expected_revision: 1 }); }`,
  submitflood: `function main() { send("ready"); call("submit", { item_key: "A1", payload: { choice: "late" }, expected_revision: 1 });
    setTimeout(() => { for (let i = 0; i < 200; i++) send("notice", { level: "info", text: "spam" }); }, 300); }`,
};

async function serveModules(page: Page) {
  await page.route("http://modules.test/**", (route) => {
    const url = new URL(route.request().url());
    const [, , name, file] = url.pathname.split("/");
    if (!CASES[name]) return route.fulfill({ status: 404, body: "" });
    if (file === "index.html") return route.fulfill({ status: 200, contentType: "text/html",
      headers: { "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff" },
      body: `<!doctype html><html><body><p>module</p><script src="app.js"></script></body></html>` });
    return route.fulfill({ status: 200, contentType: "text/javascript", body: PRELUDE + CASES[name] });
  });
}
const lab = (page: Page) => page.evaluate(() => (window as unknown as { __lab: unknown[] }).__lab);

test("a well-behaved module reads a granted dataset and saves only after the Hub confirms", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=good");
  await expect(page.getByText("rows 3, saved revision 4")).toBeVisible();
  expect(await lab(page)).toEqual([{ getData: ["good", "read:dataset/crowns", "plot-a1"] }, { confirm: { choice: "conifer" } },
    { submit: ["good", "A1", { choice: "conifer" }, 3], placement: "project-a/placement-1/v1/g1" }]);
  await expect(page.getByTitle("Test module")).toHaveAttribute("sandbox", "allow-scripts");
  await expect.poll(() => page.getByTitle("Test module").evaluate((el) => (el as HTMLIFrameElement).style.height)).toBe("640px");
});

test("a declined confirmation saves nothing", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=good&decline=1");
  await expect.poll(async () => (await lab(page)).length).toBe(2);
  await page.waitForTimeout(300);
  expect((await lab(page)).some((entry) => typeof entry === "object" && entry !== null && "submit" in entry)).toBe(false);
});

test("the sandbox blocks network, the Hub page, storage, cookies and top navigation", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=escape");
  await expect(page.getByText("fetch:blocked dom:blocked storage:blocked cookie:blocked nav:blocked")).toBeVisible();
  expect(new URL(page.url()).pathname).toBe("/e2e/fixtures/module-lab.html");
});

for (const [name, text] of [["flood", "too many messages"], ["capability", "wasn't granted"], ["forged", "broke a safety rule"],
  ["oversize", "too large"]] as const) {
  test(`a module that misbehaves (${name}) is stopped and nothing is saved`, async ({ page }) => {
    await serveModules(page);
    await page.goto(`/e2e/fixtures/module-lab.html?case=${name}`);
    await expect(page.getByRole("alert")).toContainText(text);
    expect(await lab(page)).toEqual([]);
    await expect(page.getByRole("button", { name: "Restart module" })).toBeVisible();
  });
}

test("a module that never says ready is stopped after 10 seconds", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=silent");
  await expect(page.getByRole("alert")).toContainText("didn't start within 10 seconds", { timeout: 15000 });
});

// A confirmation that is still open when its frame stops, restarts, unmounts or is
// replaced must never save. Positive control first: resolving normally does save.
const submits = async (page: Page) => (await lab(page)).filter((entry) => typeof entry === "object" && entry !== null && "submit" in entry);
const confirmOpen = (page: Page) => expect.poll(async () => (await lab(page)).some((e) => typeof e === "object" && e !== null && "confirm" in e)).toBe(true);
const resolve = (page: Page, index = 0) => page.evaluate((i) => (window as unknown as { __resolvers: ((ok: boolean) => void)[] }).__resolvers[i](true), index);

test("control: a deferred confirmation that is approved saves once", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=submitonly&defer=1");
  await confirmOpen(page);
  await resolve(page);
  await expect.poll(async () => (await submits(page)).length).toBe(1);
});

test("a frame stopped while the confirmation is open saves nothing when it is approved", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=submitflood&defer=1");
  await confirmOpen(page);
  await expect(page.getByRole("alert")).toContainText("too many messages");
  await resolve(page);
  await page.waitForTimeout(400);
  expect(await submits(page)).toEqual([]);
});

test("a restart while the old confirmation is open voids the old confirmation", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=submitflood&defer=1");
  await confirmOpen(page);
  await expect(page.getByRole("alert")).toContainText("too many messages");
  await page.getByRole("button", { name: "Restart module" }).click();
  await resolve(page, 0);
  await page.waitForTimeout(400);
  expect(await submits(page)).toEqual([]);
});

test("unmounting while the confirmation is open saves nothing", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=submitonly&defer=1");
  await confirmOpen(page);
  await page.evaluate(() => (window as unknown as { __unmount: () => void }).__unmount());
  await resolve(page);
  await page.waitForTimeout(400);
  expect(await submits(page)).toEqual([]);
});

test("switching to another module while the confirmation is open never saves through the new placement", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=submitonly&defer=1");
  await confirmOpen(page);
  await page.evaluate(() => (window as unknown as { __switch: (n: string) => void }).__switch("silent"));
  await resolve(page);
  await page.waitForTimeout(400);
  expect(await submits(page)).toEqual([]);
});

test("binary payloads are refused before any size check can be fooled", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=binary");
  await expect(page.getByRole("alert")).toContainText("malformed message");
  expect(await submits(page)).toEqual([]);
});

test("too many waiting requests stop the module", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=manyreads&slowdata=1");
  await expect(page.getByRole("alert")).toContainText("too many requests waiting");
});

test("a request number reused while still pending stops the module", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=dupreads&slowdata=1");
  await expect(page.getByRole("alert")).toContainText("reused a request number");
});

test("after a stop, earlier completed saves are reported as saved, not denied", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=savethenflood");
  await expect(page.getByRole("alert")).toContainText("1 earlier save was completed and stay saved.");
  expect(await submits(page)).toHaveLength(1);
});

test("an open confirmation is reported as cancelled when the module stops", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=submitflood&defer=1");
  await expect(page.getByRole("alert")).toContainText("1 open request to save was cancelled and not saved.");
});

test("a failing confirmation dialog declines without an unhandled error", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=submitonly&confirmreject=1");
  await expect.poll(async () => (await lab(page)).length).toBe(1);
  await page.waitForTimeout(300);
  expect(await submits(page)).toEqual([]);
  expect(errors).toEqual([]);
});

test("replacing the placement with the same module, address and grants voids the open confirmation", async ({ page }) => {
  await serveModules(page);
  await page.goto("/e2e/fixtures/module-lab.html?case=submitonly&defer=1");
  await confirmOpen(page);
  // Same module URL and capabilities, different project placement (e.g. navigating from project A to B).
  await page.evaluate(() => (window as unknown as { __placement: (k: string) => void }).__placement("project-b/placement-7/v1/g1"));
  await resolve(page, 0);
  await page.waitForTimeout(400);
  expect(await submits(page)).toEqual([]);
});
