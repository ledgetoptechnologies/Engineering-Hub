"""Real Linux UDS/SCM_RIGHTS, synthetic independently signed pair authority."""
import array
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
from uuid import uuid4

import pytest
pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux SCM_RIGHTS required')
pytest.importorskip('fcntl')
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from hub_container_executor.handoff_identity import (
    PAIR_EXECUTOR_PROTOCOL, PAIR_COORDINATOR_PROTOCOL, PAIR_EXECUTOR_REQUEST_PROTOCOL,
    PAIR_COORDINATOR_REQUEST_PROTOCOL, PAIR_EXECUTOR_DOMAIN, PAIR_COORDINATOR_DOMAIN,
    PAIR_COMMIT_PROTOCOL, PAIR_COMMIT_DOMAIN, AUDIENCE, COORDINATOR_AUDIENCE)
from hub_container_executor.launch_transport import UnixLaunchReceiptTransport, _canonical
from hub_container_executor.runtime_identity import RuntimeIdentityError
from tests.test_hub_executor_channel_handoff import enrollment


def public(key):
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def sign(key, domain, payload):
    return {'payload': payload, 'signature': base64.urlsafe_b64encode(
        key.sign(domain + _canonical(payload))).rstrip(b'=').decode()}


def exercise(tmp_path, *, role='executor', failure=None, paused=None):
    path = str(tmp_path / 'pair.sock')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    keys = {name: Ed25519PrivateKey.generate() for name in ('executor', 'coordinator', 'pair')}
    releases = {name: enrollment(keys[name]) for name in ('executor', 'coordinator')}
    pair_id, executor_id, coordinator_id, boot_id = (str(uuid4()) for _ in range(4))
    manifest = 'sha256:' + 'f' * 64
    errors = []
    def serve():
        try:
            connection, _ = listener.accept()
            with connection:
                def read(n):
                    data = b''
                    while len(data) < n:
                        chunk = connection.recv(n - len(data))
                        if not chunk:
                            raise RuntimeError('unexpected EOF')
                        data += chunk
                    return data
                request = json.loads(read(struct.unpack('!I', read(4))[0]))
                assert request['protocol'] == (PAIR_EXECUTOR_REQUEST_PROTOCOL if role == 'executor' else PAIR_COORDINATOR_REQUEST_PROTOCOL)
                assert connection.recv(1) == b''
                now = int(time.time() * 1000)
                envelopes = {}
                for name, channel, launch, peer in (('executor', left, executor_id, coordinator_id),
                                                     ('coordinator', right, coordinator_id, executor_id)):
                    item = releases[name]
                    info = os.fstat(channel.fileno())
                    payload = dict(protocol=PAIR_EXECUTOR_PROTOCOL if name == 'executor' else PAIR_COORDINATOR_PROTOCOL,
                        audience=AUDIENCE if name == 'executor' else COORDINATOR_AUDIENCE,
                        nonce=request['nonce'] if name == role else 'a' * 64, role=name,
                        pair_id=pair_id, launch_id=launch, peer_launch_id=peer, boot_id=boot_id,
                        authenticated_host_pid=101 if name == 'executor' else 102,
                        process_start_ticks=1, container_id=('e' if name == 'executor' else 'c') * 64,
                        issuer_id=item.issuer_id, implementation_commit=item.implementation_commit,
                        runnable_digest=item.runnable_digest, platform_manifest_digest=item.platform_manifest_digest,
                        image_config_id=item.image_config_id, platform=item.platform.model_dump(mode='json'),
                        launch_profile_digest=item.launch_profile_digest, manifest_digest=manifest,
                        channel_device=info.st_dev, channel_inode=info.st_ino,
                        issued_at_ms=now, expires_at_ms=now + 10000)
                    if failure == 'peer_launch' and name != role:
                        payload['peer_launch_id'] = str(uuid4())
                    if failure == 'peer_release' and name != role:
                        payload['image_config_id'] = 'sha256:' + '0' * 64
                    if failure == 'peer_manifest' and name != role:
                        payload['manifest_digest'] = 'sha256:' + '0' * 64
                    if failure == 'own_inode' and name == role:
                        payload['channel_inode'] += 1
                    if name == 'coordinator' and failure == 'same_process':
                        payload['authenticated_host_pid'] = 101
                    if name == 'coordinator' and failure == 'same_container':
                        payload['container_id'] = 'e' * 64
                    if name == 'coordinator' and failure == 'same_endpoint':
                        other = os.fstat(left.fileno())
                        payload['channel_device'], payload['channel_inode'] = other.st_dev, other.st_ino
                    if name != role and failure == 'peer_monotonic_expired':
                        payload['expires_at_ms'] = now + 1000
                    domain = PAIR_EXECUTOR_DOMAIN if name == 'executor' else PAIR_COORDINATOR_DOMAIN
                    envelopes[name] = sign(keys[name], domain, payload)
                if failure == 'peer_signature':
                    other = 'coordinator' if role == 'executor' else 'executor'
                    envelopes[other]['signature'] = 'A' * 86
                own = _canonical(envelopes[role])
                own_channel = left if role == 'executor' else right
                connection.sendmsg([struct.pack('!I', len(own)) + own], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                    array.array('i', [own_channel.fileno()]))])
                if paused:
                    paused[0].set()
                    assert paused[1].wait(3)
                if failure != 'missing_commit':
                    payload = {'protocol': PAIR_COMMIT_PROTOCOL, 'pair_id': pair_id, 'manifest_digest': manifest,
                        'executor_launch_id': executor_id, 'coordinator_launch_id': coordinator_id,
                        'executor_receipt': envelopes['executor'], 'coordinator_receipt': envelopes['coordinator'],
                        'executor_receipt_digest': 'sha256:' + hashlib.sha256(_canonical(envelopes['executor'])).hexdigest(),
                        'coordinator_receipt_digest': 'sha256:' + hashlib.sha256(_canonical(envelopes['coordinator'])).hexdigest()}
                    if failure == 'pair_id': payload['pair_id'] = str(uuid4())
                    if failure == 'peer_digest': payload[('coordinator' if role == 'executor' else 'executor') + '_receipt_digest'] = 'sha256:' + '0' * 64
                    commit = sign(keys['pair'], PAIR_COMMIT_DOMAIN, payload)
                    if failure == 'pair_signature': commit['signature'] = 'A' * 86
                    raw = _canonical(commit)
                    connection.sendall(struct.pack('!I', len(raw)) + raw)
                connection.shutdown(socket.SHUT_WR)
        except BrokenPipeError as exc:
            if not failure: errors.append(exc)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=serve)
    thread.start()
    transport = UnixLaunchReceiptTransport(socket_path=path, expected_issuer_uid=os.getuid(), expected_issuer_gid=os.getgid())
    try:
        kwargs = dict(role=role, pair_id=pair_id, manifest_digest=manifest, executor_launch_id=executor_id,
            coordinator_launch_id=coordinator_id, executor_enrollment=releases['executor'],
            coordinator_enrollment=releases['coordinator'], pair_authority_public_key=public(keys['pair']),
            max_receipt_lifetime_seconds=30, timeout_ms=2000)
        if failure:
            with pytest.raises(RuntimeIdentityError):
                transport.issue_verified_pair_channel(**kwargs)
            with pytest.raises(RuntimeIdentityError):
                transport.issue_verified_pair_channel(**kwargs)
            return None, left, right
        return transport.issue_verified_pair_channel(**kwargs), left, right
    finally:
        transport.close()
        thread.join(4)
        listener.close()
        assert not thread.is_alive() and not errors


