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
import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
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
from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor import launch_supervisor as launch
from hub_container_executor.configuration_commitment import configuration_commitment
from hub_container_executor.launch_transport import UnixLaunchReceiptTransport
from hub_container_executor.runtime_identity import ExecutorReleaseEnrollment, LaunchReceiptIdentityVerifier
from tests.test_hub_container_executor_daemon_backend import verified_image, oci_archive
from tests.test_hub_container_executor_installed_runner import configuration


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
    authority._receipt_served = False
    authority._expires = time.monotonic() + 30
    authority._approval_expires_at_ms = int(time.time() * 1000) + 30000
    authority._daemon_pin = launch._DaemonPin('/synthetic/docker.sock', os.getpid(),
        authority._consumer.start, 10001, 10001, 'synthetic-engine', '1.45')
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


def current_ready(authority):
    """Synthetic existing receipt state; not a real approved container launch."""
    authority._issuance_open = False
    authority._receipt_served = True


def current_pins(authority):
    from engineering_hub.current_executor_observation import CurrentExecutorReleasePins, CurrentLaunchPins
    release = CurrentExecutorReleasePins(**{name: getattr(authority.release, name)
        for name in CurrentExecutorReleasePins.__dataclass_fields__})
    pin = authority._daemon_pin
    launch_pin = CurrentLaunchPins(authority._launch_id, authority._consumer.boot,
        authority._consumer.pid, authority._consumer.start, authority._container_id,
        pin.daemon_id, pin.pid, pin.start_ticks, authority._approval_expires_at_ms)
    return release, launch_pin


def current_exchange(path, *, nonce, timeout_ms):
    """Test-only actual UDS client; no installed executor relay is implemented."""
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(timeout_ms / 1000)
        peer.connect(str(path))
        request = launch._canonical({'protocol': launch.CURRENT_REQUEST_PROTOCOL, 'nonce': nonce})
        peer.sendall(struct.pack('!I', len(request)) + request)
        peer.shutdown(socket.SHUT_WR)
        def read(size):
            result = b''
            while len(result) < size:
                part = peer.recv(size - len(result))
                assert part
                result += part
            return result
        size = struct.unpack('!I', read(4))[0]
        assert 0 < size <= 8192
        raw = read(size)
        assert peer.recv(1) == b''
        return raw


def serve_current_fixture(authority, *, timeout_ms=1000):
    results = []
    def run():
        try:
            results.append(authority.serve_one_current_observation(timeout_ms=timeout_ms))
        except BaseException as error:
            results.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    return thread, results


def test_current_observation_actual_uds_core_signature_and_no_effects(synthetic_issuer):
    from engineering_hub.current_executor_observation import CurrentExecutorObserver
    from engineering_hub.startup_reconciliation import ExecutorSessionIdentity
    authority, path, checks = synthetic_issuer
    # Complete the existing, unchanged one-shot exchange first.
    thread, original = serve_fixture(authority)
    with_transport = UnixLaunchReceiptTransport(socket_path=str(path), expected_issuer_uid=os.getuid(),
                                                expected_issuer_gid=os.getgid())
    try:
        verifier = LaunchReceiptIdentityVerifier(enrollment=authority.release, issuer_transport=with_transport,
            max_receipt_lifetime_seconds=30, exchange_timeout_ms=1000)
        verifier.close()
    finally:
        with_transport.close()
        thread.join(5)
    assert not thread.is_alive() and isinstance(original[0], str)
    release, pins = current_pins(authority)
    class Transport:
        def observe_current_launch(self, **kwargs):
            return current_exchange(path, **kwargs)
    observer = CurrentExecutorObserver(release=release, launch=pins, transport=Transport())
    origin_expiry = authority._expires, authority._approval_expires_at_ms
    for _ in range(2):
        thread, results = serve_current_fixture(authority)
        try:
            assert observer() == ExecutorSessionIdentity(authority._launch_id, authority.release.image_config_id)
        finally:
            thread.join(5)
        assert not thread.is_alive() and results == [True]
    assert not authority._held and not authority._issuance_open
    assert (authority._expires, authority._approval_expires_at_ms) == origin_expiry
    assert len(checks) == 8  # Four old receipt + two live checks for each current response.
    with authority._journal.transaction() as db:
        assert db.execute('SELECT state FROM issuance').fetchone() == ('sent',)
    observer.close()


