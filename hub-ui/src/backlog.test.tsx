import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { HubClient, resolutionBody } from "./api";
import { draftFor, isActive, laterHint, parseAcceptance, WORK_FILTERS, waitingItemKey, waitingTasks } from "./backlog";
import { projectStats, STATUSES, WorkBoard } from "./pages/ProjectPages";
import { waitingOn } from "./dashboardText";
import type { Hub } from "./hub";
import { createDemoOverview, DEMO_SESSION } from "./mocks";
import { HomePage } from "./pages/HomePage";
import { BacklogDetail, statusLabel, statusTone } from "./pages/tasks";
import { InboxPage, WaitingItemCard } from "./pages/WorkPages";
import type { Task } from "./types";

const base: Task = { id: "t1", revision: 3, updated_at: "2026-11-16T10:00:00Z", project: "engineering-hub",
  title: "Unify the backlog", status: "claimed", owner: "claude", candidate_commit: null, review: null, repair_count: 0 };
const waiting = (type: "action" | "decision", id = "t-wait"): Task => ({ ...base, id, status: "waiting_on_beau",
  waiting_on: { type, text: type === "action" ? "Add the DNS record." : "Pick the domain name.",
    previous_status: "claimed", by: "claude", at: "2026-11-16T10:00:00Z" } });
const question = (id = "t-q", level: "now" | "later" = "now"): Task => ({ ...waiting("decision", id), waiting_on: {
  ...waiting("decision", id).waiting_on!, text: `Question ${id}`, level, recommended: 1, recommended_reason: "Smaller reviews",
  options: [{ label: "Keep it" }, { label: "Split it", detail: "Two PRs" }, { label: "Drop it" }] } });

function hubWith(tasks: Task[], overrides: Partial<Hub> = {}): Hub {
  const data = { ...createDemoOverview(new Date("2026-11-16T10:00:00Z")), tasks };
  return { demo: true, session: DEMO_SESSION, data, agents: [], stale: false, loading: false, writable: true,
    connection: "demo", projectName: (id: string) => id, decisionChoices: {}, chooseAnswer: () => {},
    recordDecision: () => {}, requestPause: () => {}, withdraw: async () => true, resolveWaiting: async () => true,
    clearPrivateWorkspace: () => {}, refresh: async () => {}, peers: null, topicDraft: "", setTopicDraft: () => {},
    checkTopic: () => {}, topic: "", selectTopic: () => {}, topicError: "", topicStale: false, peerState: () => "",
    ...overrides } as Hub;
}

describe("backlog helpers", () => {
  it("lists only waiting tasks with an item, optionally per project", () => {
    const tasks = [base, waiting("action"), { ...waiting("decision", "t2"), project: "3d-viewer" },
      { ...base, id: "t3", status: "waiting_on_beau" } as Task];
    expect(waitingTasks(tasks).map((task) => task.id)).toEqual(["t-wait", "t2"]);
    expect(waitingTasks(tasks, "3d-viewer").map((task) => task.id)).toEqual(["t2"]);
  });
  it("treats review-passed, done and withdrawn as not active", () => {
    const states: Task["status"][] = ["assigned", "claimed", "in_review", "waiting_on_beau", "verified", "done", "withdrawn"];
    expect(states.filter((status) => isActive({ ...base, status }))).toEqual(["assigned", "claimed", "in_review", "waiting_on_beau"]);
  });
  it("splits acceptance criteria per non-blank line and offers the new filters", () => {
    expect(parseAcceptance(" First \n\n Second\n ")).toEqual(["First", "Second"]);
    expect(WORK_FILTERS).toContain("waiting_on_beau");
    expect(WORK_FILTERS).toContain("done");
  });
  it("labels the new states", () => {
    expect(statusLabel(waiting("action"))).toBe("Waiting on Beau");
    expect(statusTone(waiting("action"))).toBe("amber");
    expect(statusLabel({ ...base, status: "done" })).toBe("Done");
    expect(statusTone({ ...base, status: "done" })).toBe("forest");
    expect(waitingOn(waiting("action"), (id) => id)).toBe("Waiting on Beau: Add the DNS record.");
    expect(waitingOn({ ...base, status: "done" }, (id) => id)).toBe("Done.");
  });
});

