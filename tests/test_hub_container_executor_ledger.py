"""Real private SQLite and kernel-owner tests; no Docker or listener."""
import multiprocessing
import os
import sqlite3
import stat
import subprocess
import threading
import time
import types
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from engineering_hub.container_pilot_client import ContainerPilotEvidence, ContainerPilotRequest
import hub_container_executor.ledger as ledger_module
from hub_container_executor.ledger import OwnerBusy, SQLiteLedger, _mount_type_for, single_pilot_outcome_digest

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


def _single_pilot_synthetic_terminal(ledger, cmd):
    assert ledger.admit(cmd).accepted
    for kind in ('state_copy', 'create_candidate', 'start_candidate'):
        operation = ledger.begin_operation(cmd.command_id, kind)
        ledger.complete_operation(operation, {'synthetic': kind})
    result = ledger.finish(cmd.command_id, state='succeeded', reason=None,
        evidence=ContainerPilotEvidence(container_id='c'*64, image_config_id='sha256:'+'d'*64,
                                       health='unknown', evidence_verified=True))
    return result


def test_single_pilot_gate_requires_exact_verified_outcome_and_ack_after_reopen(tmp_path):
    from dataclasses import replace
    ledger = open_ledger(tmp_path)
    cmd = request(); other = request()
    ledger.enable_single_pilot_protocol()
    assert not ledger.admit(cmd).accepted
    ledger.arm_single_pilot(cmd)
    with pytest.raises(PermissionError): ledger.arm_single_pilot(cmd)
    assert not ledger.admit(other).accepted
    result = _single_pilot_synthetic_terminal(ledger, cmd)
    # An already recorded success is not surfaced until the trusted caller has
    # verified external effects and explicitly stores the exact outcome.
    assert ledger.admit(cmd).response.state == 'held'
    assert ledger.get(cmd.command_id).state == 'held'
    ledger.record_single_pilot_verified_outcome(cmd, result)
    ledger.record_single_pilot_verified_outcome(cmd, result)
    assert ledger.get(cmd.command_id).state == 'succeeded'
    assert ledger.inspect_single_pilot()['state'] == 'awaiting_ack'
    replay = ledger.admit(cmd)
    assert replay.replayed and replay.response.state == 'succeeded'
    assert not ledger.admit(other).accepted
    ledger.close()
    ledger = open_ledger(tmp_path)
    try:
        assert ledger.inspect_single_pilot()['state'] == 'awaiting_ack'
        conflict = cmd.model_copy(update={'fencing_generation':cmd.fencing_generation+1})
        with pytest.raises(PermissionError): ledger.acknowledge_single_pilot(conflict, single_pilot_outcome_digest(result))
        altered = result.model_copy(update={'reason':'effect_failed'})
        with pytest.raises(PermissionError): ledger.acknowledge_single_pilot(cmd, single_pilot_outcome_digest(altered))
        ledger.acknowledge_single_pilot(cmd,single_pilot_outcome_digest(replay.response))
        ledger.acknowledge_single_pilot(cmd,single_pilot_outcome_digest(result))
        with pytest.raises(PermissionError): ledger.acknowledge_single_pilot(conflict,single_pilot_outcome_digest(result))
        assert ledger.inspect_single_pilot() is None
        assert ledger.admit(cmd).replayed and ledger.admit(cmd).response.state == 'succeeded'
        assert single_pilot_outcome_digest(replay.response) == single_pilot_outcome_digest(result)
        ledger.arm_single_pilot(other)
        assert ledger.admit(other).accepted is False  # Existing target phase still fences it.
    finally:
        ledger.close()


def test_single_pilot_held_survives_reopen_and_never_rearms_or_replays(tmp_path):
    ledger = open_ledger(tmp_path); cmd = request()
    ledger.enable_single_pilot_protocol(); ledger.arm_single_pilot(cmd)
    assert ledger.admit(cmd).accepted
    operation = ledger.begin_operation(cmd.command_id, 'create_candidate')
    ledger.close()
    ledger = open_ledger(tmp_path)
    try:
        assert ledger.inspect_single_pilot()['state'] == 'effect_held'
        assert ledger.get(cmd.command_id).state == 'held'
        assert ledger.operation_rows(cmd.command_id)[0]['operation_id'] == operation
        assert not ledger.admit(cmd).accepted
        assert not ledger.admit(request()).accepted
        with pytest.raises(PermissionError): ledger.arm_single_pilot(cmd)
    finally:
        ledger.close()