@pytest.mark.parametrize('mutation', ['protocol', 'nonce', 'selector', 'duplicate', 'oversize', 'trailing'])
def test_invalid_current_wire_does_not_hold_next_valid_request(synthetic_issuer, mutation):
    authority, path, _ = synthetic_issuer
    current_ready(authority)
    request = {'protocol': launch.CURRENT_REQUEST_PROTOCOL, 'nonce': 'a' * 64}
    if mutation == 'protocol': request['protocol'] = launch.REQUEST_PROTOCOL
    if mutation == 'nonce': request['nonce'] = '\u2603'
    if mutation == 'selector': request['pid'] = os.getpid()
    raw = launch._canonical(request)
    if mutation == 'duplicate': raw = b'{"nonce":"a","nonce":"b"}'
    frame = struct.pack('!I', 257 if mutation == 'oversize' else len(raw)) + raw
    if mutation == 'trailing': frame += b'x'
    thread, results = serve_current_fixture(authority)
    try:
        with socket.socket(socket.AF_UNIX) as peer:
            peer.settimeout(2)
            peer.connect(str(path))
            peer.sendall(frame)
            peer.shutdown(socket.SHUT_WR)
            try:
                assert peer.recv(1) == b''
            except ConnectionResetError:
                pass
    finally:
        thread.join(5)
    assert not thread.is_alive() and results == [False] and not authority._held
    thread, results = serve_current_fixture(authority)
    try:
        payload = json.loads(current_exchange(path, nonce='b' * 64, timeout_ms=1000))['payload']
        assert payload['nonce'] == 'b' * 64
    finally:
        thread.join(5)
    assert results == [True] and not authority._held


def test_current_same_uid_fork_peer_cannot_claim_init(synthetic_issuer):
    authority, path, _ = synthetic_issuer
    current_ready(authority)
    thread, results = serve_current_fixture(authority)
    pid = os.fork()
    if pid == 0:
        try:
            with socket.socket(socket.AF_UNIX) as peer:
                peer.connect(str(path))
                request = launch._canonical({'protocol': launch.CURRENT_REQUEST_PROTOCOL, 'nonce': 'a' * 64})
                peer.sendall(struct.pack('!I', len(request)) + request)
                peer.shutdown(socket.SHUT_WR)
                peer.recv(1)
        except (ConnectionResetError, BrokenPipeError):
            pass
        os._exit(0)
    try:
        assert os.waitpid(pid, 0)[1] == 0
    finally:
        thread.join(5)
    assert results == [False] and not authority._held


@pytest.mark.parametrize('failure', ['identity', 'expiry', 'wall-expiry', 'signer'])
def test_current_identity_failure_holds_evidence_without_effect(synthetic_issuer, failure):
    authority, _, _ = synthetic_issuer
    current_ready(authority)
    if failure == 'identity':
        def fail(deadline): raise RuntimeError('synthetic identity mismatch')
        authority._validate_live = fail
    elif failure == 'expiry': authority._expires = time.monotonic() - 1
    elif failure == 'wall-expiry': authority._approval_expires_at_ms = int(time.time() * 1000) - 1
    else:
        class BadSigner:
            def sign(self, raw): raise RuntimeError('synthetic signer failure')
        authority._signer = BadSigner()
    with pytest.raises(launch.LaunchSupervisorError):
        authority.observe_current_launch(nonce='a' * 64, timeout_ms=1000)
    assert authority._held
    with authority._journal.transaction() as db:
        assert db.execute('SELECT state FROM launch').fetchone() == ('held',)
    assert authority._listener.fileno() >= 0  # No unlink/stop/cleanup on uncertainty.


@pytest.mark.parametrize('nonce,timeout', [('bad', 1000), ('a' * 64, True), ('a' * 64, 0)])
def test_public_current_invalid_input_does_not_hold(synthetic_issuer, nonce, timeout):
    authority, _, _ = synthetic_issuer
    with pytest.raises(launch.LaunchSupervisorError):
        authority.observe_current_launch(nonce=nonce, timeout_ms=timeout)
    assert not authority._held and authority._issuance_open


