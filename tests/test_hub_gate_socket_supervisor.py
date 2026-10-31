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
from engineering_hub.executor_launch_profile import ExecutorLaunchProfile
from hub_container_executor.mount_gate_start_enrollment import REQUEST
from tests.test_hub_application_mount_gate import pins
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.test_hub_application_gate_enrollment import vector, raw
from tests.test_executor_launch_profile import gate_profile
from tests.test_hub_container_executor_launch_supervisor import short_root


def v4_policy(supervisor, data):
    profile = supervisor.profile.enrollment_value()
    profile['gate_completion'] = {'candidate_executable_digest': 'sha256:' + 'a' * 64,
        'rollback_executable_digest': 'sha256:' + 'b' * 64,
        'health_contract': gate.COMPLETION_HEALTH_CONTRACT}
    supervisor.profile = ExecutorLaunchProfile.model_validate(profile)
    supervisor.release = replace(supervisor.release, launch_profile_digest=supervisor.profile.digest())
    supervisor._mounts = {mount.role: mount for mount in supervisor.profile.mounts}
    data['supervisor_profile_digest'] = supervisor.profile.digest()
    data.update(schema=gate.COMPLETION_SCHEMA, application_executable_digest='sha256:' + 'a' * 64,
                health_contract=gate.COMPLETION_HEALTH_CONTRACT)


@pytest.fixture
def socket_rig(short_root, monkeypatch):
    if os.geteuid() != 0: pytest.skip('requires disposable root-owned fixture')
    monkeypatch.setattr(pilot_ownership, '_local_filesystem', lambda _: None)
    short_root.chmod(0o711)
    private = short_root / 'private'; private.mkdir(mode=0o700)
    fixture_root = short_root / 'hub-pilot-fixture'; fixture_root.mkdir(mode=0o711)
    directory = fixture_root / 'gate-control'; directory.mkdir(mode=0o711)
    manifest, image, supervisor, data = vector()
    supervisor.profile = ExecutorLaunchProfile.model_validate(gate_profile(str(fixture_root)))
    supervisor.release = replace(supervisor.release, launch_profile_digest=supervisor.profile.digest())
    image = replace(image, image_config={'config': {'Entrypoint': list(gate.STATIC_ENTRYPOINT), 'Cmd': []}})
    signer = Ed25519PrivateKey.generate()
    data.update(schema=gate.SOCKET_SCHEMA, socket_source_directory=str(directory),
                consumer_host_uid=110001, consumer_host_gid=110001,
                supervisor_profile_digest=supervisor.profile.digest(),
                controller_public_key=signer.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex())
    path = private / 'policy'; path.write_bytes(raw(data)); path.chmod(0o600)
    signing_key = private / 'controller-key'
    signing_key.write_bytes(signer.private_bytes(serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw, serialization.NoEncryption())); signing_key.chmod(0o600)
    supervisor._pid, supervisor._mutex = os.getpid(), threading.Lock()
    supervisor._enrollment_path, supervisor._key_path = private / 'supervisor', private / 'key'
    supervisor._mounts = {mount.role: mount for mount in supervisor.profile.mounts}
    supervisor._journal = launch._Journal(str(private / 'journal'), create=True, gate_mode=True, start_mode=True)
    supervisor._launch_id = str(uuid4())
    record = {'fixture': True}
    supervisor._record = lambda: record
    supervisor._journal.reserve(supervisor._launch_id, record)
    supervisor._journal.state(supervisor._launch_id, 'running', record)
    supervisor._validate_live = lambda _: None  # Synthetic executor liveness, not a host claim.
    supervisor._handoff_manifest_digest = lambda: manifest.digest()
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


def test_v4_completion_pins_are_closed_and_socket_preparation_does_not_confirm(socket_rig):
    supervisor, manifest, image, path, directory, _ = socket_rig
    data = json.loads(Path(path).read_bytes())
    v4_policy(supervisor, data)
    Path(path).write_bytes(raw(data))
    enrolled = gate.load_application_gate_enrollment(path, manifest=manifest, image=image, supervisor=supervisor)
    assert enrolled.application_executable_digest == data['application_executable_digest']
    assert enrolled.health_contract == gate.COMPLETION_HEALTH_CONTRACT
    for change in ({'application_executable_digest': 'sha256:' + 'A' * 64},
                   {'application_executable_digest': 'sha256:' + 'b' * 64},
                   {'health_contract': 'http-200'}, {'unexpected': True}):
        Path(path).write_bytes(raw(dict(data, **change)))
        with pytest.raises(gate.ApplicationGateEnrollmentError):
            gate.load_application_gate_enrollment(path, manifest=manifest, image=image, supervisor=supervisor)
    Path(path).write_bytes(raw(data))
    assert supervisor.prepare_gate_socket(manifest=manifest, image=image, enrollment_file=path, timeout_seconds=2) == str(directory)
    assert supervisor._journal._start_confirmed is False


