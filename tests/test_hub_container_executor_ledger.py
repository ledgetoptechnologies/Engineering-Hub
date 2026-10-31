"""Real private SQLite and kernel-owner tests; no Docker or listener."""
import multiprocessing
import os
import sqlite3
import stat
import sys
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest

from engineering_hub.container_pilot_client import ContainerPilotEvidence, ContainerPilotRequest
import hub_container_executor.ledger as ledger_module
from hub_container_executor.ledger import OwnerBusy, SQLiteLedger, _mount_type_for

MANIFEST = "sha256:" + "a" * 64
COMMIT = "b" * 40
TARGET = "engineering-hub:staging"


def request(action="deploy_container", *, run_id=None, command_id=None, fence=1):
    return ContainerPilotRequest(
        schema_version="container_pilot_v1",
        command_id=command_id or str(uuid4()),
        run_id=run_id or str(uuid4()),
        target_id=TARGET,
        action=action,
        manifest_digest=MANIFEST,
        candidate_commit=COMMIT,
        policy_revision=1,
        fencing_generation=fence,
    )


def evidence():
    return ContainerPilotEvidence(container_id=None, image_config_id=None,
                                  health="unknown", evidence_verified=False)


def gate_fixture(ledger, *, require_gate=False):
    from tests.test_hub_mount_gate_protocol import fixture
    from hub_container_executor.mount_gate_protocol import IDENTITY_FIELDS
    command = request()
    assert ledger.admit(command).accepted
    decision = fixture()
    decision.update(run_id=command.run_id, fence=command.fencing_generation,
                    manifest_digest=command.manifest_digest)
    context = {'schema': 2, 'role': 'candidate', 'run_id': command.run_id, 'fencing_generation': command.fencing_generation,
        'manifest_digest': command.manifest_digest, 'image_config_id': decision['image_config_id'],
        'generation_id': decision['generation_id'], 'runnable_reference': decision['runnable_digest'],
        'platform_manifest_digest': decision['platform_manifest_digest']}
    operation = ledger.begin_operation(command.command_id, 'create_candidate', create_context=context)
    decision['operation_id'] = operation
    ledger.complete_operation(operation, {'container_id': decision['container_id']})
    ledger.record_target_instance(command.command_id, decision['container_id'],
                                  decision['image_config_id'], decision['generation_id'])
    if require_gate:
        ledger.require_gate_outcome(command.command_id, operation)
    return command, decision, {k: decision[k] for k in IDENTITY_FIELDS}


def test_gate_requirement_blocks_authenticated_start_failure_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    try:
        command, _, pins = gate_fixture(ledger)
        ledger.require_gate_outcome(command.command_id, pins['operation_id'])
        start_id = ledger.begin_operation(command.command_id, 'start_candidate')
        ledger.record_terminal_failure(start_id)
        result = ledger.finish(command.command_id, state='failed', reason='effect_failed', evidence=evidence())
        assert result.state == 'held' and result.reason == 'inspect_required'
        failure = next(item for item in ledger.operation_rows(command.command_id) if item['operation_id'] == start_id)
        assert failure['status'] == 'terminal_failed' and failure['receipt_authentic'] is True
        with pytest.raises(PermissionError): ledger.resolve_terminal_failure(command.command_id)
        assert ledger.get(command.command_id).state == 'held'
        assert ledger.target_snapshot(command.target_id)['state'] == 'held'
    finally:
        ledger.close()


def test_gate_challenge_is_consumed_once_under_concurrent_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    _, decision, pins = gate_fixture(ledger, require_gate=True)
    ledger.reserve_mount_gate_challenge(pins)
    barrier, results = threading.Barrier(3), []
    def consume():
        barrier.wait()
        try: results.append(ledger.consume_mount_gate_challenge(decision))
        except PermissionError: results.append(False)
    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads: thread.start()
    barrier.wait()
    for thread in threads: thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == [False, True]
    with pytest.raises(PermissionError): ledger.reserve_mount_gate_challenge(pins)
    ledger.close()