def test_owned_wait_serves_current_then_explicit_signal_only(synthetic_issuer):
    authority, path, _ = synthetic_issuer
    current_ready(authority)
    stopped = threading.Event()
    results = []
    def wait():
        try:
            results.append(authority.wait_for_stop_with_observations(
                timeout_ms=2000, stop_requested=stopped.is_set))
        except BaseException as error:
            results.append(error)
    thread = threading.Thread(target=wait)
    thread.start()
    try:
        payload = json.loads(current_exchange(path, nonce='a' * 64, timeout_ms=1000))['payload']
        assert payload['launch_id'] == authority._launch_id
    finally:
        stopped.set()
        thread.join(5)
    assert results == [True] and not authority._held
    assert authority._consumer.fd >= 0 and authority._listener.fileno() >= 0


def test_owned_wait_expiry_preserves_live_launch_and_original_ceiling(synthetic_issuer):
    authority, _, _ = synthetic_issuer
    current_ready(authority)
    authority._expires = time.monotonic() - 1
    before = authority._approval_expires_at_ms
    assert authority.wait_for_stop_with_observations(timeout_ms=50, stop_requested=lambda: False) is False
    assert authority._approval_expires_at_ms == before and not authority._held
    assert authority._listener.fileno() >= 0 and authority._consumer.fd >= 0


@pytest.mark.parametrize('boundary', ['mutex-entry', 'mutex-acquired', 'before-select'])
def test_owned_wait_deadline_races_are_not_authority_failures(synthetic_issuer, monkeypatch, boundary):
    authority, _, _ = synthetic_issuer
    current_ready(authority)
    before = (authority._expires, authority._approval_expires_at_ms, authority._receipt_served)
    real_monotonic = time.monotonic
    calls = 0
    # First call creates the deadline; second passes the outer check. Advance
    # across the deadline at each formerly throwing internal boundary.
    expire_at = {'mutex-entry': 3, 'mutex-acquired': 4, 'before-select': 6}[boundary]
    origin = real_monotonic()
    def clock():
        nonlocal calls
        calls += 1
        return origin if calls < expire_at else origin + 1
    with monkeypatch.context() as patch:
        patch.setattr(launch.time, 'monotonic', clock)
        assert authority.wait_for_stop_with_observations(
            timeout_ms=50, stop_requested=lambda: False) is False
    assert (authority._expires, authority._approval_expires_at_ms, authority._receipt_served) == before
    assert not authority._held and getattr(authority, '_observation_thread', None) is None
    assert authority.wait_for_stop_with_observations(timeout_ms=50, stop_requested=lambda: True) is True


def test_owned_wait_request_handoff_keeps_absolute_deadline(synthetic_issuer, monkeypatch):
    authority, _, _ = synthetic_issuer
    current_ready(authority)
    before = (authority._expires, authority._approval_expires_at_ms, authority._receipt_served)
    origin = time.monotonic()
    now = [origin]
    deadlines = []
    serve = authority._serve_current_peer_until
    select_ready = launch.select.select
    def delayed_ready(read, write, errors, timeout):
        if authority._listener not in read:
            return select_ready(read, write, errors, timeout)
        now[0] = origin + .1  # Scheduling consumes the original 50 ms window.
        return [authority._listener], [], []
    def capture(deadline):
        deadlines.append(deadline)
        return serve(deadline)  # Must decline before socket admission.
    with monkeypatch.context() as patch:
        patch.setattr(launch.time, 'monotonic', lambda: now[0])
        patch.setattr(launch.select, 'select', delayed_ready)
        patch.setattr(authority, '_serve_current_peer_until', capture)
        assert authority.wait_for_stop_with_observations(
            timeout_ms=50, stop_requested=lambda: False) is False
    assert deadlines == [origin + .05]
    assert not authority._held and getattr(authority, '_observation_thread', None) is None
    assert (authority._expires, authority._approval_expires_at_ms, authority._receipt_served) == before
    assert authority.wait_for_stop_with_observations(timeout_ms=50, stop_requested=lambda: True) is True


