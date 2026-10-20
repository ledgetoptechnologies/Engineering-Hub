"""Explicit synthetic launcher/issuer tests, not an installed trust authority.

Real Linux pidfds, /proc start identities, Unix peer credentials and raw HTTP
framing are exercised. Engine effects, container namespaces/profile and fixture
build approval are synthetic; no Docker socket, host mount, real signing key,
service, image or permission change outside disposable fixtures is used.
"""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import http.server
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

if sys.platform != 'linux':
    pytest.skip('Linux root-owned launcher, pidfd and kernel peer tests', allow_module_level=True)

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from engineering_hub.container_pilot_manifest import Platform
from hub_container_executor import launch_supervisor as launch
from hub_container_executor.launch_transport import UnixLaunchReceiptTransport
from hub_container_executor.runtime_identity import ExecutorReleaseEnrollment, LaunchReceiptIdentityVerifier
from tests.test_hub_container_executor_daemon_backend import verified_image, oci_archive


def profile_data(root='/operator/hub-pilot-synthetic'):
    mounts = []
    for role, target in launch._TARGETS.items():
        source = ('/run/test-docker.sock' if role == 'engine_socket' else
                  '/run/test-issuer/issuer.sock' if role == 'issuer_socket' else root + '/' + role)
        kind = 'socket' if role.endswith('_socket') else 'file' if role == 'config' else 'directory'
        mounts.append(dict(role=role, source=source, target=source if role == 'generations' else target,
            kind=kind, read_only=role not in {'state', 'generations'},
            uid=0 if role == 'issuer_socket' else 110001, gid=110001,
            mode=0o660 if kind == 'socket' else 0o600 if kind == 'file' else 0o700,
            content_digest='sha256:' + 'f' * 64 if kind == 'file' else None))
    return dict(user='10001:10001', supplementary_groups=[],
        entrypoint=['/opt/venv/bin/python', '-I', '-m', 'hub_container_executor.installed_runner'],
        command=['--config', '/operator/runner.json'], environment=['PATH=/usr/bin:/bin'],
        fixture_root=root, memory_bytes=128 * 1024**2, cpu_millicores=250, pids_limit=32,
        tmpfs_bytes=8 * 1024**2, mounts=mounts,
        runner_file='/opt/venv/lib/python3.12/site-packages/hub_container_executor/installed_runner.py',
        runner_digest='sha256:' + 'e' * 64)


def test_closed_executor_profile_has_exact_runner_roles_samepath_generations_and_config_digest():
    data = profile_data()
    profile = launch.ExecutorLaunchProfile.model_validate(data)
    assert profile.digest().startswith('sha256:')
    assert next(item for item in profile.mounts if item.role == 'generations').source == (
        next(item for item in profile.mounts if item.role == 'generations').target)
    data['mounts'][2]['content_digest'] = 'sha256:' + 'a' * 64
    assert launch.ExecutorLaunchProfile.model_validate(data).digest() != profile.digest()


@pytest.mark.parametrize('mutation', ['user', 'shell', 'no-isolation', 'arbitrary-module', 'args',
    'token-env', 'extra-mount', 'rw-config', 'wrong-generation-alias', 'shared-generation',
    'huge-memory', 'no-runner-pin', 'extra-field'])
def test_executor_profile_rejects_unapproved_runtime_authority(mutation):
    data = profile_data()
    if mutation == 'user': data['user'] = '0:0'
    elif mutation == 'shell': data['entrypoint'] = ['/bin/sh', '-c', 'anything']
    elif mutation == 'no-isolation': data['entrypoint'].remove('-I')
    elif mutation == 'arbitrary-module': data['entrypoint'][-1] = 'some.other.module'
    elif mutation == 'args': data['command'] += ['--anything']
    elif mutation == 'token-env': data['environment'] += ['CODEX_TOKEN=not-a-real-token']
    elif mutation == 'extra-mount': data['mounts'] += [deepcopy(data['mounts'][0])]
    elif mutation == 'rw-config': data['mounts'][2]['read_only'] = False
    elif mutation == 'wrong-generation-alias': data['mounts'][4]['target'] = '/alias/generations'
    elif mutation == 'shared-generation':
        data['mounts'][4]['source'] = data['mounts'][4]['target'] = '/mnt/ViewerStaging'
    elif mutation == 'huge-memory': data['memory_bytes'] = 1024**3
    elif mutation == 'no-runner-pin': data['runner_digest'] = ''
    else: data['privileged'] = True
    with pytest.raises((ValueError, RuntimeError)):
        launch.ExecutorLaunchProfile.model_validate(data)


