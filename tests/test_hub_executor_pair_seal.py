"""Permanent admission seal; it does not certify executor quiescence."""
import os
import socket
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from engineering_hub.executor_launch_profile import GateCompletionPolicy
from hub_container_executor.broker_seqpacket import PeerPin, _start
from hub_container_executor.gate_ledger_channel import GateLedgerChannelError, GateLedgerWorker, SupervisorLedgerClient
import hub_container_executor.ledger as ledger_module
from tests.test_hub_container_executor_ledger import MANIFEST, open_ledger, request
from tests.test_hub_gate_ledger_channel import verified_launch


def test_pair_seal_is_one_way_and_does_not_cancel_accepted_command(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    command = request(); assert ledger.admit(command).accepted
    pair = str(uuid4()); binding = (str(uuid4()), str(uuid4()), 123, 456, 'a'*64)
    try:
        ledger.seal_pair_drain(pair, binding, fresh_check=lambda: True)
        assert ledger.get(command.command_id).state == 'running'
        # Already accepted work may finish its existing lifecycle; the seal
        # is a new-command veto, never a claim of completed effects.
        assert ledger.begin_operation(command.command_id, 'state_copy')
        denied = ledger.admit(request())
        assert not denied.accepted and denied.response.state == 'held' and denied.response.reason == 'paused'
        ledger.set_paused(False)
        assert not ledger.admit(request()).accepted
        with pytest.raises(PermissionError): ledger.seal_pair_drain(pair, binding, fresh_check=lambda: True)
        with ledger._transaction() as db:
            db.execute("UPDATE control SET value='corrupt' WHERE key='pair_drain_seal_v1'")
        assert not ledger.admit(request()).accepted
    finally:
        ledger.close()
    reopened = open_ledger(tmp_path)
    try: assert not reopened.admit(request()).accepted
    finally: reopened.close()


def test_expired_authority_inside_transaction_does_not_seal(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    pair = str(uuid4()); binding = (str(uuid4()), str(uuid4()), 123, 456, 'a'*64)
    seen = []
    try:
        def expired():
            seen.append(True)
            return False
        with pytest.raises(PermissionError): ledger.seal_pair_drain(pair, binding, fresh_check=expired)
        assert seen == [True] and ledger.admit(request()).accepted
    finally:
        ledger.close()


@pytest.mark.parametrize('scenario', ['valid', 'wrong_key', 'expired_in_transaction'])
def test_signed_live_supervisor_channel_commits_one_seal(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    verifier = verified_launch(key, public)
    policy = GateCompletionPolicy(candidate_executable_digest='sha256:'+'a'*64,
        rollback_executable_digest='sha256:'+'b'*64, health_contract='engineering-hub.health-json-build-commit.v1')
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    for endpoint in (client, server): endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    worker = GateLedgerWorker(channel=server, ledger=ledger, supervisor_public_key=public,
        launch_verifier=verifier, manifest_digest=MANIFEST, hold_uncertain=lambda _: None,
        timeout_seconds=3, completion_policy=policy, require_sender_liveness=True)
    supervisor = SupervisorLedgerClient(channel=client, peer=peer,
        signing_key=Ed25519PrivateKey.generate() if scenario == 'wrong_key' else key,
        launch_binding=verifier.launch_binding(), manifest_digest=MANIFEST,
        timeout_seconds=1 if scenario == 'expired_in_transaction' else 3)
    original_transaction = ledger._transaction
    if scenario == 'expired_in_transaction':
        @contextmanager
        def delayed_transaction():
            with original_transaction() as db:
                time.sleep(1.3)  # Authority expires while the write transaction is held.
                yield db
        monkeypatch.setattr(ledger, '_transaction', delayed_transaction)
    results = []
    def serve():
        try: results.append(worker.serve_once())
        except Exception as error: results.append(error)
    thread = threading.Thread(target=serve); thread.start()
    try:
        if scenario != 'valid':
            with pytest.raises(GateLedgerChannelError): supervisor.invoke('seal_pair_drain_v1', {'pair_id':str(uuid4())})
            thread.join(4); assert not thread.is_alive() and isinstance(results[0], GateLedgerChannelError)
            monkeypatch.setattr(ledger, '_transaction', original_transaction)
            assert ledger.admit(request()).accepted
        else:
            assert supervisor.invoke('seal_pair_drain_v1', {'pair_id':str(uuid4())}) is True
            thread.join(4); assert not thread.is_alive() and results == ['sent_unconfirmed']
            assert not ledger.admit(request()).accepted
    finally:
        client.close(); server.close(); peer.close()
        thread.join(4); assert not thread.is_alive()
        verifier.close(); ledger.close()
