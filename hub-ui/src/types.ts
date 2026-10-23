/** Backend DTOs. Advisory readiness is never a delivery or deployment grant. */
export type Agent = "codex" | "hermes" | "claude";
/** Projects come from the server's list; the UI never assumes a fixed set. */
export type ProjectId = string;
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
  status: "assigned" | "claimed" | "in_review" | "verified" | "withdrawn";
  owner: Agent | null;
  candidate_commit: string | null;
  review: Review | null;
  repair_count: number;
  /** Optional agent context; parsed defensively by details.ts. */
  details?: string | null;
  links?: { label: string; url: string }[] | null;
  scope?: "staging" | null;
  proposed_by?: Agent | null;
  /** Audited time the task entered its current status; null when not recorded. */
  state_since?: string | null;
  /** Present on withdrawn records only: superseded work kept for history. */
  withdrawn_reason?: string | null;
  superseded_by?: string | null;
  withdrawn_by?: string | null;
  withdrawn_at?: string | null;
}
export interface Decision extends Versioned {
  project: ProjectId;
  title: string;
  options: string[];
  protected: boolean;
  status: "open" | "answered" | "withdrawn";
  answer: string | null;
  answered_by?: string;
  /** True only when the server accepts a typed answer instead of one of the options. */
  allow_text?: boolean;
  /** Present on withdrawn records only: superseded work kept for history. */
  withdrawn_reason?: string | null;
  superseded_by?: string | null;
  withdrawn_by?: string | null;
  withdrawn_at?: string | null;
  details?: string | null;
  links?: { label: string; url: string }[] | null;
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
  /** Human-managed metadata (newer servers). None of it grants access or execution. */
  description?: string;
  repository_url?: string | null;
  lead_agent?: Agent | null;
  status?: "on_track" | "at_risk" | "off_track" | "paused";
  parent_project?: string | null;
  archived?: boolean;
  revision?: number;
}
export interface AgentSummary {
  id: Agent;
  transport: string;
  chat_readiness: string;
  reason: string;
  connection?: {
    state: "connected" | "stale" | "not_observed" | "unavailable";
    source: "embedded_bus";
    scope: "this_server_process";
    observed_at: string | null;
    valid_until: string | null;
    active_streams: number | null;
    stale_streams?: number | null;
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
  /** Optional on older servers; advisory image observations, not deployment authority. */
  image_versions?: ImageVersionsSummary | null;
  /** Split out by the client on load so every list and count skips them; see withdrawn.ts. */
  withdrawn?: { tasks: Task[]; decisions: Decision[] };
}
export type ImageVersionState = "up_to_date" | "update_available" | "unknown";
export interface ImageVersionStatus {
  project: string;
  host: string;
  service: string;
  environment: "production" | "staging";
  repository: string;
  reference: string;
  state: ImageVersionState;
  reason: string;
  checked_at: string;
  running_observed_at: string | null;
  published_observed_at: string | null;
  valid_until: string | null;
  digest_kind: "config" | "manifest" | "index" | null;
  current_digest: string | null;
  available_digest: string | null;
}
export interface ImageVersionsSummary {
  configured: boolean;
  source: "agent_observation_report";
  reason: "available" | "not_configured" | "invalid_report" | "source_unavailable";
  reported_by: string | null;
  report_updated_at: string | null;
  advisory_only: true;
  items: ImageVersionStatus[];
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