@pytest.mark.parametrize('change', ['paused', 'target_fence', 'run_held', 'context_mac', 'wrong_identity'])
def test_gate_challenge_rechecks_ledger_before_consumption(tmp_path, monkeypatch, change):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    _, decision, pins = gate_fixture(ledger, require_gate=True)
    ledger.reserve_mount_gate_challenge(pins)
    if change == 'paused': ledger.set_paused(True)
    elif change == 'wrong_identity': decision['host_pid'] += 1
    else:
        with ledger._transaction() as db:
            if change == 'target_fence': db.execute('UPDATE targets SET fence=fence+1')
            elif change == 'run_held': db.execute("UPDATE runs SET state='held'")
            else: db.execute("UPDATE create_contexts SET context_mac='bad'")
    with pytest.raises(PermissionError): ledger.consume_mount_gate_challenge(decision)
    with ledger._connection() as db:
        assert db.execute('SELECT state FROM mount_gate_challenges').fetchone()[0] == 'reserved'
    ledger.close()


@pytest.mark.parametrize('consume_first', [False, True])
def test_gate_challenge_never_replays_after_owner_restart(tmp_path, monkeypatch, consume_first):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    command, decision, pins = gate_fixture(ledger, require_gate=True)
    ledger.reserve_mount_gate_challenge(pins)
    if consume_first: assert ledger.consume_mount_gate_challenge(decision) is True
    ledger.close()
    with pytest.raises(ledger_module.LedgerClosed): ledger.consume_mount_gate_challenge(decision)
    reopened = open_ledger(tmp_path)
    assert reopened.get(command.command_id).state == 'held'
    with pytest.raises(PermissionError): reopened.consume_mount_gate_challenge(decision)
    reopened.close()


