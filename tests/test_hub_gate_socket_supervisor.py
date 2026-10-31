"""Real Unix peer credentials with synthetic supervisor and namespace facts."""
import json
import os
import select
import signal
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from engineering_hub import pilot_ownership
from hub_container_executor import application_gate_enrollment as gate
from hub_container_executor import launch_supervisor as launch
from hub_container_executor.broker_seqpacket import _start
from hub_container_executor.mount_gate_start_enrollment import REQUEST
from tests.test_hub_application_mount_gate import pins
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.test_hub_application_gate_enrollment import vector, raw
from tests.test_hub_container_executor_launch_supervisor import short_root


@pytest.fixture
def socket_rig(short_root, monkeypatch):
    if os.geteuid() != 0: pytest.skip('requires disposable root-owned fixture')
    monkeypatch.setattr(pilot_ownership, '_local_filesystem', lambda _: None)
    short_root.chmod(0o711)
    private = short_root / 'private'; private.mkdir(mode=0o700)
    directory = short_root / 'gate'; directory.mkdir(mode=0o711)
    manifest, image, supervisor, data = vector()
    image = replace(image, image_config={'config': {'Entrypoint': list(gate.STATIC_ENTRYPOINT), 'Cmd': []}})
    signer = Ed25519PrivateKey.generate()
    data.update(schema=gate.SOCKET_SCHEMA, socket_source_directory=str(directory),
                consumer_host_uid=10001, consumer_host_gid=10001,
                controller_public_key=signer.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex())
    path = private / 'policy'; path.write_bytes(raw(data)); path.chmod(0o600)
    signing_key = private / 'controller-key'
    signing_key.write_bytes(signer.private_bytes(serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw, serialization.NoEncryption())); signing_key.chmod(0o600)
    supervisor._pid, supervisor._mutex = os.getpid(), threading.Lock()
    supervisor._enrollment_path, supervisor._key_path = private / 'supervisor', private / 'key'
    supervisor._mounts = {}
    supervisor._journal = launch._Journal(str(private / 'journal'), create=True, gate_mode=True, start_mode=True)
    supervisor._launch_id = str(uuid4())
    record = {'fixture': True}
    supervisor._record = lambda: record
    supervisor._journal.reserve(supervisor._launch_id, record)
    supervisor._journal.state(supervisor._launch_id, 'running', record)
    supervisor._validate_live = lambda _: None  # Synthetic executor liveness, not a host claim.
    held = []; supervisor._hold = lambda: held.append(True)
    supervisor._gate_listener = supervisor._gate_parent = None
    supervisor._gate_directory = supervisor._gate_socket_identity = supervisor._gate_enrollment = None
    supervisor._gate_accepted_channel = supervisor._gate_accepted_peer = None
    supervisor._gate_accept_used = False; supervisor._gate_start_thread = None
    try:
        yield supervisor, manifest, image, str(path), directory, held
    finally:
        for name in ('_gate_accepted_channel', '_gate_accepted_peer', '_gate_listener'):
            value = getattr(supervisor, name, None)
            if value is not None: value.close()
        if supervisor._gate_parent is not None: os.close(supervisor._gate_parent)
        supervisor._journal.close()


def test_v3_socket_enrollment_is_closed_and_rejects_alias(socket_rig):
    supervisor, manifest, image, path, directory, _ = socket_rig
    enrolled = gate.load_application_gate_enrollment(path, manifest=manifest, image=image, supervisor=supervisor)
    assert enrolled.schema == gate.SOCKET_SCHEMA and enrolled.socket_source_directory == str(directory)
    data = json.loads(Path(path).read_bytes()); data['socket_source_directory'] = str(Path(path).parent)
    Path(path).write_bytes(raw(data))
    with pytest.raises(gate.ApplicationGateEnrollmentError):
        gate.load_application_gate_enrollment(path, manifest=manifest, image=image, supervisor=supervisor)


def test_listener_rejects_wrong_kernel_peer_and_never_reaccepts(socket_rig):
    supervisor, manifest, image, path, directory, held = socket_rig
    assert supervisor.prepare_gate_socket(manifest=manifest, image=image, enrollment_file=path, timeout_seconds=2) == str(directory)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        client.connect(str(directory / 'controller.sock'))  # root, not enrolled UID 10001
        with pytest.raises(launch.LaunchSupervisorError):
            supervisor.accept_gate_channel(manifest=manifest, image=image, enrollment_file=path,
                host_pid=os.getpid(), process_start_ticks=_start(os.getpid()), timeout_seconds=1)
        assert held and supervisor._gate_accept_used
        with pytest.raises(launch.LaunchSupervisorError):
            supervisor.accept_gate_channel(manifest=manifest, image=image, enrollment_file=path,
                host_pid=os.getpid(), process_start_ticks=_start(os.getpid()), timeout_seconds=1)
    finally:
        client.close()


def test_replaced_socket_is_rejected_without_removing_replacement(socket_rig):
    supervisor, manifest, image, path, directory, held = socket_rig
    supervisor.prepare_gate_socket(manifest=manifest, image=image, enrollment_file=path, timeout_seconds=2)
    socket_path = directory / 'controller.sock'
    socket_path.unlink(); socket_path.write_text('replacement')
    with pytest.raises(launch.LaunchSupervisorError):
        supervisor.accept_gate_channel(manifest=manifest, image=image, enrollment_file=path,
            host_pid=os.getpid(), process_start_ticks=_start(os.getpid()), timeout_seconds=1)
    assert held and socket_path.read_text() == 'replacement'


