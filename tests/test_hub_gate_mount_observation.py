"""Synthetic supervisor authority with real nonce-signed Unix observations."""
import os
import json
import socket
import threading
import time
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub_container_executor import gate_mount_observation as gate_observation
from hub_container_executor.launch_transport import UnixCurrentObservationTransport
from hub_container_executor.runtime_identity import LaunchReceiptIdentityVerifier
from tests.test_hub_gate_socket_supervisor import socket_rig
from tests.test_hub_container_executor_launch_supervisor import short_root


@pytest.mark.parametrize('fault', ['none', 'nonce', 'launch', 'image', 'directory', 'socket', 'expiry', 'signature', 'source',
                                   'pid_bool', 'postcheck_timeout'])
def test_real_supervisor_observation_is_nonce_and_launch_bound(socket_rig, monkeypatch, fault):
    supervisor, manifest, image, path, directory, _held = socket_rig
    supervisor.prepare_gate_socket(manifest=manifest, image=image, enrollment_file=path, timeout_seconds=2)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    supervisor.release = replace(supervisor.release, public_key=public)
    supervisor._signer = key
    boot = str(uuid4()); host_pid = os.getpid(); host_start = 12345; container_id = 'a' * 64
    supervisor._consumer = SimpleNamespace(boot=boot, pid=host_pid, start=host_start)
    supervisor._consumer_uid, supervisor._consumer_gid = os.getuid(), os.getgid()
    supervisor._container_id = container_id
    supervisor._receipt_served, supervisor._issuance_open = True, False
    supervisor._expires = time.monotonic() + 30
    supervisor._approval_expires_at_ms = int(time.time() * 1000) + 30000
    supervisor._request_ms = 2000
    issuer_path = directory.parent / 'issuer.sock'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(issuer_path)); listener.listen(1)
    supervisor._listener = listener
    transport = UnixCurrentObservationTransport(socket_path=str(issuer_path),
        expected_issuer_uid=os.getuid(), expected_issuer_gid=os.getgid())
    verifier = object.__new__(LaunchReceiptIdentityVerifier)
    verifier._pid, verifier._lock, verifier._closed = os.getpid(), threading.Lock(), False
    verifier._not_before, verifier._deadline = time.monotonic() - 1, time.monotonic() + 30
    verifier._receipt = MappingProxyType({'launch_id': supervisor._launch_id, 'boot_id': boot,
        'authenticated_host_pid': 1 if fault == 'pid_bool' else host_pid,
        'process_start_ticks': host_start, 'container_id': container_id})
    class NamespaceOS:
        def __getattr__(self, name): return getattr(os, name)
        def getuid(self): return 110001
        def getgid(self): return 110001
        def statvfs(self, _): return SimpleNamespace(f_flag=os.ST_RDONLY)
    # Synthetic user-namespace presentation; host filesystem is not actually remapped/read-only here.
    monkeypatch.setattr(gate_observation, 'os', NamespaceOS())
    class MutatingTransport:
        def observe_gate_mount(self, *, nonce, timeout_ms):
            wire = transport.observe_gate_mount(nonce=nonce, timeout_ms=timeout_ms)
            if fault == 'none': return wire
            envelope = json.loads(wire); payload = envelope['payload']
            if fault == 'nonce': payload['nonce'] = 'f' * 64
            if fault == 'launch': payload['launch_id'] = str(uuid4())
            if fault == 'image': payload['image_config_id'] = 'sha256:' + '9' * 64
            if fault == 'directory': payload['directory_inode'] += 1
            if fault == 'socket': payload['socket_inode'] += 1
            if fault == 'expiry': payload['expires_at_ms'] = payload['issued_at_ms']
            if fault == 'source': payload['socket_source_directory'] += '-other'
            if fault == 'pid_bool': payload['authenticated_host_pid'] = True
            envelope['signature'] = (('00' * 64) if fault == 'signature' else
                key.sign(gate_observation.DOMAIN + gate_observation._canonical(payload)).hex())
            return gate_observation._canonical(envelope)
    witness = gate_observation.GateMountVerifier(release=supervisor.release, launch_verifier=verifier,
        transport=MutatingTransport(), manifest_digest=manifest.digest(), source_directory=str(directory))
    checked = []
    if fault == 'postcheck_timeout':
        original_socket_identity = witness._socket_identity
        def delayed_final_check():
            checked.append(True)
            if len(checked) == 2: time.sleep(.6)
            return original_socket_identity()
        witness._socket_identity = delayed_final_check
    errors = []
    def serve():
        try: assert supervisor._serve_current_until(time.monotonic() + 2) is True
        except BaseException as error: errors.append(error)
    worker = threading.Thread(target=serve); worker.start()
    try:
        if fault == 'none':
            assert witness.verify_current(image, timeout_ms=2000) == supervisor._gate_enrollment.bootstrap_digest
        else:
            with pytest.raises(gate_observation.GateMountObservationError):
                witness.verify_current(image, timeout_ms=400 if fault == 'postcheck_timeout' else 2000)
            if fault == 'postcheck_timeout': assert len(checked) == 2
    finally:
        worker.join(3); witness.close(); transport.close(); verifier.close(); listener.close()
        issuer_path.unlink()
    assert not worker.is_alive() and not errors
