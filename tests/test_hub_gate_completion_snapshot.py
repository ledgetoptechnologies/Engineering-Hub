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
