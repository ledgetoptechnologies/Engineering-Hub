import base64
import os
import socket
import struct
import array
import threading
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hub_container_executor import gate_ledger_handoff as handoff
from tests.test_hub_gate_ledger_channel import canonical, verified_launch
from tests.test_hub_container_executor_runtime_identity import Issuer
from tests.test_executor_launch_profile import gate_profile
from engineering_hub.executor_launch_profile import ExecutorLaunchProfile
from engineering_hub.container_pilot_manifest import Platform
from hub_container_executor.runtime_identity import ExecutorReleaseEnrollment, LaunchReceiptIdentityVerifier
from hub_container_executor.launch_transport import UnixCurrentObservationTransport
from types import SimpleNamespace


@pytest.mark.parametrize('pair_only', [False, True])
def test_signed_handoff_binds_launch_nonce_and_listener(tmp_path, monkeypatch, pair_only):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    verifier = verified_launch(key, public)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    other = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        path = str(tmp_path / 'ledger.sock'); listener.bind(path); listener.listen(1)
        other.bind(str(tmp_path / 'other')); other.listen(1)
        info, leaf = os.fstat(listener.fileno()), os.lstat(path)
        binding = verifier.launch_binding()
        wall, tick = int(time.time() * 1000), time.monotonic()
        payload = dict(zip(('launch_id', 'boot_id', 'authenticated_host_pid', 'process_start_ticks', 'container_id'), binding))
        payload.update(protocol=handoff.PAIR_PROTOCOL if pair_only else handoff.PROTOCOL, audience=handoff.PAIR_AUDIENCE if pair_only else handoff.AUDIENCE, nonce='a' * 64,
            manifest_digest='sha256:' + 'b' * 64, launch_profile_digest=verifier.launch_profile_digest(),
            listener_device=info.st_dev, listener_inode=info.st_ino, path_device=leaf.st_dev,
            path_inode=leaf.st_ino, issued_at_ms=wall, expires_at_ms=wall + 4000)
        signature = base64.urlsafe_b64encode(key.sign((handoff.PAIR_DOMAIN if pair_only else handoff.DOMAIN) + canonical(payload))).rstrip(b'=').decode()
        raw = canonical({'payload': payload, 'signature': signature})
        options = dict(verifier=verifier, nonce='a' * 64, manifest_digest='sha256:' + 'b' * 64,
                       source_directory=str(tmp_path), start_wall_ms=wall, start_monotonic=tick, pair_only=pair_only)
        assert handoff.verify_handoff(raw, listener.fileno(), **options)
        with pytest.raises(handoff.GateLedgerHandoffError):
            handoff.verify_handoff(raw, listener.fileno(), **dict(options, pair_only=not pair_only))
        with pytest.raises(handoff.GateLedgerHandoffError):
            handoff.verify_handoff(raw, other.fileno(), **options)
        options['nonce'] = 'b' * 64
        with pytest.raises(handoff.GateLedgerHandoffError):
            handoff.verify_handoff(raw, listener.fileno(), **options)
        options['nonce'] = 'a' * 64
        original_lstat, actual_time, actual_tick = os.lstat, time.time, time.monotonic
        for fault in ('late', 'wall_back', 'tick_back'):
            calls = []
            def delayed(path):
                result = original_lstat(path); calls.append(path)
                if len(calls) == 2:
                    if fault == 'late': scoped.setattr(handoff.time, 'time', lambda: actual_time() + 6)
                    elif fault == 'wall_back': scoped.setattr(handoff.time, 'time', lambda: actual_time() - 1)
                    else: scoped.setattr(handoff.time, 'monotonic', lambda: actual_tick() - 1)
                return result
            with monkeypatch.context() as scoped:
                scoped.setattr(handoff.os, 'lstat', delayed)
                with pytest.raises(handoff.GateLedgerHandoffError):
                    handoff.verify_handoff(raw, listener.fileno(), **options)
        calls = []
        def revoked(path):
            result = original_lstat(path); calls.append(path)
            if len(calls) == 2: verifier.close()
            return result
        with monkeypatch.context() as scoped:
            scoped.setattr(handoff.os, 'lstat', revoked)
            with pytest.raises(handoff.GateLedgerHandoffError):
                handoff.verify_handoff(raw, listener.fileno(), **options)
    finally:
        listener.close(); other.close(); verifier.close()


