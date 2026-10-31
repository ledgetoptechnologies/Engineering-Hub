"""Read-only completion observations; host custody remains synthetic."""
import os
import socket
import threading
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import hub_container_executor.ledger as ledger_module
from hub_container_executor.broker_seqpacket import PeerPin, _start
from hub_container_executor.gate_ledger_channel import GateLedgerWorker, SupervisorLedgerClient
from tests.test_hub_container_executor_ledger import evidence, gate_fixture, open_ledger
from tests.test_hub_gate_ledger_channel import verified_launch

PROBE = 'sha256:' + '1' * 64


@pytest.fixture
def completed(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    command, decision, pins = gate_fixture(ledger)
    ledger.require_gate_outcome(command.command_id, pins['operation_id'])
    ledger.reserve_mount_gate_challenge(pins)
    assert ledger.consume_mount_gate_challenge(decision) is True
    health_id = ledger.begin_operation(command.command_id, 'gate_health')
    now = int(time.time() * 1000)
    receipt = dict(container_id=pins['container_id'], image_config_id=pins['image_config_id'],
                   profile_digest=PROBE, health='healthy', evidence_verified=True, daemon_pid=pins['host_pid'],
                   started_at_ms=now-10, finished_at_ms=now, elapsed_ms=10)
    ledger.complete_operation(health_id, receipt)
    try:
        yield ledger, command, pins, health_id, receipt
    finally:
        ledger.close()


def test_valid_snapshot_does_not_authorize_success(completed):
    ledger, command, pins, health_id, _ = completed
    observed = ledger.gate_completion_snapshot(pins, PROBE)
    assert observed['operation_id'] == pins['operation_id']
    assert observed['health_operation_id'] == health_id and observed['probe_digest'] == PROBE
    assert observed['role'] == 'candidate'
    assert observed['health_daemon_pid'] == pins['host_pid']
    assert observed['health_started_at_ms'] <= observed['health_finished_at_ms']
    assert observed['health_elapsed_ms'] == 10
    assert all(observed[name].startswith('sha256:') and len(observed[name]) == 71
               for name in ('health_receipt_digest', 'decision_payload_digest'))
    result = ledger.finish(command.command_id, state='succeeded', reason=None, evidence=evidence())
    assert result.state == 'held' and result.reason == 'inspect_required'


@pytest.mark.parametrize('fault', ['missing_requirement', 'held_requirement', 'bad_requirement_mac',
    'missing_challenge', 'reserved_challenge', 'bad_identity_mac', 'bad_decision_mac',
    'missing_health', 'unhealthy', 'bad_health_mac', 'wrong_fence', 'wrong_identity', 'wrong_probe',
    'wrong_daemon_pid', 'bool_daemon_pid', 'stale_receipt', 'future_receipt', 'bad_elapsed'])
def test_snapshot_rejects_missing_or_mismatched_authority(completed, fault):
    ledger, command, pins, health_id, receipt = completed
    pins = dict(pins)
    probe = PROBE
    if fault == 'wrong_fence': pins['fence'] += 1
    elif fault == 'wrong_identity': pins['nonce'] = 'b' * 64
    elif fault == 'wrong_probe': probe = 'sha256:' + '2' * 64
    elif fault == 'held_requirement': ledger.hold_gate_uncertain(pins)
    else:
        with ledger._transaction() as db:
            if fault == 'missing_requirement': db.execute('DELETE FROM gate_required')
            elif fault == 'bad_requirement_mac': db.execute("UPDATE gate_required SET state_mac='bad'")
            elif fault == 'missing_challenge': db.execute('DELETE FROM mount_gate_challenges')
            elif fault == 'reserved_challenge': db.execute("UPDATE mount_gate_challenges SET state='reserved'")
            elif fault == 'bad_identity_mac': db.execute("UPDATE mount_gate_challenges SET identity_mac='bad'")
            elif fault == 'bad_decision_mac': db.execute("UPDATE mount_gate_challenges SET decision_mac='bad'")
            elif fault == 'missing_health': db.execute('DELETE FROM operations WHERE operation_id=?', (health_id,))
            elif fault == 'bad_health_mac': db.execute("UPDATE operations SET receipt_mac='bad' WHERE operation_id=?", (health_id,))
            elif fault in {'unhealthy', 'wrong_daemon_pid', 'bool_daemon_pid', 'stale_receipt', 'future_receipt', 'bad_elapsed'}:
                # Preserve a valid MAC to test semantic rejection, not merely tampering.
                import json
                changes = {'unhealthy': {'health': 'unhealthy'},
                           'wrong_daemon_pid': {'daemon_pid': pins['host_pid'] + 1},
                           'bool_daemon_pid': {'daemon_pid': True},
                           'stale_receipt': {'started_at_ms': 1, 'finished_at_ms': 2},
                           'future_receipt': {'started_at_ms': receipt['finished_at_ms'] + 60000,
                                              'finished_at_ms': receipt['finished_at_ms'] + 60001},
                           'bad_elapsed': {'elapsed_ms': -1}}[fault]
                raw = json.dumps(dict(receipt, **changes), sort_keys=True, separators=(',', ':'))
                mac = ledger._operation_mac(health_id, command.command_id, 'gate_health', 'completed', raw)
                db.execute('UPDATE operations SET receipt=?,receipt_mac=? WHERE operation_id=?', (raw, mac, health_id))
    with pytest.raises((PermissionError, ValueError)):
        ledger.gate_completion_snapshot(pins, probe)
    assert ledger.get(command.command_id).state == 'running'


def test_signed_inspection_uses_real_worker_and_ledger(completed):
    ledger, command, pins, health_id, _ = completed
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    launch = verified_launch(key, public)
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    worker = GateLedgerWorker(channel=server, ledger=ledger, supervisor_public_key=public,
        launch_verifier=launch, manifest_digest=pins['manifest_digest'], hold_uncertain=lambda _: None, timeout_seconds=3)
    supervisor = SupervisorLedgerClient(channel=client, peer=peer, signing_key=key,
        launch_binding=launch.launch_binding(), manifest_digest=pins['manifest_digest'], timeout_seconds=3)
    result = []
    def serve():
        try: result.append(worker.serve_once())
        except BaseException as exc: result.append(exc)
    thread = threading.Thread(target=serve); thread.start()
    try:
        observed = supervisor.invoke('inspect_completion', {'identity': pins, 'probe_digest': PROBE})
        assert observed['operation_id'] == pins['operation_id'] and observed['health_operation_id'] == health_id
        assert observed['role'] == 'candidate'
        assert observed['health_daemon_pid'] == pins['host_pid']
        thread.join(4)
        assert not thread.is_alive() and result == ['sent_unconfirmed']
        assert ledger.get(command.command_id).state == 'running'
        assert ledger.finish(command.command_id, state='succeeded', reason=None, evidence=evidence()).state == 'held'
    finally:
        client.close(); server.close(); thread.join(4); peer.close(); launch.close()
        assert not thread.is_alive()


def test_acceptance_is_one_shot_and_lookup_survives_held(completed):
    from hub_container_executor.application_gate_enrollment import COMPLETION_HEALTH_CONTRACT
    ledger, command, pins, _, _ = completed
    snapshot = ledger.gate_completion_snapshot(pins, PROBE)
    args = dict(role='candidate', executable_digest='sha256:' + '3' * 64,
                health_contract=COMPLETION_HEALTH_CONTRACT, probe_digest=PROBE,
                health_receipt_digest=snapshot['health_receipt_digest'],
                decision_payload_digest=snapshot['decision_payload_digest'])
    assert ledger.lookup_gate_completion(pins) is None
    accepted = ledger.accept_gate_completion(pins, **args)
    assert ledger.lookup_gate_completion(pins) == accepted
    with pytest.raises(PermissionError):
        ledger.accept_gate_completion(pins, **args)
    assert ledger.finish(command.command_id, state='succeeded', reason=None, evidence=evidence()).state == 'held'
    assert ledger.lookup_gate_completion(pins) == accepted
    with ledger._transaction() as db:
        db.execute("UPDATE gate_acceptances SET record_mac='bad'")
    with pytest.raises(PermissionError):
        ledger.lookup_gate_completion(pins)


def test_acceptance_rejects_drift_and_stale_fence(completed):
    from hub_container_executor.application_gate_enrollment import COMPLETION_HEALTH_CONTRACT
    ledger, command, pins, _, _ = completed
    snapshot = ledger.gate_completion_snapshot(pins, PROBE)
    args = dict(role='candidate', executable_digest='sha256:' + '3' * 64,
                health_contract=COMPLETION_HEALTH_CONTRACT, probe_digest=PROBE,
                health_receipt_digest='sha256:' + '0' * 64,
                decision_payload_digest=snapshot['decision_payload_digest'])
    with pytest.raises(PermissionError):
        ledger.accept_gate_completion(pins, **args)
    args['health_receipt_digest'] = snapshot['health_receipt_digest']
    with ledger._transaction() as db:
        db.execute('UPDATE targets SET fence=fence+1')
    with pytest.raises(PermissionError):
        ledger.accept_gate_completion(pins, **args)
    with pytest.raises(PermissionError):
        ledger.lookup_gate_completion(pins)


def test_acceptance_concurrent_one_winner_and_reopen(completed, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from hub_container_executor.application_gate_enrollment import COMPLETION_HEALTH_CONTRACT
    ledger, command, pins, _, _ = completed
    snapshot = ledger.gate_completion_snapshot(pins, PROBE)
    args = dict(role='candidate', executable_digest='sha256:' + '3' * 64,
                health_contract=COMPLETION_HEALTH_CONTRACT, probe_digest=PROBE,
                health_receipt_digest=snapshot['health_receipt_digest'],
                decision_payload_digest=snapshot['decision_payload_digest'])
    def attempt():
        try:
            return ledger.accept_gate_completion(pins, **args)
        except PermissionError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(item is not None for item in results) == 1
    expected = next(item for item in results if item is not None)
    ledger.close()
    reopened = open_ledger(tmp_path)
    try:
        assert reopened.lookup_gate_completion(pins) == expected
        assert reopened.get(command.command_id).state == 'held'
    finally:
        reopened.close()


def test_fenced_acceptance_enables_finish_and_lost_reply_lookup(completed):
    from hub_container_executor.application_gate_enrollment import COMPLETION_HEALTH_CONTRACT
    from engineering_hub.container_pilot_client import ContainerPilotEvidence
    ledger, command, pins, _, _ = completed
    snapshot = ledger.gate_completion_snapshot(pins, PROBE)
    accepted = ledger.accept_gate_completion(pins, role='candidate', executable_digest='sha256:' + '3' * 64,
        health_contract=COMPLETION_HEALTH_CONTRACT, probe_digest=PROBE,
        health_receipt_digest=snapshot['health_receipt_digest'], decision_payload_digest=snapshot['decision_payload_digest'])
    assert ledger.wait_for_gate_acceptance(command.command_id, time.monotonic() + 1)
    ev = ContainerPilotEvidence(container_id=pins['container_id'], image_config_id=pins['image_config_id'],
                                health='unknown', evidence_verified=True)
    result = ledger.finish(command.command_id, state='succeeded', reason=None, evidence=ev,
                           generation_id=pins['generation_id'])
    assert result.state == 'succeeded'
    assert ledger.lookup_gate_completion(pins) == accepted


def test_wait_timeout_and_racing_held_are_conservative(completed):
    ledger, command, pins, _, _ = completed
    assert not ledger.wait_for_gate_acceptance(command.command_id, time.monotonic() + .05)
    assert ledger.finish(command.command_id, state='succeeded', reason=None, evidence=evidence()).state == 'held'
    with pytest.raises(PermissionError):
        ledger.gate_completion_snapshot(pins, PROBE)


@pytest.mark.parametrize('fault', ['stale_fence', 'bad_mac', 'wrong_container'])
def test_committed_acceptance_never_bypasses_changed_authority(completed, fault):
    from hub_container_executor.application_gate_enrollment import COMPLETION_HEALTH_CONTRACT
    from engineering_hub.container_pilot_client import ContainerPilotEvidence
    ledger, command, pins, _, _ = completed
    snapshot = ledger.gate_completion_snapshot(pins, PROBE)
    ledger.accept_gate_completion(pins, role='candidate', executable_digest='sha256:' + '3' * 64,
        health_contract=COMPLETION_HEALTH_CONTRACT, probe_digest=PROBE,
        health_receipt_digest=snapshot['health_receipt_digest'], decision_payload_digest=snapshot['decision_payload_digest'])
    if fault in {'stale_fence', 'bad_mac'}:
        with ledger._transaction() as db:
            if fault == 'stale_fence': db.execute('UPDATE targets SET fence=fence+1')
            else: db.execute("UPDATE gate_acceptances SET record_mac='bad'")
    ev = ContainerPilotEvidence(container_id=('f' * 64 if fault == 'wrong_container' else pins['container_id']),
        image_config_id=pins['image_config_id'], health='unknown', evidence_verified=True)
    result = ledger.finish(command.command_id, state='succeeded', reason=None, evidence=ev,
                           generation_id=pins['generation_id'])
    assert result.state == 'held'


def test_lost_reply_hold_after_commit_cannot_revoke_acceptance(completed):
    from hub_container_executor.application_gate_enrollment import COMPLETION_HEALTH_CONTRACT
    from engineering_hub.container_pilot_client import ContainerPilotEvidence
    ledger, command, pins, _, _ = completed
    snapshot = ledger.gate_completion_snapshot(pins, PROBE)
    accepted = ledger.accept_gate_completion(pins, role='candidate', executable_digest='sha256:' + '3' * 64,
        health_contract=COMPLETION_HEALTH_CONTRACT, probe_digest=PROBE,
        health_receipt_digest=snapshot['health_receipt_digest'], decision_payload_digest=snapshot['decision_payload_digest'])
    ledger.hold_gate_uncertain(pins)  # Response was lost after commit.
    ev = ContainerPilotEvidence(container_id=pins['container_id'], image_config_id=pins['image_config_id'],
                                health='unknown', evidence_verified=True)
    assert ledger.finish(command.command_id, state='succeeded', reason=None, evidence=ev,
                         generation_id=pins['generation_id']).state == 'succeeded'
    assert ledger.lookup_gate_completion(pins) == accepted


@pytest.mark.parametrize('deadline', [float('inf'), float('-inf'), float('nan'), True])
def test_wait_rejects_invalid_deadlines(completed, deadline):
    ledger, command, _, _, _ = completed
    with pytest.raises(ValueError):
        ledger.wait_for_gate_acceptance(command.command_id, deadline)


def test_health_readiness_requires_bound_consumed_gate_and_authenticated_health(completed):
    ledger, _, pins, health_id, receipt = completed
    assert ledger.gate_health_ready(pins, PROBE) == {'ready': True}
    with ledger._transaction() as db:
        db.execute('DELETE FROM operations WHERE operation_id=?', (health_id,))
    assert ledger.gate_health_ready(pins, PROBE) == {'ready': False}
    with ledger._transaction() as db:
        db.execute("UPDATE mount_gate_challenges SET decision_mac='bad'")
    with pytest.raises(PermissionError): ledger.gate_health_ready(pins, PROBE)


def test_health_readiness_rejects_invalid_health_and_stale_fence(completed):
    ledger, _, pins, health_id, _ = completed
    with ledger._transaction() as db:
        db.execute("UPDATE operations SET receipt_mac='bad' WHERE operation_id=?", (health_id,))
    with pytest.raises(PermissionError): ledger.gate_health_ready(pins, PROBE)
    with ledger._transaction() as db: db.execute('UPDATE targets SET fence=fence+1')
    with pytest.raises(PermissionError): ledger.gate_health_ready(pins, PROBE)


def test_health_readiness_intent_is_pending(completed):
    ledger, _, pins, health_id, _ = completed
    with ledger._transaction() as db:
        db.execute("UPDATE operations SET status='intent',receipt=NULL,receipt_mac=NULL WHERE operation_id=?", (health_id,))
    assert ledger.gate_health_ready(pins, PROBE) == {'ready': False}
