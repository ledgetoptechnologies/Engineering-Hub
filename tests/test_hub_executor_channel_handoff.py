"""Real UDS SCM_RIGHTS transport tests; no Engine or host enrollment."""
import array
import base64
import hashlib
import os
from pathlib import Path
import socket
import struct
import sys
import threading
import time
from uuid import uuid4

import pytest
pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux SCM_RIGHTS required')
pytest.importorskip('fcntl')

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from engineering_hub.container_pilot_manifest import Platform
from hub_container_executor.handoff_identity import (PROTOCOL, REQUEST_PROTOCOL, AUDIENCE,
    SIGNATURE_DOMAIN, COMMIT_PROTOCOL, COMMIT_DOMAIN)
from hub_container_executor.launch_transport import UnixLaunchReceiptTransport, _canonical
from hub_container_executor.runtime_identity import ExecutorReleaseEnrollment, RuntimeIdentityError


def enrollment(key):
    return ExecutorReleaseEnrollment('fixture-issuer', key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw), 'c' * 40,
        'sha256:'+'a'*64, 'sha256:'+'b'*64, 'sha256:'+'c'*64,
        Platform(os='linux', architecture='amd64', variant=None), 'sha256:'+'d'*64)


def test_handoff_real_socket_returns_exact_verified_connected_fd(tmp_path):
    received, original, peer = exercise_handoff(tmp_path)
    try:
        assert os.fstat(received).st_ino == os.fstat(original.fileno()).st_ino
        assert not os.get_inheritable(received)
        channel = socket.socket(fileno=received)
        channel.send(b'hello')
        assert peer.recv(10) == b'hello'
        channel.close()
    finally:
        original.close(); peer.close()


def exercise_handoff(tmp_path, *, failure=None, precommit=None):
    path = str(tmp_path / 'issuer.sock')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path); listener.listen(1)
    original, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    key = Ed25519PrivateKey.generate()
    release = enrollment(key)
    errors = []
    def serve():
        try:
            connection, _ = listener.accept()
            with connection:
                def read(n):
                    value = b''
                    while len(value) < n:
                        part = connection.recv(n-len(value))
                        if not part: raise RuntimeError('unexpected EOF')
                        value += part
                    return value
                size = struct.unpack('!I', read(4))[0]
                import json
                request = json.loads(read(size))
                assert request['protocol'] == REQUEST_PROTOCOL and connection.recv(1) == b''
                info = os.fstat(original.fileno())
                now = int(time.time()*1000)
                payload = dict(protocol=PROTOCOL, audience=AUDIENCE, nonce=request['nonce'], role='executor',
                    launch_id=str(uuid4()), boot_id=str(uuid4()), authenticated_host_pid=os.getpid(),
                    process_start_ticks=1, container_id='e'*64, issuer_id=release.issuer_id,
                    implementation_commit=release.implementation_commit, runnable_digest=release.runnable_digest,
                    platform_manifest_digest=release.platform_manifest_digest, image_config_id=release.image_config_id,
                    platform=release.platform.model_dump(mode='json'), launch_profile_digest=release.launch_profile_digest,
                    manifest_digest='sha256:'+'f'*64, channel_device=info.st_dev, channel_inode=info.st_ino,
                    issued_at_ms=now, expires_at_ms=now+10000)
                if failure == 'role': payload['role'] = 'coordinator'
                if failure == 'nonce': payload['nonce'] = '0'*64
                if failure == 'inode': payload['channel_inode'] += 1
                if failure == 'manifest': payload['manifest_digest'] = 'sha256:' + '0'*64
                if failure == 'expired': payload['expires_at_ms'] = now - 1
                if failure == 'release': payload['implementation_commit'] = '0'*40
                signature = key.sign(SIGNATURE_DOMAIN + _canonical(payload))
                if failure == 'signature': signature = b'0'*64
                envelope = _canonical({'payload': payload, 'signature': base64.urlsafe_b64encode(signature).rstrip(b'=').decode()})
                rights = ([] if failure == 'missing' else
                          [original.fileno()] * (100 if failure == 'truncated' else 2 if failure == 'extra' else 1))
                controls = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', rights))] if rights else []
                connection.sendmsg([struct.pack('!I',len(envelope)) + envelope], controls)
                if precommit is not None:
                    precommit[0].set()
                    assert precommit[1].wait(3)
                if failure != 'lost_commit':
                    commit = dict(protocol=COMMIT_PROTOCOL, nonce=request['nonce'], launch_id=payload['launch_id'],
                                  receipt_digest='sha256:' + hashlib.sha256(envelope).hexdigest())
                    if failure == 'commit_digest': commit['receipt_digest'] = 'sha256:' + '0'*64
                    commit_signature = key.sign(COMMIT_DOMAIN + _canonical(commit))
                    if failure == 'commit_signature': commit_signature = b'0'*64
                    raw = _canonical({'payload': commit, 'signature': base64.urlsafe_b64encode(commit_signature).rstrip(b'=').decode()})
                    connection.sendall(struct.pack('!I', len(raw)) + raw)
                connection.shutdown(socket.SHUT_WR)
        except BrokenPipeError as error:
            if not failure:
                errors.append(error)
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=serve); thread.start()
    transport = UnixLaunchReceiptTransport(socket_path=path, expected_issuer_uid=os.getuid(), expected_issuer_gid=os.getgid())
    try:
        if failure:
            with pytest.raises(RuntimeIdentityError):
                transport.issue_verified_channel(enrollment=release, manifest_digest='sha256:'+'f'*64,
                    max_receipt_lifetime_seconds=30, timeout_ms=2000)
            with pytest.raises(RuntimeIdentityError):
                transport.issue_verified_channel(enrollment=release, manifest_digest='sha256:'+'f'*64,
                    max_receipt_lifetime_seconds=30, timeout_ms=2000)
            return None, original, peer
        fd = transport.issue_verified_channel(enrollment=release, manifest_digest='sha256:'+'f'*64,
            max_receipt_lifetime_seconds=30, timeout_ms=2000)
        return fd, original, peer
    finally:
        transport.close(); thread.join(4); listener.close()
        assert not thread.is_alive() and not errors


@pytest.mark.parametrize('failure', ['role', 'nonce', 'inode', 'manifest', 'expired', 'release', 'signature', 'missing', 'extra', 'truncated', 'lost_commit', 'commit_digest', 'commit_signature'])
def test_handoff_rejects_invalid_receipts_and_never_retries(tmp_path, failure):
    before = len(os.listdir('/proc/self/fd'))
    _, original, peer = exercise_handoff(tmp_path, failure=failure)
    original.close(); peer.close()
    assert len(os.listdir('/proc/self/fd')) == before


def test_fd_remains_quarantined_until_signed_commit(tmp_path):
    sent, release = threading.Event(), threading.Event()
    result, errors = [], []
    def exchange():
        try:
            result.append(exercise_handoff(tmp_path, precommit=(sent, release)))
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=exchange)
    worker.start()
    try:
        assert sent.wait(2)
        assert worker.is_alive() and result == []
        release.set()
        worker.join(4)
        assert not worker.is_alive() and not errors and len(result) == 1
        received, original, peer = result[0]
        os.close(received)
        original.close()
        peer.close()
    finally:
        release.set()
        worker.join(4)
