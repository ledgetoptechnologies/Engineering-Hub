"""Fenced state intent observations cross the authenticated supervisor channel."""
import os
import socket
import threading

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from engineering_hub.executor_launch_profile import GateCompletionPolicy
from hub_container_executor.broker_seqpacket import PeerPin, _start
from hub_container_executor.gate_ledger_channel import GateLedgerChannelError, GateLedgerWorker, SupervisorLedgerClient
from tests.test_hub_container_executor_ledger import MANIFEST, open_ledger, request
from tests.test_hub_gate_ledger_channel import verified_launch
import hub_container_executor.ledger as ledger_module


def test_signed_pending_state_read_is_fenced_and_not_a_grant(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    command = request()
    ledger.admit(command)
    operation = ledger.begin_operation(command.command_id, 'state_copy')
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    verifier = verified_launch(key, public)
    policy = GateCompletionPolicy(candidate_executable_digest='sha256:' + 'a' * 64,
        rollback_executable_digest='sha256:' + 'b' * 64,
        health_contract='engineering-hub.health-json-build-commit.v1')

    def exchange():
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        for endpoint in (client, server): endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
        worker = GateLedgerWorker(channel=server, ledger=ledger, supervisor_public_key=public,
            launch_verifier=verifier, manifest_digest=MANIFEST, hold_uncertain=ledger.hold_gate_uncertain,
            timeout_seconds=3, completion_policy=policy, require_sender_liveness=True)
        supervisor = SupervisorLedgerClient(channel=client, peer=peer, signing_key=key,
            launch_binding=verifier.launch_binding(), manifest_digest=MANIFEST, timeout_seconds=3)
        outcomes = []
        def serve():
            try: outcomes.append(worker.serve_once())
            except Exception as error: outcomes.append(error)
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            try: return supervisor.invoke('pending_state_v1', {'operation_id': operation})
            finally:
                thread.join(4)
                assert not thread.is_alive()
        finally:
            client.close(); server.close(); peer.close()
            thread.join(4)
            assert not thread.is_alive()

    try:
        result = exchange()
        assert result == {'operation_id': operation, 'command_id': command.command_id,
            'run_id': command.run_id, 'target_id': command.target_id,
            'fencing_generation': command.fencing_generation, 'manifest_digest': MANIFEST, 'role': 'candidate'}
        with ledger._transaction() as db:
            db.execute('UPDATE targets SET fence=fence+1')
        with pytest.raises(GateLedgerChannelError): exchange()
    finally:
        verifier.close(); ledger.close()
