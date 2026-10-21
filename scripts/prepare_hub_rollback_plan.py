"""Pure offline rollback campaign planning; no current-host or launch authority."""
from __future__ import annotations

from copy import deepcopy
import json
from uuid import UUID

from engineering_hub.container_pilot_manifest import PilotManifest
from scripts.prepare_hub_unenrolled_record import canonical, sha, native_validators


UNVERIFIED_DAEMON = "UNVERIFIED-REQUIRES-FRESH-DAEMON"


def prepare_plan(*, previous_manifest: dict, candidate_record_raw: bytes,
                 healthy_record_raw: bytes, executor_record_raw: bytes,
                 proposed_campaign_id: str, proposed_runner_id: str,
                 expected_seed_digest: str) -> bytes:
    """Return an explicitly inert, native-shape-checked public plan.

    Existing record bytes are only referenced by their exact hashes; no record
    invocation or historical receipt is rewritten. The healthy former candidate
    becomes the proposed rollback image. This function does not authenticate
    records, apply reviews, enroll a builder or attest a fresh healthy baseline.
    """
    for value in (proposed_campaign_id, proposed_runner_id):
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError("New canonical proposed UUIDs required")
    old = PilotManifest.from_bytes(canonical(previous_manifest))
    if (proposed_campaign_id == old.campaign_id
            or proposed_runner_id == old.runner_binding.runner_id
            or proposed_campaign_id == proposed_runner_id):
        raise ValueError("Proposed campaign/runner must be fresh and distinct")
    if expected_seed_digest != old.state_policy.baseline_backup_digest:
        raise ValueError("Expected historical seed bytes do not match old manifest")
    native, native_digest = native_validators()
    candidate, healthy, executor = (native._load_record(raw) for raw in (
        candidate_record_raw, healthy_record_raw, executor_record_raw))
    if (candidate["source"]["commit"] != healthy["source"]["commit"]
            or healthy["source"]["commit"] != old.candidate.source_commit
            or healthy["selection"]["image_config_id"] != old.candidate.image_config_id
            or not all(item["platform"] == old.runner_binding.platform.model_dump(mode="json")
                       for item in (candidate, healthy, executor))):
        raise ValueError("Records must preserve healthy application source and platform")

    def selection(record, raw):
        source = record["source"]
        return {"source_repository": source["repository"], "source_commit": source["commit"],
            "image_config_id": record["selection"]["image_config_id"],
            "provenance": {"build_context_digest": source["build_context_digest"],
                "build_recipe_digest": source["build_recipe_digest"],
                "build_inputs_digest": source["build_inputs_digest"],
                "attestation_digest": sha(raw)}}

    if selection(healthy, healthy_record_raw) != old.candidate.model_dump(mode="json"):
        raise ValueError("Healthy rollback must reference the exact previous candidate record")

    template = deepcopy(previous_manifest)
    template["campaign_id"] = proposed_campaign_id
    template["driver"]["implementation_commit"] = executor["source"]["commit"]
    template["runner_binding"]["runner_id"] = proposed_runner_id
    template["runner_binding"]["daemon_identity"] = UNVERIFIED_DAEMON
    template["candidate"] = selection(candidate, candidate_record_raw)
    template["rollback"] = selection(healthy, healthy_record_raw)
    template["runtime"]["resource_name_prefix"] = "hub-pilot-rollback-" + proposed_campaign_id[:8]
    template["state_policy"]["baseline_id"] = "baseline-" + proposed_campaign_id
    parsed = PilotManifest.from_bytes(canonical(template))
    # Native parsing proves only schema and closed policy consistency. Its
    # placeholder daemon and historical seed are deliberately not live inputs.
    validated_template = parsed.canonical_bytes()
    return canonical({
        "schema": "hub_offline_rollback_campaign_plan_v1", "status": "UNENROLLED_INERT",
        "runtime_ready": False, "execution_authority_granted": False,
        "shape_validation": "native_PilotManifest_only_not_host_or_trust_acceptance",
        "native_record_validator_digest": native_digest,
        "proposed_campaign_id": proposed_campaign_id, "proposed_runner_id": proposed_runner_id,
        "manifest_template": json.loads(validated_template),
        "manifest_template_digest": sha(validated_template),
        "records": {
            "candidate": {"digest": sha(candidate_record_raw), "selection": candidate["selection"]},
            "rollback": {"digest": sha(healthy_record_raw), "selection": healthy["selection"],
                         "basis": "exact_existing_healthy_record_bytes_no_rewrite"},
            "executor": {"digest": sha(executor_record_raw), "selection": executor["selection"],
                         "source_commit": executor["source"]["commit"]}},
        "expected_seed": {"digest": expected_seed_digest,
            "basis": "historical_seed_expected_bytes_only_not_fresh_snapshot_or_health",
            "historical_campaign": old.campaign_id, "fresh_observation": False},
        "fresh_evidence_required": [
            "Current daemon/socket/PID/start/Engine/resource-envelope and original retained inventory proof",
            "Fresh healthy D0 baseline application with native compiled health/assets/auth probes",
            "Fresh frozen baseline snapshot with actual hash and manifest-bound native observation receipt",
            "Explicit independent build/artifact trust and fresh genuine reviewed task for this campaign",
            "Existing approved TEST bindings/public key reviewed without minting new credentials",
            "Fresh protected executor/issuer/profile/config DAG after baseline and daemon evidence",
            "New native owner ledger and immutable queue policy; never edit previous campaign DB",
            "Actual candidate known failure then verified healthy D0 rollback and clean native shutdown"],
        "not_created": ["runner_configuration", "profile", "config_commitment", "signature",
                        "enrollment", "baseline_receipt", "new_task_or_review", "runtime"],
        "fault_promotion": "never_promote_TEST_fault_candidate",
    })