@pytest.mark.parametrize('drift', ['none', 'measurement', 'nonce', 'fence', 'mount'])
def test_post_exec_observation_rechecks_supervisor_measurements(socket_rig, monkeypatch, drift):
    from hub_container_executor.mount_gate_protocol import PROTOCOL, AUDIENCE
    supervisor, manifest, image, path, directory, held = socket_rig
    data = json.loads(Path(path).read_bytes())
    v4_policy(supervisor, data)
    Path(path).write_bytes(raw(data))
    supervisor._gate_enrollment = gate.load_application_gate_enrollment(path, manifest=manifest,
                                                                         image=image, supervisor=supervisor)
    identity = pins(directory)
    identity.update(manifest_digest=manifest.digest(), image_config_id=image.image_config_id,
                    runnable_digest=image.runnable_reference, platform_manifest_digest=image.platform_manifest_digest,
                    implementation_commit=image.source_commit)
    base = {k: v for k, v in identity.items() if k != 'nonce'}
    start = {'protocol': 'engineering-hub.application-gate-start-enrollment.v1',
             'audience': 'engineering-hub.static-application-bootstrap', 'identity': identity,
             'bootstrap_digest': data['bootstrap_digest'], 'issued_at_ms': 100000, 'expires_at_ms': 104000}
    supervisor._journal.reserve_gate_start(supervisor._launch_id, supervisor._record(), base,
                                           data['bootstrap_digest'], start, 'd' * 64)
    decision = dict(identity, protocol=PROTOCOL, audience=AUDIENCE, decision='allow',
                    observed_device=identity['expected_device'], observed_inode=identity['expected_inode'],
                    issued_at_ms=100000, expires_at_ms=104000)
    supervisor._journal.reserve_gate_issuance(supervisor._launch_id, supervisor._record(), identity,
                                              decision, 'e' * 64)
    class Peer:
        def close(self): pass
    peer = Peer(); supervisor._gate_accepted_peer = peer
    checks = []
    monkeypatch.setattr(supervisor, '_gate_socket_check', lambda _: checks.append('socket'))
    monkeypatch.setattr(supervisor, '_gate_peer_engine_check', lambda *_: checks.append('engine'))
    def measure(*_):
        checks.append('measure')
        return {'value': checks.count('measure') if drift == 'measurement' else 1}
    monkeypatch.setattr(launch, '_measure_gate_process', measure)
    supplied = dict(identity)
    if drift == 'nonce': supplied['nonce'] = 'f' * 64
    if drift == 'fence': supplied['fence'] += 1
    if drift == 'mount': supplied['expected_inode'] += 1
    kwargs = dict(peer=peer, pins=supplied, manifest=manifest, image=image, enrollment_file=path, timeout_seconds=2)
    if drift != 'none':
        with pytest.raises(launch.LaunchSupervisorError): supervisor.measure_gate_post_exec(**kwargs)
        assert held
    else:
        result = supervisor.measure_gate_post_exec(**kwargs)
        assert result['value'] == 1 and result['decision_payload_digest'].startswith('sha256:')
        assert not held
    expected_checks = 0 if drift in {'nonce', 'fence', 'mount'} else 2
    assert checks.count('engine') == expected_checks and checks.count('measure') == expected_checks


