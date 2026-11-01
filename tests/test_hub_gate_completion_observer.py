"""Actual signed ledger exchange; supervisor host measurements are synthetic."""
import hashlib
import json
import os
import socket
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from engineering_hub.container_pilot_manifest import Platform
from engineering_hub.executor_launch_profile import ExecutorLaunchProfile, GateCompletionPolicy, canonical
from hub_container_executor import launch_supervisor as launch_module
from hub_container_executor.broker_seqpacket import PeerPin, _start
from hub_container_executor.gate_ledger_channel import (GateLedgerChannelError, GateLedgerWorker,
                                                         OwnedGateLedgerService, SupervisorLedgerClient)
from hub_container_executor.runtime_identity import ExecutorReleaseEnrollment, LaunchReceiptIdentityVerifier
from hub_container_executor.runtime_profile import probe_set_digest
from tests.test_hub_container_executor_launch_supervisor import short_root
from tests.test_hub_gate_socket_supervisor import socket_rig
from tests.test_hub_gate_completion_snapshot import completed
from tests.test_hub_container_executor_runtime_identity import Issuer


@pytest.mark.parametrize('fault', ['none', 'drift', 'post_read_failure', 'wrong_fence', 'wrong_peer', 'wrong_key',
                                  'wrong_decision', 'wrong_policy', 'wrong_app_pid', 'stale_receipt'])
def test_observer_binds_real_signed_snapshot_and_rechecks(socket_rig, completed, monkeypatch, fault):
    supervisor, manifest_template, image, path, _, held = socket_rig
    ledger, command, pins, health_id, receipt = completed
    manifest = SimpleNamespace(digest=lambda: pins['manifest_digest'], probes=manifest_template.probes)
    probe_digest = probe_set_digest(manifest.probes.candidate)
    new_receipt = dict(receipt, profile_digest=probe_digest)
    if fault == 'stale_receipt': new_receipt.update(started_at_ms=1, finished_at_ms=2)
    raw = json.dumps(new_receipt, sort_keys=True, separators=(',', ':'))
    mac = ledger._operation_mac(health_id, command.command_id, 'gate_health', 'completed', raw)
    with ledger._transaction() as db:
        db.execute('UPDATE operations SET receipt=?,receipt_mac=? WHERE operation_id=?', (raw, mac, health_id))
        decision = db.execute('SELECT decision FROM mount_gate_challenges WHERE operation_id=?',
                              (pins['operation_id'],)).fetchone()['decision']
    decision_digest = 'sha256:' + hashlib.sha256(decision.encode()).hexdigest()
    executable_digest = 'sha256:' + 'a' * 64
    profile_data = supervisor.profile.enrollment_value()
    profile_data['gate_completion'] = {'candidate_executable_digest': executable_digest,
        'rollback_executable_digest': 'sha256:' + 'b'*64,
        'health_contract': 'engineering-hub.health-json-build-commit.v1'}
    supervisor.profile = ExecutorLaunchProfile.model_validate(profile_data)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    enrollment = ExecutorReleaseEnrollment('synthetic', public, 'c' * 40,
        'sha256:'+'1'*64, 'sha256:'+'2'*64, 'sha256:'+'3'*64,
        Platform(os='linux', architecture='amd64', variant=None), supervisor.profile.digest())
    clock = SimpleNamespace(wall=time.time(), mono=time.monotonic())
    def pin_process(payload):
        payload.update(authenticated_host_pid=os.getpid(), process_start_ticks=_start(os.getpid()))
    verifier = LaunchReceiptIdentityVerifier(enrollment=enrollment,
        issuer_transport=Issuer(enrollment, key, clock, mutate=pin_process),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
    launch_id, boot, host_pid, start, container = verifier.launch_binding()
    supervisor._launch_id, supervisor._container_id = launch_id, container
    supervisor._consumer = SimpleNamespace(pid=host_pid, boot=boot, start=start)
    supervisor._consumer_uid, supervisor._consumer_gid = os.getuid(), os.getgid()
    supervisor.release = replace(supervisor.release, public_key=(b'\x00' * 32 if fault == 'wrong_key' else public),
                                 launch_profile_digest=supervisor.profile.digest())
    observed = []; peer_arg = SimpleNamespace(pid=pins['host_pid'] + (fault == 'wrong_app_pid'))
    def measure(**kwargs):
        observed.append(kwargs['pins'].copy())
        if fault == 'post_read_failure' and len(observed) == 2: raise RuntimeError('synthetic loss')
        return {'measurement': len(observed) if fault == 'drift' else 1,
                'executable_digest': executable_digest,
                'decision_payload_digest': 'sha256:' + '0'*64 if fault == 'wrong_decision' else decision_digest}
    monkeypatch.setattr(supervisor, 'measure_gate_post_exec', measure)
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    for endpoint in (client, server): endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid() + (fault == 'wrong_peer'), gid=os.getgid(),
                   start_ticks=_start(os.getpid()))
    binding = verifier.launch_binding()
    policy = supervisor.profile.gate_completion
    if fault == 'wrong_policy':
        policy = GateCompletionPolicy(candidate_executable_digest='sha256:'+'f'*64,
            rollback_executable_digest=policy.rollback_executable_digest, health_contract=policy.health_contract)
    worker = GateLedgerWorker(channel=server, ledger=ledger, supervisor_public_key=public,
        launch_verifier=verifier, manifest_digest=pins['manifest_digest'], hold_uncertain=lambda _: None,
        timeout_seconds=2, completion_policy=policy)
    channel = SupervisorLedgerClient(channel=client, peer=peer, signing_key=key, launch_binding=binding,
                                      manifest_digest=pins['manifest_digest'], timeout_seconds=2)
    results = []
    def serve():
        try: results.append(worker.serve_once())
        except BaseException as error: results.append(error)
    thread = threading.Thread(target=serve); thread.start()
    supplied = dict(pins)
    if fault == 'wrong_fence': supplied['fence'] += 1
    try:
        args = dict(peer=peer_arg, pins=supplied, manifest=manifest, image=image, enrollment_file=path,
                    ledger_client=channel, timeout_seconds=2)
        if fault == 'none':
            answer = supervisor.observe_gate_completion_facts(**args)
            assert answer['identity'] == pins and answer['ledger_snapshot']['operation_id'] == pins['operation_id']
            assert answer['ledger_snapshot']['health_operation_id'] == health_id
            assert answer['ledger_snapshot']['probe_digest'] == probe_digest and len(observed) == 2 and not held
            assert answer['ledger_snapshot']['decision_payload_digest'] == decision_digest
            assert answer['ledger_snapshot']['role'] == 'candidate'
            assert answer['ledger_snapshot']['health_daemon_pid'] == peer_arg.pid
            assert ledger.get(command.command_id).state == 'running'
        else:
            with pytest.raises(launch_module.LaunchSupervisorError):
                supervisor.observe_gate_completion_facts(**args)
            assert held and ledger.get(command.command_id).state == 'running'
            if fault in {'wrong_peer', 'wrong_key'}: assert observed == []
            elif fault in {'wrong_fence', 'wrong_policy', 'stale_receipt'}: assert len(observed) == 1
            else: assert len(observed) == 2
    finally:
        client.close(); server.close(); thread.join(4); peer.close(); verifier.close()
        assert not thread.is_alive()