@pytest.mark.parametrize('fault', ['none', 'after_accept', 'during_callback', 'wrong_manifest', 'keyread_mutation'])
def test_real_peer_credentials_flow_to_start_controller(socket_rig, monkeypatch, tmp_path, fault):
    supervisor, manifest, image, path, directory, held = socket_rig
    supervisor.prepare_gate_socket(manifest=manifest, image=image, enrollment_file=path, timeout_seconds=2)
    expected = pins(tmp_path)
    ready_read, ready_write = os.pipe(); release_read, release_write = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            os.close(ready_read); os.close(release_write)
            os.setgid(10001); os.setuid(10001)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            client.connect(str(directory / 'controller.sock'))
            request = json.dumps({'protocol': REQUEST, 'nonce': expected['nonce']}, sort_keys=True,
                                 separators=(',', ':')).encode()
            client.send(request)
            os.write(ready_write, b'C')
            if not client.recv(8192): raise RuntimeError('no signed enrollment')
            os.write(ready_write, b'R')
            os.read(release_read, 1)
        except BaseException:
            try: os.write(ready_write, b'E')
            except OSError: pass
        os._exit(0)
    os.close(ready_write); os.close(release_read)
    try:
        assert select.select([ready_read], [], [], 5)[0] and os.read(ready_read, 1) == b'C'
        ticks = _start(child)
        original = Path.read_text
        def synthetic_nspid(self, *args, **kwargs):
            text = original(self, *args, **kwargs)
            if str(self) == f'/proc/{child}/status':
                text = '\n'.join(('NSpid:\t' + str(child) + '\t1') if line.startswith('NSpid:')
                                 else line for line in text.splitlines()) + '\n'
            return text
        # Actual credential/pidfd and UID maps are real; NSpid=1 is synthetic.
        monkeypatch.setattr(Path, 'read_text', synthetic_nspid)
        checks = []
        def verify_live(_deadline):
            checks.append(True)
            if fault == 'after_accept' and len(checks) == 2:
                os.chmod(directory / 'controller.sock', 0o666)
        supervisor._validate_live = verify_live
        if fault == 'after_accept':
            with pytest.raises(launch.LaunchSupervisorError):
                supervisor.accept_gate_channel(manifest=manifest, image=image, enrollment_file=path,
                    host_pid=child, process_start_ticks=ticks, timeout_seconds=2)
            assert len(checks) == 2 and held
            return
        channel, peer = supervisor.accept_gate_channel(manifest=manifest, image=image, enrollment_file=path,
            host_pid=child, process_start_ticks=ticks, timeout_seconds=2)
        assert peer.pid == child and peer.uid == peer.gid == 10001
        expected.update(host_pid=child, process_start_ticks=ticks, manifest_digest=manifest.digest(),
            image_config_id=image.image_config_id, runnable_digest=image.runnable_reference,
            platform_manifest_digest=image.platform_manifest_digest, implementation_commit=image.source_commit)
        if fault == 'wrong_manifest': expected['manifest_digest'] = 'sha256:' + '9' * 64
        def verify_app(_pins):
            if fault == 'during_callback': os.chmod(directory / 'controller.sock', 0o666)
            return True
        kwargs = dict(channel=channel, peer=peer,
            expected_without_nonce={k: v for k, v in expected.items() if k != 'nonce'},
            manifest=manifest, image=image, enrollment_file=path,
            controller_key_file=str(Path(path).parent / 'controller-key'), verify_live=verify_app,
            timeout_seconds=2)
        if fault == 'keyread_mutation':
            original_root_file = launch._root_file
            def mutate_during_keyread(file_path, maximum):
                if str(file_path) == kwargs['controller_key_file']:
                    kwargs['expected_without_nonce']['manifest_digest'] = 'sha256:' + '9' * 64
                return original_root_file(file_path, maximum)
            monkeypatch.setattr(launch, '_root_file', mutate_during_keyread)
        if fault in ('during_callback', 'wrong_manifest'):
            with pytest.raises(launch.LaunchSupervisorError):
                supervisor.serve_one_gate_start_enrollment(**kwargs)
            with supervisor._journal.transaction() as db:
                assert db.execute('SELECT count(*) FROM gate_start').fetchone() == (0,)
            assert held
            channel.close()
            return
        result = supervisor.serve_one_gate_start_enrollment(**kwargs)
        assert result == {'identity': expected, 'delivery': 'sent_unconfirmed'} and not held
        assert select.select([ready_read], [], [], 2)[0] and os.read(ready_read, 1) == b'R'
        with supervisor._journal.transaction() as db:
            assert db.execute('SELECT count(*) FROM gate_start').fetchone() == (1,)
    finally:
        os.close(ready_read); os.close(release_write)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            waited, _ = os.waitpid(child, os.WNOHANG)
            if waited == child: break
            time.sleep(.01)
        else:
            try: os.kill(child, signal.SIGKILL)
            except ProcessLookupError: pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                waited, _ = os.waitpid(child, os.WNOHANG)
                if waited == child: break
                time.sleep(.01)
            else: pytest.fail('owned peer did not exit')
