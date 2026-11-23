import { expect, test, type Page } from "@playwright/test";
import type { ActivitySnapshot } from "../src/agentActivityApi";
import { demoAgentConnections } from "../src/agentConnectionsApi";
import { createDemoOverview, createDemoTopic } from "../src/mocks";

const NOW = Date.parse("2026-11-05T12:00:00Z");
const NOW_ISO = new Date(NOW).toISOString();
const ACTIVITY = "/api/hub/v1/agents/activity";
const OWNER = "human:cf:" + "a".repeat(64), OTHER = "human:cf:" + "b".repeat(64);
const TOPIC = "synthetic-owner-topic", OTHER_TOPIC = "synthetic-other-topic", LATE = "synthetic-late-topic";
const SESSION = "synthetic owner session";
const DENIED = "Only the Hub owner can see the map.";

function snapshot(topic = TOPIC, truncated = false) {
  const topics = [topic, OTHER_TOPIC, ...Array.from({ length: 12 }, (_, i) => `synthetic-spare-${i}`)];
  const openTopics = truncated ? [topic, OTHER_TOPIC, ...Array<string>(98).fill(OTHER_TOPIC)] : [topic, OTHER_TOPIC];
  return {
    observed_at: NOW_ISO, window_hours: 24, aggregates_truncated: truncated,
    agents: [{ agent: "hermes", pending: 2, claimed: 0, oldest_pending_at: NOW_ISO }],
    topics: topics.map((name, index) => ({
      topic: name, agents: ["codex", "hermes"], messages: 1, pending: index < 2 ? 1 : 0, claimed: 0,
      pending_by_agent: index < 2 ? { hermes: 1 } : {}, claimed_by_agent: {}, oldest_pending_at: index < 2 ? NOW_ISO : null,
      last_activity_at: NOW_ISO,
    })),
    open: openTopics.map((name, i) => ({
      id: `synthetic-${i}`, sender: "codex", recipient: "hermes", message_type: "note", priority: "normal",
      status: "pending", topic: name, created_at: NOW_ISO,
    })),
    open_total: truncated ? 143 : 2,
  } satisfies Omit<ActivitySnapshot, "aggregates_truncated"> & { aggregates_truncated: boolean };
}

async function fixture(page: Page, baseURL: string | undefined, { ignoreActivityAbort = false } = {}) {
  const directory = demoAgentConnections(NOW);
  const active = directory.installations[0];
  const sessions = [{ ...directory.sessions[active.id][0], label: SESSION }];
  const state = {
    actor: OWNER, renewal: "one", grant: true, directoryGrant: true, status: 200, truncated: false,
    holdNext: false, held: [] as (() => void)[], activityReads: 0, directoryReads: 0,
  };
  await page.clock.install({ time: new Date(NOW) });
  if (ignoreActivityAbort) {
    // Let a controlled fixture answer complete even after its request was canceled by the app.
    await page.addInitScript(path => {
      const realFetch = window.fetch.bind(window);
      window.fetch = (input, init) => input === path ? realFetch(input, { ...init, signal: undefined }) : realFetch(input, init);
    }, ACTIVITY);
  }
  await page.route("**/*", route => {
    const url = new URL(route.request().url());
    return url.origin === baseURL ? route.fallback() : route.abort();
  });
  await page.route("**/api/hub/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const respond = (json: unknown, status = 200) => route.fulfill({ status, json });
    if (route.request().method() !== "GET") return respond({}, 405);
    if (path.endsWith("/auth/me")) return respond({ authenticated: true, actor: state.actor, github_id: null,
      auth_provider: "cloudflare_access", email: "map-fixture@example.test", csrf_token: `synthetic-session-${state.renewal}` });
    if (path.endsWith("/overview")) return respond(createDemoOverview(new Date(NOW)));
    if (path.endsWith("/events/status")) return respond({ available: false, reason: "native_source_unsupported", observer_epoch: "fixture", cursor: 0 });
    if (path.endsWith("/peers")) return respond(createDemoTopic("auto-wake-v1-test", new Date(NOW)));
    if (path.endsWith("/work/status")) return respond({}, 423);
    if (path === ACTIVITY) {
      state.activityReads++;
      if (state.holdNext) {
        state.holdNext = false;
        await new Promise<void>(resolve => state.held.push(resolve));
        return respond(snapshot(LATE));
      }
      if (state.status !== 200) return respond({ detail: { code: "fixture_unavailable" } }, state.status);
      if (state.actor !== OWNER || !state.grant) return respond({ detail: { code: "agent_activity_owner_required" } }, 403);
      return respond(snapshot(TOPIC, state.truncated));
    }
    if (path.endsWith("/agent-connections/installations")) {
      state.directoryReads++;
      if (!state.directoryGrant) return respond({ detail: { code: "session_owner_required" } }, 403);
      return respond({ installations: [active] });
    }
    if (path.endsWith(`/installations/${active.id}/sessions`)) return respond({ sessions });
    if (path.endsWith("/agent-connections/capabilities")) return respond({ enabled: true, owner: state.actor === OWNER, environment: "staging" });
    return respond({}, 404);
  });
  return state;
}

const map = (page: Page) => page.getByRole("region", { name: "Agent map", exact: true });
const open = (page: Page) => page.getByRole("region", { name: "Open messages", exact: true });
const topic = (page: Page, name = TOPIC) => map(page).getByRole("button", { name: `${name}, 1 not picked up, 1 messages` });
const session = (page: Page) => map(page).getByRole("button", { name: new RegExp(SESSION) });
const refresh = (page: Page) => page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));