def test_real_kernel_pidfd_identity_denies_changed_start_and_dead_process():
    live = launch._Process(os.getpid())
    try:
        live.check()
        with pytest.raises(RuntimeError):
            launch._Process(os.getpid(), expected_start=live.start + 1)
    finally:
        live.close()
    child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(20)'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    retained = launch._Process(child.pid)
    try:
        child.terminate()  # Only this test's exact synthetic subprocess.
        child.wait(timeout=5)
        with pytest.raises(RuntimeError):
            retained.check()
    finally:
        retained.close()
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


@contextmanager
def fake_engine_http(root, *, image=None, blobs=None, rootless=True):
    """Real Unix HTTP transport/kernel peer, FAKE Engine metadata/effects."""
    requests = []
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args): pass
        def do_GET(self):
            requests.append(self.path)
            if self.path.endswith('/info'):
                value = launch._canonical({'ID': 'test-daemon', 'OSType': 'linux', 'Architecture': 'amd64',
                                           'SecurityOptions': ['name=rootless'] if rootless else []})
            elif self.path.endswith('/json') and image is not None:
                value = launch._canonical({'Id': image.image_config_id, 'Config': image.image_config['config']})
            elif self.path.endswith('/get') and image is not None:
                value = oci_archive(image, blobs)
            else:
                value = b'{}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(value)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(value)
    path = root / 'fake-engine.sock'
    server = socketserver.UnixStreamServer(str(path), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    # This root-owned fake HTTP server is NOT the installed rootless daemon.
    # Bypass only the independently enrolled UID precondition for this fixture;
    # actual socket peer/start/pidfd authentication remains exercised below.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(launch, '_rootless_daemon_uid', lambda uid: None)
        pin = launch._DaemonPin(str(path), os.getpid(), launch._proc_start(os.getpid()),
                                os.getuid(), os.getgid(), 'test-daemon', '1.54')
        try:
            yield pin, requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.fixture
def short_root():
    with tempfile.TemporaryDirectory(prefix='eh-launch-', dir='/tmp') as directory:
        yield Path(directory)


def test_actual_unix_http_peer_and_original_oci_metadata_chain(short_root):
    _, image, blobs = verified_image()
    with fake_engine_http(short_root, image=image, blobs=blobs) as (pin, requests):
        engine = launch._UnixEngine(pin)
        try:
            defaults = engine.image(image, time.monotonic() + 5)
            assert defaults == image.image_config['config']
            assert requests == ['/v1.54/info', '/v1.54/images/' + image.runnable_reference + '/json',
                                '/v1.54/images/' + image.runnable_reference + '/get']
        finally:
            engine.close()


def test_real_unix_engine_wrong_peer_gid_and_socket_replacement_deny_before_http(short_root):
    with fake_engine_http(short_root) as (pin, requests):
        altered = launch._DaemonPin(pin.socket_path, pin.pid, pin.start_ticks, pin.uid, pin.gid + 1,
                                    pin.daemon_id, pin.api_version)
        engine = launch._UnixEngine(altered)
        try:
            with pytest.raises(RuntimeError):
                engine.json('GET', '/info', time.monotonic() + 1)
            assert requests == []
        finally:
            engine.close()
        engine = launch._UnixEngine(pin)
        original = short_root / 'original-engine.sock'
        Path(pin.socket_path).rename(original)
        replacement = socket.socket(socket.AF_UNIX)
        try:
            replacement.bind(pin.socket_path)
            with pytest.raises(RuntimeError):
                engine.json('GET', '/info', time.monotonic() + 1)
            assert requests == []
        finally:
            engine.close()
            replacement.close()


@pytest.fixture
def root_journal(short_root, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip('Root-owned fixture journal requires explicit UID0 test container')
    import engineering_hub.pilot_ownership as ownership
    # The fixture lives on disposable tmpfs/overlay. This explicit UNIT-only
    # substitution exercises SQLite ownership/protocol, NOT durable-filesystem acceptance.
    observations = []
    monkeypatch.setattr(ownership, '_local_filesystem', lambda fd: observations.append(os.fstat(fd).st_ino))
    journal = launch._Journal(str(short_root / 'journal.sqlite3'), create=True)
    try:
        yield journal
        assert observations
    finally:
        journal.close()


def test_root_private_journal_reserves_before_effect_and_never_adopts_restart(root_journal):
    journal = root_journal
    identity = str(uuid4())
    journal.reserve(identity, {'test': 'no-effect'})
    with pytest.raises(RuntimeError):
        journal.reserve(str(uuid4()), {})
    journal.state(identity, 'running', {'container_id': 'a' * 64})
    journal.reserve_issuance(identity, 'b' * 64)
    with pytest.raises(Exception):
        journal.reserve_issuance(identity, 'c' * 64)
    journal.issuance_state(identity, 'held', None)
    path = str(journal.path)
    journal.close()
    reopened = launch._Journal(path, create=False)
    try:
        with reopened.transaction() as db:
            assert db.execute('SELECT state FROM issuance').fetchone() == ('held',)
        with pytest.raises(RuntimeError):
            reopened.reserve(str(uuid4()), {})
    finally:
        reopened.close()


@pytest.fixture
def synthetic_issuer(short_root, root_journal):
    """Real peer/PID/framing/signature slice; namespace/Engine approval FAKE.

    Bypass constructor only in this clearly marked fixture. No production key,
    root enrollment, actual container or successful launch attestation is claimed.
    The positive fixture's consumer is this process, NOT a real container init.
    """
    authority = launch.LaunchSupervisor.__new__(launch.LaunchSupervisor)
    authority._pid, authority._mutex = os.getpid(), threading.Lock()
    authority._journal, authority._engine = root_journal, None
    authority._consumer = launch._Process(os.getpid())
    authority._consumer_uid, authority._consumer_gid = os.getuid(), os.getgid()
    authority._listener = socket.socket(socket.AF_UNIX)
    path = short_root / 'issuer.sock'
    authority._listener.bind(str(path))
    path.chmod(0o600)
    authority._listener.listen(1)
    authority._socket_parent = authority._socket_identity = None
    authority._socket_path = path
    authority._launch_id, authority._container_id = str(uuid4()), 'd' * 64
    authority._closed, authority._held, authority._issuance_open = False, False, True
    authority._expires = time.monotonic() + 30
    authority._request_ms = 1000
    authority._signer = Ed25519PrivateKey.generate()  # Synthetic TEST key only.
    authority.release = ExecutorReleaseEnrollment('synthetic-issuer',
        authority._signer.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
        'c' * 40, 'sha256:' + '1' * 64, 'sha256:' + '2' * 64, 'sha256:' + '3' * 64,
        Platform(os='linux', architecture='amd64', variant=None), 'sha256:' + '4' * 64)
    checks = []
    authority._guard = lambda **kwargs: launch._require(not authority._closed and (kwargs.get('allow_held') or not authority._held))
    def fake_container_validation(deadline, **kwargs):
        checks.append(True)
        authority._consumer.check()
        launch._remaining(deadline)
    authority._validate_live = fake_container_validation
    root_journal.reserve(authority._launch_id, {'synthetic_fixture': True})
    root_journal.state(authority._launch_id, 'running', authority._record())
    try:
        yield authority, path, checks
    finally:
        authority.close()


def serve_fixture(authority):
    result = []
    def run():
        try:
            result.append(authority.serve_one_receipt())
        except BaseException as error:
            result.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_actual_direct_peer_exchange_is_consumed_by_existing_signature_verifier(synthetic_issuer):
    authority, path, checks = synthetic_issuer
    thread, result = serve_fixture(authority)
    transport = UnixLaunchReceiptTransport(socket_path=str(path), expected_issuer_uid=os.getuid(),
                                          expected_issuer_gid=os.getgid())
    try:
        verified = LaunchReceiptIdentityVerifier(enrollment=authority.release, issuer_transport=transport,
            max_receipt_lifetime_seconds=30, exchange_timeout_ms=1000)
        try:
            assert verified.implementation_commit() == 'c' * 40
        finally:
            verified.close()
    finally:
        transport.close()
        thread.join(timeout=5)
    assert not thread.is_alive() and len(result) == 1 and isinstance(result[0], str)
    assert len(checks) == 4 and not authority._issuance_open
    with authority._journal.transaction() as db:
        assert db.execute('SELECT state FROM issuance').fetchone() == ('sent',)
    with pytest.raises(RuntimeError):
        authority.serve_one_receipt()


@pytest.mark.parametrize('mutation', ['extra-selector', 'trailing', 'second-frame', 'no-halfclose', 'oversize'])
def test_issuer_rejects_selectors_trailing_frames_and_missing_eof_before_issuance(synthetic_issuer, mutation):
    authority, path, _ = synthetic_issuer
    authority._request_ms = 150
    request = {'protocol': launch.REQUEST_PROTOCOL, 'nonce': 'a' * 64}
    if mutation == 'extra-selector': request['container_id'] = 'f' * 64
    raw = launch._canonical(request)
    framed = struct.pack('!I', len(raw)) + raw
    if mutation == 'oversize': framed = struct.pack('!I', 257) + b'a' * 257
    if mutation == 'trailing': framed += b'x'
    if mutation == 'second-frame': framed += framed
    thread, result = serve_fixture(authority)
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(2)
        peer.connect(str(path))
        peer.sendall(framed)
        if mutation != 'no-halfclose': peer.shutdown(socket.SHUT_WR)
        try:
            assert peer.recv(1) == b''
        except ConnectionResetError:
            pass
    thread.join(timeout=3)
    assert not thread.is_alive() and isinstance(result[0], launch.LaunchSupervisorError)
    with authority._journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM issuance').fetchone() == (0,)
        assert db.execute('SELECT state FROM launch').fetchone() == ('held',)


def test_real_same_uid_forked_consumer_cannot_claim_retained_init_pid(synthetic_issuer):
    authority, path, _ = synthetic_issuer
    thread, result = serve_fixture(authority)
    child = """
import socket,sys
with socket.socket(socket.AF_UNIX) as peer:
    peer.settimeout(2)
    peer.connect(sys.argv[1])
    try: peer.recv(1)
    except ConnectionResetError: pass
"""
    subprocess.run([sys.executable, '-I', '-c', child, str(path)], check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)
    thread.join(timeout=3)
    assert not thread.is_alive() and isinstance(result[0], launch.LaunchSupervisorError)
    with authority._journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM issuance').fetchone() == (0,)


def test_dead_retained_consumer_denies_before_receipt(synthetic_issuer):
    authority, path, _ = synthetic_issuer
    authority._consumer.close()
    thread, result = serve_fixture(authority)
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(2)
        peer.connect(str(path))
        peer.shutdown(socket.SHUT_WR)
        assert peer.recv(1) == b''
    thread.join(timeout=3)
    assert not thread.is_alive() and isinstance(result[0], launch.LaunchSupervisorError)
    with authority._journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM issuance').fetchone() == (0,)


def synthetic_launcher(journal, *, failure=None, image_kind='config'):
    """Exercise real launch state machine with FAKE Engine effects only.

    Kernel namespace/cgroup/image-enrollment proofs are separately gated in the
    real implementation; this fixture explicitly substitutes those prerequisites.
    It never starts a container, installs a runner or creates trusted enrollment.
    """
    authority = launch.LaunchSupervisor.__new__(launch.LaunchSupervisor)
    authority._pid, authority._mutex = os.getpid(), threading.Lock()
    authority._journal = journal
    authority._consumer = authority._listener = authority._socket_parent = None
    authority._socket_identity = authority._launch_id = authority._container_id = None
    authority._closed, authority._held, authority._issuance_open = False, False, False
    authority._source_pins = {}
    authority.profile = launch.ExecutorLaunchProfile.model_validate(profile_data())
    authority.release = SimpleNamespace(launch_profile_digest=authority.profile.digest())
    authority.image = SimpleNamespace(runnable_reference='sha256:' + '1' * 64,
        image_config_id='sha256:' + '2' * 64, platform_manifest_digest='sha256:' + '3' * 64)
    authority._launch_ms, authority._ttl = 1000, 30
    events = []
    authority._guard = lambda **kwargs: launch._require(not authority._closed and (kwargs.get('allow_held') or not authority._held))
    authority._bind_issuer = lambda: events.append('bind-fixture')
    authority._pin_sources = lambda: events.append('source-fixture')
    class FakeEngine:
        namespace = launch._identity(os.stat('/proc/self/ns/pid'))
        created = False
        running = False
        effect_calls = []
        def image(self, image, deadline):
            events.append('image-fixture')
            return dict(Entrypoint=list(authority.profile.entrypoint), Cmd=list(authority.profile.command),
                        Env=list(authority.profile.environment))
        def json(self, method, route, deadline, *, body=None, statuses=(200,)):
            self.effect_calls.append((method, route))
            if method == 'POST' and route.startswith('/containers/create?'):
                with journal.transaction() as db:
                    assert db.execute('SELECT state FROM launch').fetchone() == ('create_reserved',)
                assert body == authority._create_body()
                assert not body['HostConfig']['Init'] and not body['HostConfig']['Privileged']
                self.created = True
                if failure == 'ambiguous-create': raise TimeoutError('synthetic lost create response')
                return 201, {'Id': 'a' * 64, 'Warnings': []}
            if method == 'POST' and route.endswith('/start'):
                with journal.transaction() as db:
                    assert db.execute('SELECT state FROM launch').fetchone() == ('start_reserved',)
                self.running = True
                if failure == 'ambiguous-start': raise TimeoutError('synthetic lost start response')
                return 204, None
            assert method == 'GET' and route == '/containers/' + 'a' * 64 + '/json'
            original = authority._create_body()
            host = deepcopy(original.pop('HostConfig'))
            if failure == 'privileged-inspect': host['Privileged'] = True
            if failure == 'wrong-tmpfs': host['Tmpfs']['/tmp'] = 'rw,size=1024'
            if failure == 'tag-config': original['Image'] = 'unapproved:latest'
            mounts = [{'Type': 'bind', 'Source': item.source, 'Destination': item.target,
                       'RW': not item.read_only, 'Propagation': 'rprivate'} for item in authority.profile.mounts]
            if failure == 'invented-tmpfs-entry': mounts.append({'Type': 'tmpfs', 'Destination': '/tmp'})
            image_id = {'config': authority.image.image_config_id,
                        'manifest': authority.image.platform_manifest_digest,
                        'index': authority.image.runnable_reference}[image_kind]
            return 200, {'Id': 'a' * 64, 'Image': image_id,
                'Config': original, 'HostConfig': host, 'Mounts': mounts,
                'State': {'Running': self.running, 'Status': 'running' if self.running else 'created',
                          'Pid': os.getpid() if self.running else 0},
                'Path': authority.profile.entrypoint[0],
                'Args': list((*authority.profile.entrypoint[1:], *authority.profile.command))}
        def close(self): pass
    authority._engine = FakeEngine()
    def explicitly_fake_namespace_proof(deadline, **kwargs):
        authority._consumer.check()
        authority._inspect(deadline, running=True)
    authority._validate_live = explicitly_fake_namespace_proof
    return authority, events


@pytest.mark.parametrize('image_kind', ['config', 'manifest', 'index'])
def test_launcher_reserves_exact_single_create_and_start_before_fake_engine_effect(root_journal, image_kind):
    authority, events = synthetic_launcher(root_journal, image_kind=image_kind)
    try:
        assert authority.launch() == 'a' * 64
        calls = list(authority._engine.effect_calls)
        assert events[:3] == ['bind-fixture', 'source-fixture', 'image-fixture']
        assert len([call for call in calls if call[0] == 'POST' and '/create?' in call[1]]) == 1
        assert len([call for call in calls if call[0] == 'POST' and call[1].endswith('/start')]) == 1
        with root_journal.transaction() as db:
            assert db.execute('SELECT state FROM launch').fetchone() == ('running',)
        with pytest.raises(RuntimeError):
            authority.launch()
        assert authority._engine.effect_calls == calls
    finally:
        authority.close()


@pytest.mark.parametrize('failure', ['ambiguous-create', 'ambiguous-start', 'privileged-inspect',
                                   'tag-config', 'wrong-tmpfs', 'invented-tmpfs-entry'])
def test_fake_effect_ambiguity_or_wrong_effective_profile_holds_without_retry_or_adoption(root_journal, failure):
    authority, _ = synthetic_launcher(root_journal, failure=failure)
    try:
        with pytest.raises(RuntimeError):
            authority.launch()
        calls = list(authority._engine.effect_calls)
        with root_journal.transaction() as db:
            assert db.execute('SELECT state FROM launch').fetchone() == ('held',)
        with pytest.raises(RuntimeError):
            authority.launch()
        assert calls == authority._engine.effect_calls and not authority._issuance_open
        assert not any('/kill' in route or method == 'DELETE' for method, route in calls)
    finally:
        authority.close()


def test_real_namespace_guard_rejects_issuer_namespace_as_executor_private_pid_namespace(root_journal):
    authority, _ = synthetic_launcher(root_journal)
    authority._consumer = launch._Process(os.getpid())
    try:
        with pytest.raises(RuntimeError):
            launch.LaunchSupervisor._validate_live(authority, time.monotonic() + 1)
        assert authority._engine.effect_calls == []
    finally:
        authority.close()


@pytest.mark.parametrize('kind', ['file', 'symlink', 'socket'])
def test_issuer_socket_cleanup_refuses_foreign_replacement_and_retains_it(synthetic_issuer, kind):
    authority, path, _ = synthetic_issuer
    authority._socket_parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    authority._socket_identity = launch._identity(path.lstat())
    path.chmod(0o660)
    path.unlink()  # This test's own socket only, to simulate external replacement.
    foreign = None
    if kind == 'file': path.write_bytes(b'foreign-fixture')
    elif kind == 'symlink': path.symlink_to(path.parent / 'foreign-absent')
    else:
        foreign = socket.socket(socket.AF_UNIX)
        foreign.bind(str(path))
        path.chmod(0o660)
    info = path.lstat()
    try:
        with pytest.raises(RuntimeError):
            authority._remove_owned_socket()
        assert launch._identity(path.lstat()) == launch._identity(info)
    finally:
        if foreign is not None: foreign.close()


def test_nonregular_journal_lock_is_rejected_and_not_removed(root_journal):
    path = root_journal.path
    root_journal.close()
    lock = path.with_name(path.name + '.lock')
    lock.unlink()  # Only the now-closed fixture's own original lock file.
    os.mkfifo(lock, 0o600)
    before = lock.lstat()
    with pytest.raises(RuntimeError):
        launch._Journal(str(path), create=False)
    assert launch._identity(lock.lstat()) == launch._identity(before)


def test_close_waits_for_admitted_receipt_operation_before_closing_any_dependency(synthetic_issuer):
    authority, path, _ = synthetic_issuer
    entered, release, close_started, closed = (threading.Event() for _ in range(4))
    original = authority._validate_live
    def blocked(deadline, **kwargs):
        entered.set()
        assert release.wait(3)
        original(deadline, **kwargs)
    authority._request_ms = 4000
    authority._validate_live = blocked
    serving, result = serve_fixture(authority)
    client_results = []
    def consume():
        transport = UnixLaunchReceiptTransport(socket_path=str(path), expected_issuer_uid=os.getuid(),
                                              expected_issuer_gid=os.getgid())
        try:
            client_results.append(transport.issue_receipt(nonce='a' * 64, timeout_ms=4000))
        except BaseException as error:
            client_results.append(error)
        finally:
            transport.close()
    consuming = threading.Thread(target=consume)
    consuming.start()
    def close():
        close_started.set()
        authority.close()
        closed.set()
    closing = threading.Thread(target=close)
    try:
        assert entered.wait(2)
        closing.start()
        assert close_started.wait(1)
        assert not closed.is_set() and not authority._closed
        assert authority._consumer.fd is not None and authority._journal.lock is not None
        release.set()
    finally:
        release.set()
        serving.join(timeout=5)
        consuming.join(timeout=5)
        if closing.ident is not None: closing.join(timeout=5)
    assert not serving.is_alive() and not consuming.is_alive() and not closing.is_alive()
    assert isinstance(result[0], str) and isinstance(client_results[0], bytes) and closed.is_set()


def test_revalidate_after_durable_issuance_reservation_precedes_signing(synthetic_issuer):
    authority, path, _ = synthetic_issuer
    original = authority._validate_live
    calls, signatures = [], []
    def changed_after_reservation(deadline, **kwargs):
        calls.append(True)
        if len(calls) == 3:
            raise launch.LaunchSupervisorError()
        original(deadline, **kwargs)
    authority._validate_live = changed_after_reservation
    signer = authority._signer
    authority._signer = SimpleNamespace(sign=lambda value: signatures.append(value) or signer.sign(value))
    serving, result = serve_fixture(authority)
    transport = UnixLaunchReceiptTransport(socket_path=str(path), expected_issuer_uid=os.getuid(),
                                          expected_issuer_gid=os.getgid())
    try:
        with pytest.raises(RuntimeError):
            transport.issue_receipt(nonce='a' * 64, timeout_ms=1000)
    finally:
        transport.close()
        serving.join(timeout=3)
    assert not serving.is_alive() and isinstance(result[0], launch.LaunchSupervisorError)
    assert len(calls) == 3 and signatures == []
    with authority._journal.transaction() as db:
        assert db.execute('SELECT state FROM issuance').fetchone() == ('held',)


def test_hold_persistence_failure_is_sanitized_without_claiming_held_was_saved(synthetic_issuer):
    authority, _, _ = synthetic_issuer
    def fail(*args): raise RuntimeError('synthetic-private-journal-error')
    authority._journal.state = fail
    authority._request_ms = 100
    with pytest.raises(launch.LaunchSupervisorError) as failed:
        authority.serve_one_receipt()  # Bounded accept timeout, no peer.
    assert 'synthetic-private' not in str(failed.value)
    assert authority._held and authority._hold_persisted is False and not authority._issuance_open


@pytest.mark.parametrize('uid', [0, 10000, 10002])
def test_daemon_pin_rejects_any_host_uid_except_allocated_rootless_pilot(uid):
    with pytest.raises(RuntimeError):
        launch._DaemonPin('/run/synthetic.sock', 1, 1, uid, 10001, 'synthetic', '1.54')


def test_fake_engine_missing_rootless_identity_denied_before_image_fetch(short_root):
    _, image, blobs = verified_image()
    with fake_engine_http(short_root, image=image, blobs=blobs, rootless=False) as (pin, requests):
        engine = launch._UnixEngine(pin)
        try:
            with pytest.raises(RuntimeError):
                engine.image(image, time.monotonic() + 2)
            assert requests == ['/v1.54/info']
        finally:
            engine.close()


def kernel_profile_fixture():
    """Synthetic /proc text, NOT actual container/userns/mount acceptance."""
    profile = launch.ExecutorLaunchProfile.model_validate(profile_data())
    status = {'Uid': '110001\t110001\t110001\t110001',
              'Gid': '110001\t110001\t110001\t110001', 'Groups': '110001', 'NoNewPrivs': '1'}
    status.update({name: '0000000000000000' for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')})
    mapping = '0 10001 1\n1 100001 65536\n'
    rows = ['1 0 0:1 / / ro - overlay overlay rw',
            '2 1 0:2 / /tmp rw,nosuid,nodev,noexec - tmpfs tmpfs rw']
    for index, mount in enumerate(profile.mounts, 3):
        rows.append(f'{index} 1 0:{index} / {mount.target} {"ro" if mount.read_only else "rw"} - ext4 fixture rw')
    return profile, status, mapping, mapping, ('\n'.join(rows) + '\n').encode()


@pytest.mark.parametrize('groups', ['', '110001'])
def test_synthetic_kernel_profile_accepts_only_optional_primary_group(groups):
    profile, status, uid_map, gid_map, mounts = kernel_profile_fixture()
    status['Groups'] = groups
    launch._kernel_profile(profile, status, uid_map, gid_map, mounts, 110001, 110001)


@pytest.mark.parametrize('mutation', ['uid-map', 'gid-map', 'unmapped-group', 'missing-group',
    'extra-group', 'duplicate-group', 'CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb',
    'rw-root', 'rw-config', 'ro-state', 'missing-bind', 'duplicate-bind', 'tmp-executable'])
def test_synthetic_kernel_profile_denies_engine_claims_not_supported_by_proc(mutation):
    profile, status, uid_map, gid_map, mounts = kernel_profile_fixture()
    if mutation == 'uid-map': uid_map = '0 10001 1\n1 200001 65536\n'
    elif mutation == 'gid-map': gid_map = '0 10001 1\n1 200001 65536\n'
    elif mutation in {'unmapped-group', 'missing-group'}:
        raw = profile.model_dump(mode='json')
        raw['supplementary_groups'] = [999999 if mutation == 'unmapped-group' else 10002]
        profile = launch.ExecutorLaunchProfile.model_validate(raw)
    elif mutation == 'extra-group': status['Groups'] += ' 110002'
    elif mutation == 'duplicate-group': status['Groups'] += ' 110001'
    elif mutation.startswith('Cap'): status[mutation] = '0000000000000001'
    elif mutation == 'rw-root': mounts = mounts.replace(b'/ / ro -', b'/ / rw -')
    elif mutation == 'rw-config': mounts = mounts.replace(b'/operator/runner.json ro -', b'/operator/runner.json rw -')
    elif mutation == 'ro-state': mounts = mounts.replace(b'/var/lib/engineering-hub/executor rw -', b'/var/lib/engineering-hub/executor ro -')
    elif mutation == 'missing-bind': mounts = b'\n'.join(row for row in mounts.splitlines() if b'/operator/runner.json ' not in row)
    elif mutation == 'duplicate-bind': mounts += mounts.splitlines()[-1] + b'\n'
    elif mutation == 'tmp-executable': mounts = mounts.replace(b',noexec', b'')
    with pytest.raises(RuntimeError):
        launch._kernel_profile(profile, status, uid_map, gid_map, mounts, 110001, 110001)


def test_synthetic_kernel_profile_translates_declared_supplementary_group():
    profile, status, uid_map, gid_map, mounts = kernel_profile_fixture()
    raw = profile.model_dump(mode='json')
    raw['supplementary_groups'] = [10002]
    profile = launch.ExecutorLaunchProfile.model_validate(raw)
    status['Groups'] = '110001 110002'
    launch._kernel_profile(profile, status, uid_map, gid_map, mounts, 110001, 110001)


@contextmanager
def root_file_constructor_fixture(short_root, root_journal):
    """Actual protected files/journal/proc constructor, synthetic release/key/HTTP.

    Fake image is deliberately NOT an approved installed runner; constructor
    must not perform effects or turn these test pins into deployment approval.
    """
    private = short_root / 'authority'
    private.mkdir(mode=0o700)
    journal_parent = short_root / 'constructor-journal'
    journal_parent.mkdir(mode=0o700)
    signer = Ed25519PrivateKey.generate()  # Synthetic TEST key, never installed.
    _, image, blobs = verified_image()
    with fake_engine_http(short_root, image=image, blobs=blobs) as (pin, requests):
        raw_profile = profile_data(str(short_root / 'hub-pilot-constructor'))
        issuer_path = str(short_root / 'issuer' / 'issuer.sock')
        for mount in raw_profile['mounts']:
            if mount['role'] == 'engine_socket': mount['source'] = pin.socket_path
            if mount['role'] == 'issuer_socket': mount['source'] = issuer_path
        profile = launch.ExecutorLaunchProfile.model_validate(raw_profile)
        release = ExecutorReleaseEnrollment('synthetic-root-files',
            signer.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
            image.source_commit, image.runnable_reference, image.platform_manifest_digest,
            image.image_config_id, image.platform, profile.digest())
        release_data, image_data = asdict(release), asdict(image)
        release_data['platform'] = image_data['platform'] = image.platform.model_dump(mode='json')
        release_data['public_key'] = release.public_key.hex()
        data = dict(schema='executor_launch_supervisor_v1', release=release_data,
            image=image_data, profile=profile.model_dump(mode='json'), daemon=asdict(pin),
            socket_path=issuer_path, consumer_host_uid=110001, consumer_host_gid=110001,
            receipt_lifetime_seconds=30, request_timeout_ms=1000, launch_timeout_ms=1000)
        enrollment, key, journal = private / 'enrollment.json', private / 'signing.key', journal_parent / 'launch.sqlite3'
        enrollment.write_bytes(launch._canonical(data))
        key.write_bytes(signer.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                             serialization.NoEncryption()))
        enrollment.chmod(0o600)
        key.chmod(0o600)
        yield data, dict(enrollment_file=str(enrollment), signing_key_file=str(key),
                         journal_file=str(journal), create_journal=True), requests


def test_whole_constructor_reads_protected_root_files_without_engine_effects(short_root, root_journal):
    with root_file_constructor_fixture(short_root, root_journal) as (data, kwargs, requests):
        authority = launch.LaunchSupervisor(**kwargs)
        try:
            assert authority.profile.digest() == data['release']['launch_profile_digest']
            assert authority._engine.process.fd is not None
            assert requests == [] and authority._consumer is None and authority._listener is None
            assert Path(kwargs['journal_file']).is_file()
            authority._guard()
        finally:
            authority.close()
        assert Path(kwargs['signing_key_file']).is_file() and Path(kwargs['enrollment_file']).is_file()
        assert requests == []


@pytest.mark.parametrize('mutation', ['public-key', 'profile-pin', 'extra-field', 'consumer-mapping',
                                    'key-mode', 'enrollment-mode', 'key-symlink'])
def test_whole_constructor_rejects_invalid_authority_before_journal_or_engine_effects(short_root, root_journal, mutation):
    with root_file_constructor_fixture(short_root, root_journal) as (data, kwargs, requests):
        enrollment, key = Path(kwargs['enrollment_file']), Path(kwargs['signing_key_file'])
        if mutation == 'public-key': data['release']['public_key'] = 'f' * 64
        elif mutation == 'profile-pin': data['release']['launch_profile_digest'] = 'sha256:' + '0' * 64
        elif mutation == 'extra-field': data['unapproved'] = True
        elif mutation == 'consumer-mapping': data['consumer_host_uid'] += 1
        elif mutation == 'key-mode': key.chmod(0o644)
        elif mutation == 'enrollment-mode': enrollment.chmod(0o644)
        elif mutation == 'key-symlink':
            retained = key.with_name('test-only-original.key')
            key.rename(retained)
            key.symlink_to(retained)
        enrollment.write_bytes(launch._canonical(data))
        with pytest.raises(launch.LaunchSupervisorError):
            launch.LaunchSupervisor(**kwargs)
        assert requests == [] and not Path(kwargs['journal_file']).exists()


@pytest.mark.parametrize('terminate', [True, False])
def test_graceful_stop_exact_sigterm_quiescence_or_timeout_never_forces_cleanup(root_journal, terminate):
    authority, events = synthetic_launcher(root_journal)
    child = None
    try:
        authority.launch()
        authority._consumer.close()
        child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(20)'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        authority._consumer = launch._Process(child.pid)
        authority._launch_ms = 1000 if terminate else 100
        original = authority._engine.json
        def fake_sigterm(method, route, deadline, **kwargs):
            if method == 'POST' and route == '/containers/' + 'a' * 64 + '/kill?signal=SIGTERM':
                with root_journal.transaction() as db:
                    assert db.execute('SELECT state FROM launch').fetchone() == ('term_reserved',)
                authority._engine.effect_calls.append((method, route))
                if terminate:
                    child.terminate()  # Only this exact disposable test-owned child.
                    authority._engine.running = False
                return 204, None
            code, info = original(method, route, deadline, **kwargs)
            if method == 'GET' and authority._engine.running:
                info['State']['Pid'] = child.pid
            return code, info
        authority._engine.json = fake_sigterm
        authority._remove_owned_socket = lambda: events.append('synthetic-owned-socket-cleanup')
        if terminate:
            authority.graceful_stop()
            child.wait(timeout=3)
            assert events[-1] == 'synthetic-owned-socket-cleanup'
            expected_state = 'stopped'
        else:
            with pytest.raises(launch.LaunchSupervisorError):
                authority.graceful_stop()
            assert child.poll() is None and 'synthetic-owned-socket-cleanup' not in events
            expected_state = 'held'
        with root_journal.transaction() as db:
            assert db.execute('SELECT state FROM launch').fetchone() == (expected_state,)
        effects = [route for method, route in authority._engine.effect_calls if method != 'GET']
        assert len(effects) == 3 and effects[-1] == '/containers/' + 'a' * 64 + '/kill?signal=SIGTERM'
        assert all('/stop' not in route and 'SIGKILL' not in route for route in effects)
        assert not authority._issuance_open
    finally:
        if child is not None and child.poll() is None:
            child.terminate()  # Fixture teardown, NOT supervisor timeout escalation.
            child.wait(timeout=3)
        authority.close()
