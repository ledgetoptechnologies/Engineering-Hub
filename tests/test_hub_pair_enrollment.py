"""Synthetic pair enrollment over real UDS; no live Engine or host authority."""
import os
from dataclasses import replace
import json
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
pytestmark = pytest.mark.skipif(sys.platform != 'linux' or os.geteuid() != 0, reason='root private fixture')
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hub_container_executor import pair_handoff as pair
from hub_container_executor import handoff_identity as identity
from hub_container_executor.launch_supervisor import LaunchSupervisor
from hub_container_executor.launch_transport import UnixLaunchReceiptTransport
from hub_container_executor.runtime_identity import RuntimeIdentityError
from tests.test_hub_executor_channel_handoff import enrollment


def test_protected_pair_enrollment_real_uds_and_durable_no_reissue(tmp_path, monkeypatch):
    import engineering_hub.pilot_ownership as ownership
    monkeypatch.setattr(ownership, '_local_filesystem', lambda fd: None)
    os.chmod(tmp_path, 0o700)
    keys = [Ed25519PrivateKey.generate() for _ in range(3)]
    releases = [enrollment(key) for key in keys[:2]]
    raw_key = keys[2].private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                    serialization.NoEncryption())
    public = keys[2].public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    policy = {'pair_id': str(uuid4()), 'manifest_digest': 'sha256:' + 'f' * 64,
              'executor_launch_id': str(uuid4()), 'coordinator_launch_id': str(uuid4()),
              'executor_release_digest': pair._release_digest(releases[0]),
              'coordinator_release_digest': pair._release_digest(releases[1])}
    policy_path, key_path = tmp_path / 'policy', tmp_path / 'key'
    for path, raw in ((policy_path, pair._canonical(policy)), (key_path, raw_key)):
        path.write_bytes(raw); os.chmod(path, 0o600)
    r = pair.PairLaunchReservation.__new__(pair.PairLaunchReservation)
    r._pid, r._lock, r._closed = os.getpid(), threading.Lock(), False
    r._policy_path, r._key_path, r._policy = policy_path, key_path, policy
    r._policy_identity = pair._root_file(policy_path, 65536)[1]
    r._key_identity = pair._root_file(key_path, 32)[1]
    r._used = {'executor', 'coordinator'}
    r._journal = pair._Journal(str(tmp_path / 'pair.db'), create=True)
    r._journal.reserve(policy['pair_id'], {})
    listeners, supervisors = [], {}
    for index, role in enumerate(('executor', 'coordinator')):
        sup = LaunchSupervisor.__new__(LaunchSupervisor)
        sup._pid, sup._mutex = os.getpid(), threading.Lock()
        sup._guard = lambda: None
        sup._validate_live = lambda deadline: None
        sup._handoff_manifest_digest = lambda: policy['manifest_digest']
        sup._launch_id, sup.release = policy[role + '_launch_id'], releases[index]
        sup._consumer = SimpleNamespace(pid=os.getpid())
        sup._consumer_uid, sup._consumer_gid = os.getuid(), os.getgid()
        sup._approval_expires_at_ms = int(time.time() * 1000) + 20000
        sup._expires = time.monotonic() + 20
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(tmp_path / (role + '.sock'))); listener.listen(1)
        sup._listener = listener
        def receive(peer, count, deadline):
            data = b''
            while len(data) < count:
                part = peer.recv(count - len(data))
                assert part
                data += part
            return data
        sup._receive = receive
        supervisors[role] = sup; listeners.append(listener)
    r._supervisors = supervisors
    errors = []
    def issue():
        try: pair.serve_pair_enrollment(r, role='executor', timeout_seconds=3)
        except BaseException as error: errors.append(error)
    thread = threading.Thread(target=issue)
    transport = UnixLaunchReceiptTransport(socket_path=str(tmp_path / 'executor.sock'),
                                           expected_issuer_uid=os.getuid(), expected_issuer_gid=os.getgid())
    try:
        thread.start()
        result = transport.issue_pair_enrollment(role='executor', authority_public_key=public,
            own_enrollment=releases[0], manifest_digest=policy['manifest_digest'], timeout_ms=3000)
        thread.join(4)
        assert not thread.is_alive() and not errors
        assert result.pair_id == policy['pair_id'] and result.executor_enrollment == releases[0]
        assert result.coordinator_enrollment == releases[1]
        delayed = replace(result)
        real_time = identity.time
        monkeypatch.setattr(identity, 'time', SimpleNamespace(monotonic=lambda: real_time.monotonic() + 6,
            time=lambda: real_time.time() + 6))
        later_transport = UnixLaunchReceiptTransport(socket_path=str(tmp_path / 'executor.sock'),
            expected_issuer_uid=os.getuid(), expected_issuer_gid=os.getgid())
        read_fd, write_fd = os.pipe()
        verifier = SimpleNamespace(close=lambda: None)
        try:
            monkeypatch.setattr(later_transport, 'issue_verified_pair_channel', lambda **kwargs: (read_fd, verifier))
            assert later_transport.issue_enrolled_pair_channel(enrollment=delayed, role='executor',
                authority_public_key=public, own_enrollment=releases[0], manifest_digest=policy['manifest_digest'],
                max_receipt_lifetime_seconds=30, timeout_ms=1000) == (read_fd, verifier)
        finally:
            monkeypatch.setattr(identity, 'time', real_time)
            later_transport.close(); os.close(read_fd); os.close(write_fd)
        payload = json.loads(result.signed_envelope)['payload']
        for mutation in ('nonce', 'role', 'own_release', 'expired', 'signature'):
            changed = dict(payload)
            if mutation == 'nonce': changed['nonce'] = '0' * 64
            if mutation == 'role': changed['role'] = 'coordinator'
            if mutation == 'own_release': changed['executor_release'] = changed['coordinator_release']
            if mutation == 'expired': changed['expires_at_ms'] = changed['issued_at_ms'] - 1
            signer = Ed25519PrivateKey.generate() if mutation == 'signature' else keys[2]
            raw = pair._canonical(pair._envelope(signer, identity.PAIR_ENROLLMENT_DOMAIN, changed))
            with pytest.raises(RuntimeIdentityError):
                identity.verify_pair_enrollment(raw, authority_public_key=public, role='executor',
                    nonce=result.nonce, manifest_digest=policy['manifest_digest'], own_enrollment=releases[0],
                    start_monotonic=time.monotonic(), start_wall_ms=int(time.time() * 1000))
        forged = replace(result, coordinator_launch_id=str(uuid4()))
        next_transport = UnixLaunchReceiptTransport(socket_path=str(tmp_path / 'executor.sock'),
            expected_issuer_uid=os.getuid(), expected_issuer_gid=os.getgid())
        try:
            monkeypatch.setattr(next_transport, 'issue_verified_pair_channel',
                                lambda **kwargs: pytest.fail('forged enrollment reached receipt exchange'))
            with pytest.raises(RuntimeIdentityError):
                next_transport.issue_enrolled_pair_channel(enrollment=forged, role='executor',
                    authority_public_key=public, own_enrollment=releases[0],
                    manifest_digest=policy['manifest_digest'], max_receipt_lifetime_seconds=30, timeout_ms=1000)
        finally:
            next_transport.close()
        real_time = identity.time
        monkeypatch.setattr(identity, 'time', SimpleNamespace(monotonic=real_time.monotonic,
            time=lambda: (result._clock['wall'] - 1) / 1000))
        with pytest.raises(RuntimeIdentityError): result.check()
        monkeypatch.setattr(identity, 'time', real_time)
        with pytest.raises(RuntimeIdentityError): result.check()  # rollback cannot recover
        with r._journal.transaction() as db:
            assert db.execute('SELECT state FROM issuance').fetchall() == [('enrollment_sent',)]
        replay = UnixLaunchReceiptTransport(socket_path=str(tmp_path / 'executor.sock'),
            expected_issuer_uid=os.getuid(), expected_issuer_gid=os.getgid())
        thread = threading.Thread(target=issue)
        try:
            thread.start()
            with pytest.raises(RuntimeIdentityError):
                replay.issue_pair_enrollment(role='executor', authority_public_key=public,
                    own_enrollment=releases[0], manifest_digest=policy['manifest_digest'], timeout_ms=3000)
            thread.join(4)
            assert not thread.is_alive() and len(errors) == 1
        finally:
            replay.close()
    finally:
        transport.close(); r.close()
        for listener in listeners: listener.close()
