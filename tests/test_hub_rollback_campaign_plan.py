"""Offline native shape checks only; no daemon, key, Docker or host access."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from engineering_hub.container_pilot_manifest import PilotManifest
from scripts.prepare_hub_rollback_plan import prepare_plan, UNVERIFIED_DAEMON
from scripts.prepare_hub_unenrolled_record import canonical, sha
from tests.test_hub_container_pilot_manifest import make_manifest_data
from tests.test_hub_unenrolled_record_preparation import inputs as draft_inputs
from scripts.prepare_hub_unenrolled_record import prepare_record


def parameters():
    old = make_manifest_data()
    healthy = json.loads(prepare_record(**draft_inputs()).record)
    old["candidate"] = {"source_repository": healthy["source"]["repository"],
        "source_commit": healthy["source"]["commit"],
        "image_config_id": healthy["selection"]["image_config_id"],
        "provenance": {key: healthy["source"][key] for key in (
            "build_context_digest", "build_recipe_digest", "build_inputs_digest")}}
    old["candidate"]["provenance"]["attestation_digest"] = sha(canonical(healthy))
    # Synthetic records change only selected metadata identity; native shape is
    # checked here, not image contents or trusted builder enrollment.
    fault, executor = deepcopy(healthy), deepcopy(healthy)
    fault["selection"]["image_config_id"] = "sha256:" + "d" * 64
    executor["source"]["commit"] = "e" * 40
    return dict(previous_manifest=old, candidate_record_raw=canonical(fault),
        healthy_record_raw=canonical(healthy), executor_record_raw=canonical(executor),
        proposed_campaign_id=str(uuid4()), proposed_runner_id=str(uuid4()),
        expected_seed_digest=old["state_policy"]["baseline_backup_digest"])


def test_plan_is_inert_preserves_closed_policy_and_references_healthy_exact_bytes():
    args = parameters()
    before = deepcopy(args["previous_manifest"])
    raw = prepare_plan(**args)
    assert raw == prepare_plan(**args)
    plan = json.loads(raw)
    assert plan["status"] == "UNENROLLED_INERT" and plan["runtime_ready"] is False
    assert plan["execution_authority_granted"] is False
    template = plan["manifest_template"]
    assert template["runner_binding"]["daemon_identity"] == UNVERIFIED_DAEMON
    assert template["driver"]["implementation_commit"] == "e" * 40
    assert plan["records"]["rollback"]["digest"] == sha(args["healthy_record_raw"])
    assert template["rollback"]["provenance"]["attestation_digest"] == sha(args["healthy_record_raw"])
    assert template["rollback"]["image_config_id"] == before["candidate"]["image_config_id"]
    for key in ("probes", "timeouts", "failure_policy", "retention"):
        assert template[key] == before[key]
    for key, value in before["runtime"].items():
        if key != "resource_name_prefix":
            assert template["runtime"][key] == value
    assert args["previous_manifest"] == before
    assert not plan["expected_seed"]["fresh_observation"]
    assert template["state_policy"]["baseline_backup_digest"] == args["expected_seed_digest"]
    assert "baseline_receipt" in plan["not_created"]
    assert PilotManifest.from_bytes(canonical(template)).digest() == plan["manifest_template_digest"]


@pytest.mark.parametrize("change", ["old_campaign", "old_runner", "same_ids", "bad_uuid", "different_seed",
                                    "wrong_source", "same_image", "wrong_platform", "rewritten_healthy"])
def test_reused_or_inconsistent_planning_inputs_denied(change):
    args = parameters()
    if change == "old_campaign":
        args["proposed_campaign_id"] = args["previous_manifest"]["campaign_id"]
    elif change == "old_runner":
        args["previous_manifest"]["runner_binding"]["runner_id"] = args["proposed_runner_id"]
    elif change == "same_ids":
        args["proposed_runner_id"] = args["proposed_campaign_id"]
    elif change == "bad_uuid":
        args["proposed_campaign_id"] = "not-a-fresh-campaign"
    elif change == "different_seed":
        args["expected_seed_digest"] = "sha256:" + "0" * 64
    elif change == "rewritten_healthy":
        healthy = json.loads(args["healthy_record_raw"])
        healthy["invocation_id"] = str(uuid4())
        args["healthy_record_raw"] = canonical(healthy)
    else:
        fault = json.loads(args["candidate_record_raw"])
        if change == "wrong_source":
            fault["source"]["commit"] = "f" * 40
        elif change == "same_image":
            fault["selection"]["image_config_id"] = json.loads(args["healthy_record_raw"])["selection"]["image_config_id"]
        else:
            fault["platform"]["architecture"] = "arm64"
            fault["platform"]["variant"] = "v8"
        args["candidate_record_raw"] = canonical(fault)
    with pytest.raises(ValueError):
        prepare_plan(**args)