def test_current_missing_eof_times_out_peer_without_holding(synthetic_issuer):
    authority, path, _ = synthetic_issuer
    current_ready(authority)
    authority._request_ms = 100
    thread, results = serve_current_fixture(authority)
    try:
        with socket.socket(socket.AF_UNIX) as peer:
            peer.settimeout(2)
            peer.connect(str(path))
            request = launch._canonical({'protocol': launch.CURRENT_REQUEST_PROTOCOL, 'nonce': 'a' * 64})
            peer.sendall(struct.pack('!I', len(request)) + request)
            assert peer.recv(1) == b''  # No SHUT_WR: bounded malformed request, not identity loss.
    finally:
        thread.join(5)
    assert results == [False] and not authority._held


def test_current_reentry_caught_by_signer_still_holds(synthetic_issuer):
    authority, _, _ = synthetic_issuer
    current_ready(authority)
    signer = authority._signer
    class ReentrantSigner:
        def sign(self, raw):
            with pytest.raises(launch.LaunchSupervisorError):
                authority.observe_current_launch(nonce='b' * 64, timeout_ms=100)
            return signer.sign(raw)
    authority._signer = ReentrantSigner()
    # The synthetic fixture's _validate_live lacks the real guard, so require it
    # explicitly to reproduce actual _validate_live behavior after signing.
    original = authority._validate_live
    def validate(deadline):
        authority._guard()
        original(deadline)
    authority._validate_live = validate
    with pytest.raises(launch.LaunchSupervisorError):
        authority.observe_current_launch(nonce='a' * 64, timeout_ms=1000)
    assert authority._held


def test_public_current_real_fork_denied_before_inherited_mutex(synthetic_issuer):
    authority, _, _ = synthetic_issuer
    current_ready(authority)
    authority._mutex.acquire()
    try:
        pid = os.fork()
        if pid == 0:
            try:
                authority.observe_current_launch(nonce='a' * 64, timeout_ms=100)
            except launch.LaunchSupervisorError:
                os._exit(0)
            os._exit(1)
        assert os.waitpid(pid, 0)[1] == 0
    finally:
        authority._mutex.release()
    assert not authority._held


def test_launch_original_wall_ceiling_durable_before_create(root_journal):
    authority, _ = synthetic_launcher(root_journal)
    original_reserve = root_journal.reserve
    retained = []
    def reserve(identity, data):
        assert data['approval_expires_at_ms'] == authority._approval_expires_at_ms
        assert authority._expires == authority._origin + authority._ttl
        retained.append(data['approval_expires_at_ms'])
        original_reserve(identity, data)
    root_journal.reserve = reserve
    try:
        authority.launch()
        with root_journal.transaction() as db:
            data = json.loads(db.execute('SELECT data FROM launch').fetchone()[0])
        assert retained == [authority._approval_expires_at_ms]
        assert data['approval_expires_at_ms'] == retained[0]
    finally:
        authority.close()


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


def test_actual_delayed_connect_readiness_preserves_short_exchange_budget(synthetic_issuer):
    """Real Unix/pidfd/signature slice; Engine/profile remain synthetic."""
    authority, path, checks = synthetic_issuer
    authority._request_ms = 100
    waiting = threading.Event()
    results, startup_seconds = [], []

    def serve_after_ready():
        started = time.monotonic()
        waiting.set()
        try:
            assert authority.wait_for_receipt_request(timeout_ms=1000)
            startup_seconds.append(time.monotonic() - started)
            results.append(authority.serve_one_receipt())
        except BaseException as error:
            results.append(error)

    serving = threading.Thread(target=serve_after_ready)
    serving.start()
    transport = UnixLaunchReceiptTransport(socket_path=str(path), expected_issuer_uid=os.getuid(),
                                          expected_issuer_gid=os.getgid())
    verified = None
    try:
        assert waiting.wait(1)
        time.sleep(.25)  # Startup delay exceeds the enrolled 100ms accept budget.
        verified = LaunchReceiptIdentityVerifier(enrollment=authority.release, issuer_transport=transport,
            max_receipt_lifetime_seconds=30, exchange_timeout_ms=1000)
        assert verified.implementation_commit() == 'c' * 40
    finally:
        if verified is not None:
            verified.close()
        transport.close()
        serving.join(timeout=3)
    assert not serving.is_alive() and len(results) == 1 and isinstance(results[0], str)
    assert .2 <= startup_seconds[0] < 1 and authority._request_ms == 100
    assert len(checks) == 4 and not authority._issuance_open and not authority._held
    with authority._journal.transaction() as db:
        assert db.execute('SELECT state FROM issuance').fetchone() == ('sent',)