def test_single_pilot_unverified_or_incomplete_result_cannot_advance(tmp_path):
    ledger = open_ledger(tmp_path); cmd = request()
    try:
        ledger.enable_single_pilot_protocol(); ledger.arm_single_pilot(cmd)
        assert ledger.admit(cmd).accepted
        result = ledger.finish(cmd.command_id, state='succeeded', reason=None,
            evidence=ContainerPilotEvidence(container_id='c'*64, image_config_id='sha256:'+'d'*64,
                                           health='unknown', evidence_verified=True))
        with pytest.raises(PermissionError): ledger.record_single_pilot_verified_outcome(cmd,result)
        assert ledger.inspect_single_pilot()['state'] == 'effect_held'
        with pytest.raises(PermissionError): ledger.acknowledge_single_pilot(cmd,single_pilot_outcome_digest(result))
    finally:
        ledger.close()


def _single_pilot_crash_child(database_path, payload, stage):
    cmd = ContainerPilotRequest.model_validate(payload)
    ledger = SQLiteLedger(database_path, manifest_digest=MANIFEST, candidate_commit=COMMIT, policy_revision=1)
    ledger.enable_single_pilot_protocol(); ledger.arm_single_pilot(cmd)
    if stage == 'awaiting_ack':
        outcome = _single_pilot_synthetic_terminal(ledger, cmd)
        ledger.record_single_pilot_verified_outcome(cmd, outcome)
    os._exit(0)


@pytest.mark.parametrize('stage', ['effect_held', 'awaiting_ack'])
def test_single_pilot_process_exit_preserves_gate(tmp_path, stage):
    ledger = open_ledger(tmp_path); path = str(ledger.path); ledger.close()
    cmd = request()
    context = multiprocessing.get_context('spawn')
    child = context.Process(target=_single_pilot_crash_child, args=(path,cmd.model_dump(mode='json'),stage))
    child.start(); child.join(15)
    assert child.exitcode == 0
    ledger = open_ledger(tmp_path)
    try:
        assert ledger.inspect_single_pilot()['state'] == stage
        assert not ledger.admit(request()).accepted
        with pytest.raises(PermissionError): ledger.arm_single_pilot(cmd)
    finally:
        ledger.close()


def _single_pilot_uncommitted_child(database_path):
    db = sqlite3.connect(database_path, isolation_level=None)
    db.execute('BEGIN IMMEDIATE')
    db.execute("DELETE FROM control WHERE key='single_pilot_gate'")
    os._exit(0)


def test_uncommitted_ack_crash_does_not_release_gate(tmp_path):
    ledger = open_ledger(tmp_path); cmd = request()
    ledger.enable_single_pilot_protocol(); ledger.arm_single_pilot(cmd)
    result = _single_pilot_synthetic_terminal(ledger,cmd)
    ledger.record_single_pilot_verified_outcome(cmd,result)
    path=str(ledger.path); ledger.close()
    ctx=multiprocessing.get_context('spawn')
    child=ctx.Process(target=_single_pilot_uncommitted_child,args=(path,))
    child.start(); child.join(15); assert child.exitcode == 0
    ledger=open_ledger(tmp_path)
    try:
        assert ledger.inspect_single_pilot()['state'] == 'awaiting_ack'
        assert not ledger.admit(request()).accepted
        ledger.acknowledge_single_pilot(cmd,single_pilot_outcome_digest(result))
        ledger.acknowledge_single_pilot(cmd,single_pilot_outcome_digest(result))
    finally:
        ledger.close()


def test_concurrent_arming_has_one_winner(tmp_path):
    ledger=open_ledger(tmp_path); ledger.enable_single_pilot_protocol()
    results=[]; barrier=threading.Barrier(3)
    def arm(cmd):
        barrier.wait()
        try:
            ledger.arm_single_pilot(cmd); results.append('armed')
        except PermissionError:
            results.append('held')
    threads=[threading.Thread(target=arm,args=(request(),)) for _ in range(2)]
    try:
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads: thread.join(10)
        assert sorted(results) == ['armed','held']
    finally:
        ledger.close()


def test_protocol_enable_refuses_existing_control_or_run_state(tmp_path):
    ledger=open_ledger(tmp_path)
    try:
        ledger.set_paused(True)
        with pytest.raises(PermissionError): ledger.enable_single_pilot_protocol()
    finally:
        ledger.close()


