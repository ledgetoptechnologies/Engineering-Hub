"""Synthetic supervisor authority, real seqpacket, pidfd and SQLite ledger."""
import os
import array
import hashlib
import json
import socket
import threading
import time
from types import SimpleNamespace
import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from engineering_hub.executor_launch_profile import GateCompletionPolicy, ExecutorLaunchProfile
from hub_container_executor.broker_seqpacket import PeerPin, _start
from hub_container_executor.gate_ledger_channel import GateLedgerChannelError, GateLedgerWorker, SupervisorLedgerClient, OwnedGateLedgerService, DOMAIN, REQUEST
from tests.test_hub_gate_completion_snapshot import completed, PROBE
from tests.test_hub_gate_ledger_channel import verified_launch, canonical
from tests.test_hub_container_executor_ledger import evidence
from tests.test_hub_gate_ledger_handoff_supervisor import rig
from tests.test_hub_container_executor_launch_supervisor import short_root
from tests.test_hub_application_gate_enrollment import vector
from tests.test_hub_container_executor_runtime_identity import Issuer
from tests.test_executor_launch_profile import gate_profile
from hub_container_executor.runtime_identity import ExecutorReleaseEnrollment, LaunchReceiptIdentityVerifier
from hub_container_executor.runtime_profile import probe_set_digest
from engineering_hub.container_pilot_manifest import Platform
from hub_container_executor import launch_supervisor as launch


def test_signed_acceptance_and_authenticated_read_leave_finish_held(completed):
    ledger, command, pins, _, _ = completed
    snapshot = ledger.gate_completion_snapshot(pins, PROBE)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    verifier = verified_launch(key, public)
    policy = GateCompletionPolicy(candidate_executable_digest='sha256:' + 'a' * 64,
        rollback_executable_digest='sha256:' + 'b' * 64,
        health_contract='engineering-hub.health-json-build-commit.v1')
    value = {'identity': pins, 'role': 'candidate', 'executable_digest': policy.candidate_executable_digest,
             'health_contract': policy.health_contract, 'probe_digest': PROBE,
             'health_receipt_digest': snapshot['health_receipt_digest'],
             'decision_payload_digest': snapshot['decision_payload_digest']}
    def exchange(action, body):
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        for endpoint in (client, server): endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
        worker = GateLedgerWorker(channel=server, ledger=ledger, supervisor_public_key=public,
            launch_verifier=verifier, manifest_digest=pins['manifest_digest'],
            hold_uncertain=ledger.hold_gate_uncertain, timeout_seconds=3,
            completion_policy=policy, require_sender_liveness=True)
        supervisor = SupervisorLedgerClient(channel=client, peer=peer, signing_key=key,
            launch_binding=verifier.launch_binding(), manifest_digest=pins['manifest_digest'], timeout_seconds=3)
        results = []
        def serve():
            try: results.append(worker.serve_once())
            except BaseException as error: results.append(error)
        thread = threading.Thread(target=serve); thread.start()
        try:
            result = supervisor.invoke(action, body)
            thread.join(4)
            assert not thread.is_alive() and results == ['sent_unconfirmed']
            return result
        finally:
            client.close(); server.close(); thread.join(4); peer.close()
            assert not thread.is_alive()
    try:
        pending = exchange('pending_gate_v5', {'role': 'candidate'})
        assert pending['operation_id'] == pins['operation_id'] and pending['run_id'] == pins['run_id']
        assert pending['fence'] == pins['fence'] and pending['container_id'] == pins['container_id']
        assert pending['generation_id'] == pins['generation_id'] and pending['role'] == 'candidate'
        assert exchange('health_ready_v5', {'identity': pins, 'probe_digest': PROBE}) == {'ready': True}
        assert exchange('accept_completion_v5', value) == value
        assert exchange('lookup_completion_v5', {'identity': pins}) == value
        result = ledger.finish(command.command_id, state='succeeded', reason=None, evidence=evidence())
        assert result.state == 'held'
        assert exchange('lookup_completion_v5', {'identity': pins}) == value
    finally:
        verifier.close()