def test_actual_retained_consumer_pidfd_exit_aborts_readiness(synthetic_issuer):
    """Only the disposable child is terminated; no container/Engine exists."""
    authority, _, checks = synthetic_issuer
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'import time; time.sleep(10)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results = []
    waiting = threading.Event()
    serving = None
    try:
        authority._consumer.close()
        authority._consumer = launch._Process(child.pid)

        def wait_owned():
            waiting.set()
            try:
                results.append(authority.wait_for_receipt_request(timeout_ms=1000))
            except BaseException as error:
                results.append(error)

        serving = threading.Thread(target=wait_owned)
        serving.start()
        assert waiting.wait(1)
        child.terminate()  # Exact test-owned child only, not supervisor escalation.
        child.wait(timeout=3)
        serving.join(timeout=3)
        assert not serving.is_alive() and len(results) == 1
        assert isinstance(results[0], launch.LaunchSupervisorError)
        assert authority._held and not authority._issuance_open and checks == []
        with authority._journal.transaction() as db:
            assert db.execute('SELECT count(*) FROM issuance').fetchone() == (0,)
            assert db.execute('SELECT state FROM launch').fetchone() == ('held',)
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=3)
        if serving is not None:
            serving.join(timeout=3)


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
        Path(issuer_path).parent.mkdir(mode=0o700)
        issuer = socket.socket(socket.AF_UNIX)
        issuer.bind(issuer_path)
        os.chown(issuer_path, 0, 110001)
        os.chmod(issuer_path, 0o660)
        for mount in raw_profile['mounts']:
            if mount['role'] == 'engine_socket':
                mount['source'] = pin.socket_path
                info = os.stat(pin.socket_path)
                mount.update(uid=info.st_uid, gid=info.st_gid, mode=stat.S_IMODE(info.st_mode))
            if mount['role'] == 'issuer_socket': mount['source'] = issuer_path
            if mount['role'] not in {'engine_socket', 'issuer_socket'}:
                path = Path(mount['source'])
                path.parent.mkdir(mode=0o700, exist_ok=True)
                if mount['kind'] == 'directory': path.mkdir(mode=0o700)
                else: path.touch(mode=0o600)
                os.chown(path, mount['uid'], mount['gid'])
        release = ExecutorReleaseEnrollment('synthetic-root-files',
            signer.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
            image.source_commit, image.runnable_reference, image.platform_manifest_digest,
            image.image_config_id, image.platform, 'sha256:' + '0' * 64)
        release_data, image_data = asdict(release), asdict(image)
        release_data['platform'] = image_data['platform'] = image.platform.model_dump(mode='json')
        release_data['public_key'] = release.public_key.hex()
        # A SINGLE actual canonical runner drives C -> profile D -> final runner
        # bytes -> independently pinned raw SHA. No fixed-point/hash patching.
        runner = configuration()
        runner['manifest']['driver']['implementation_commit'] = image.source_commit
        runner['manifest']['runner_binding']['daemon_identity'] = pin.daemon_id
        runner['docker_binding'].update(daemon_identity=pin.daemon_id,
            api_version=pin.api_version, unix_socket_path=launch._TARGETS['engine_socket'],
            generation_root=next(m['source'] for m in raw_profile['mounts'] if m['role'] == 'generations'))
        manifest = PilotManifest.from_bytes(launch._canonical(runner['manifest']))
        for row in runner['resources']['build_enrollments']:
            row['manifest_digest'] = manifest.digest()
        runner['release_enrollment'] = {**release_data, 'public_key_hex': release_data['public_key']}
        runner['release_enrollment'].pop('public_key')
        runner['launch_transport']['socket_path'] = launch._TARGETS['issuer_socket']
        runner['resources'].update(
            adapter_database=launch._TARGETS['state'] + '/adapter/state.sqlite3',
            journal_database=launch._TARGETS['state'] + '/journal/state.sqlite3',
            reservation_root=launch._TARGETS['state'] + '/reservations',
            record_root=launch._TARGETS['records'], metadata_root=launch._TARGETS['metadata'],
            baseline_receipt_root=launch._TARGETS['baseline'], secret_root=launch._TARGETS['secrets'],
            frozen_backup=runner['docker_binding']['generation_root'] + '/frozen.sqlite3')
        for mount in raw_profile['mounts']:
            if mount['role'] == 'config':
                mount['content_digest'] = configuration_commitment(launch._canonical(runner)).content_digest
        profile = launch.ExecutorLaunchProfile.model_validate(raw_profile)
        release_data['launch_profile_digest'] = runner['release_enrollment']['launch_profile_digest'] = profile.digest()
        runner_raw = launch._canonical(runner)
        commitment = configuration_commitment(runner_raw)
        assert commitment.content_digest == next(m.content_digest for m in profile.mounts if m.role == 'config')
        config_path = Path(next(m.source for m in profile.mounts if m.role == 'config'))
        config_path.write_bytes(runner_raw)
        data = dict(schema='executor_launch_supervisor_v2', configuration_raw_digest=commitment.raw_digest,
            release=release_data,
            image=image_data, profile=profile.model_dump(mode='json'), daemon=asdict(pin),
            socket_path=issuer_path, consumer_host_uid=110001, consumer_host_gid=110001,
            receipt_lifetime_seconds=30, request_timeout_ms=1000, launch_timeout_ms=1000)
        enrollment, key, journal = private / 'enrollment.json', private / 'signing.key', journal_parent / 'launch.sqlite3'
        enrollment.write_bytes(launch._canonical(data))
        key.write_bytes(signer.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                             serialization.NoEncryption()))
        enrollment.chmod(0o600)
        key.chmod(0o600)
        try:
            yield data, dict(enrollment_file=str(enrollment), signing_key_file=str(key),
                             journal_file=str(journal), create_journal=True), requests
        finally:
            issuer.close()