@pytest.mark.parametrize('case', ['matching', 'changed_bytes', 'missing_policy', 'wrong_key', 'legacy'])
def test_owned_service_requires_matching_signed_profile_policy(socket_rig, completed, tmp_path, case):
    supervisor, _, _, _, _, _ = socket_rig
    ledger, _, pins, _, _ = completed
    data = supervisor.profile.enrollment_value()
    if case not in {'missing_policy', 'legacy'}:
        data['gate_completion'] = {'candidate_executable_digest': 'sha256:'+'a'*64,
            'rollback_executable_digest': 'sha256:'+'b'*64,
            'health_contract': 'engineering-hub.health-json-build-commit.v1'}
    profile = ExecutorLaunchProfile.model_validate(data)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    enrollment = ExecutorReleaseEnrollment('synthetic', public, 'c'*40,
        'sha256:'+'1'*64, 'sha256:'+'2'*64, 'sha256:'+'3'*64,
        Platform(os='linux', architecture='amd64', variant=None), profile.digest())
    clock = SimpleNamespace(wall=time.time(), mono=time.monotonic())
    verifier = LaunchReceiptIdentityVerifier(enrollment=enrollment, issuer_transport=Issuer(enrollment, key, clock),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
    raw = canonical(data)
    if case == 'changed_bytes':
        data['gate_completion']['candidate_executable_digest'] = 'sha256:'+'f'*64
        raw = canonical(data)
    if case == 'legacy': raw = None
    if case == 'wrong_key': public = b'\x00'*32
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(str(tmp_path / 'ledger.sock')); listener.listen(1)
    try:
        args = dict(listener=listener, ledger=ledger, supervisor_public_key=public,
                    launch_verifier=verifier, manifest_digest=pins['manifest_digest'],
                    hold_uncertain=lambda _: None, timeout_seconds=2, completion_profile_bytes=raw)
        if case in {'matching', 'legacy'}:
            worker = OwnedGateLedgerService(**args)
            assert (worker._completion_policy is None) == (case == 'legacy')
            worker.close()
        else:
            with pytest.raises(GateLedgerChannelError): OwnedGateLedgerService(**args)
    finally:
        listener.close(); verifier.close()