def test_supervisor_owned_path_accepts_exact_fenced_evidence(rig, completed, monkeypatch):
    supervisor, journal, key, _, _, held = rig
    ledger, command, pins, health_id, receipt = completed
    template, _, _, _ = vector()
    manifest = SimpleNamespace(digest=lambda: pins['manifest_digest'], probes=template.probes)
    probe = probe_set_digest(manifest.probes.candidate)
    raw = json.dumps(dict(receipt, profile_digest=probe), sort_keys=True, separators=(',', ':'))
    mac = ledger._operation_mac(health_id, command.command_id, 'gate_health', 'completed', raw)
    with ledger._transaction() as db:
        db.execute('UPDATE operations SET receipt=?,receipt_mac=? WHERE operation_id=?', (raw, mac, health_id))
        decision = db.execute('SELECT decision FROM mount_gate_challenges WHERE operation_id=?', (pins['operation_id'],)).fetchone()[0]
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    release = ExecutorReleaseEnrollment('test-issuer', public, 'c' * 40, 'sha256:' + '1' * 64,
        'sha256:' + '2' * 64, 'sha256:' + '3' * 64, Platform(os='linux', architecture='amd64', variant=None),
        supervisor.release.launch_profile_digest)
    clock = SimpleNamespace(wall=time.time(), mono=time.monotonic())
    verifier = LaunchReceiptIdentityVerifier(enrollment=release, issuer_transport=Issuer(release, key, clock,
        mutate=lambda value: value.update(authenticated_host_pid=os.getpid())),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
    binding = verifier.launch_binding(); old_id = supervisor._launch_id
    supervisor._launch_id, supervisor._container_id = binding[0], binding[4]
    supervisor._consumer = SimpleNamespace(boot=binding[1], pid=binding[2], start=binding[3])
    # PeerPin still uses actual kernel process start ticks.
    supervisor._consumer.start = _start(os.getpid())
    supervisor._consumer_uid, supervisor._consumer_gid = os.getuid(), os.getgid()
    supervisor._mutex = threading.Lock(); supervisor._observation_thread = None
    supervisor._held = False; supervisor._hold_persisted = False
    def guard(*, allow_held=False):
        if supervisor._held and not allow_held: raise launch.LaunchSupervisorError()
    supervisor._guard = guard
    supervisor._validate_live = lambda *_, **__: None
    supervisor.release = release; supervisor._handoff_manifest_digest = manifest.digest
    # Reissue the synthetic launch receipt with the actual start tick.
    verifier.close()
    verifier = LaunchReceiptIdentityVerifier(enrollment=release, issuer_transport=Issuer(release, key, clock,
        mutate=lambda value: value.update(authenticated_host_pid=os.getpid(),
                                          process_start_ticks=supervisor._consumer.start, launch_id=binding[0], boot_id=binding[1])),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
    record = {'boot_id': binding[1], 'container_id': binding[4], 'pid': os.getpid(),
              'start_ticks': supervisor._consumer.start, 'manifest': release.launch_profile_digest,
              'approval_expires_at_ms': supervisor._approval_expires_at_ms}
    supervisor._record = lambda: record
    with journal.transaction() as db:
        db.execute('UPDATE launch SET id=?,data=? WHERE id=?', (binding[0], launch._canonical(record).decode(), old_id))
        db.execute('UPDATE issuance SET launch_id=? WHERE launch_id=?', (binding[0], old_id))
    measurement = {'executable_digest': 'sha256:' + 'a' * 64,
                   'decision_payload_digest': 'sha256:' + hashlib.sha256(decision.encode()).hexdigest()}
    monkeypatch.setattr(supervisor, 'measure_gate_post_exec', lambda **_: measurement.copy())
    policy = GateCompletionPolicy(candidate_executable_digest=measurement['executable_digest'],
        rollback_executable_digest='sha256:' + 'b' * 64, health_contract='engineering-hub.health-json-build-commit.v1')
    receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    listener = None
    try:
        assert supervisor._issue_ledger_handoff_locked(sender, 'e' * 64, time.monotonic() + 3)
        _, anc, _, _ = receiver.recvmsg(16384, socket.CMSG_SPACE(4), socket.MSG_CMSG_CLOEXEC)
        fds = array.array('i'); fds.frombytes(anc[0][2]); listener = socket.socket(fileno=fds[0]); listener.settimeout(3)
        results = []
        def serve():
            try:
                for _ in range(3):
                    connection, _ = listener.accept()
                    with connection:
                        connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
                        worker = GateLedgerWorker(channel=connection, ledger=ledger, supervisor_public_key=public,
                            launch_verifier=verifier, manifest_digest=pins['manifest_digest'],
                            hold_uncertain=ledger.hold_gate_uncertain, timeout_seconds=3,
                            completion_policy=policy, require_sender_liveness=True)
                        results.append(worker.serve_once())
            except BaseException as error: results.append(error)
        thread = threading.Thread(target=serve); thread.start()
        try:
            accepted = supervisor.accept_gate_completion(peer=SimpleNamespace(pid=pins['host_pid']), pins=pins,
                manifest=manifest, image=SimpleNamespace(role='candidate'), enrollment_file='synthetic', timeout_seconds=5)
            assert accepted['identity'] == pins and ledger.lookup_gate_completion(pins) == accepted
            assert ledger.get(command.command_id).state == 'running' and not held
            supervisor._held = supervisor._hold_persisted = True
            assert supervisor.lookup_gate_completion(identity=pins, manifest_digest=pins['manifest_digest'],
                                                      timeout_seconds=2) == accepted
            assert supervisor._held is True
        finally:
            thread.join(4)
            if thread.is_alive(): listener.close(); thread.join(4)
            assert not thread.is_alive() and results == ['sent_unconfirmed', 'sent_unconfirmed', 'sent_unconfirmed']
    finally:
        if listener is not None: listener.close()
        receiver.close(); sender.close(); verifier.close()


def test_owned_service_keeps_only_lookup_after_accept_reply_loss(completed, tmp_path, monkeypatch):
    ledger, command, pins, _, _ = completed
    observed = ledger.gate_completion_snapshot(pins, PROBE)
    data = gate_profile()
    data['gate_completion'] = {'candidate_executable_digest': 'sha256:' + 'a' * 64,
        'rollback_executable_digest': 'sha256:' + 'b' * 64,
        'health_contract': 'engineering-hub.health-json-build-commit.v1'}
    data['gate_ledger_channel'] = 'supervisor-listener-v1'
    profile = ExecutorLaunchProfile.model_validate(data)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    release = ExecutorReleaseEnrollment('test-issuer', public, 'c' * 40, 'sha256:' + '1' * 64,
        'sha256:' + '2' * 64, 'sha256:' + '3' * 64, Platform(os='linux', architecture='amd64', variant=None), profile.digest())
    clock = SimpleNamespace(wall=time.time(), mono=time.monotonic())
    verifier = LaunchReceiptIdentityVerifier(enrollment=release, issuer_transport=Issuer(release, key, clock),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    path = str(tmp_path / 'ledger.sock'); listener.bind(path); listener.listen(2)
    committed, release_reply, held = threading.Event(), threading.Event(), threading.Event()
    original = ledger.accept_gate_completion
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs); committed.set()
        if not release_reply.wait(3): raise RuntimeError('test deadline')
        return result
    def hold(value): ledger.hold_gate_uncertain(value); held.set()
    monkeypatch.setattr(ledger, 'accept_gate_completion', delayed)
    service = OwnedGateLedgerService(listener=listener, ledger=ledger, supervisor_public_key=public,
        launch_verifier=verifier, manifest_digest=pins['manifest_digest'], hold_uncertain=hold,
        timeout_seconds=3, completion_profile_bytes=canonical(profile.enrollment_value()), require_sender_liveness=True)
    failed = threading.Event(); service.start(on_failure=failed.set)
    value = {'identity': pins, 'role': 'candidate', 'executable_digest': data['gate_completion']['candidate_executable_digest'],
             'health_contract': data['gate_completion']['health_contract'], 'probe_digest': PROBE,
             'health_receipt_digest': observed['health_receipt_digest'], 'decision_payload_digest': observed['decision_payload_digest']}
    client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET); client.settimeout(3); client.connect(path)
    try:
        challenge = json.loads(client.recv(8192))['challenge']; now = int(time.time() * 1000)
        payload = {'protocol': REQUEST, 'challenge': challenge, 'launch_binding': list(verifier.launch_binding()),
            'manifest_digest': pins['manifest_digest'], 'action': 'accept_completion_v5', 'value': value,
            'issued_at_ms': now, 'expires_at_ms': now + 2500}
        client.send(canonical({'payload': payload, 'signature': key.sign(DOMAIN + canonical(payload)).hex()}))
        assert committed.wait(3)
        client.close(); release_reply.set()
        assert held.wait(3)
        peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
        lookup = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        lookup.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1); lookup.connect(path)
        try:
            result = SupervisorLedgerClient(channel=lookup, peer=peer, signing_key=key,
                launch_binding=verifier.launch_binding(), manifest_digest=pins['manifest_digest'],
                timeout_seconds=2, lookup_only=True).invoke('lookup_completion_v5', {'identity': pins})
            assert result == value and not failed.is_set()
            assert ledger.finish(command.command_id, state='succeeded', reason=None, evidence=evidence()).state == 'held'
        finally: lookup.close(); peer.close()
        replay = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        replay.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1); replay.connect(path)
        peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
        try:
            with pytest.raises(GateLedgerChannelError):
                SupervisorLedgerClient(channel=replay, peer=peer, signing_key=key,
                    launch_binding=verifier.launch_binding(), manifest_digest=pins['manifest_digest'],
                    timeout_seconds=2).invoke('accept_completion_v5', value)
            assert failed.wait(3) and ledger.lookup_gate_completion(pins) == value
        finally: replay.close(); peer.close()
    finally:
        client.close(); release_reply.set()
        try:
            if failed.is_set():
                with pytest.raises(GateLedgerChannelError): service.close()
            else: service.close()
        finally: verifier.close()