def test_whole_constructor_reads_protected_root_files_without_engine_effects(short_root, root_journal):
    with root_file_constructor_fixture(short_root, root_journal) as (data, kwargs, requests):
        authority = launch.LaunchSupervisor(**kwargs)
        try:
            assert authority.profile.digest() == data['release']['launch_profile_digest']
            assert authority._engine.process.fd is not None
            assert requests == [] and authority._consumer is None and authority._listener is None
            assert Path(kwargs['journal_file']).is_file()
            authority._guard()
            authority._pin_sources()
        finally:
            authority.close()
        assert Path(kwargs['signing_key_file']).is_file() and Path(kwargs['enrollment_file']).is_file()
        assert requests == []


@pytest.mark.parametrize('mutation', ['public-key', 'profile-pin', 'extra-field', 'consumer-mapping',
                                    'key-mode', 'enrollment-mode', 'key-symlink',
                                    'missing-raw-pin', 'invalid-raw-pin', 'legacy-v1'])
def test_whole_constructor_rejects_invalid_authority_before_journal_or_engine_effects(short_root, root_journal, mutation):
    with root_file_constructor_fixture(short_root, root_journal) as (data, kwargs, requests):
        enrollment, key = Path(kwargs['enrollment_file']), Path(kwargs['signing_key_file'])
        if mutation == 'public-key': data['release']['public_key'] = 'f' * 64
        elif mutation == 'profile-pin': data['release']['launch_profile_digest'] = 'sha256:' + '0' * 64
        elif mutation == 'extra-field': data['unapproved'] = True
        elif mutation == 'consumer-mapping': data['consumer_host_uid'] += 1
        elif mutation == 'missing-raw-pin': data.pop('configuration_raw_digest')
        elif mutation == 'invalid-raw-pin': data['configuration_raw_digest'] = 'not-a-digest'
        elif mutation == 'legacy-v1': data['schema'] = 'executor_launch_supervisor_v1'
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


@pytest.mark.parametrize('mutation', ['raw-pin', 'normalized-as-raw', 'commitment-pin', 'self-digest', 'other-field',
                                    'noncanonical', 'duplicate-key', 'missing-self', 'other-release'])