@pytest.mark.parametrize('role', ['executor', 'coordinator'])
def test_real_pair_receipts_verify_both_roles_before_fd_release(tmp_path, role):
    fd, left, right = exercise(tmp_path, role=role)
    try:
        expected = left if role == 'executor' else right
        assert os.fstat(fd).st_ino == os.fstat(expected.fileno()).st_ino
        assert not os.get_inheritable(fd)
    finally:
        os.close(fd)
        left.close()
        right.close()


@pytest.mark.parametrize('role', ['executor', 'coordinator'])
@pytest.mark.parametrize('failure', ['peer_launch', 'peer_release', 'peer_manifest', 'peer_signature',
    'own_inode', 'missing_commit', 'pair_id', 'peer_digest', 'pair_signature',
    'same_process', 'same_container', 'same_endpoint'])
def test_pair_bad_peer_or_commit_never_releases_fd(tmp_path, role, failure):
    before = len(os.listdir('/proc/self/fd'))
    _, left, right = exercise(tmp_path, role=role, failure=failure)
    left.close()
    right.close()
    assert len(os.listdir('/proc/self/fd')) == before


def test_pair_fd_quarantined_until_commit(tmp_path):
    sent, release = threading.Event(), threading.Event()
    result, errors = [], []
    def run():
        try:
            result.append(exercise(tmp_path, paused=(sent, release)))
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert sent.wait(2) and worker.is_alive() and result == []
        release.set()
        worker.join(4)
        assert not worker.is_alive() and not errors
        fd, left, right = result[0]
        os.close(fd)
        left.close()
        right.close()
    finally:
        release.set()
        worker.join(4)


def test_peer_expiry_uses_exchange_monotonic_ceiling(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hub_container_executor import handoff_identity
    monkeypatch.setattr(handoff_identity, 'time', SimpleNamespace(
        time=time.time, monotonic=lambda: time.monotonic() + 2))
    _, left, right = exercise(tmp_path, failure='peer_monotonic_expired')
    left.close()
    right.close()
