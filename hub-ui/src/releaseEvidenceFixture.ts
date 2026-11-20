import type { ReleaseEvidence } from "./releaseEvidenceApi";

export function evidenceBody(): ReleaseEvidence {
  const digest = `sha256:${"c".repeat(64)}`;
  const step = (action: ReleaseEvidence["candidate_deploy"]["action"]) => ({
    action,
    command_id: "22222222-3333-4444-8555-666666666666",
    state: "not_started" as const,
    expected_image_config_id: digest,
    reported_image_config_id: null,
    reported_health: null,
    worker_state: null,
    worker_evidence_verified: null,
    protocol_verified: false,
    journal_verified: false,
    reason: null,
  });
  return {
    run_id: "11111111-2222-4333-8444-555555555555",
    task_id: "33333333-4444-4555-8666-777777777777",
    project: "engineering-hub",
    source_commit: "a".repeat(40),
    review: { actor: "claude", commit: "a".repeat(40), verdict: "approve" },
    run_status: "pending",
    candidate_image_config_id: digest,
    pilot_manifest_digest: `sha256:${"d".repeat(64)}`,
    rollback_image_config_id: `sha256:${"e".repeat(64)}`,
    candidate_deploy: {
      ...step("deploy_container"), state: "succeeded", reported_image_config_id: digest,
      reported_health: "healthy", worker_state: "succeeded", worker_evidence_verified: true,
      protocol_verified: true, journal_verified: true,
    },
    candidate_health_check: {
      ...step("smoke_container"), state: "failed", reported_image_config_id: digest,
      reported_health: "unhealthy", worker_state: "failed", worker_evidence_verified: true,
      protocol_verified: true, journal_verified: true, reason: "health_failed",
    },
    rollback_deploy: step("rollback_container"),
    rollback_health_check: step("smoke_container_rollback"),
    evidence_basis: "authenticated_worker_report",
    independently_verified: false,
  };
}
