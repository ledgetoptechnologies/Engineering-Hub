"""Permanent admission seal; it does not certify executor quiescence."""
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
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


def test_sealed_outcome_reports_pending_and_bad_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    binding = (str(uuid4()), str(uuid4()), 123, 456, 'a'*64)
    pair, command = str(uuid4()), request()
    try:
        assert ledger.admit(command).accepted
        operation = ledger.begin_operation(command.command_id, 'state_copy')
        ledger.complete_operation(operation, {'generation_id': 'synthetic'})
        ledger.seal_pair_drain(pair, binding, fresh_check=lambda: True)
        with ledger._transaction() as db:
            db.execute("UPDATE operations SET receipt_mac='invalid' WHERE operation_id=?", (operation,))
        result = ledger.pair_drain_outcome(pair, binding, fresh_check=lambda: True)
        assert result['commands']['running'] == 1
        assert result['operations']['completed'] == 1 and result['invalid_receipts'] == 1
        with pytest.raises(PermissionError):
            ledger.pair_drain_outcome(str(uuid4()), binding, fresh_check=lambda: True)
    finally:
        ledger.close()


@pytest.mark.parametrize('sealed', [False, True])
def test_supervisor_stop_checks_seal_then_signals_then_observes(monkeypatch, sealed):
    from hub_container_executor import launch_supervisor as launch
    events, pair = [], str(uuid4())
    observed = {'commands': {}, 'runs': {}, 'targets': {}, 'operations': {}, 'invalid_receipts': 0}
    class Client:
        def invoke(self, action, value):
            assert action == 'pair_outcome_v1' and value == {'pair_id': pair}
            events.append('query')
            if not sealed: raise ValueError('missing seal')
            return None if events.count('query') == 1 else observed
    @contextmanager
    def client(**_):
        yield Client()
    fake = SimpleNamespace(_pid=os.getpid(), _mutex=threading.Lock(),
        profile=SimpleNamespace(gate_ledger_channel='supervisor-listener-v1'),
        _launch_id='launch', _container_id='a'*64, _consumer=SimpleNamespace(fd=123),
        _guard=lambda: None, _validate_live=lambda *_: None, _record=lambda: {},
        _journal=SimpleNamespace(state=lambda *args: events.append(args[1])),
        _engine=SimpleNamespace(json=lambda *args, **kwargs: events.append('signal')),
        _wait_stopped=lambda *args, **kwargs: events.append('wait'),
        _remove_owned_socket=lambda: events.append('cleanup'),
        _hold=lambda: events.append('held'), open_gate_ledger_client=client)
    monkeypatch.setattr(launch.select, 'select', lambda *args: ([123], [], []))
    if sealed:
        assert launch.LaunchSupervisor.graceful_stop_sealed_executor(fake, pair_id=pair, manifest_digest=MANIFEST, timeout_ms=2000) == observed
        assert events == ['query', 'term_reserved', 'signal', 'query', 'wait', 'stopped', 'cleanup']
    else:
        with pytest.raises(launch.LaunchSupervisorError):
            launch.LaunchSupervisor.graceful_stop_sealed_executor(fake, pair_id=pair, manifest_digest=MANIFEST, timeout_ms=2000)
        assert events == ['query', 'held']


@pytest.mark.parametrize('scenario', ['valid', 'tamper', 'lost_ack', 'wrong_sender'])
def test_post_drain_signed_outcome_requires_original_seal_and_ack(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    verifier = verified_launch(key, public)
    pair = str(uuid4()); ledger.seal_pair_drain(pair, verifier.launch_binding(), fresh_check=lambda: True)
    if scenario == 'tamper':
        with ledger._transaction() as db:
            db.execute("UPDATE control SET value='corrupt' WHERE key='pair_drain_seal_v1'")
    policy = GateCompletionPolicy(candidate_executable_digest='sha256:'+'a'*64,
        rollback_executable_digest='sha256:'+'b'*64, health_contract='engineering-hub.health-json-build-commit.v1')
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    for endpoint in (client, server): endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    worker = GateLedgerWorker(channel=server, ledger=ledger, supervisor_public_key=public,
        launch_verifier=verifier, manifest_digest=MANIFEST, hold_uncertain=lambda _: None,
        timeout_seconds=3, completion_policy=policy, require_sender_liveness=True, post_drain_only=True)
    supervisor = SupervisorLedgerClient(channel=client, peer=peer, signing_key=key,
        launch_binding=verifier.launch_binding(), manifest_digest=MANIFEST, timeout_seconds=3)
    results = []
    def serve():
        try: results.append(worker.serve_once())
        except Exception as error: results.append(error)
    thread = threading.Thread(target=serve); thread.start()
    try:
        if scenario == 'tamper':
            with pytest.raises(GateLedgerChannelError): supervisor.invoke('pair_outcome_v1', {'pair_id': pair})
        else:
            outcome = supervisor.invoke('pair_outcome_v1', {'pair_id': pair})
            assert set(outcome) == {'commands', 'runs', 'targets', 'operations', 'invalid_receipts'}
            assert outcome['invalid_receipts'] == 0
            assert thread.is_alive()  # no success until supervisor validates and acknowledges
            if scenario == 'valid': supervisor.confirm_pair_outcome()
            elif scenario == 'lost_ack': client.close()
            else:
                from hub_container_executor.mount_gate_protocol import _canonical
                challenge, _ = supervisor._outcome_ack
                ack = _canonical({'protocol': 'engineering-hub.pair-outcome-ack.v1', 'challenge': challenge})
                result = subprocess.run([sys.executable, '-I', '-B', '-c',
                    'import socket,sys; socket.socket(fileno=int(sys.argv[1])).send(bytes.fromhex(sys.argv[2]))',
                    str(client.fileno()), ack.hex()], pass_fds=(client.fileno(),), timeout=3,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                assert result.returncode == 0
        thread.join(4); assert not thread.is_alive()
        assert results == ['sent_unconfirmed'] if scenario == 'valid' else isinstance(results[0], GateLedgerChannelError)
    finally:
        client.close(); server.close(); peer.close(); thread.join(4)
        verifier.close(); ledger.close()


@pytest.mark.parametrize('scenario', ['valid', 'wrong_key', 'expired_in_transaction', 'pair_only', 'cross_domain'])
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
        timeout_seconds=3, completion_policy=None if scenario in ('pair_only', 'cross_domain') else policy,
        require_sender_liveness=True, pair_only=scenario in ('pair_only', 'cross_domain'))
    supervisor = SupervisorLedgerClient(channel=client, peer=peer,
        signing_key=Ed25519PrivateKey.generate() if scenario == 'wrong_key' else key,
        launch_binding=verifier.launch_binding(), manifest_digest=MANIFEST,
        timeout_seconds=1 if scenario == 'expired_in_transaction' else 3, pair_only=scenario == 'pair_only')
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
        if scenario not in ('valid', 'pair_only'):
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
