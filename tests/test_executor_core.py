import json
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest

from hub_executor.core import ExecutorError, HubExecutor

TARGET = "3d-viewer:staging"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
ROLLBACK_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
DIGEST_V1 = "sha256:3ab6e82eb9e517ba1b2a107b4d8b838e7e872a4f7a62c068a07b07d4227103ec"
DIGEST_V2 = "sha256:4c031a5fbabf2e39b2aa313c390470863285ffea20396317c6c996a8e1fb8725"


def executor_for(tmp_path):
    return HubExecutor(
        tmp_path / "ledger.sqlite3",
        {TARGET: tmp_path / "targets" / "3d-viewer-staging"},
        {
            DIGEST_V1: b"engineering hub fixture v1\n",
            DIGEST_V2: b"engineering hub fixture v2\n",
        },
        candidate_commit=COMMIT,
        artifact_commits={DIGEST_V1: ROLLBACK_COMMIT, DIGEST_V2: COMMIT},
        artifact_roles={DIGEST_V1: "rollback", DIGEST_V2: "candidate"},
        policy_revision=1,
    )


def command(**overrides):
    payload = {
        "command_id": "550e8400-e29b-41d4-a716-446655440000",
        "target_id": TARGET,
        "action": "deploy_fixture",
        "artifact_digest": DIGEST_V2,
        "candidate_commit": COMMIT,
        "policy_revision": 1,
        "fencing_generation": 1,
    }
    return {**payload, **overrides}


def test_same_command_id_and_payload_replays_without_second_effect(tmp_path):
    executor = executor_for(tmp_path)
    payload = command()
    state_path = tmp_path / "targets/3d-viewer-staging/state.json"

    first = executor.execute_command(payload)
    first_state = json.loads(state_path.read_text(encoding="utf-8"))
    replay = executor.execute_command(payload)
    second_state = json.loads(state_path.read_text(encoding="utf-8"))

    assert first["state"] == "succeeded"
    assert replay["replayed"] is True
    assert {key: value for key, value in replay.items() if key != "replayed"} == first
    assert second_state["effect_count"] == first_state["effect_count"] == 1


def test_digest_must_match_its_manifest_commit_before_any_effect(tmp_path):
    executor = executor_for(tmp_path)

    with pytest.raises(ExecutorError) as error:
        executor.execute_command(command(artifact_digest=DIGEST_V1))

    assert error.value.code == "policy_conflict"
    assert not (tmp_path / "targets/3d-viewer-staging/state.json").exists()


def test_pause_persists_and_blocks_new_admission_until_resumed(tmp_path):
    executor = executor_for(tmp_path)
    executor.set_paused(True)

    with pytest.raises(ExecutorError) as error:
        executor.execute_command(command())

    assert error.value.code == "paused"
    assert not (tmp_path / "targets/3d-viewer-staging/state.json").exists()
    reopened = executor_for(tmp_path)
    assert reopened.is_paused() is True

    reopened.set_paused(False)
    result = reopened.execute_command(command())
    assert result["state"] == "succeeded"


def test_command_result_is_durable_and_status_read_does_not_reapply_effect(tmp_path):
    executor = executor_for(tmp_path)
    payload = command()
    result = executor.execute_command(payload)
    state_path = tmp_path / "targets/3d-viewer-staging/state.json"
    state_before = state_path.read_bytes()

    reopened = executor_for(tmp_path)
    status = reopened.get_command(payload["command_id"])

    assert status == result
    assert state_path.read_bytes() == state_before


def test_interruption_after_effect_holds_then_reconciles_from_actual_marker(tmp_path):
    class SimulatedInterruption(RuntimeError):
        pass

    class InterruptAfterEffect(HubExecutor):
        def _apply_fixture(self, payload):
            super()._apply_fixture(payload)
            raise SimulatedInterruption("fixture interruption after atomic effect")

    target_root = tmp_path / "targets/3d-viewer-staging"
    interrupted = InterruptAfterEffect(
        tmp_path / "ledger.sqlite3",
        {TARGET: target_root},
        {
            DIGEST_V1: b"engineering hub fixture v1\n",
            DIGEST_V2: b"engineering hub fixture v2\n",
        },
        candidate_commit=COMMIT,
        artifact_commits={DIGEST_V1: ROLLBACK_COMMIT, DIGEST_V2: COMMIT},
        artifact_roles={DIGEST_V1: "rollback", DIGEST_V2: "candidate"},
        policy_revision=1,
    )
    payload = command()

    with pytest.raises(SimulatedInterruption):
        interrupted.execute_command(payload)

    state_path = target_root / "state.json"
    actual_before_reconcile = json.loads(state_path.read_text(encoding="utf-8"))
    held_before_restart = interrupted.get_command(payload["command_id"])
    restarted = executor_for(tmp_path)
    held = restarted.get_command(payload["command_id"])
    replay = restarted.execute_command(payload)
    reconciled = restarted.reconcile_command(payload["command_id"])
    actual_after_reconcile = json.loads(state_path.read_text(encoding="utf-8"))

    assert held_before_restart["state"] == "held"
    assert held["state"] == "held"
    assert replay["state"] == "held"
    assert replay["replayed"] is True
    assert reconciled["state"] == "succeeded"
    assert reconciled["observed"]["marker"]["command_id"] == payload["command_id"]
    assert actual_after_reconcile == actual_before_reconcile
    assert actual_after_reconcile["effect_count"] == 1


