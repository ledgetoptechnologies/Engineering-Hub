import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { HubClient } from "./api";
import { isActive, parseAcceptance, WORK_FILTERS, waitingTasks } from "./backlog";
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
    await expect(client.resolveWaiting(task.id, task.revision, "", csrf)).rejects.toMatchObject({ code: "not_waiting" });
  });
});