@pytest.mark.parametrize('pair_only', [False, True])
def test_transport_receives_one_listener_and_independently_pinned_profile(tmp_path, pair_only):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fixture_root = tmp_path / 'hub-pilot-fixture'; fixture_root.mkdir()
    gate_dir = fixture_root / 'gate-control'; gate_dir.mkdir()
    data = gate_profile(str(fixture_root))
    data['gate_completion'] = {'candidate_executable_digest': 'sha256:' + 'a' * 64,
        'rollback_executable_digest': 'sha256:' + 'b' * 64,
        'health_contract': 'engineering-hub.health-json-build-commit.v1'}
    data['gate_ledger_channel'] = 'supervisor-listener-v1'
    if pair_only:
        del data['gate_channel']; del data['gate_completion']
        data['gate_ledger_channel'] = 'pair-outcome-listener-v1'
    profile = ExecutorLaunchProfile.model_validate(data)
    profile_bytes = canonical(profile.enrollment_value())
    enrollment = ExecutorReleaseEnrollment('test-issuer', public, 'c' * 40, 'sha256:' + '1' * 64,
        'sha256:' + '2' * 64, 'sha256:' + '3' * 64, Platform(os='linux', architecture='amd64', variant=None),
        profile.digest())
    clock = SimpleNamespace(wall=time.time(), mono=time.monotonic())
    verifier = LaunchReceiptIdentityVerifier(enrollment=enrollment, issuer_transport=Issuer(enrollment, key, clock),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
    issuer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    issuer_path, listener_path = str(tmp_path / 'issuer'), str(gate_dir / 'ledger.sock')
    issuer.bind(issuer_path); issuer.listen(1); issuer.settimeout(3)
    listener.bind(listener_path); listener.listen(1)
    info, leaf = os.fstat(listener.fileno()), os.lstat(listener_path)
    failure = []
    def serve():
        try:
            connection, _ = issuer.accept()
            with connection:
                connection.settimeout(3)
                size = struct.unpack('!I', connection.recv(4))[0]
                import json
                request = json.loads(connection.recv(size))
                assert request['protocol'] == ('engineering-hub.executor-pair-outcome-handoff-request.v1' if pair_only else 'engineering-hub.executor-gate-ledger-handoff-request.v1')
                binding = verifier.launch_binding(); now = int(time.time() * 1000)
                payload = dict(zip(('launch_id', 'boot_id', 'authenticated_host_pid', 'process_start_ticks', 'container_id'), binding))
                payload.update(protocol=handoff.PAIR_PROTOCOL if pair_only else handoff.PROTOCOL, audience=handoff.PAIR_AUDIENCE if pair_only else handoff.AUDIENCE, nonce=request['nonce'],
                    manifest_digest='sha256:' + 'b' * 64, launch_profile_digest=profile.digest(),
                    listener_device=info.st_dev, listener_inode=info.st_ino, path_device=leaf.st_dev,
                    path_inode=leaf.st_ino, issued_at_ms=now, expires_at_ms=now + 4000)
                signature = base64.urlsafe_b64encode(key.sign((handoff.PAIR_DOMAIN if pair_only else handoff.DOMAIN) + canonical(payload))).rstrip(b'=').decode()
                raw = canonical({'payload': payload, 'signature': signature})
                wire = struct.pack('!I', len(raw)) + raw + struct.pack('!I', len(profile_bytes)) + profile_bytes
                assert connection.sendmsg([wire], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [listener.fileno()]))]) == len(wire)
        except BaseException as exc: failure.append(exc)
    thread = threading.Thread(target=serve); thread.start()
    transport = UnixCurrentObservationTransport(socket_path=issuer_path,
        expected_issuer_uid=os.getuid(), expected_issuer_gid=os.getgid())
    fd = None
    try:
        fd, _, _, _, _, got_profile = transport.issue_ledger_listener(verifier=verifier,
            manifest_digest='sha256:' + 'b' * 64, source_directory=str(gate_dir), timeout_ms=2000, pair_only=pair_only)
        assert got_profile == profile_bytes and os.fstat(fd).st_ino == info.st_ino
        with pytest.raises(Exception):
            transport.issue_ledger_listener(verifier=verifier, manifest_digest='sha256:' + 'b' * 64,
                source_directory=str(gate_dir), timeout_ms=2000)
    finally:
        if fd is not None: os.close(fd)
        transport.close(); thread.join(4); issuer.close(); listener.close(); verifier.close()
        assert not thread.is_alive() and not failure