def test_rollback_fixture_restores_only_preconfigured_prior_artifact(tmp_path):
    executor = executor_for(tmp_path)
    executor.execute_command(command())
    rollback = command(
        command_id="550e8400-e29b-41d4-a716-446655440001",
        action="rollback_fixture",
        artifact_digest=DIGEST_V1,
        candidate_commit=ROLLBACK_COMMIT,
        fencing_generation=2,
    )

    result = executor.execute_command(rollback)
    state = json.loads(
        (tmp_path / "targets/3d-viewer-staging/state.json").read_text(encoding="utf-8")
    )

    assert result["state"] == "succeeded"
    assert state["active_artifact_digest"] == DIGEST_V1
    assert state["artifact_bytes"] == "engineering hub fixture v1\n"
    assert state["marker"]["action"] == "rollback_fixture"
    assert state["effect_count"] == 2


def test_stale_fencing_generation_is_rejected_after_restart_without_effect(tmp_path):
    executor = executor_for(tmp_path)
    current = command(fencing_generation=5)
    executor.execute_command(current)
    state_path = tmp_path / "targets/3d-viewer-staging/state.json"
    before = state_path.read_bytes()
    restarted = executor_for(tmp_path)
    stale = command(
        command_id="550e8400-e29b-41d4-a716-446655440002",
        fencing_generation=4,
    )

    with pytest.raises(ExecutorError) as error:
        restarted.execute_command(stale)

    assert error.value.code == "stale_fence"
    assert restarted.get_command(stale["command_id"]) is None
    assert state_path.read_bytes() == before


def test_concurrent_target_takeover_leaves_highest_fence_active(tmp_path):
    executor = executor_for(tmp_path)
    start = threading.Barrier(2)
    low = command(
        command_id="550e8400-e29b-41d4-a716-446655440003",
        fencing_generation=10,
        artifact_digest=DIGEST_V1,
        candidate_commit=ROLLBACK_COMMIT,
        action="rollback_fixture",
    )
    high = command(
        command_id="550e8400-e29b-41d4-a716-446655440004",
        fencing_generation=11,
    )

    def submit(payload):
        start.wait(timeout=3)
        try:
            return executor.execute_command(payload)
        except ExecutorError as error:
            return {"state": "rejected", "reason": error.code}

    with ThreadPoolExecutor(max_workers=2) as workers:
        low_future = workers.submit(submit, low)
        high_future = workers.submit(submit, high)
        low_result = low_future.result(timeout=5)
        high_result = high_future.result(timeout=5)

    state = json.loads(
        (tmp_path / "targets/3d-viewer-staging/state.json").read_text(encoding="utf-8")
    )
    assert high_result["state"] == "succeeded"
    assert low_result["state"] in {"succeeded", "rejected"}
    assert state["marker"]["command_id"] == high["command_id"]
    assert state["marker"]["fencing_generation"] == 11


def test_production_entry_fixture_manifest_binds_candidate_and_rollback(tmp_path):
    from hub_executor.fixtures import (
        FIXTURE_CANDIDATE_COMMIT,
        FIXTURE_ROLLBACK_COMMIT,
        FIXTURE_ROLLBACK_DIGEST,
        FIXTURE_TARGET,
        make_fixture_executor,
    )

    executor = make_fixture_executor(tmp_path)
    candidate = {
        "command_id": "42a55e0f-443c-5b68-a342-94cebbe799cf",
        "target_id": FIXTURE_TARGET,
        "action": "deploy_fixture",
        "artifact_digest": "sha256:4c031a5fbabf2e39b2aa313c390470863285ffea20396317c6c996a8e1fb8725",
        "candidate_commit": FIXTURE_CANDIDATE_COMMIT,
        "policy_revision": 1,
        "fencing_generation": 1,
    }
    rollback = {
        **candidate,
        "command_id": "a0fdab7b-119e-4a84-a918-8ac5e2d00317",
        "action": "rollback_fixture",
        "artifact_digest": FIXTURE_ROLLBACK_DIGEST,
        "candidate_commit": FIXTURE_ROLLBACK_COMMIT,
        "fencing_generation": 2,
    }

    assert set(executor.target_roots) == {"3d-viewer:staging"}
    assert executor.execute_command(candidate)["state"] == "succeeded"
    assert executor.execute_command(rollback)["state"] == "succeeded"
    state = json.loads(
        (tmp_path / "targets/3d-viewer-staging/state.json").read_text(encoding="utf-8")
    )
    assert state["active_artifact_digest"] == FIXTURE_ROLLBACK_DIGEST


def test_smoke_failure_retains_fence_until_higher_generation_rollback(tmp_path):
    executor = executor_for(tmp_path)
    executor.execute_command(command())
    failed_smoke = command(
        command_id="550e8400-e29b-41d4-a716-446655440050",
        action="smoke_fixture",
        artifact_digest=DIGEST_V1,
        candidate_commit=ROLLBACK_COMMIT,
        fencing_generation=2,
    )

    failed = executor.execute_command(failed_smoke)
    state_path = tmp_path / "targets/3d-viewer-staging/state.json"
    state_after_smoke = json.loads(state_path.read_text(encoding="utf-8"))
    stale_rollback = command(
        command_id="550e8400-e29b-41d4-a716-446655440051",
        action="rollback_fixture",
        artifact_digest=DIGEST_V1,
        candidate_commit=ROLLBACK_COMMIT,
        fencing_generation=2,
    )
    with pytest.raises(ExecutorError) as error:
        executor.execute_command(stale_rollback)
    rollback = executor.execute_command({**stale_rollback, "fencing_generation": 3})

    assert failed["state"] == "failed"
    assert failed["reason"] == "fixture_state_mismatch"
    assert state_after_smoke["effect_count"] == 1
    assert error.value.code == "stale_fence"
    assert rollback["state"] == "succeeded"
    assert json.loads(state_path.read_text(encoding="utf-8"))["active_artifact_digest"] == DIGEST_V1