def test_listener_rejects_wrong_kernel_peer_and_never_reaccepts(socket_rig):
    supervisor, manifest, image, path, directory, held = socket_rig
    assert supervisor.prepare_gate_socket(manifest=manifest, image=image, enrollment_file=path, timeout_seconds=2) == str(directory)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        client.connect(str(directory / 'controller.sock'))  # root, not enrolled UID 110001
        with pytest.raises(launch.LaunchSupervisorError):
            supervisor.accept_gate_channel(manifest=manifest, image=image, enrollment_file=path,
                host_pid=os.getpid(), process_start_ticks=_start(os.getpid()), timeout_seconds=1)
        assert held and supervisor._gate_accept_used
        with pytest.raises(launch.LaunchSupervisorError):
            supervisor.accept_gate_channel(manifest=manifest, image=image, enrollment_file=path,
                host_pid=os.getpid(), process_start_ticks=_start(os.getpid()), timeout_seconds=1)
    finally:
        client.close()


def test_policy_manifest_must_match_committed_runner_before_bind(socket_rig):
    supervisor, manifest, image, path, directory, held = socket_rig
    supervisor._handoff_manifest_digest = lambda: 'sha256:' + '9' * 64
    with pytest.raises(launch.LaunchSupervisorError):
        supervisor.prepare_gate_socket(manifest=manifest, image=image, enrollment_file=path, timeout_seconds=2)
    assert held and not (directory / 'controller.sock').exists()


def test_replaced_socket_is_rejected_without_removing_replacement(socket_rig):
    supervisor, manifest, image, path, directory, held = socket_rig
    supervisor.prepare_gate_socket(manifest=manifest, image=image, enrollment_file=path, timeout_seconds=2)
    socket_path = directory / 'controller.sock'
    socket_path.unlink(); socket_path.write_text('replacement')
    with pytest.raises(launch.LaunchSupervisorError):
        supervisor.accept_gate_channel(manifest=manifest, image=image, enrollment_file=path,
            host_pid=os.getpid(), process_start_ticks=_start(os.getpid()), timeout_seconds=1)
    assert held and socket_path.read_text() == 'replacement'


@pytest.mark.parametrize('fault', ['none', 'v4', 'after_accept', 'during_callback', 'wrong_manifest', 'keyread_mutation',
                                   'daemon_pid', 'daemon_image', 'daemon_restart', 'daemon_changes_in_callback'])
def test_real_peer_credentials_flow_to_start_controller(socket_rig, monkeypatch, tmp_path, fault):
    supervisor, manifest, image, path, directory, held = socket_rig
    if fault == 'v4':
        data = json.loads(Path(path).read_bytes())
        v4_policy(supervisor, data)
        Path(path).write_bytes(raw(data))
    supervisor.prepare_gate_socket(manifest=manifest, image=image, enrollment_file=path, timeout_seconds=2)
    expected = pins(tmp_path)
    ready_read, ready_write = os.pipe(); release_read, release_write = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            os.close(ready_read); os.close(release_write)
            os.setgid(110001); os.setuid(110001)
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
            elif str(self) in {f'/proc/{child}/uid_map', f'/proc/{child}/gid_map'}:
                text = '0 10002 1\n1 100001 65536\n'
            return text
        # Actual credentials and pidfd are real; NSpid and uid/gid maps are synthetic.
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
        assert peer.pid == child and peer.uid == peer.gid == 110001
        expected.update(host_pid=child, process_start_ticks=ticks, manifest_digest=manifest.digest(),
            image_config_id=image.image_config_id, runnable_digest=image.runnable_reference,
            platform_manifest_digest=image.platform_manifest_digest, implementation_commit=image.source_commit)
        changed = []
        class SyntheticEngine:
            def json(self, method, route, deadline):
                assert method == 'GET' and route == '/containers/' + expected['container_id'] + '/json'
                return 200, {'Id': expected['container_id'],
                    'Image': 'sha256:' + '9' * 64 if fault == 'daemon_image' else image.image_config_id,
                    'State': {'Running': True, 'Status': 'running', 'Paused': False,
                        'Restarting': fault == 'daemon_restart', 'Dead': False,
                        'Pid': child + 1 if fault == 'daemon_pid' or changed else child}}
        supervisor._engine = SyntheticEngine()  # Real peer credentials, synthetic daemon response.
        if fault == 'wrong_manifest': expected['manifest_digest'] = 'sha256:' + '9' * 64
        def verify_app(_pins):
            if fault == 'during_callback': os.chmod(directory / 'controller.sock', 0o666)
            if fault == 'daemon_changes_in_callback': changed.append(True)
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
        if fault in ('during_callback', 'wrong_manifest', 'daemon_pid', 'daemon_image', 'daemon_restart',
                     'daemon_changes_in_callback'):
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
