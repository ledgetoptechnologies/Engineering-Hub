"""Real ledger and Unix seqpacket; supervisor and peer authority are synthetic."""
import json
import os
import select
import signal
import socket
import threading
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import hub_container_executor.ledger as ledger_module
from hub_container_executor.gate_ledger_channel import (DOMAIN, REQUEST, RESPONSE,
    GateLedgerChannelError, GateLedgerWorker, SupervisorLedgerClient, OwnedGateLedgerService)
from hub_container_executor.broker_seqpacket import PeerPin, _start
from hub_container_executor.runtime_identity import ExecutorReleaseEnrollment, LaunchReceiptIdentityVerifier
from engineering_hub.container_pilot_manifest import Platform
from tests.test_hub_container_executor_ledger import gate_fixture, open_ledger
from tests.test_hub_container_executor_runtime_identity import Issuer


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def verified_launch(key, public):
    enrollment = ExecutorReleaseEnrollment('test-issuer', public, 'c' * 40,
        'sha256:' + '1' * 64, 'sha256:' + '2' * 64, 'sha256:' + '3' * 64,
        Platform(os='linux', architecture='amd64', variant=None), 'sha256:' + '4' * 64)
    clock = SimpleNamespace(wall=time.time(), mono=time.monotonic())
    return LaunchReceiptIdentityVerifier(enrollment=enrollment, issuer_transport=Issuer(enrollment, key, clock),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    _, decision, pins = gate_fixture(ledger, require_gate=True)
    now = int(time.time() * 1000)
    decision.update(issued_at_ms=now, expires_at_ms=now + 4000)
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    launch = verified_launch(key, pub)
    try: yield ledger, decision, pins, key, pub, launch
    finally: launch.close(); ledger.close()


def exchange(context, *, action='reserve', value=None, signer=None, mutate=None,
             close_after_send=False, hold=None, timeout=2, ttl_ms=1000):
    ledger, decision, pins, key, public, launch = context
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    client.settimeout(3)
    worker = GateLedgerWorker(channel=server, ledger=ledger, supervisor_public_key=public,
        launch_verifier=launch, manifest_digest=pins['manifest_digest'],
        hold_uncertain=hold or (lambda _: None), timeout_seconds=timeout)
    results = []
    def serve():
        try: results.append(worker.serve_once())
        except Exception as exc: results.append(exc)
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        challenge = json.loads(client.recv(8192))['challenge']
        now = int(time.time() * 1000)
        payload = dict(protocol=REQUEST, challenge=challenge, launch_binding=list(launch.launch_binding()),
            manifest_digest=pins['manifest_digest'], action=action,
            value=(pins if action == 'reserve' else decision) if value is None else value,
            issued_at_ms=now, expires_at_ms=now + ttl_ms)
        if mutate: mutate(payload)
        signed = (signer or key).sign(DOMAIN + canonical(payload)).hex()
        client.send(canonical(dict(payload=payload, signature=signed)))
        if close_after_send:
            client.close(); response = None
        else:
            thread.join(3)
            assert not thread.is_alive(), 'worker did not finish'
            if results and results[0] == 'sent_unconfirmed':
                response = json.loads(client.recv(8192))
            else:
                response = None
        thread.join(3)
        assert not thread.is_alive(), 'worker did not drain'
        return challenge, response, results[0]
    finally:
        client.close(); server.close()
        thread.join(3)
        assert not thread.is_alive(), 'worker thread left active'


def state(ledger):
    with ledger._connection() as db:
        return [row[0] for row in db.execute('SELECT state FROM mount_gate_challenges')]


def test_real_ledger_reservation_and_consumption(context):
    ledger, _, _, _, _, _ = context
    for action, expected in [('reserve', 'reserved'), ('consume', 'consumed')]:
        challenge, response, result = exchange(context, action=action)
        assert result == 'sent_unconfirmed'
        assert response == dict(protocol=RESPONSE, challenge=challenge, action=action, outcome='committed')
        assert state(ledger) == [expected]


@pytest.mark.parametrize('fault', ['wrong_key', 'expired', 'replayed_challenge', 'wrong_manifest', 'bool_fence'])
def test_invalid_request_denied_before_ledger_effect(context, fault):
    signer = Ed25519PrivateKey.generate() if fault == 'wrong_key' else None
    def mutate(payload):
        if fault == 'expired': payload.update(issued_at_ms=1, expires_at_ms=2)
        elif fault == 'replayed_challenge': payload['challenge'] = '0' * 64
        elif fault == 'wrong_manifest': payload['manifest_digest'] = 'sha256:' + '0' * 64
        elif fault == 'bool_fence': payload['value']['fence'] = True
    _, response, result = exchange(context, signer=signer, mutate=mutate)
    assert response is None and isinstance(result, GateLedgerChannelError)
    assert state(context[0]) == []


def test_changed_launch_binding_is_denied_before_ledger_effect(context):
    launch = context[5]
    def change(_):
        launch.close()
    _, response, result = exchange(context, mutate=change)
    assert response is None and isinstance(result, GateLedgerChannelError)
    assert state(context[0]) == []


def test_paused_owner_is_denied_by_real_ledger(context):
    context[0].set_paused(True)
    _, response, result = exchange(context)
    assert response is None and isinstance(result, GateLedgerChannelError)
    assert state(context[0]) == []


def test_expired_inner_decision_rejected_before_consumption(context):
    ledger, decision, pins, _, _, _ = context
    ledger.reserve_mount_gate_challenge(pins)
    now = int(time.time() * 1000)
    decision.update(issued_at_ms=now - 2000, expires_at_ms=now - 1000)
    _, response, result = exchange(context, action='consume', timeout=2, ttl_ms=1500)
    assert response is None and isinstance(result, GateLedgerChannelError)
    assert state(ledger) == ['reserved']


def test_inner_decision_expires_during_commit_is_consumed_and_held(context, monkeypatch):
    ledger, decision, pins, _, _, _ = context
    ledger.reserve_mount_gate_challenge(pins)
    now = int(time.time() * 1000)
    decision.update(issued_at_ms=now - 100, expires_at_ms=now + 400)
    actual_consume = ledger.consume_mount_gate_challenge
    def commit_then_wait(value):
        result = actual_consume(value)
        deadline = time.monotonic() + 2
        while int(time.time() * 1000) <= decision['expires_at_ms']:
            assert time.monotonic() < deadline, 'wall clock did not advance'
            time.sleep(.01)
        return result
    monkeypatch.setattr(ledger, 'consume_mount_gate_challenge', commit_then_wait)
    held = []
    _, response, result = exchange(context, action='consume', timeout=3, ttl_ms=2500,
                                   hold=lambda value: held.append(value))
    assert response is None and isinstance(result, GateLedgerChannelError)
    assert state(ledger) == ['consumed'] and held


def test_lost_response_after_commit_is_held_and_not_reissued(context):
    held = []
    _, response, result = exchange(context, close_after_send=True, hold=lambda value: held.append(value))
    assert response is None and isinstance(result, GateLedgerChannelError)
    assert held and state(context[0]) == ['reserved']
    _, response, result = exchange(context)
    assert response is None and isinstance(result, GateLedgerChannelError)
    assert state(context[0]) == ['reserved']


def test_peer_loss_does_not_abandon_inflight_transaction(context, monkeypatch):
    ledger, _, pins, key, public, launch = context
    entered, release = threading.Event(), threading.Event()
    actual_reserve = ledger.reserve_mount_gate_challenge
    def delayed(value):
        entered.set()
        assert release.wait(2), 'test did not release transaction'
        return actual_reserve(value)
    monkeypatch.setattr(ledger, 'reserve_mount_gate_challenge', delayed)
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    client.settimeout(2)
    held, results = [], []
    worker = GateLedgerWorker(channel=server, ledger=ledger, supervisor_public_key=public,
        launch_verifier=launch, manifest_digest=pins['manifest_digest'],
        hold_uncertain=lambda value: held.append(value), timeout_seconds=2)
    def serve():
        try: results.append(worker.serve_once())
        except Exception as exc: results.append(exc)
    thread = threading.Thread(target=serve); thread.start()
    try:
        challenge = json.loads(client.recv(8192))['challenge']; now = int(time.time() * 1000)
        payload = dict(protocol=REQUEST, challenge=challenge, launch_binding=list(launch.launch_binding()),
                       manifest_digest=pins['manifest_digest'], action='reserve', value=pins,
                       issued_at_ms=now, expires_at_ms=now + 1000)
        client.send(canonical(dict(payload=payload, signature=key.sign(DOMAIN + canonical(payload)).hex())))
        assert entered.wait(2), 'ledger transaction was not entered'
        client.close()
        assert thread.is_alive(), 'inflight transaction was abandoned'
        release.set(); thread.join(3)
        assert not thread.is_alive() and isinstance(results[0], GateLedgerChannelError)
        assert state(ledger) == ['reserved'] and held
    finally:
        release.set(); client.close(); server.close(); thread.join(3)
        assert not thread.is_alive(), 'worker thread left active'


@pytest.mark.parametrize('wrong_peer', [False, True])
def test_supervisor_client_checks_executor_peer_with_real_ledger(context, wrong_peer):
    ledger, _, pins, key, public, launch = context
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    for channel in (client, server): channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid() + int(wrong_peer), gid=os.getgid(),
                   start_ticks=_start(os.getpid()))
    worker = GateLedgerWorker(channel=server, ledger=ledger, supervisor_public_key=public,
        launch_verifier=launch, manifest_digest=pins['manifest_digest'],
        hold_uncertain=lambda _: None, timeout_seconds=2)
    supervisor = SupervisorLedgerClient(channel=client, peer=peer, signing_key=key,
        launch_binding=launch.launch_binding(), manifest_digest=pins['manifest_digest'], timeout_seconds=2)
    results = []
    def serve():
        try: results.append(worker.serve_once())
        except Exception as exc: results.append(exc)
    thread = threading.Thread(target=serve); thread.start()
    try:
        if wrong_peer:
            with pytest.raises(GateLedgerChannelError): supervisor.invoke('reserve', pins)
            client.close()
        else:
            assert supervisor.invoke('reserve', pins) is True
        thread.join(3)
        assert not thread.is_alive()
        assert state(ledger) == ([] if wrong_peer else ['reserved'])
        assert isinstance(results[0], GateLedgerChannelError) if wrong_peer else results[0] == 'sent_unconfirmed'
    finally:
        client.close(); server.close(); thread.join(3); peer.close()
        assert not thread.is_alive(), 'worker thread left active'


