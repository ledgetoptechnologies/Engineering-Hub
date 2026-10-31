"""Real listener transfer and journal; synthetic daemon and launch authority."""
import array
import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from engineering_hub import pilot_ownership
from hub_container_executor import launch_supervisor as launch
from hub_container_executor import gate_ledger_handoff as handoff
from tests.test_hub_container_executor_launch_supervisor import short_root
from tests.test_executor_launch_profile import gate_profile
from tests.test_hub_container_executor_runtime_identity import Issuer
from engineering_hub.executor_launch_profile import ExecutorLaunchProfile
from engineering_hub.container_pilot_manifest import Platform
from hub_container_executor.runtime_identity import ExecutorReleaseEnrollment, LaunchReceiptIdentityVerifier
from hub_container_executor.launch_transport import UnixCurrentObservationTransport
from cryptography.hazmat.primitives import serialization


@pytest.fixture
def rig(short_root, monkeypatch):
    if os.geteuid() != 0: pytest.skip('requires disposable root-owned fixture')
    monkeypatch.setattr(pilot_ownership, '_local_filesystem', lambda _: None)
    fixture_root = short_root / 'hub-pilot-fixture'; fixture_root.mkdir(mode=0o711)
    directory = fixture_root / 'gate-control'; directory.mkdir(mode=0o711)
    private = short_root / 'private'; private.mkdir(mode=0o700)
    journal = launch._Journal(str(private / 'journal'), create=True,
                              gate_mode=True, start_mode=True, ledger_mode=True)
    key = Ed25519PrivateKey.generate()
    profile_data = {'synthetic': 'signed-profile'}
    profile_raw = launch._canonical(profile_data)
    profile_digest = 'sha256:' + hashlib.sha256(profile_raw).hexdigest()
    supervisor = object.__new__(launch.LaunchSupervisor)
    supervisor._pid = os.getpid(); supervisor._receipt_served = True; supervisor._issuance_open = False
    supervisor._journal = journal; supervisor._signer = key; supervisor._launch_id = str(uuid4())
    supervisor._container_id = 'a' * 64
    supervisor._consumer = SimpleNamespace(boot=str(uuid4()), pid=123, start=456)
    supervisor._approval_expires_at_ms = int(time.time() * 1000) + 30000
    supervisor._expires = time.monotonic() + 30
    supervisor.release = SimpleNamespace(launch_profile_digest=profile_digest)
    supervisor.profile = SimpleNamespace(gate_ledger_channel='supervisor-listener-v1',
                                         enrollment_value=lambda: profile_data)
    supervisor._mounts = {'gate_channel_directory': SimpleNamespace(source=str(directory))}
    supervisor._source_pins = {'gate_channel_directory': launch._identity(os.lstat(directory))}
    supervisor._ledger_handoff_parent = supervisor._ledger_handoff_listener = None
    supervisor._ledger_handoff_directory = supervisor._ledger_handoff_path_identity = None
    record = {'boot_id': supervisor._consumer.boot, 'container_id': supervisor._container_id,
              'pid': 123, 'start_ticks': 456, 'manifest': profile_digest,
              'approval_expires_at_ms': supervisor._approval_expires_at_ms}
    supervisor._record = lambda: record
    supervisor._guard = lambda: None
    supervisor._validate_live = lambda _: None
    supervisor._handoff_manifest_digest = lambda: 'sha256:' + 'b' * 64
    held = []; supervisor._hold = lambda: held.append(True)
    journal.reserve(supervisor._launch_id, record); journal.state(supervisor._launch_id, 'running', record)
    journal.reserve_issuance(supervisor._launch_id, 'c' * 64)
    journal.issuance_state(supervisor._launch_id, 'sent', 'd' * 64)
    try: yield supervisor, journal, key, directory, profile_raw, held
    finally:
        if supervisor._ledger_handoff_listener is not None: supervisor._ledger_handoff_listener.close()
        if supervisor._ledger_handoff_parent is not None: os.close(supervisor._ledger_handoff_parent)
        journal.close()