def test_gate_consumed_state_cannot_be_reset_by_database_tampering(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    _, decision, pins = gate_fixture(ledger, require_gate=True)
    ledger.reserve_mount_gate_challenge(pins)
    assert ledger.consume_mount_gate_challenge(decision) is True
    with ledger._transaction() as db:
        db.execute("UPDATE mount_gate_challenges SET state='reserved',decision=NULL,decision_mac=NULL")
    with pytest.raises(PermissionError): ledger.consume_mount_gate_challenge(decision)
    ledger.close()


def test_gate_legacy_create_without_authenticated_context_cannot_reserve(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    _, _, pins = gate_fixture(ledger)
    with ledger._transaction() as db: db.execute('DELETE FROM create_contexts')
    with pytest.raises(PermissionError): ledger.reserve_mount_gate_challenge(pins)
    ledger.close()


def test_gate_failure_after_commit_remains_consumed(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from hub_container_executor import mount_gate_protocol as protocol
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    _, decision, pins = gate_fixture(ledger, require_gate=True)
    ledger.reserve_mount_gate_challenge(pins)
    key = Ed25519PrivateKey.generate()
    ticks = iter((11.0, 14.0))
    with pytest.raises(protocol.MountGateProtocolError):
        protocol.verify_decision(protocol.issue_decision(decision, key), public_key=key.public_key(),
            expected=pins, exchange_wall_ms=100000, exchange_monotonic=10.0,
            consume_challenge=ledger.consume_mount_gate_challenge,
            wall_clock=lambda: 101.0, monotonic=lambda: next(ticks))
    with ledger._connection() as db:
        row = db.execute('SELECT state,decision,decision_mac FROM mount_gate_challenges').fetchone()
        assert row['state'] == 'consumed' and row['decision'] and row['decision_mac']
    with pytest.raises(PermissionError): ledger.consume_mount_gate_challenge(decision)
    ledger.close()


def test_gate_transaction_failure_does_not_partially_consume(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    _, decision, pins = gate_fixture(ledger, require_gate=True)
    ledger.reserve_mount_gate_challenge(pins)
    with ledger._transaction() as db:
        db.execute("CREATE TRIGGER fail_gate BEFORE UPDATE ON mount_gate_challenges BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError): ledger.consume_mount_gate_challenge(decision)
    with ledger._transaction() as db:
        row = db.execute('SELECT state,decision,decision_mac FROM mount_gate_challenges').fetchone()
        assert tuple(row) == ('reserved', None, None)
        db.execute('DROP TRIGGER fail_gate')
    assert ledger.consume_mount_gate_challenge(decision) is True
    ledger.close()


@pytest.mark.parametrize('fault', ['missing', 'held', 'tampered'])
def test_gate_consume_requires_authenticated_pending_requirement(tmp_path, monkeypatch, fault):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    try:
        _, decision, pins = gate_fixture(ledger, require_gate=fault != 'missing')
        ledger.reserve_mount_gate_challenge(pins)
        if fault == 'held':
            ledger.hold_gate_uncertain(pins)
        elif fault == 'tampered':
            with ledger._transaction() as db:
                db.execute("UPDATE gate_required SET state_mac='invalid'")
        with pytest.raises(PermissionError):
            ledger.consume_mount_gate_challenge(decision)
        with ledger._connection() as db:
            row = db.execute('SELECT state,decision FROM mount_gate_challenges').fetchone()
            assert tuple(row) == ('reserved', None)
    finally:
        ledger.close()


@pytest.mark.parametrize('requirement', ['pending', 'held', 'tampered'])
@pytest.mark.parametrize('requested', ['succeeded', 'failed'])
def test_required_gate_outcome_never_finishes_as_success_or_failure(tmp_path, monkeypatch, requirement, requested):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    try:
        command, _, pins = gate_fixture(ledger)
        ledger.require_gate_outcome(command.command_id, pins['operation_id'])
        if requirement == 'held': ledger.hold_gate_uncertain(pins)
        if requirement == 'tampered':
            with ledger._transaction() as db: db.execute("UPDATE gate_required SET state='complete',state_mac='invalid'")
        result = ledger.finish(command.command_id, state=requested,
                               reason=None if requested == 'succeeded' else 'effect_failed', evidence=evidence())
        assert result.state == 'held' and result.reason == 'inspect_required'
        assert ledger.get(command.command_id).state == 'held'
    finally:
        ledger.close()


def test_gate_hold_racing_finish_cannot_commit_success(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    try:
        command, _, pins = gate_fixture(ledger)
        ledger.require_gate_outcome(command.command_id, pins['operation_id'])
        barrier = threading.Barrier(2); outcomes = []
        def hold():
            barrier.wait()
            try: ledger.hold_gate_uncertain(pins); outcomes.append('held')
            except PermissionError: outcomes.append('already_finished')
        def finish():
            barrier.wait()
            try: outcomes.append(ledger.finish(command.command_id, state='succeeded', reason=None, evidence=evidence()).state)
            except Exception as exc: outcomes.append(exc)
        threads = [threading.Thread(target=hold), threading.Thread(target=finish)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        assert all(not thread.is_alive() for thread in threads)
        assert 'held' in outcomes and all(outcome in {'held', 'already_finished'} for outcome in outcomes)
        assert ledger.get(command.command_id).state == 'held'
    finally:
        ledger.close()


@pytest.mark.parametrize('mark_held', [False, True])
def test_gate_requirement_survives_owner_restart_as_held(tmp_path, monkeypatch, mark_held):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    command, _, pins = gate_fixture(ledger)
    ledger.require_gate_outcome(command.command_id, pins['operation_id'])
    if mark_held: ledger.hold_gate_uncertain(pins)
    ledger.close()
    reopened = open_ledger(tmp_path)
    try:
        assert reopened.get(command.command_id).state == 'held'
        with reopened._connection() as db:
            assert db.execute('SELECT state FROM gate_required').fetchone()[0] == ('held' if mark_held else 'pending')
        with pytest.raises(PermissionError): reopened.hold_gate_uncertain(pins)
    finally:
        reopened.close()


@pytest.mark.parametrize('fault', ['context_mac', 'target_fence'])
@pytest.mark.parametrize('method', ['require', 'hold'])
def test_gate_requirement_rechecks_authenticated_context_and_fence(tmp_path, monkeypatch, fault, method):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    try:
        command, _, pins = gate_fixture(ledger)
        if method == 'hold': ledger.require_gate_outcome(command.command_id, pins['operation_id'])
        with ledger._transaction() as db:
            if fault == 'context_mac': db.execute("UPDATE create_contexts SET context_mac='invalid'")
            else: db.execute('UPDATE targets SET fence=fence+1')
        with pytest.raises(PermissionError):
            if method == 'require': ledger.require_gate_outcome(command.command_id, pins['operation_id'])
            else: ledger.hold_gate_uncertain(pins)
        with ledger._connection() as db:
            rows = db.execute('SELECT state FROM gate_required').fetchall()
        assert [row[0] for row in rows] == (['pending'] if method == 'hold' else [])
    finally:
        ledger.close()


@pytest.mark.parametrize('requested', ['succeeded', 'failed'])
def test_legacy_command_without_gate_requirement_retains_finish_behavior(tmp_path, monkeypatch, requested):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    try:
        command, _, _ = gate_fixture(ledger)
        result = ledger.finish(command.command_id, state=requested,
                               reason=None if requested == 'succeeded' else 'effect_failed', evidence=evidence())
        assert result.state == requested
    finally:
        ledger.close()


def open_ledger(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700, exist_ok=True)
    return SQLiteLedger(root / "ledger.sqlite3", manifest_digest=MANIFEST,
                        candidate_commit=COMMIT, policy_revision=1)


def _child_owner_attempt(database_path, result_pipe):
    try:
        ledger = SQLiteLedger(database_path, manifest_digest=MANIFEST,
                              candidate_commit=COMMIT, policy_revision=1)
    except OwnerBusy:
        result_pipe.send("busy")
    else:
        ledger.close()
        result_pipe.send("unexpected-owner")


def _crash_after_running_admission(database_path, payload, result_pipe):
    ledger = SQLiteLedger(database_path, manifest_digest=MANIFEST,
                          candidate_commit=COMMIT, policy_revision=1)
    command = ContainerPilotRequest.model_validate(payload)
    ledger.admit(command)
    operation_id = ledger.begin_operation(command.command_id, "create")
    result_pipe.send(operation_id)
    while True:
        time.sleep(1)


def test_kernel_owner_is_held_for_lifetime_and_second_owner_cannot_recover(tmp_path):
    first = open_ledger(tmp_path)
    command = request()
    first.admit(command)
    with pytest.raises(OwnerBusy):
        open_ledger(tmp_path)
    assert first.get(command.command_id).state == "running"
    first.close()

    recovered = open_ledger(tmp_path)
    assert recovered.get(command.command_id).state == "held"
    assert recovered.get(command.command_id).reason == "recovery_required"
    assert recovered.operation_rows(command.command_id) == ()
    recovered.close()


def test_active_owner_is_excluded_across_processes(tmp_path):
    ledger = open_ledger(tmp_path)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_child_owner_attempt,
                              args=(str(ledger.path), child))
    process.start()
    child.close()
    try:
        assert parent.poll(10)
        assert parent.recv() == "busy"
    finally:
        process.join(10)
        ledger.close()
        parent.close()
    assert process.exitcode == 0


def test_sigkill_orphan_keeps_intent_held_without_effect_replay(tmp_path):
    ledger = open_ledger(tmp_path)
    database_path = str(ledger.path)
    ledger.close()
    command = request()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_crash_after_running_admission,
                              args=(database_path, command.model_dump(mode="json"), child))
    process.start()
    child.close()
    try:
        assert parent.poll(15)
        operation_id = parent.recv()
        assert operation_id
    finally:
        process.kill()
        process.join(10)
        parent.close()
    assert process.exitcode == -9
    recovered = open_ledger(tmp_path)
    assert recovered.get(command.command_id).state == "held"
    assert recovered.get(command.command_id).reason == "recovery_required"
    assert recovered.operation_rows(command.command_id)[0]["status"] == "intent"
    assert recovered.admit(command).response.state == "held"
    assert recovered.operation_rows(command.command_id)[0]["status"] == "intent"
    recovered.close()


def test_stable_command_replay_and_payload_collision_do_not_repeat_phase(tmp_path):
    ledger = open_ledger(tmp_path)
    run_id = str(uuid4())
    original = request(run_id=run_id)
    admitted = ledger.admit(original)
    assert admitted.accepted
    replay = ledger.admit(original)
    assert replay.replayed
    assert replay.response.model_copy(update={"replayed": False}) == admitted.response

    collision = request(run_id=run_id, command_id=original.command_id, fence=1)
    collision = collision.model_copy(update={"action": "smoke_container"})
    response = ledger.admit(collision)
    assert not response.accepted
    assert response.response.state == "held"
    assert response.response.reason == "identity_conflict"

    duplicate_phase = request(run_id=run_id)
    response = ledger.admit(duplicate_phase)
    assert not response.accepted
    assert response.response.reason == "identity_conflict"
    ledger.close()


def test_phase_order_fence_and_target_reservation_are_durable(tmp_path):
    ledger = open_ledger(tmp_path)
    run_id = str(uuid4())
    deploy = request(run_id=run_id, fence=10)
    assert ledger.admit(deploy).accepted

    # Smoke cannot run until a successful deploy has been durably finalized.
    smoke = request("smoke_container", run_id=run_id, fence=10)
    assert ledger.admit(smoke).response.reason == "target_busy"
    ledger.finish(deploy.command_id, state="succeeded", reason=None,
                  evidence=ContainerPilotEvidence(container_id="c" * 64,
                                                  image_config_id=MANIFEST,
                                                  health="unknown", evidence_verified=True),
                  generation_id="generation-1")
    assert ledger.target_snapshot()["generation_id"] == "generation-1"
    smoke = request("smoke_container", run_id=run_id, fence=10)
    assert ledger.admit(smoke).accepted
    ledger.finish(smoke.command_id, state="succeeded", reason=None,
                  evidence=ContainerPilotEvidence(container_id="c" * 64,
                                                  image_config_id=MANIFEST,
                                                  health="healthy", evidence_verified=True))

    # A new run must use a strictly higher fence; the old active run stays bound.
    stale = request(fence=9)
    assert ledger.admit(stale).response.reason == "stale_fence"
    same_fence_new_run = request(fence=10)
    assert ledger.admit(same_fence_new_run).response.reason == "identity_conflict"
    newer = request(fence=11)
    assert ledger.admit(newer).accepted

    ledger.close()


def test_pause_and_fence_are_rechecked_before_every_operation(tmp_path):
    ledger = open_ledger(tmp_path)
    command = request()
    assert ledger.admit(command).accepted
    ledger.set_paused(True)
    with pytest.raises(PermissionError, match="paused"):
        ledger.begin_operation(command.command_id, "create")
    assert ledger.operation_rows(command.command_id) == ()
    ledger.set_paused(False)
    operation_id = ledger.begin_operation(command.command_id, "create")
    assert operation_id
    with pytest.raises(RuntimeError, match="already reserved"):
        ledger.begin_operation(command.command_id, "create")
    ledger.close()


def test_reconcile_data_is_read_only_and_orphan_intents_stay_held(tmp_path):
    ledger = open_ledger(tmp_path)
    command = request()
    ledger.admit(command)
    operation_id = ledger.begin_operation(command.command_id, "create")
    before = ledger.operation_rows(command.command_id)
    assert before[0]["operation_id"] == operation_id
    ledger.close()

    recovered = open_ledger(tmp_path)
    assert recovered.reconcile_snapshot(command.command_id)["response"]["state"] == "held"
    assert recovered.operation_rows(command.command_id)[0]["status"] == "intent"
    with pytest.raises(PermissionError):
        recovered.begin_operation(command.command_id, "create")
    recovered.close()


def test_only_inspected_terminal_failure_can_unhold_phase_for_rollback(tmp_path):
    ledger = open_ledger(tmp_path)
    run_id = str(uuid4())
    deploy = request(run_id=run_id)
    ledger.admit(deploy)
    operation_id = ledger.begin_operation(deploy.command_id, "create")
    ledger.close()

    recovered = open_ledger(tmp_path)
    assert recovered.get(deploy.command_id).state == "held"
    recovered.record_terminal_failure(operation_id)
    failed = recovered.resolve_terminal_failure(deploy.command_id)
    assert failed.state == "failed" and failed.reason == "effect_failed"
    rollback = request("rollback_container", run_id=run_id, fence=1)
    assert recovered.admit(rollback).accepted
    assert recovered.begin_operation(rollback.command_id, "rollback_state")
    recovered.close()


def test_verified_instance_and_generation_are_private_durable_target_state(tmp_path):
    ledger = open_ledger(tmp_path)
    command = request()
    assert ledger.admit(command).accepted
    ledger.record_target_instance(command.command_id, "c" * 64, MANIFEST, "generation-1")
    target = ledger.target_snapshot()
    assert target["container_id"] == "c" * 64
    assert target["image_config_id"] == MANIFEST
    assert target["generation_id"] == "generation-1"
    ledger.close()


def test_local_filesystem_detection_chooses_most_specific_mount(tmp_path):
    path = tmp_path / "private" / "db.sqlite3"
    mountinfo = (
        "10 1 0:1 / / rw - ext4 /dev/root rw\n"
        f"11 10 0:2 / {tmp_path} rw - ext4 /dev/root rw\n"
        f"12 11 0:3 / {tmp_path}/private rw - cifs //server/share rw\n"
    )
    assert _mount_type_for(path, mountinfo) == "cifs"


def test_ledger_refuses_smb_mount_before_creating_lock_or_database(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    mountfile = tmp_path / "mountinfo"
    mountfile.write_text(
        "10 1 0:1 / / rw - ext4 /dev/root rw\n"
        f"11 10 0:2 / {root} rw - cifs //server/share rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ledger_module, "MOUNTINFO_PATH", mountfile)
    with pytest.raises(ValueError, match="approved local filesystem"):
        SQLiteLedger(root / "ledger.sqlite3", manifest_digest=MANIFEST,
                     candidate_commit=COMMIT, policy_revision=1)
    assert not (root / "ledger.sqlite3").exists()
    assert not (root / "ledger.sqlite3.owner.lock").exists()


def test_private_operation_receipts_are_authenticated_and_tampering_stays_unknown(tmp_path):
    ledger = open_ledger(tmp_path)
    command = request()
    ledger.admit(command)
    operation_id = ledger.begin_operation(command.command_id, "create")
    ledger.complete_operation(operation_id, {"container_id": "c" * 64})
    evidence_key = ledger.path.with_name(ledger.path.name + ".evidence.key")
    assert stat.S_IMODE(evidence_key.stat().st_mode) == 0o600
    assert ledger.operation_rows(command.command_id)[0]["receipt_authentic"] is True

    connection = sqlite3.connect(ledger.path)
    connection.execute("UPDATE operations SET receipt=? WHERE operation_id=?",
                        ('{"container_id":"' + "d" * 64 + '"}', operation_id))
    connection.commit()
    connection.close()
    row = ledger.operation_rows(command.command_id)[0]
    assert row["status"] == "invalid"
    assert row["receipt"] is None and row["receipt_authentic"] is False
    ledger.close()


def test_missing_evidence_key_never_replaces_key_for_existing_ledger(tmp_path):
    ledger = open_ledger(tmp_path)
    database_path = ledger.path
    key_path = database_path.with_name(database_path.name + ".evidence.key")
    ledger.close()
    key_path.unlink()
    with pytest.raises(ValueError, match="key is missing"):
        open_ledger(tmp_path)
    assert database_path.exists() and not key_path.exists()


def test_close_waits_for_sqlite_writer_already_waiting_on_transaction_lock(tmp_path):
    ledger = open_ledger(tmp_path)
    holder_entered = threading.Event()
    release_holder = threading.Event()
    holder_errors = []

    def hold_write_lock():
        try:
            with ledger._transaction():
                holder_entered.set()
                assert release_holder.wait(5)
        except BaseException as exc:
            holder_errors.append(exc)

    holder = threading.Thread(target=hold_write_lock)
    holder.start()
    assert holder_entered.wait(5)

    writer_id = None
    writer_lease_entered = threading.Event()
    writer_done = threading.Event()
    writer_errors = []
    original_connection = ledger._connection

    @contextmanager
    def tracked_connection():
        with original_connection() as connection:
            if threading.get_ident() == writer_id:
                writer_lease_entered.set()
            yield connection

    ledger._connection = tracked_connection

    def write_pause():
        nonlocal writer_id
        writer_id = threading.get_ident()
        try:
            ledger.set_paused(True)
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_done.set()

    writer = threading.Thread(target=write_pause)
    writer.start()
    assert writer_lease_entered.wait(5)
    close_done = threading.Event()
    closer = threading.Thread(target=lambda: (ledger.close(), close_done.set()))
    closer.start()
    try:
        assert not close_done.wait(0.05), "close released the owner while a writer held a connection"
        with pytest.raises(OwnerBusy):
            open_ledger(tmp_path)
    finally:
        release_holder.set()
        holder.join(10)
        writer.join(10)
        closer.join(10)
    assert not holder.is_alive() and not writer.is_alive() and not closer.is_alive()
    assert not holder_errors and not writer_errors
    assert writer_done.is_set() and close_done.is_set()

    recovered = open_ledger(tmp_path)
    assert recovered.is_paused() is True
    recovered.close()


def test_forked_ledger_use_is_rejected_and_child_close_preserves_parent_owner(tmp_path):
    ledger = open_ledger(tmp_path)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            try:
                ledger.set_paused(True)
            except Exception as exc:
                result = "rejected:" + type(exc).__name__
            else:
                result = "mutated"
            ledger.close()
            os.write(write_fd, result.encode("ascii"))
        finally:
            os.close(write_fd)
            os._exit(0)

    os.close(write_fd)
    try:
        result = os.read(read_fd, 256).decode("ascii")
    finally:
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert result.startswith("rejected:"), result
    assert ledger.is_paused() is False
    with pytest.raises(OwnerBusy):
        open_ledger(tmp_path)
    ledger.close()


def test_terminal_failure_resolution_rejects_forged_private_receipt_atomically(tmp_path):
    ledger = open_ledger(tmp_path)
    command = request()
    ledger.admit(command)
    operation_id = ledger.begin_operation(command.command_id, "create")
    ledger.close()

    recovered = open_ledger(tmp_path)
    connection = sqlite3.connect(recovered.path)
    connection.execute(
        "UPDATE operations SET status='terminal_failed',receipt='{}',receipt_mac='00' "
        "WHERE operation_id=?", (operation_id,)
    )
    connection.commit()
    connection.close()

    try:
        recovered.resolve_terminal_failure(command.command_id)
    except PermissionError:
        pass
    else:
        raise AssertionError("forged terminal-failure receipt released the reservation")
    assert recovered.get(command.command_id).state == "held"
    with sqlite3.connect(recovered.path) as check:
        phase, run, target = check.execute(
            "SELECT p.state,r.state,t.state FROM phases p "
            "JOIN runs r ON r.run_id=p.run_id JOIN targets t ON t.current_run_id=r.run_id "
            "WHERE p.command_id=?", (command.command_id,)
        ).fetchone()
    assert (phase, run, target) == ("held", "held", "held")
    recovered.close()


def test_create_context_is_atomic_authenticated_and_survives_reopen(tmp_path):
    ledger = open_ledger(tmp_path)
    command = request()
    ledger.admit(command)
    context = {"schema": 1, "generation_id": "synthetic-g1"}
    with ledger._connection() as db:
        db.execute("CREATE TRIGGER reject_context BEFORE INSERT ON create_contexts BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(RuntimeError):
        ledger.begin_operation(command.command_id, "create_candidate", create_context=context)
    assert ledger.operation_rows(command.command_id) == ()
    with ledger._connection() as db:
        db.execute("DROP TRIGGER reject_context")
    operation = ledger.begin_operation(command.command_id, "create_candidate", create_context=context)
    assert ledger.create_context(operation) == context
    ledger.close()
    reopened = open_ledger(tmp_path)
    assert reopened.get(command.command_id).state == "held"
    assert reopened.create_context(operation) == context
    with reopened._connection() as db:
        db.execute("UPDATE create_contexts SET context=? WHERE operation_id=?",
                   ('{"generation_id":"other","schema":1}', operation))
    assert reopened.create_context(operation) is None
    reopened.close()


def test_legacy_create_has_no_context_authority(tmp_path):
    ledger = open_ledger(tmp_path)
    command = request()
    ledger.admit(command)
    operation = ledger.begin_operation(command.command_id, "create_candidate")
    assert ledger.create_context(operation) is None
    ledger.close()


def _crash_after_bound_create_intent(database_path, payload, pipe):
    ledger = SQLiteLedger(database_path, manifest_digest=MANIFEST,
                          candidate_commit=COMMIT, policy_revision=1)
    command = ContainerPilotRequest.model_validate(payload)
    ledger.admit(command)
    operation = ledger.begin_operation(command.command_id, "create_candidate",
                                       create_context={"schema": 1, "generation_id": "synthetic-g1"})
    pipe.send(operation)
    while True:
        time.sleep(1)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux SIGKILL and owner semantics required")
def test_sigkill_after_bound_intent_retains_context_and_holds(tmp_path):
    ledger = open_ledger(tmp_path)
    path = str(ledger.path)
    ledger.close()
    command = request()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_crash_after_bound_create_intent,
                              args=(path, command.model_dump(mode="json"), child))
    process.start()
    child.close()
    try:
        assert parent.poll(15)
        operation = parent.recv()
    finally:
        process.kill()
        process.join(10)
        parent.close()
    assert process.exitcode == -9
    recovered = open_ledger(tmp_path)
    assert recovered.get(command.command_id).state == "held"
    assert recovered.create_context(operation) == {"schema": 1, "generation_id": "synthetic-g1"}
    assert recovered.operation_rows(command.command_id)[0]["status"] == "intent"
    assert recovered.admit(command).response.state == "held"
    recovered.close()


@pytest.mark.parametrize("raw,mac", [
    ("x" * 16385, "0" * 64),
    ("é" * 8193, "0" * 64),
    (b"binary", "0" * 64),
    ("{}", "not-a-mac"),
    ("{}", b"0" * 64),
    ("{", "0" * 64),
])
def test_context_rejects_untrusted_bytes_before_parsing(tmp_path, monkeypatch, raw, mac):
    ledger = open_ledger(tmp_path)
    command = request()
    ledger.admit(command)
    operation = ledger.begin_operation(command.command_id, "create_candidate", create_context={"schema": 2})
    with ledger._connection() as db:
        db.execute("UPDATE create_contexts SET context=?,context_mac=? WHERE operation_id=?",
                   (raw, mac, operation))
    def refuse_parse(*args, **kwargs):
        raise AssertionError("unauthenticated context must not be parsed")
    monkeypatch.setattr(ledger_module.json, "loads", refuse_parse)
    assert ledger.create_context(operation) is None
    ledger.close()


def test_pending_gate_context_returns_only_fenced_nonsecret_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    try:
        assert ledger.pending_gate_context(role='candidate') is None
        command, _, pins = gate_fixture(ledger)
        ledger.require_gate_outcome(command.command_id, pins['operation_id'])
        result = ledger.pending_gate_context(role='candidate')
        expected = {name: pins[name] for name in ('operation_id', 'run_id', 'fence', 'generation_id',
                    'container_id', 'image_config_id', 'runnable_digest', 'platform_manifest_digest')}
        assert result == dict(expected, role='candidate')
        with pytest.raises(PermissionError): ledger.pending_gate_context(role='rollback')
        with ledger._transaction() as db: db.execute('UPDATE targets SET fence=fence+1')
        with pytest.raises(PermissionError): ledger.pending_gate_context(role='candidate')
    finally:
        ledger.close()


@pytest.mark.parametrize('fault', ['requirement_mac', 'context_mac', 'receipt_mac', 'held'])
def test_pending_gate_context_rejects_tampered_or_held(tmp_path, monkeypatch, fault):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    try:
        command, _, pins = gate_fixture(ledger)
        ledger.require_gate_outcome(command.command_id, pins['operation_id'])
        if fault == 'held': ledger.hold_gate_uncertain(pins)
        else:
            with ledger._transaction() as db:
                if fault == 'requirement_mac': db.execute("UPDATE gate_required SET state_mac='bad'")
                elif fault == 'context_mac': db.execute("UPDATE create_contexts SET context_mac='bad'")
                else: db.execute("UPDATE operations SET receipt_mac='bad' WHERE operation_id=?", (pins['operation_id'],))
        with pytest.raises(PermissionError): ledger.pending_gate_context(role='candidate')
    finally:
        ledger.close()