def listener_and_service(context, *, on_failure, hold=lambda _: None):
    ledger, _, pins, _, public, launch = context
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    address = b'\0hub-gate-ledger-test-' + os.urandom(16).hex().encode()
    try:
        listener.bind(address); listener.listen(4)
        service = OwnedGateLedgerService(listener=listener, ledger=ledger, supervisor_public_key=public,
            launch_verifier=launch, manifest_digest=pins['manifest_digest'],
            hold_uncertain=hold, timeout_seconds=2)
        service.start(on_failure=on_failure)
        return address, service
    except BaseException:
        listener.close()
        raise


@pytest.mark.parametrize('fault', ['wrong_key', 'closed', 'expired', 'duck_verifier'])
def test_owned_service_requires_live_exact_verifier_and_its_enrolled_key(context, fault):
    ledger, _, pins, _, public, launch = context
    if fault == 'wrong_key':
        public = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw,
                                                                        serialization.PublicFormat.Raw)
    elif fault == 'closed': launch.close()
    elif fault == 'expired': launch._deadline = 0
    elif fault == 'duck_verifier':
        launch = SimpleNamespace(launch_binding=lambda: context[5].launch_binding(), issuer_public_key=lambda: public)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
        listener.bind(b'\0hub-gate-identity-deny-' + os.urandom(16).hex().encode()); listener.listen(1)
        with pytest.raises(GateLedgerChannelError):
            OwnedGateLedgerService(listener=listener, ledger=ledger, supervisor_public_key=public,
                launch_verifier=launch, manifest_digest=pins['manifest_digest'],
                hold_uncertain=lambda _: None, timeout_seconds=2)
    assert state(ledger) == []


