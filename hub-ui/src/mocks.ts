/** Explicit demo fixtures. None of these observations describe live machines. */
import type {
  Agent,
  Overview,
  ProjectId,
  PublicReachability,
  Session,
  TopicSnapshot,
} from "./types";

export const DEMO_LABEL = "Demo fixtures — not live infrastructure";
export const DEMO_SESSION: Session = {
  authenticated: true,
  actor: "human:1",
  github_id: 1,
  csrf_token: "demo-only-csrf",
};
const projectNames: Array<[ProjectId, string]> = [
  ["engineering-hub", "Engineering Hub"],
  ["3d-viewer", "3D Viewer"],
  ["project-alpha", "Project Alpha"],
  ["ledge-top-ops", "LedgeTop Ops"],
  ["treedar", "TreeDar"],
];

export function createDemoOverview(now = new Date()): Overview {
  const updated_at = now.toISOString();
  const commit = "a".repeat(40);
  return {
    paused: false,
    projects: projectNames.map(([id, name]) => ({
      id,
      name,
      environment: "staging",
      executor_enabled: false,
      health: "unknown",
      reason: "demo_fixture_not_live_inventory",
    })),
    tasks: [
      {
        id: "demo-task-viewer",
        revision: 4,
        updated_at,
        project: "3d-viewer",
        title: "Verify the isolated Viewer staging candidate",
        status: "verified",
        owner: "codex",
        candidate_commit: commit,
        review: { actor: "claude", commit, verdict: "approve" },
        repair_count: 0,
      },
      {
        id: "demo-task-storage",
        revision: 2,
        updated_at,
        project: "engineering-hub",
        title: "Document staging storage acceptance checks",
        status: "claimed",
        owner: "hermes",
        candidate_commit: null,
        review: null,
        repair_count: 0,
      },
    ],
    decisions: [
      {
        id: "demo-decision-storage",
        revision: 1,
        updated_at,
        project: "engineering-hub",
        title: "Choose the initial scratch-storage benchmark",
        options: ["NAS-backed small fixture", "Wait for local disk inspection"],
        protected: false,
        status: "open",
        answer: null,
        execution_authorization: false,
      },
    ],
    runs: [],
    agents: (["codex", "hermes", "claude"] as Agent[]).map((id) => ({
      id,
      transport: "unknown",
      chat_readiness: "unverified",
      reason: "demo_fixture_not_live_presence",
    })),
    autonomy: {
      configured: false,
      state: "disabled",
      running: false,
      receipts: [],
    },
    capabilities: {
      production: false,
      host_dispatch: false,
      github_merge: false,
      processing: false,
      fixture_dispatch: false,
      merge_gate_strength: "not_enforced",
    },
  };
}

export function createDemoTopic(
  topic: string,
  now = new Date(),
): TopicSnapshot {
  const base: Omit<PublicReachability, "agent" | "status" | "reason"> = {
    topic,
    observed_at: now.toISOString(),
    state_since: null,
    last_evidence_at: now.toISOString(),
    valid_until: new Date(now.getTime() + 120_000).toISOString(),
    advisory_only: true,
    link_state: "linked",
    transport_state: "attached",
    availability_state: "available",
    evidence_source: "connector_observer",
    evidence_level: "runtime_verified",
    snapshot_revision: null,
    binding_revision: null,
  };
  return {
    topic,
    advisory: true,
    delivery_guaranteed: false,
    snapshot_revision: null,
    items: [
      { ...base, agent: "codex", status: "reachable", reason: "session_ready" },
      {
        ...base,
        agent: "hermes",
        status: "busy_queued",
        reason: "session_busy_queue_verified",
        availability_state: "busy",
      },
      {
        ...base,
        agent: "claude",
        status: "unknown_stale",
        reason: "observer_unavailable",
        observed_at: null,
        last_evidence_at: null,
        valid_until: null,
        transport_state: "unknown",
        availability_state: "unknown",
        evidence_source: null,
        evidence_level: "unverified",
      },
    ],
  };
}