def test_single_pilot_denied_fresh_phase_releases_only_its_gate(tmp_path):
    ledger = open_ledger(tmp_path)
    denied, permitted = request('smoke_container'), request()
    try:
        ledger.enable_single_pilot_protocol()
        ledger.arm_single_pilot(denied)
        admission = ledger.admit(denied)
        assert not admission.accepted and admission.response.reason == 'identity_conflict'
        assert ledger.inspect_single_pilot() is None
        assert ledger.operation_rows(denied.command_id) == ()
        replay = ledger.admit(denied)
        assert replay.replayed and replay.response.reason == 'identity_conflict'
        assert ledger.get(denied.command_id).reason == 'identity_conflict'
        changed = denied.model_copy(update={'fencing_generation': denied.fencing_generation + 1})
        assert ledger.admit(changed).response.reason == 'identity_conflict'
        ledger.arm_single_pilot(permitted)
        assert ledger.admit(permitted).accepted
        # Replaying a denied identity must not release a different gate.
        assert not ledger.admit(denied).accepted
        assert ledger.inspect_single_pilot()['request']['command_id'] == permitted.command_id
    finally:
        ledger.close()
    ledger = open_ledger(tmp_path)
    try:
        assert ledger.admit(denied).replayed
        assert ledger.get(denied.command_id).reason == 'identity_conflict'
        assert ledger.inspect_single_pilot()['request']['command_id'] == permitted.command_id
    finally:
        ledger.close()


def _crash_during_single_pilot_denial(database_path, payload):
    ledger = SQLiteLedger(database_path, manifest_digest=MANIFEST,
                          candidate_commit=COMMIT, policy_revision=1)
    original = ledger._store_denial
    def crash_after_uncommitted_delete(*args):
        original(*args)
        os._exit(0)
    ledger._store_denial = crash_after_uncommitted_delete
    ledger.admit(ContainerPilotRequest.model_validate(payload))


def test_single_pilot_denial_crash_preserves_gate(tmp_path):
    ledger = open_ledger(tmp_path)
    denied = request('smoke_container')
    ledger.enable_single_pilot_protocol()
    ledger.arm_single_pilot(denied)
    path = str(ledger.path)
    ledger.close()
    child = multiprocessing.get_context('spawn').Process(
        target=_crash_during_single_pilot_denial, args=(path, denied.model_dump(mode='json')))
    child.start(); child.join(15)
    assert child.exitcode == 0
    ledger = open_ledger(tmp_path)
    try:
        assert ledger.inspect_single_pilot()['request']['command_id'] == denied.command_id
        assert ledger.get(denied.command_id) is None
        with pytest.raises(PermissionError): ledger.arm_single_pilot(request())
        assert not ledger.admit(denied).accepted
        assert ledger.inspect_single_pilot() is None
    finally:
        ledger.close()


def test_single_pilot_denial_and_competing_arm_are_serialized(tmp_path):
    ledger = open_ledger(tmp_path)
    denied, other = request('smoke_container'), request()
    ledger.enable_single_pilot_protocol(); ledger.arm_single_pilot(denied)
    barrier = threading.Barrier(3); outcomes = []
    def reject():
        barrier.wait(); outcomes.append(('denied', ledger.admit(denied).accepted))
    def arm():
        barrier.wait()
        try:
            ledger.arm_single_pilot(other); outcomes.append(('arm', True))
        except PermissionError:
            outcomes.append(('arm', False))
    threads = [threading.Thread(target=reject), threading.Thread(target=arm)]
    try:
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads: thread.join(10)
        assert all(not thread.is_alive() for thread in threads)
        assert ('denied', False) in outcomes
        armed = ('arm', True) in outcomes
        gate = ledger.inspect_single_pilot()
        assert (gate is not None) == armed
        if armed:
            assert gate['request']['command_id'] == other.command_id
    finally:
        ledger.close()


def test_single_pilot_corrupt_denial_marker_fails_closed(tmp_path):
    ledger = open_ledger(tmp_path); denied = request('smoke_container')
    try:
        ledger.enable_single_pilot_protocol(); ledger.arm_single_pilot(denied)
        assert not ledger.admit(denied).accepted
        with ledger._transaction() as db:
            db.execute("UPDATE control SET value='{}' WHERE key=?", ('single_pilot_denial:' + denied.command_id,))
        with pytest.raises(PermissionError): ledger.admit(denied)
        with pytest.raises(PermissionError): ledger.get(denied.command_id)
    finally:
        ledger.close()