describe("backlog views", () => {
  it("shows reviewer, acceptance criteria and completion evidence on a task", () => {
    const html = renderToStaticMarkup(<BacklogDetail hub={hubWith([])} task={{ ...base, status: "done", reviewer: "codex",
      acceptance: ["Agents can create tasks", "Old rows load"], completed_by: "claude", completion_evidence: "PR 200 merged" }} />);
    expect(html).toContain("Named reviewer");
    expect(html).toContain("<li>Agents can create tasks</li>");
    expect(html).toContain("PR 200 merged");
    const quoted = renderToStaticMarkup(<BacklogDetail hub={hubWith([])} task={{ ...base, proposed_by: "claude",
      request_text: "Make the Hub the backlog" }} />);
    expect(quoted).toMatch(/Request, as quoted by [^:]+: Make the Hub the backlog/);
  });
  it("offers Done for an action and a required answer for a decision", () => {
    const action = renderToStaticMarkup(<WaitingItemCard hub={hubWith([])} task={waiting("action")} />);
    expect(action).toContain("Add the DNS record.");
    expect(action).toMatch(/<button class="primary">Done<\/button>/);
    const decision = renderToStaticMarkup(<WaitingItemCard hub={hubWith([])} task={waiting("decision")} />);
    expect(decision).toMatch(/<button class="primary" disabled="">Send answer<\/button>/);
    const readOnly = renderToStaticMarkup(<WaitingItemCard hub={hubWith([], { writable: false })} task={waiting("action")} />);
    expect(readOnly).toMatch(/<button class="primary" disabled="">Done<\/button>/);
  });
  it("shows three options with the recommended one marked and its reason, plus a text box and Dismiss", () => {
    const html = renderToStaticMarkup(<WaitingItemCard hub={hubWith([])} task={question()} />);
    expect(html.match(/class="waiting-option( recommended)?"/g)).toHaveLength(3);
    expect(html).toMatch(/class="waiting-option recommended"><span class="waiting-option-label">Split it<\/span>/);
    expect(html).toContain("Recommended");
    expect(html).toContain("Smaller reviews");
    expect(html).toContain("Two PRs");
    expect(html).toContain('placeholder="Your answer"');
    expect(html).toMatch(/<button type="button">Dismiss<\/button>/);
    expect(html).toMatch(/<button type="button">Later<\/button>/);
  });
  it("keeps later items out of the counts and collapses them behind their count", () => {
    const hub = hubWith([question("now-1"), question("later-1", "later"), question("later-2", "later")]);
    expect(waitingTasks(hub.data.tasks).map((task) => task.id)).toEqual(["now-1"]);
    expect(waitingTasks(hub.data.tasks, undefined, "later")).toHaveLength(2);
    const inbox = renderToStaticMarkup(<InboxPage hub={hub} actions={null} />);
    expect(inbox).toContain("Question now-1");
    expect(inbox).not.toContain("Question later-1");
    expect(inbox).toMatch(/aria-expanded="false">Later <span>2<\/span>/);
    const home = renderToStaticMarkup(<HomePage hub={hub} actions={null} />);
    expect(home).not.toContain("Question later-1");
    expect(projectStats(hub, "engineering-hub").items.map((task) => task.id)).toEqual(["now-1"]);
  });
  it("hints at later items on Home, the project and an Inbox chip for a project whose items are all later", () => {
    const hub = hubWith([question("now-1"), { ...question("later-1", "later"), project: "3d-viewer" }]);
    expect(laterHint(hub.data.tasks)).toBe("1 later");
    expect(laterHint(hub.data.tasks, "engineering-hub")).toBe("");
    const home = renderToStaticMarkup(<HomePage hub={hub} actions={null} />);
    expect(home).toContain(" · 1 later");
    expect(projectStats(hub, "3d-viewer")).toMatchObject({ needsYou: projectStats(hub, "3d-viewer").waiting.length, later: "1 later" });
    const inbox = renderToStaticMarkup(<InboxPage hub={hub} actions={null} />);
    expect(inbox).toMatch(/<span>\d+<\/span><small class="later-hint">1 later<\/small><\/button>/);
  });
  it("lists task items with decisions in the Inbox and the Home Needs you card", () => {
    const hub = hubWith([base, waiting("action")]);
    const inbox = renderToStaticMarkup(<InboxPage hub={hub} actions={null} />);
    expect(inbox).toContain("From tasks");
    expect(inbox).toContain("Add the DNS record.");
    expect(inbox).toContain("Choose the initial scratch-storage benchmark");
    const home = renderToStaticMarkup(<HomePage hub={hub} actions={null} />);
    expect(home).toContain("Add the DNS record.");
    expect(home).toContain("Choose the initial scratch-storage benchmark");
    expect(home).toMatch(/<strong>2<\/strong><span class="kpi-note">items<\/span>/);
  });
});