def test_same_runner_bytes_must_satisfy_raw_commitment_and_independent_release(short_root, root_journal, mutation):
    with root_file_constructor_fixture(short_root, root_journal) as (data, kwargs, requests):
        config_mount = next(mount for mount in data['profile']['mounts'] if mount['role'] == 'config')
        path = Path(config_mount['source'])
        raw = path.read_bytes()
        runner = json.loads(raw)
        if mutation == 'raw-pin': data['configuration_raw_digest'] = 'sha256:' + '0' * 64
        elif mutation == 'normalized-as-raw': data['configuration_raw_digest'] = config_mount['content_digest']
        elif mutation == 'commitment-pin':
            config_mount['content_digest'] = 'sha256:' + '0' * 64
            # Preserve root profile consistency but do not silently approve runner changes.
            data['release']['launch_profile_digest'] = launch.ExecutorLaunchProfile.model_validate(data['profile']).digest()
        else:
            if mutation == 'self-digest': runner['release_enrollment']['launch_profile_digest'] = 'sha256:' + '0' * 64
            elif mutation == 'other-field': runner['exchange_timeout_ms'] += 1
            elif mutation == 'missing-self': runner['release_enrollment'].pop('launch_profile_digest')
            elif mutation == 'other-release': runner['release_enrollment']['issuer_id'] = 'different-issuer'
            raw = launch._canonical(runner)
            if mutation == 'noncanonical': raw += b'\n'
            elif mutation == 'duplicate-key': raw = raw[:-1] + b',"schema":"duplicate"}'
            path.write_bytes(raw)
            # Even pinning changed raw bytes cannot defeat C or the root self-D equality.
            data['configuration_raw_digest'] = 'sha256:' + hashlib.sha256(raw).hexdigest()
        Path(kwargs['enrollment_file']).write_bytes(launch._canonical(data))
        authority = launch.LaunchSupervisor(**kwargs)
        try:
            with pytest.raises(RuntimeError):
                authority._pin_sources()
            assert requests == [] and authority._consumer is None
        finally:
            authority.close()


def test_configuration_rewrite_after_first_verified_pin_is_rejected(short_root, root_journal):
    with root_file_constructor_fixture(short_root, root_journal) as (_, kwargs, requests):
        authority = launch.LaunchSupervisor(**kwargs)
        try:
            authority._pin_sources()
            path = Path(authority._mounts['config'].source)
            original_inode = path.stat().st_ino
            runner = json.loads(path.read_bytes())
            runner['exchange_timeout_ms'] += 1
            path.write_bytes(launch._canonical(runner))
            assert path.stat().st_ino == original_inode
            with pytest.raises(RuntimeError):
                authority._pin_sources()
            assert requests == [] and authority._consumer is None
        finally:
            authority.close()


@pytest.mark.parametrize('terminate', [True, False])
@pytest.mark.parametrize('accounting_lag', [0, 2])
def test_graceful_stop_exact_sigterm_quiescence_or_timeout_never_forces_cleanup(
        root_journal, terminate, accounting_lag):
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
        stop_sent = False
        delayed_reads = 0
        def fake_sigterm(method, route, deadline, **kwargs):
            nonlocal stop_sent, delayed_reads
            if method == 'POST' and route == '/containers/' + 'a' * 64 + '/kill?signal=SIGTERM':
                with root_journal.transaction() as db:
                    assert db.execute('SELECT state FROM launch').fetchone() == ('term_reserved',)
                authority._engine.effect_calls.append((method, route))
                if terminate:
                    child.terminate()  # Only this exact disposable test-owned child.
                    authority._engine.running = False
                stop_sent = True
                return 204, None
            code, info = original(method, route, deadline, **kwargs)
            if method == 'GET' and stop_sent and terminate:
                if delayed_reads < accounting_lag:
                    delayed_reads += 1
                    info['State'].update(Running=True, Status='running', Pid=child.pid)
                else:
                    info['State'].update(Running=False, Status='exited', Pid=0)
            if method == 'GET' and authority._engine.running:
                info['State']['Pid'] = child.pid
            return code, info
        authority._engine.json = fake_sigterm
        authority._remove_owned_socket = lambda: events.append('synthetic-owned-socket-cleanup')
        if terminate:
            authority.graceful_stop()
            child.wait(timeout=3)
            assert delayed_reads == accounting_lag
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