def client_for_service(context, address):
    _, _, pins, key, _, launch = context
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    channel.settimeout(2); channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    try:
        channel.connect(address)
        peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
        client = SupervisorLedgerClient(channel=channel, peer=peer, signing_key=key,
            launch_binding=launch.launch_binding(), manifest_digest=pins['manifest_digest'], timeout_seconds=2)
        return client, channel, peer
    except BaseException:
        channel.close()
        raise


def test_owned_service_real_listener_and_close_refuses_new_clients(context):
    failed = []
    address, service = listener_and_service(context, on_failure=lambda: failed.append(True))
    try:
        assert service.ledger is context[0]
        client, channel, peer = client_for_service(context, address)
        try: assert client.invoke('reserve', context[2]) is True
        finally: channel.close(); peer.close()
        service.close()
        assert not failed and state(context[0]) == ['reserved']
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as late:
            late.settimeout(1)
            with pytest.raises(OSError): late.connect(address)
    finally:
        service.close()


def test_owned_service_close_drains_inflight_transaction(context, monkeypatch):
    ledger = context[0]; entered, release = threading.Event(), threading.Event()
    actual_reserve = ledger.reserve_mount_gate_challenge
    def delayed(value):
        entered.set()
        assert release.wait(2), 'test did not release transaction'
        return actual_reserve(value)
    monkeypatch.setattr(ledger, 'reserve_mount_gate_challenge', delayed)
    failures, client_results, close_results = [], [], []
    address, service = listener_and_service(context, on_failure=lambda: failures.append(True))
    client, channel, peer = client_for_service(context, address)
    def invoke():
        try: client_results.append(client.invoke('reserve', context[2]))
        except Exception as exc: client_results.append(exc)
    def close():
        try: service.close(); close_results.append(True)
        except Exception as exc: close_results.append(exc)
    caller = threading.Thread(target=invoke); closer = threading.Thread(target=close)
    caller.start()
    try:
        assert entered.wait(2), 'transaction was not entered'
        closer.start()
        closer.join(.1)
        assert closer.is_alive(), 'close returned before accepted work drained'
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as late:
            late.settimeout(.1)
            late.connect(address)
            with pytest.raises(TimeoutError): late.recv(8192)
        release.set(); caller.join(3); closer.join(3)
        assert not caller.is_alive() and not closer.is_alive()
        assert client_results == [True] and close_results == [True]
        assert state(ledger) == ['reserved'] and not failures
    finally:
        release.set(); channel.close(); caller.join(3)
        if closer.ident is not None: closer.join(3)
        peer.close(); service.close()
        assert not caller.is_alive() and not closer.is_alive()


