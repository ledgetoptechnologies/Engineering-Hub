/** Shared test fixture: one release-evidence DTO as the Hub sends it. */
/* eslint-disable @typescript-eslint/no-explicit-any */
export const run = "11111111-2222-4333-8444-555555555555";
export const commit = "a".repeat(40);
export const candidate = `sha256:${"c".repeat(64)}`;
export const previous = `sha256:${"d".repeat(64)}`;
export const step = (action: string, expected: string, extra: Record<string, unknown> = {}): Record<string, unknown> => ({
  action, command_id: "99999999-2222-4333-8444-555555555555", state: "not_started", expected_image_config_id: expected,
  reported_image_config_id: null, reported_health: null, worker_state: null, worker_evidence_verified: null,
  protocol_verified: false, journal_verified: false, reason: null, ...extra });
export const evidenceBody = (): Record<string, any> => ({
  run_id: run, task_id: "22222222-2222-4333-8444-555555555555", project: "engineering-hub", source_commit: commit,
  review: { actor: "claude", commit, verdict: "approve" }, run_status: "succeeded",
  candidate_image_config_id: candidate, pilot_manifest_digest: `sha256:${"e".repeat(64)}`, rollback_image_config_id: previous,
  candidate_deploy: step("deploy_container", candidate, { state: "succeeded", reported_image_config_id: candidate,
    worker_state: "succeeded", worker_evidence_verified: true, protocol_verified: true, journal_verified: true }),
  candidate_health_check: step("smoke_container", candidate, { state: "failed", reported_image_config_id: candidate,
    reported_health: "unhealthy", worker_state: "failed", worker_evidence_verified: true, protocol_verified: true,
    journal_verified: true, reason: "health_failed" }),
  rollback_deploy: step("rollback_container", previous),
  rollback_health_check: step("smoke_container_rollback", previous),
  evidence_basis: "authenticated_worker_report", independently_verified: false,
});