describe("demo client", () => {
  it("creates tasks with acceptance criteria and a reviewer, and clears a waiting item", async () => {
    const client = new HubClient({ mode: "demo" });
    const csrf = DEMO_SESSION.csrf_token!;
    const task = await client.createTask("engineering-hub", "Backlog", "k1", csrf, { acceptance: ["A"], reviewer: "codex" });
    expect(task.acceptance).toEqual(["A"]);
    expect(task.reviewer).toBe("codex");
    const overview = await client.getOverview();
    const stored = overview.tasks.find((item) => item.id === task.id)!;
    expect(stored.status).toBe("assigned");
    await expect(client.resolveWaiting(task.id, task.revision, {}, csrf)).rejects.toMatchObject({ code: "not_waiting" });
  });
  it("answers by option or text, dismisses, moves levels and applies a bulk change all or nothing", async () => {
    const client = new HubClient({ mode: "demo" });
    const csrf = DEMO_SESSION.csrf_token!;
    const tasks = (await client.getOverview()).tasks;
    const [a, b, c] = ["q1", "q2", "q3"].map((id) => ({ ...question(id), project: tasks[0]?.project ?? "engineering-hub" }));
    (client as unknown as { demo: { tasks: Task[] } }).demo.tasks.push(a, b, c);
    await expect(client.resolveWaiting("q1", 3, {}, csrf)).rejects.toMatchObject({ code: "answer_required" });
    expect((await client.resolveWaiting("q1", 3, { option: 1 }, csrf)).waiting_resolved?.answer).toEqual({ option: 1, label: "Split it" });
    expect((await client.setWaitingLevel("q2", 3, "later", csrf)).waiting_on?.level).toBe("later");
    await expect(client.resolveWaitingMany([{ id: "q2", revision: 4, resolution: { dismiss: true } },
      { id: "q3", revision: 9, resolution: { option: 0 } }], csrf)).rejects.toMatchObject({ code: "revision_conflict" });
    expect((await client.getOverview()).tasks.find((task) => task.id === "q2")?.status).toBe("waiting_on_beau");
    const done = await client.resolveWaitingMany([{ id: "q2", revision: 4, resolution: { dismiss: true, note: " Moot " } },
      { id: "q3", revision: 3, resolution: { answer: "Ship behind a flag" } }], csrf);
    expect(done.map((task) => task.waiting_resolved?.outcome)).toEqual(["dismissed", "answered"]);
    expect(done[0].waiting_resolved?.note).toBe("Moot");
    expect(resolutionBody({ answer: "  ", note: "", option: 0 })).toEqual({ option: 0 });
  });
});

describe("waiting item drafts and project counts", () => {
  it("keys a draft to one waiting item even when options and level match", () => {
    const a = question("t-q");
    const b = { ...a, revision: 7, waiting_on: { ...a.waiting_on!, at: "2026-11-16T12:00:00Z" } };
    expect(draftFor({ item: waitingItemKey(a), text: "For A" }, waitingItemKey(b))).toBe("");
  });
  it("keys a draft to one waiting item, so a later item on the same task starts empty", () => {
    const a = waiting("decision");
    const b = { ...a, revision: 5, waiting_on: { ...a.waiting_on!, text: "Item B", at: "2026-11-16T11:00:00Z" } };
    const draft = { item: waitingItemKey(a), text: "Answer meant for A" };
    expect(waitingItemKey(a)).not.toBe(waitingItemKey(b));
    expect(draftFor(draft, waitingItemKey(a))).toBe("Answer meant for A");
    expect(draftFor(draft, waitingItemKey(b))).toBe("");
  });
  it("counts waiting task items in a project's Needs you, and shows them as a board column without a Done column", () => {
    const done: Task = { ...base, id: "t-done", status: "done", title: "Finished work" };
    const hub = hubWith([waiting("action"), done, { ...waiting("decision", "t-other"), project: "3d-viewer" }]);
    const stats = projectStats(hub, "engineering-hub");
    expect(stats.items.map((task) => task.id)).toEqual(["t-wait"]);
    expect(stats.needsYou).toBe(stats.waiting.length + 1);
    expect(stats.byStatus.done).toBe(1);
    expect(stats.active).toBe(1);
    expect(STATUSES).toContain("waiting_on_beau");
    expect(STATUSES).not.toContain("done");
    const board = renderToStaticMarkup(<WorkBoard hub={hub} tasks={stats.tasks} />);
    expect(board).toContain('aria-label="Waiting on Beau"');
    expect(board).not.toContain("Finished work");
  });
});