def test_listener_transfer_is_signed_and_reserved_before_send(rig):
    supervisor, journal, key, directory, profile_raw, held = rig
    client, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    received = []
    try:
        assert supervisor._issue_ledger_handoff_locked(sender, 'e' * 64, time.monotonic() + 5)
        data, ancillary, flags, _ = client.recvmsg(16384, socket.CMSG_SPACE(4), socket.MSG_CMSG_CLOEXEC)
        assert not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
        for level, kind, value in ancillary:
            assert (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
            descriptors = array.array('i'); descriptors.frombytes(value); received.extend(descriptors)
        while True:
            chunk = client.recv(16384)
            if not chunk: break
            data += chunk
        first = struct.unpack('!I', data[:4])[0]; raw = data[4:4 + first]
        second = struct.unpack('!I', data[4 + first:8 + first])[0]
        assert data[8 + first:] == profile_raw and second == len(profile_raw) and len(received) == 1
        envelope = json.loads(raw); payload = envelope['payload']
        key.public_key().verify(base64.urlsafe_b64decode(envelope['signature'] + '=='),
                                handoff.DOMAIN + launch._canonical(payload))
        fd_info, path_info = os.fstat(received[0]), os.lstat(directory / 'ledger.sock')
        assert (fd_info.st_dev, fd_info.st_ino) == (payload['listener_device'], payload['listener_inode'])
        assert (path_info.st_dev, path_info.st_ino) == (payload['path_device'], payload['path_inode'])
        with journal.transaction() as db:
            row = db.execute('SELECT binding,digest FROM ledger_handoff').fetchone()
        assert json.loads(row[0]) == payload and row[1] == hashlib.sha256(raw).hexdigest() and not held
        with pytest.raises(launch.LaunchSupervisorError):
            supervisor._issue_ledger_handoff_locked(sender, 'f' * 64, time.monotonic() + 5)
    finally:
        for fd in received: os.close(fd)
        client.close(); sender.close()


def test_existing_path_is_preserved_without_reservation(rig):
    supervisor, journal, _, directory, _, held = rig
    existing = directory / 'ledger.sock'; existing.write_bytes(b'preserve')
    client, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with pytest.raises(launch.LaunchSupervisorError):
            supervisor._issue_ledger_handoff_locked(sender, 'e' * 64, time.monotonic() + 5)
        assert existing.read_bytes() == b'preserve' and held
        with journal.transaction() as db:
            assert db.execute('SELECT count(*) FROM ledger_handoff').fetchone() == (0,)
    finally: client.close(); sender.close()


def test_disconnected_recipient_preserves_reservation_and_holds(rig):
    supervisor, journal, _, _, _, held = rig
    client, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client.close()
    try:
        with pytest.raises(launch.LaunchSupervisorError):
            supervisor._issue_ledger_handoff_locked(sender, 'e' * 64, time.monotonic() + 5)
        assert held
        with journal.transaction() as db:
            assert db.execute('SELECT count(*) FROM ledger_handoff').fetchone() == (1,)
    finally: sender.close()


def test_actual_dispatcher_and_receiver_exchange(rig):
    supervisor, journal, key, directory, _, held = rig
    data = gate_profile(str(directory.parent))
    data['gate_completion'] = {'candidate_executable_digest': 'sha256:' + 'a' * 64,
        'rollback_executable_digest': 'sha256:' + 'b' * 64,
        'health_contract': 'engineering-hub.health-json-build-commit.v1'}
    data['gate_ledger_channel'] = 'supervisor-listener-v1'
    profile = ExecutorLaunchProfile.model_validate(data)
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    release = ExecutorReleaseEnrollment('test-issuer', public, 'c' * 40, 'sha256:' + '1' * 64,
        'sha256:' + '2' * 64, 'sha256:' + '3' * 64, Platform(os='linux', architecture='amd64', variant=None), profile.digest())
    clock = SimpleNamespace(wall=time.time(), mono=time.monotonic())
    verifier = LaunchReceiptIdentityVerifier(enrollment=release, issuer_transport=Issuer(release, key, clock,
        mutate=lambda payload: payload.update(authenticated_host_pid=os.getpid())),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
    binding = verifier.launch_binding()
    old_id = supervisor._launch_id
    supervisor._launch_id, supervisor._container_id = binding[0], binding[4]
    supervisor._consumer = SimpleNamespace(boot=binding[1], pid=binding[2], start=binding[3])
    supervisor._consumer_uid, supervisor._consumer_gid = os.getuid(), os.getgid()
    supervisor.release, supervisor.profile = release, profile
    record = {'boot_id': binding[1], 'container_id': binding[4], 'pid': binding[2],
              'start_ticks': binding[3], 'manifest': profile.digest(),
              'approval_expires_at_ms': supervisor._approval_expires_at_ms}
    supervisor._record = lambda: record
    with journal.transaction() as db:
        db.execute('UPDATE launch SET id=?,data=? WHERE id=?', (binding[0], launch._canonical(record).decode(), old_id))
        db.execute('UPDATE issuance SET launch_id=? WHERE launch_id=?', (binding[0], old_id))
    issuer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    issuer_path = str(directory.parent / 'issuer.sock')
    issuer.bind(issuer_path); issuer.listen(1)
    supervisor._listener = issuer; supervisor._mutex = threading.Lock()
    supervisor._observation_thread = None
    errors = []
    def serve():
        try: supervisor._serve_current_until(time.monotonic() + 3)
        except BaseException as exc: errors.append(exc)
    thread = threading.Thread(target=serve); thread.start()
    transport = UnixCurrentObservationTransport(socket_path=issuer_path,
        expected_issuer_uid=os.getuid(), expected_issuer_gid=os.getgid())
    fd = None
    try:
        fd, _, _, _, _, profile_bytes = transport.issue_ledger_listener(verifier=verifier,
            manifest_digest='sha256:' + 'b' * 64, source_directory=str(directory), timeout_ms=2000)
        assert profile_bytes == launch._canonical(profile.enrollment_value()) and not held
        with journal.transaction() as db:
            assert db.execute('SELECT count(*) FROM ledger_handoff').fetchone() == (1,)
    finally:
        if fd is not None: os.close(fd)
        transport.close(); thread.join(4); issuer.close(); verifier.close()
        assert not thread.is_alive() and not errors