async function expectRedacted(page: Page) {
  await expect(map(page)).toHaveCount(0);
  await expect(open(page)).toHaveCount(0);
  await expect(page.getByText(TOPIC, { exact: true })).toHaveCount(0);
  await expect(page.getByText(SESSION, { exact: true })).toHaveCount(0);
}

async function completeLateReading(page: Page, release: () => void) {
  const late = page.waitForResponse(response => new URL(response.url()).pathname === ACTIVITY && response.ok());
  release();
  expect((await (await late).json()).open[0].topic).toBe(LATE);
  // Give the app both the response and a rendering opportunity, not just a request count.
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
}

test("owner grant shows family messages, exact open total and partial aggregate notice", async ({ page, baseURL }) => {
  const state = await fixture(page, baseURL);
  state.truncated = true;
  await page.goto("/agents");
  await page.getByRole("navigation", { name: "Workspace" }).getByRole("link", { name: "Map", exact: true }).click();
  await expect(page).toHaveURL(/\/agents\/map$/);
  await expect(topic(page)).toBeVisible();
  await expect(session(page)).toBeVisible();
  await expect(open(page).getByText("Oldest 100 of 143 shown.")).toBeVisible();
  await expect(page.getByText(/family-routed messages.*not paired installation mailboxes/i)).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: /activity aggregates are partial/i })).toBeVisible();
  await page.reload();
  await expect(topic(page)).toBeVisible();
  await page.goto("/agents/map");
  await expect(session(page)).toBeVisible();
});

test("a signed-in non-owner sees the denial without loading the owner directory", async ({ page, baseURL }) => {
  const state = await fixture(page, baseURL);
  state.actor = OTHER;
  await page.goto("/#/agents/map");
  await expect(page.getByText(DENIED)).toBeVisible();
  await expectRedacted(page);
  expect(state.directoryReads).toBe(0);
  await expect(page.getByRole("button", { name: "Refresh workspace" })).toBeEnabled();
});

test("owner denial clears the map and controls before a competing older refresh can finish", async ({ page, baseURL }) => {
  const state = await fixture(page, baseURL, { ignoreActivityAbort: true });
  await page.goto("/#/agents/map");
  await expect(session(page)).toBeVisible();
  await expect(page.getByText(/activity aggregates are partial/i)).toHaveCount(0);
  await topic(page).click();
  await map(page).getByRole("button", { name: /more topics/ }).click();
  await expect(topic(page)).toHaveAttribute("aria-pressed", "true");
  await expect(open(page).getByText(OTHER_TOPIC, { exact: true })).toHaveCount(0);

  state.holdNext = true;
  await refresh(page);
  await expect.poll(() => state.held.length).toBe(1);
  state.grant = false;
  await refresh(page);
  await expect(page.getByText(DENIED)).toBeVisible();
  await expectRedacted(page);
  await completeLateReading(page, state.held[0]);
  await expect(page.getByText(DENIED)).toBeVisible();
  await expectRedacted(page);
  await expect(page.getByText(LATE, { exact: true })).toHaveCount(0);
  expect(state.activityReads).toBe(3);

  state.grant = true;
  await refresh(page);
  await expect(topic(page)).toHaveAttribute("aria-pressed", "false");
  await expect(map(page).getByRole("button", { name: /more topics/ })).toBeVisible();
  await expect(open(page).getByText(OTHER_TOPIC, { exact: true })).toBeVisible();
  await expect(session(page)).toBeVisible();
  expect(state.directoryReads).toBe(2);
});

test("an owner denial from the session directory also clears the whole map", async ({ page, baseURL }) => {
  const state = await fixture(page, baseURL, { ignoreActivityAbort: true });
  await page.goto("/agents/map");
  await expect(session(page)).toBeVisible();
  state.directoryGrant = false;
  state.holdNext = true;
  await page.clock.fastForward(60_000);
  await expect.poll(() => state.held.length).toBe(1);
  await expect(page.getByText(DENIED)).toBeVisible();
  await expectRedacted(page);
  await completeLateReading(page, state.held[0]);
  await expectRedacted(page);
});

test("only the same workspace keeps its snapshot on a transient failure", async ({ page, baseURL }) => {
  const state = await fixture(page, baseURL, { ignoreActivityAbort: true });
  await page.goto("/#/agents/map");
  await expect(session(page)).toBeVisible();
  state.status = 503;
  await refresh(page);
  await expect(page.getByText("Couldn't refresh. Showing the last reading.")).toBeVisible();
  await expect(topic(page)).toBeVisible();
  state.holdNext = true;
  await refresh(page);
  await expect.poll(() => state.held.length).toBe(1);
  state.renewal = "two";
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect(page.getByText("Activity couldn't be read.")).toBeVisible();
  await expectRedacted(page);
  await completeLateReading(page, state.held[0]);
  await expectRedacted(page);
  await expect(page.getByText(LATE, { exact: true })).toHaveCount(0);
  state.status = 200;
  await refresh(page);
  await expect(topic(page)).toBeVisible();
  state.actor = OTHER;
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect(page.getByText(DENIED)).toBeVisible();
  await expectRedacted(page);
});

for (const status of [401, 403, 404]) {
  test(`terminal activity status ${status} drops the owner snapshot`, async ({ page, baseURL }) => {
    const state = await fixture(page, baseURL);
    await page.goto("/#/agents/map");
    await expect(session(page)).toBeVisible();
    state.status = status;
    await refresh(page);
    if (status === 404) await expect(page.getByText("This Hub needs an update before it can show the map.")).toBeVisible();
    else await expect(page.getByRole("link", { name: /Sign in securely/ })).toBeVisible();
    await expectRedacted(page);
  });
}