def test_owned_service_fatal_request_signals_failure(context):
    failed = threading.Event()
    address, service = listener_and_service(context, on_failure=failed.set)
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        channel.settimeout(2); channel.connect(address)
        assert json.loads(channel.recv(8192))['challenge']
        channel.send(b'{}')
        assert failed.wait(3), 'service did not report failed worker'
        assert state(context[0]) == []
        with pytest.raises(GateLedgerChannelError): service.close()
    finally:
        channel.close()
        try: service.close()
        except GateLedgerChannelError: pass


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='requires fork')
def test_fork_child_rejects_start_before_inherited_locked_mutex(context):
    ledger, _, pins, _, public, launch = context
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(b'\0hub-gate-fork-test-' + os.urandom(16).hex().encode()); listener.listen(1)
    service = OwnedGateLedgerService(listener=listener, ledger=ledger, supervisor_public_key=public,
        launch_verifier=launch, manifest_digest=pins['manifest_digest'],
        hold_uncertain=lambda _: None, timeout_seconds=1)
    read_fd, write_fd = os.pipe()
    child = None
    service._lock.acquire()
    try:
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            try:
                service.start(on_failure=lambda: None)
            except GateLedgerChannelError:
                os.write(write_fd, b'R')
                os._exit(0)
            except BaseException:
                os.write(write_fd, b'E')
                os._exit(1)
            os.write(write_fd, b'A')
            os._exit(1)
        os.close(write_fd); write_fd = None
        ready, _, _ = select.select([read_fd], [], [], 2)
        assert ready and os.read(read_fd, 1) == b'R', 'forked child blocked or incorrectly started service'
        deadline = time.monotonic() + 2
        while True:
            waited, status = os.waitpid(child, os.WNOHANG)
            if waited:
                child = None
                break
            assert time.monotonic() < deadline, 'forked child did not exit'
            time.sleep(.01)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        if child is not None:
            try: os.kill(child, signal.SIGKILL)
            except ProcessLookupError: pass
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try: waited, _ = os.waitpid(child, os.WNOHANG)
                except ChildProcessError: waited = child
                if waited:
                    child = None
                    break
                time.sleep(.01)
        service._lock.release()
        os.close(read_fd)
        if write_fd is not None: os.close(write_fd)
        service.close()
        assert child is None, 'forked child did not exit after SIGKILL'
