/** Backend DTOs. Advisory readiness is never a delivery or deployment grant. */
export type Agent = "codex" | "hermes" | "claude";
export type ProjectId =
  | "engineering-hub"
  | "3d-viewer"
  | "project-alpha"
  | "ledge-top-ops"
  | "treedar";
export interface Session {
  authenticated: boolean;
  actor: string | null;
  csrf_token: string | null;
  github_id: number | null;
  auth_provider?: "cloudflare_access";
  email?: string | null;
}
export interface Versioned {
  id: string;
  revision: number;
  updated_at: string;
}
export interface Review {
  actor: Agent;
  commit: string;
  verdict: "approve" | "reject";
}
export interface Task extends Versioned {
  project: ProjectId;
  title: string;
  status: "assigned" | "claimed" | "in_review" | "verified";
  owner: Agent | null;
  candidate_commit: string | null;
  review: Review | null;
  repair_count: number;
  scope?: "staging" | null;
  proposed_by?: Agent | null;
}
export interface Decision extends Versioned {
  project: ProjectId;
  title: string;
  options: string[];
  protected: boolean;
  status: "open" | "answered";
  answer: string | null;
  answered_by?: string;
  execution_authorization: false;
}
export interface Run extends Versioned {
  task_id: string;
  project: ProjectId;
  target: string;
  commit: string;
  artifact_digest: string;
  policy_revision: number;
  review: Review;
  status:
    "pending" | "running" | "held" | "failed" | "succeeded" | "rolled_back";
  command_id: string | null;
  fencing_generation: number;
  created_by: Agent;
  execution_actor: string | null;
  execution_manifest_digest: string | null;
  events: Array<{
    event: string;
    event_id?: string | null;
    actor: string;
    command_id: string;
    fencing_generation: number;
    reason?: "candidate_outcome_uncertain" | "staging_paused" | "rollback_failed" |
      "rollback_outcome_uncertain" | "rollback_smoke_failed" | "rollback_smoke_outcome_uncertain" | null;
  }>;
}
export interface Project {
  id: ProjectId;
  name: string;
  environment: string;
  executor_enabled: boolean;
  health: string;
  reason: string;
}
export interface AgentSummary {
  id: Agent;
  transport: string;
  chat_readiness: string;
  reason: string;
  connection?: {
    state: "connected" | "not_observed" | "unavailable";
    source: "embedded_bus";
    scope: "this_server_process";
    observed_at: string | null;
    valid_until: string | null;
    active_streams: number | null;
    inbox_subscriptions: number | null;
    subscribed: boolean | null;
    last_stream_write_at: string | null;
    native_push_available: boolean | null;
  };
}
export interface AutonomyStatus {
  configured: boolean;
  state: string;
  running: boolean;
  last_result?: Record<string, unknown> | null;
  receipts: Array<Record<string, unknown>>;
}
export interface Capabilities {
  production: boolean;
  host_dispatch: boolean;
  github_merge: boolean;
  processing: boolean;
  fixture_dispatch: boolean;
  container_pilot_dispatch: boolean;
  merge_gate_strength: string;
}
export interface Overview {
  paused: boolean;
  projects: Project[];
  tasks: Task[];
  decisions: Decision[];
  runs: Run[];
  agents: AgentSummary[];
  autonomy: AutonomyStatus;
  capabilities: Capabilities;
}
export type ReadinessState =
  | "unlinked"
  | "linked_unverified"
  | "reachable"
  | "busy_queued"
  | "offline"
  | "unknown_stale";
export type ReachabilityReason =
  | "no_exact_binding"
  | "no_observer_evidence"
  | "observer_unavailable"
  | "observer_restarted"
  | "binding_generation_mismatch"
  | "evidence_expired"
  | "evidence_in_future"
  | "conflicting_evidence"
  | "stream_only"
  | "session_ready"
  | "session_busy_queue_verified"
  | "session_disconnected"
  | "session_offline"
  | "runtime_readiness_unverified"
  | "inventory_incomplete"
  | "owner_selection_unverified";
export interface PublicReachability {
  topic: string;
  agent: Agent;
  status: ReadinessState;
  reason: ReachabilityReason;
  observed_at: string | null;
  state_since: string | null;
  last_evidence_at: string | null;
  valid_until: string | null;
  advisory_only: true;
  link_state: "linked" | "unlinked" | "unknown";
  transport_state: "attached" | "disconnected" | "unverified" | "unknown";
  availability_state:
    "available" | "busy" | "offline" | "unverified" | "unknown";
  evidence_source:
    "connector_observer" | "runtime_control" | "bus_stream" | null;
  evidence_level: "runtime_verified" | "observed" | "unverified";
  snapshot_revision: string | null;
  binding_revision: string | null;
  attachment_revision?: string | null;
}
export type TopicPeer = PublicReachability;
export interface TopicSnapshot {
  tracking?: "not_configured" | "configured" | "unavailable";
  registry_epoch?: string | null;
  event_source?: "hub_only" | "canonical_registry" | "none";
  topic: string;
  items: PublicReachability[];
  advisory: true;
  delivery_guaranteed: false;
  snapshot_revision: string | null;
}
export interface TopicPreflight {
  topic: string;
  recipient: Agent;
  peer: PublicReachability;
  advisory: true;
  delivery_guaranteed: false;
  // Optional while older installed Hub versions do not provide send guidance.
  guidance?: {
    recipient_state: "online" | "busy" | "offline" | "unknown" | "unlinked";
    send_mode: "notify_if_still_reachable" | "queue_only" | "link_first";
    response_expectation: "may_respond" | "may_be_delayed" | "not_verified";
    durable_delivery_required: true;
    receipt_required: true;
  };
}
export interface EventsStatus {
  available: boolean;
  reason: string;
  observer_epoch: string;
  cursor: number;
}
export interface EventPointer {
  observer_epoch: string;
  cursor: number;
  kind: "invalidate" | "source_unavailable";
}
