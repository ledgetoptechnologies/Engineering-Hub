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

    if high_result.get('reason') == 'target_busy':
        # A newer fence cannot take over a running/unknown operation. Only
        # retry the definitively unaccepted original command after inspection.
        assert low_result['state'] == 'succeeded'
        assert executor.get_command(high['command_id']) is None
        assert executor.reconcile_command(low['command_id'])['state'] == 'succeeded'
        high_result = executor.execute_command(high)
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


def test_smoke_failure_allows_same_run_generation_rollback(tmp_path):
    executor = executor_for(tmp_path)
    executor.execute_command(command())
    failed_smoke = command(
        command_id="550e8400-e29b-41d4-a716-446655440050",
        action="smoke_fixture", artifact_digest=DIGEST_V1,
        candidate_commit=ROLLBACK_COMMIT, fencing_generation=1,
    )
    failed = executor.execute_command(failed_smoke)
    assert failed['state'] == 'failed'
    assert failed['reason'] == 'fixture_state_mismatch'
    assert executor._read_observed(TARGET)['effect_count'] == 1
    rollback = executor.execute_command(command(
        command_id="550e8400-e29b-41d4-a716-446655440051",
        action="rollback_fixture", artifact_digest=DIGEST_V1,
        candidate_commit=ROLLBACK_COMMIT, fencing_generation=1,
    ))
    assert rollback['state'] == 'succeeded'
    assert executor._read_observed(TARGET)['active_artifact_digest'] == DIGEST_V1


def test_new_id_cannot_repeat_an_action_within_same_run_generation(tmp_path):
    executor = executor_for(tmp_path)
    executor.execute_command(command())
    with pytest.raises(ExecutorError) as error:
        executor.execute_command(command(command_id="550e8400-e29b-41d4-a716-446655440099"))
    assert error.value.code == "command_conflict"
    assert executor._read_observed(TARGET)["effect_count"] == 1


def test_pause_after_admission_stops_before_atomic_effect(tmp_path, monkeypatch):
    from contextlib import contextmanager
    executor = executor_for(tmp_path)
    original_db = executor._db
    armed = True

    @contextmanager
    def pause_at_boundary():
        nonlocal armed
        with original_db() as connection:
            yield connection
            accepted = armed and connection.execute(
                "SELECT 1 FROM commands WHERE status='running'"
            ).fetchone() is not None
        if accepted:
            armed = False
            executor.set_paused(True)

    monkeypatch.setattr(executor, '_db', pause_at_boundary)
    result = executor.execute_command(command())
    assert result['state'] == 'held'
    assert result['reason'] == 'paused_before_effect'
    assert not (tmp_path / 'targets/3d-viewer-staging/state.json').exists()


def test_unresolved_target_blocks_a_new_generation_until_reconciled(tmp_path, monkeypatch):
    executor = executor_for(tmp_path)
    original_apply = executor._apply_fixture

    def lose_outcome(payload):
        original_apply(payload)
        raise RuntimeError('fixture lost outcome')

    monkeypatch.setattr(executor, '_apply_fixture', lose_outcome)
    with pytest.raises(RuntimeError):
        executor.execute_command(command())
    monkeypatch.setattr(executor, '_apply_fixture', original_apply)
    later = command(command_id='550e8400-e29b-41d4-a716-446655440098', fencing_generation=2)
    with pytest.raises(ExecutorError) as error:
        executor.execute_command(later)
    assert error.value.code == 'target_busy'
    assert executor._read_observed(TARGET)['effect_count'] == 1
    assert executor.reconcile_command(command()['command_id'])['state'] == 'succeeded'
    assert executor.execute_command(later)['state'] == 'succeeded'


def test_fixture_effect_does_not_widen_parent_or_world_permissions(tmp_path):
    import stat
    parent = tmp_path / 'targets'
    parent.mkdir(mode=0o750)
    before = stat.S_IMODE(parent.stat().st_mode)
    executor = executor_for(tmp_path)
    import os
    previous_umask = os.umask(0o022)  # Match the isolated container's default.
    try:
        executor.execute_command(command())
    finally:
        os.umask(previous_umask)
    target = parent / '3d-viewer-staging'
    assert stat.S_IMODE(parent.stat().st_mode) == before
    assert stat.S_IMODE(target.stat().st_mode) & 0o007 == 0
    assert stat.S_IMODE(target.stat().st_mode) & 0o070 == 0o070
    assert stat.S_IMODE((target / 'state.json').stat().st_mode) & 0o007 == 0


def test_reconcile_does_not_trust_forged_target_marker(tmp_path, monkeypatch):
    executor = executor_for(tmp_path)
    def no_effect(_payload):
        raise RuntimeError('fixture interruption before effect')
    monkeypatch.setattr(executor, '_apply_fixture', no_effect)
    payload = command()
    with pytest.raises(RuntimeError):
        executor.execute_command(payload)
    target = executor.target_roots[TARGET]
    target.mkdir(parents=True)
    forged = {'active_artifact_digest': DIGEST_V2,
              'artifact_bytes': 'engineering hub fixture v2\n', 'effect_count': 1,
              'marker': {key: payload[key] for key in ('command_id', 'action', 'artifact_digest', 'fencing_generation')}}
    (target / 'state.json').write_text(json.dumps(forged))
    result = executor.reconcile_command(payload['command_id'])
    assert result['state'] == 'held'
    assert result['observed'] == {}


def test_second_instance_cannot_adopt_a_genuinely_active_command(tmp_path, monkeypatch):
    executor = executor_for(tmp_path)
    entered, release = threading.Event(), threading.Event()
    from contextlib import contextmanager
    original_db = executor._db
    armed = True
    @contextmanager
    def executing():
        nonlocal armed
        with original_db() as connection:
            yield connection
            accepted = armed and connection.execute("SELECT 1 FROM commands WHERE status='running'").fetchone() is not None
        if accepted:
            armed = False
            entered.set()
            assert release.wait(timeout=3)
    monkeypatch.setattr(executor, '_db', executing)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(executor.execute_command, command())
        assert entered.wait(timeout=2)
        try:
            with pytest.raises(RuntimeError, match='owner'):
                executor_for(tmp_path)
            assert executor.get_command(command()['command_id'])['state'] == 'running'
        finally:
            release.set()
        assert future.result(timeout=5)['state'] == 'succeeded'


def test_inflight_identical_command_replays_without_adopting_owner(tmp_path, monkeypatch):
    executor = executor_for(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = executor._apply_fixture
    def active(payload):
        entered.set()
        assert release.wait(timeout=3)
        return original(payload)
    monkeypatch.setattr(executor, '_apply_fixture', active)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(executor.execute_command, command())
        assert entered.wait(timeout=2)
        try:
            replay = executor.execute_command(command())
            assert replay['replayed'] is True and replay['state'] == 'running'
            with pytest.raises(ExecutorError) as error:
                executor.execute_command(command(action='smoke_fixture'))
            assert error.value.code == 'command_conflict'
        finally:
            release.set()
        assert future.result(timeout=5)['state'] == 'succeeded'
    assert executor._read_observed(TARGET)['effect_count'] == 1