def test_single_pilot_denial_with_operation_conflict_keeps_gate(tmp_path):
    ledger = open_ledger(tmp_path); denied = request('smoke_container')
    try:
        ledger.enable_single_pilot_protocol(); ledger.arm_single_pilot(denied)
        # Model an inconsistent retained intent; never interpret it as no effect.
        with ledger._transaction() as db:
            db.execute("INSERT INTO operations VALUES (?,?,?,'intent',?,NULL,NULL)",
                       (str(uuid4()), denied.command_id, 'probe', '{}'))
        assert not ledger.admit(denied).accepted
        assert ledger.inspect_single_pilot()['request']['command_id'] == denied.command_id
        with ledger._connection() as db:
            assert db.execute('SELECT 1 FROM control WHERE key=?',
                              ('single_pilot_denial:' + denied.command_id,)).fetchone() is None
        with pytest.raises(PermissionError): ledger.arm_single_pilot(request())
    finally:
        ledger.close()


def _prior_protocol_ledger():
    """Load the exact reviewed old source, without changing the worktree."""
    result = None
    for executable in ('git', 'git.exe'):
        try:
            result = subprocess.run([executable, 'show', '85411ff5639470a2176111285bc5634837b24479:hub_container_executor/ledger.py'],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True, check=True, timeout=10)
            break
        except (OSError, subprocess.SubprocessError):
            continue
    if result is None:
        if os.environ.get('HUB_VERIFY_PRIOR_PILOT_LEDGER') == '1':
            pytest.fail('required reviewed prior ledger source is unavailable')
        pytest.skip('optional cross-version check: prior Git object unavailable; set HUB_VERIFY_PRIOR_PILOT_LEDGER=1 to require it')
    module = types.ModuleType('prior_protocol_ledger_for_test')
    exec(compile(result.stdout, 'prior_protocol_ledger.py', 'exec'), module.__dict__)
    return module.SQLiteLedger


@pytest.mark.parametrize('required', [False, True])
def test_cross_version_check_reports_missing_history(monkeypatch, required):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError('synthetic missing Git')
    monkeypatch.setattr(subprocess, 'run', unavailable)
    if required:
        monkeypatch.setenv('HUB_VERIFY_PRIOR_PILOT_LEDGER', '1')
    else:
        monkeypatch.delenv('HUB_VERIFY_PRIOR_PILOT_LEDGER', raising=False)
    with pytest.raises(pytest.fail.Exception if required else pytest.skip.Exception):
        _prior_protocol_ledger()


def test_previous_protocol_executor_holds_new_denial_and_preserves_other_gate(tmp_path):
    prior = _prior_protocol_ledger()
    ledger = open_ledger(tmp_path); denied, other = request('smoke_container'), request()
    ledger.enable_single_pilot_protocol(); ledger.arm_single_pilot(denied)
    assert not ledger.admit(denied).accepted
    ledger.arm_single_pilot(other)
    path = ledger.path; ledger.close()
    old = prior(path, manifest_digest=MANIFEST, candidate_commit=COMMIT, policy_revision=1)
    try:
        assert old.get(denied.command_id).reason == 'recovery_required'
        retry = old.admit(denied)
        assert not retry.accepted and not retry.replayed and retry.response.reason == 'recovery_required'
        assert old.inspect_single_pilot()['request']['command_id'] == other.command_id
    finally:
        old.close()


def test_new_executor_does_not_release_prior_unmarked_denial(tmp_path):
    prior = _prior_protocol_ledger()
    initial = open_ledger(tmp_path); path = initial.path; initial.close()
    old = prior(path, manifest_digest=MANIFEST, candidate_commit=COMMIT, policy_revision=1)
    denied = request('smoke_container')
    old.enable_single_pilot_protocol(); old.arm_single_pilot(denied)
    assert not old.admit(denied).accepted
    old.close()
    current = open_ledger(tmp_path)
    try:
        assert current.get(denied.command_id).reason == 'recovery_required'
        assert not current.admit(denied).accepted
        assert current.inspect_single_pilot()['request']['command_id'] == denied.command_id
        with pytest.raises(PermissionError): current.arm_single_pilot(request())
    finally:
        current.close()
