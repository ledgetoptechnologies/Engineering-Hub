"""Synthetic parser/lifecycle fixtures, not installed PID1, mount or Engine proof."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip('fcntl', reason='Optional executor uses Linux ownership providers')

from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor import installed_runner as module
from tests.test_hub_container_pilot_manifest import make_manifest_data


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(char):
    return 'sha256:' + char * 64


def configuration():
    manifest_data = make_manifest_data()
    manifest = PilotManifest.from_bytes(canonical(manifest_data))
    return dict(schema=module.SCHEMA, manifest=manifest_data,
        docker_binding=dict(runner_id=manifest.runner_binding.runner_id,
            daemon_identity=manifest.runner_binding.daemon_identity,
            platform=manifest.runner_binding.platform.model_dump(mode='json'),
            unix_socket_path='/run/fixture/engine.sock', api_version='1.41',
            generation_root='/fixture/generations'),
        resources=dict(adapter_database='/fixture/adapter/state.sqlite3',
            journal_database='/fixture/journal/state.sqlite3', journal_create=True,
            record_root='/fixture/records', metadata_root='/fixture/metadata',
            baseline_receipt_root='/fixture/receipts', secret_root='/fixture/secrets',
            frozen_backup='/fixture/generations/frozen.sqlite3', reservation_root='/fixture/reservations',
            state_reservations_create=True, baseline_receipt_digest=digest('5'),
            baseline_runtime_profile_digest=digest('6'),
            secret_bindings=[['test-codex', 1, digest('1')], ['test-hermes', 1, digest('2')],
                             ['test-claude', 1, digest('3')], ['test-executor-service', 1, digest('4')]],
            build_enrollments=[dict(manifest_digest=manifest.digest(), role=role,
                record_digest=getattr(manifest, role).provenance.attestation_digest,
                builder_id='synthetic-builder', invocation_id=str(uuid4()))
                for role in ('candidate', 'rollback')]),
        release_enrollment=dict(issuer_id='synthetic-issuer', public_key_hex='a' * 64,
            implementation_commit=manifest.driver.implementation_commit,
            runnable_digest=digest('7'), platform_manifest_digest=digest('8'),
            image_config_id=digest('9'), platform=manifest.runner_binding.platform.model_dump(mode='json'),
            launch_profile_digest=digest('a')),
        launch_transport=dict(socket_path='/run/fixture/issuer.sock',
            expected_issuer_uid=65534, expected_issuer_gid=65534),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000,
        supervisor_stop_grace_seconds=1200, executor_socket_path='/run/fixture-executor/service.sock',
        bearer_test_binding=dict(slot_id='test-executor-service', version=1))


def pair_configuration():
    data = configuration()
    data['schema'] = module.PAIR_SCHEMA
    data['pair'] = {'authority_public_key_hex': 'b' * 64, 'peer_uid': 10002, 'peer_gid': 10002}
    return data


def test_gate_v3_config_is_closed_and_source_changes_commitment():
    from hub_container_executor.configuration_commitment import configuration_commitment
    data = configuration(); data['schema'] = module.GATE_SCHEMA
    data['gate_socket_directory'] = '/fixture/gate-control'
    parsed = module.parse_configuration(canonical(data))
    assert parsed.gate_socket_directory == '/fixture/gate-control' and parsed.pair is None
    first = configuration_commitment(canonical(data)).content_digest
    data['gate_socket_directory'] = '/fixture/other-gate'
    assert configuration_commitment(canonical(data)).content_digest != first
    data['gate_socket_directory'] = '/fixture/secrets/nested'
    with pytest.raises(module.InstalledRunnerError): module.parse_configuration(canonical(data))


def test_pair_ledger_v7_is_explicit_disjoint_and_preserves_gate_guard():
    data = configuration(); data['schema'] = module.PAIR_LEDGER_SCHEMA
    data['pair_ledger_directory'] = '/fixture/pair-ledger'
    parsed = module.parse_configuration(canonical(data))
    assert parsed.pair_ledger_directory == '/fixture/pair-ledger'
    assert parsed.gate_socket_directory is None and not parsed.gate_ledger_channel
    module._require_supported_gate_runtime(parsed)
    data['pair_ledger_directory'] = '/fixture/secrets/nested'
    with pytest.raises(module.InstalledRunnerError): module.parse_configuration(canonical(data))
    gate = configuration(); gate['schema'] = module.GATE_LEDGER_SCHEMA
    gate['gate_socket_directory'] = '/fixture/gate-control'
    with pytest.raises(module.InstalledRunnerError): module._require_supported_gate_runtime(module.parse_configuration(canonical(gate)))


@pytest.mark.asyncio
async def test_installed_pair_ledger_constructs_real_owned_worker(tmp_path, monkeypatch):
    """Actual runner and signed FD verification; synthetic issuer and service owner."""
    import base64, socket, threading, time
    from types import SimpleNamespace
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from engineering_hub.executor_launch_profile import ExecutorLaunchProfile
    from hub_container_executor import gate_ledger_handoff as handoff, ledger as ledger_module
    from hub_container_executor.broker_seqpacket import PeerPin, _start
    from hub_container_executor.gate_ledger_channel import SupervisorLedgerClient
    from tests.test_executor_launch_profile import gate_profile
    from tests.test_hub_container_executor_runtime_identity import Issuer
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fixture_root = tmp_path / 'hub-pilot-fixture'; fixture_root.mkdir(mode=0o700)
    directory = fixture_root / 'ledger-channel'; directory.mkdir(mode=0o700)
    data = configuration(); data['schema'] = module.PAIR_LEDGER_SCHEMA; data['pair_ledger_directory'] = str(directory)
    profile_data = gate_profile(str(fixture_root)); del profile_data['gate_channel']
    profile_data['gate_ledger_channel'] = 'pair-outcome-listener-v1'
    mount = next(m for m in profile_data['mounts'] if m['role'] == 'gate_channel_directory')
    mount['source'] = mount['target'] = str(directory)
    profile = ExecutorLaunchProfile.model_validate(profile_data)
    data['release_enrollment']['public_key_hex'] = public.hex()
    data['release_enrollment']['launch_profile_digest'] = profile.digest()
    config = module.parse_configuration(canonical(data))
    profile_raw = canonical(profile.enrollment_value())
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(str(directory / 'ledger.sock')); listener.listen(1)
    info, leaf = os.fstat(listener.fileno()), os.lstat(directory / 'ledger.sock')
    ledger_root = tmp_path / 'db'; ledger_root.mkdir(mode=0o700)
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = ledger_module.SQLiteLedger(str(ledger_root / 'ledger.sqlite'), manifest_digest=config.manifest.digest(),
        candidate_commit=config.manifest.candidate.source_commit, policy_revision=config.manifest.policy_revision)
    clock = SimpleNamespace(wall=time.time(), mono=time.monotonic())
    monkeypatch.setattr(module, '_process', lambda: None)
    monkeypatch.setattr(module, 'UnixLaunchReceiptTransport', lambda **_: Issuer(config.release, key, clock))
    class Observations:
        def __init__(self, **_): pass
        def close(self): pass
        def issue_ledger_listener(self, *, verifier, manifest_digest, source_directory, timeout_ms, pair_only):
            assert pair_only and source_directory == str(directory)
            wall, tick, nonce = int(time.time()*1000), time.monotonic(), 'a'*64
            binding = verifier.launch_binding()
            payload = dict(zip(('launch_id','boot_id','authenticated_host_pid','process_start_ticks','container_id'), binding))
            payload.update(protocol=handoff.PAIR_PROTOCOL, audience=handoff.PAIR_AUDIENCE, nonce=nonce,
                manifest_digest=manifest_digest, launch_profile_digest=profile.digest(), listener_device=info.st_dev,
                listener_inode=info.st_ino, path_device=leaf.st_dev, path_inode=leaf.st_ino,
                issued_at_ms=wall, expires_at_ms=wall+4000)
            signature = base64.urlsafe_b64encode(key.sign(handoff.PAIR_DOMAIN+canonical(payload))).rstrip(b'=').decode()
            return os.dup(listener.fileno()), canonical({'payload':payload,'signature':signature}), nonce, wall, tick, profile_raw
    class Secrets:
        def __init__(self, *_, **__): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def resolve_test_secret(self, *_): return 'synthetic-bearer'
    monkeypatch.setattr(module, 'UnixCurrentObservationTransport', Observations)
    monkeypatch.setattr(module, 'ImmutableTestSecretResolver', Secrets)
    seen = []
    async def service(_factory, **kwargs):
        worker = kwargs['gate_worker_factory'](SimpleNamespace(ledger=ledger))
        worker.start(on_failure=lambda: seen.append('failed'))
        def exchange():
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as channel:
                channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1); channel.connect(str(directory / 'ledger.sock'))
                peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
                try:
                    client = SupervisorLedgerClient(channel=channel, peer=peer, signing_key=key,
                        launch_binding=worker._launch.launch_binding(), manifest_digest=config.manifest.digest(),
                        timeout_seconds=2, pair_only=True)
                    assert client.invoke('seal_pair_drain_v1', {'pair_id':str(uuid4())}) is True
                finally: peer.close()
        try:
            await asyncio.to_thread(exchange)
            seen.append('sealed')
        finally:
            await asyncio.to_thread(worker.close)
    monkeypatch.setattr(module, 'serve_owned_host_executor_unix', service)
    try:
        await module.run_installed_executor(config)
        assert seen == ['sealed'] and ledger.has_pair_drain_seal()
    finally:
        listener.close(); ledger.close()


@pytest.mark.asyncio
async def test_gate_v3_dispatch_denied_before_identity_or_provider_effects(monkeypatch):
    data = configuration(); data['schema'] = module.GATE_SCHEMA
    data['gate_socket_directory'] = '/fixture/gate-control'
    config = module.parse_configuration(canonical(data))
    monkeypatch.setattr(module, '_process', lambda: None)
    def forbidden(*_args, **_kwargs): pytest.fail('v3 entered provider or issuer')
    monkeypatch.setattr(module, 'UnixLaunchReceiptTransport', forbidden)
    monkeypatch.setattr(module, 'local_host_adapter_resources', forbidden)
    with pytest.raises(module.InstalledRunnerError): await module.run_installed_executor(config)


def test_pair_configuration_has_only_independent_authority_and_peer_pins():
    data = pair_configuration()
    parsed = module.parse_configuration(canonical(data))
    assert parsed.pair['authority_public_key'] == bytes.fromhex('b' * 64)
    assert parsed.pair['peer_uid'] == 10002
    data['pair']['pair_id'] = str(uuid4())
    with pytest.raises(module.InstalledRunnerError): module.parse_configuration(canonical(data))


@pytest.mark.asyncio
async def test_pair_runner_wires_two_one_shot_exchanges_then_owned_broker(monkeypatch):
    config = module.parse_configuration(canonical(pair_configuration()))
    events, pipes = [], []
    monkeypatch.setattr(module, '_process', lambda: None)
    class Verifier:
        def claim_channel(self, fd): events.append(('claim', fd))
        def implementation_commit(self): return config.release.implementation_commit
        def close(self): events.append('identity-close')
    class Transport:
        def __init__(self, **kwargs):
            self.index = len([item for item in events if item == 'transport'])
            events.append('transport')
        def issue_pair_enrollment(self, **kwargs):
            assert self.index == 0 and kwargs['own_enrollment'] is config.release
            events.append('enrollment')
            return object()
        def issue_enrolled_pair_channel(self, **kwargs):
            assert self.index == 1 and kwargs['authority_public_key'] == bytes.fromhex('b' * 64)
            events.append('channel')
            fd, writer = os.pipe(); pipes.append(writer)
            return fd, Verifier()
        def close(self): events.append('transport-close')
    @contextmanager
    def signals(stop): yield
    async def serve(factory, *, fd, peer, stop_event, cleanup_report):
        assert (peer.uid, peer.gid) == (10002, 10002)
        assert factory.keywords['driver_identity_verifier'].__class__ is Verifier
        events.append('broker-drain')
        os.close(fd)
        cleanup_report.state = 'clean'
    monkeypatch.setattr(module, 'UnixLaunchReceiptTransport', Transport)
    monkeypatch.setattr(module, '_signal_stop', signals)
    monkeypatch.setattr(module, 'serve_owned_inherited_broker', serve)
    try:
        await module.run_installed_executor(config)
        assert [entry for entry in events if isinstance(entry, str)] == ['transport', 'enrollment',
            'transport', 'channel', 'broker-drain', 'identity-close', 'transport-close', 'transport-close']
    finally:
        for fd in pipes: os.close(fd)


@pytest.mark.asyncio
async def test_pair_runner_closes_received_fd_if_identity_claim_fails(monkeypatch):
    config = module.parse_configuration(canonical(pair_configuration()))
    monkeypatch.setattr(module, '_process', lambda: None)
    read_fd, write_fd = os.pipe()
    events = []
    class Verifier:
        def claim_channel(self, fd): raise RuntimeError('synthetic refusal')
        def close(self): events.append('identity-close')
    class Transport:
        def __init__(self, **kwargs): pass
        def issue_pair_enrollment(self, **kwargs): return object()
        def issue_enrolled_pair_channel(self, **kwargs): return read_fd, Verifier()
        def close(self): events.append('transport-close')
    @contextmanager
    def signals(stop): yield
    monkeypatch.setattr(module, 'UnixLaunchReceiptTransport', Transport)
    monkeypatch.setattr(module, '_signal_stop', signals)
    try:
        with pytest.raises(module.InstalledRunnerError): await module.run_installed_executor(config)
        with pytest.raises(OSError): os.fstat(read_fd)
        assert events == ['identity-close', 'transport-close', 'transport-close']
    finally:
        os.close(write_fd)


@pytest.mark.asyncio
@pytest.mark.parametrize('interruption', ['stop_after_enrollment', 'cancel_after_channel'])
async def test_pair_runner_interruptions_close_pre_adoption_resources(monkeypatch, interruption):
    config = module.parse_configuration(canonical(pair_configuration()))
    monkeypatch.setattr(module, '_process', lambda: None)
    read_fd, write_fd = os.pipe()
    events, state = [], {}
    class Verifier:
        def claim_channel(self, fd):
            if interruption == 'cancel_after_channel': asyncio.current_task().cancel()
        def implementation_commit(self): return config.release.implementation_commit
        def close(self): events.append('identity-close')
    class Transport:
        def __init__(self, **kwargs): events.append('transport')
        def issue_pair_enrollment(self, **kwargs):
            if interruption == 'stop_after_enrollment': state['stop'].set()
            return object()
        def issue_enrolled_pair_channel(self, **kwargs): return read_fd, Verifier()
        def close(self): events.append('transport-close')
    @contextmanager
    def signals(stop):
        state['stop'] = stop
        yield
    async def unexpected_broker(*args, **kwargs): pytest.fail('broker must not be entered')
    monkeypatch.setattr(module, 'UnixLaunchReceiptTransport', Transport)
    monkeypatch.setattr(module, '_signal_stop', signals)
    monkeypatch.setattr(module, 'serve_owned_inherited_broker', unexpected_broker)
    try:
        expected = module.InstalledRunnerError if interruption == 'stop_after_enrollment' else asyncio.CancelledError
        with pytest.raises(expected): await module.run_installed_executor(config)
        if interruption == 'cancel_after_channel':
            with pytest.raises(OSError): os.fstat(read_fd)
            assert events == ['transport', 'transport', 'identity-close', 'transport-close', 'transport-close']
        else:
            os.close(read_fd)
            assert events == ['transport', 'transport-close']
    finally:
        os.close(write_fd)


def changed(value, path, replacement):
    keys = path.split('.')
    target = value
    for key in keys[:-1]:
        target = target[int(key)] if type(target) is list else target[key]
    key = int(keys[-1]) if type(target) is list else keys[-1]
    target[key] = replacement


def test_closed_configuration_converts_only_existing_explicit_provider_arguments():
    source = configuration()
    parsed = module.parse_configuration(canonical(source))
    assert type(parsed.manifest) is PilotManifest
    assert parsed.release.implementation_commit == parsed.manifest.driver.implementation_commit
    assert parsed.release.public_key == bytes.fromhex('a' * 64)
    assert parsed.bearer_slot == ('test-executor-service', 1)
    assert type(parsed.resources['secret_bindings']) is tuple
    assert type(parsed.resources['secret_bindings'][0]) is tuple
    assert type(parsed.resources['build_enrollments']) is tuple
    assert parsed.process_id == os.getpid()
    source['resources']['secret_bindings'][0][0] = 'changed-after-parse'
    assert parsed.resources['secret_bindings'][0][0] == 'test-codex'
    with pytest.raises(TypeError):
        parsed.resources['journal_create'] = False


@pytest.mark.parametrize('path,value', [
    ('schema', 'unknown'), ('extra', 'unapproved'),
    ('resources.extra', '/unexpected'), ('resources.journal_create', 1),
    ('resources.state_reservations_create', 'true'),
    ('resources.record_root', 'relative'), ('resources.metadata_root', '/a/../b'),
    ('resources.secret_bindings.0.1', True),
    ('resources.secret_bindings.0.0', 'production-codex'),
    ('resources.secret_bindings.0.2', 'not-a-digest'),
    ('resources.build_enrollments.0.extra', 'unapproved'),
    ('resources.build_enrollments.0.invocation_id', 'not-a-uuid'),
    ('docker_binding.api_version', '1.40'), ('docker_binding.runner_id', 'different'),
    ('docker_binding.platform.extra', 1),
    ('release_enrollment.public_key_hex', 'AA' * 32),
    ('release_enrollment.implementation_commit', 'd' * 40),
    ('release_enrollment.platform.architecture', 'arm64'),
    ('launch_transport.extra', 'unexpected'), ('launch_transport.expected_issuer_uid', True),
    ('launch_transport.expected_issuer_gid', -1), ('launch_transport.socket_path', '/a//b'),
    ('max_receipt_lifetime_seconds', True), ('max_receipt_lifetime_seconds', 3601),
    ('exchange_timeout_ms', 99), ('exchange_timeout_ms', 10001),
    ('supervisor_stop_grace_seconds', 100), ('supervisor_stop_grace_seconds', True),
    ('executor_socket_path', '/run/fixture/issuer.sock'),
    ('executor_socket_path', '/run/fixture/engine.sock'),
    ('bearer_test_binding.slot_id', 'test-missing'), ('bearer_test_binding.version', True),
])
def test_invalid_closed_configurations_are_sanitized(path, value):
    source = configuration()
    changed(source, path, value)
    with pytest.raises(module.InstalledRunnerError, match='^Installed executor unavailable$'):
        module.parse_configuration(canonical(source))


@pytest.mark.parametrize('kind', ['same-slot', 'same-digest', 'duplicate', 'missing-app-slot'])
def test_executor_bearer_cannot_be_an_application_agent_credential(kind):
    source = configuration()
    if kind == 'same-slot':
        source['bearer_test_binding'] = dict(slot_id='test-codex', version=1)
    elif kind == 'same-digest':
        source['resources']['secret_bindings'][-1][2] = digest('1')
    elif kind == 'duplicate':
        source['resources']['secret_bindings'].append(source['resources']['secret_bindings'][-1])
    else:
        source['resources']['secret_bindings'].pop(0)
    with pytest.raises(module.InstalledRunnerError):
        module.parse_configuration(canonical(source))


@pytest.mark.parametrize('transform', [
    lambda raw: b' ' + raw,
    lambda raw: raw.replace(b'"exchange_timeout_ms":1000', b'"exchange_timeout_ms":1.0'),
    lambda raw: raw.replace(b'"exchange_timeout_ms":1000', b'"exchange_timeout_ms":NaN'),
    lambda raw: raw[:-1] + b',"schema":"duplicate"}',
    lambda raw: b'\xff', lambda raw: b'x' * (module.MAX_CONFIG_BYTES + 1),
])
def test_bounded_canonical_duplicate_free_json(transform):
    with pytest.raises(module.InstalledRunnerError):
        module.parse_configuration(transform(canonical(configuration())))


@pytest.mark.parametrize('platform,uid,gid,pid', [
    ('win32', 10001, 10001, 1), ('linux', 0, 10001, 1),
    ('linux', 10001, 0, 1), ('linux', 10001, 10001, 2),
])
def test_kernel_process_contract_denies_before_config_io(monkeypatch, platform, uid, gid, pid):
    monkeypatch.setattr(module.sys, 'platform', platform)
    monkeypatch.setattr(module.os, 'getuid', lambda: uid)
    monkeypatch.setattr(module.os, 'getgid', lambda: gid)
    monkeypatch.setattr(module.os, 'getpid', lambda: pid)
    monkeypatch.setattr(module.os, 'open', lambda *a, **k: pytest.fail('No config I/O allowed'))
    with pytest.raises(module.InstalledRunnerError):
        module.load_configuration('/never-opened/config.json')


def test_config_read_requires_readonly_mount_and_preserves_file(monkeypatch, tmp_path):
    path = tmp_path / 'runner.json'
    raw = canonical(configuration())
    path.write_bytes(raw)
    path.chmod(0o600)
    monkeypatch.setattr(module, '_process', lambda: None)  # Synthetic process context only.
    with pytest.raises(module.InstalledRunnerError):
        module.load_configuration(str(path))  # Actual writable tmpfs/fixture filesystem.
    assert path.read_bytes() == raw
    monkeypatch.setattr(module.os, 'fstatvfs', lambda fd: SimpleNamespace(f_flag=os.ST_RDONLY))
    assert module.load_configuration(str(path)).manifest.driver.implementation_commit == 'c' * 40


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'mode', 'changed', 'oversized', 'empty'])
def test_config_file_integrity_denials(monkeypatch, tmp_path, kind):
    path = tmp_path / 'runner.json'
    path.write_bytes(canonical(configuration()))
    path.chmod(0o600)
    monkeypatch.setattr(module, '_process', lambda: None)
    monkeypatch.setattr(module.os, 'fstatvfs', lambda fd: SimpleNamespace(f_flag=os.ST_RDONLY))
    if kind == 'symlink':
        alias = tmp_path / 'alias.json'
        alias.symlink_to(path)
        path = alias
    elif kind == 'hardlink':
        os.link(path, tmp_path / 'other.json')
    elif kind == 'mode':
        path.chmod(0o640)
    elif kind == 'empty':
        path.write_bytes(b'')
    elif kind == 'oversized':
        path.write_bytes(b'x' * (module.MAX_CONFIG_BYTES + 1))
    else:
        original = module.os.read
        once = []
        def read(fd, size):
            data = original(fd, size)
            if not once:
                once.append(True)
                with path.open('ab') as stream:
                    stream.write(b'changed')
            return data
        monkeypatch.setattr(module.os, 'read', read)
    with pytest.raises(module.InstalledRunnerError):
        module.load_configuration(str(path))


@pytest.fixture
def lifecycle(monkeypatch):
    config = module.parse_configuration(canonical(configuration()))
    events = []
    monkeypatch.setattr(module, '_process', lambda: None)
    class Transport:
        def __init__(self, **kwargs):
            assert kwargs == dict(config.transport)
            events.append('transport')
        def close(self):
            events.append('transport-close')
    class Verifier:
        def __init__(self, **kwargs):
            assert kwargs['enrollment'] is config.release
            events.append('identity')
        def implementation_commit(self):
            events.append('identity-check')
            return config.release.implementation_commit
        def close(self):
            events.append('identity-close')
    class Observations:
        def __init__(self, **kwargs):
            assert kwargs == dict(config.transport)
            events.append('observation-transport')
        def close(self):
            events.append('observation-close')
    class Secrets:
        def __init__(self, root, *, bindings):
            assert root == config.resources['secret_root']
            assert bindings == config.resources['secret_bindings']
            events.append('secret-open')
        def __enter__(self):
            return self
        def __exit__(self, *args):
            events.append('secret-close')
        def resolve_test_secret(self, slot, version):
            assert (slot, version) == config.bearer_slot
            events.append('secret-read')
            return 'synthetic-executor-only-token'
    @contextmanager
    def resources(**kwargs):
        assert kwargs['manifest'] is config.manifest and kwargs['binding'] is config.binding
        assert type(kwargs['driver_identity_verifier']) is Verifier
        assert set(kwargs) == set(config.resources) | {'manifest', 'binding', 'driver_identity_verifier'}
        events.append('resources')
        try:
            yield object()
        finally:
            events.append('resources-close')
    async def service(factory, **kwargs):
        assert type(kwargs.pop('current_observation_transport')) is Observations
        assert kwargs.pop('current_observation_timeout_ms') == config.timeout_ms
        assert kwargs == dict(bearer_token='synthetic-executor-only-token',
            supervisor_stop_grace_seconds=config.stop_grace_seconds, socket_path=config.socket_path)
        events.append('service')
        with factory():
            events.append('draining')
        events.append('drained')
    monkeypatch.setattr(module, 'UnixLaunchReceiptTransport', Transport)
    monkeypatch.setattr(module, 'UnixCurrentObservationTransport', Observations)
    monkeypatch.setattr(module, 'LaunchReceiptIdentityVerifier', Verifier)
    monkeypatch.setattr(module, 'ImmutableTestSecretResolver', Secrets)
    monkeypatch.setattr(module, 'local_host_adapter_resources', resources)
    monkeypatch.setattr(module, 'serve_owned_host_executor_unix', service)
    return config, events


@pytest.mark.asyncio
async def test_direct_identity_precedes_secret_provider_state_and_service_drains_first(lifecycle):
    config, events = lifecycle
    await module.run_installed_executor(config)
    assert events == ['transport', 'identity', 'identity-check', 'observation-transport', 'secret-open', 'secret-read',
        'secret-close', 'service', 'resources', 'draining', 'resources-close', 'drained',
        'observation-close', 'identity-close', 'transport-close']


@pytest.mark.asyncio
async def test_failed_receipt_never_opens_providers_or_service(lifecycle, monkeypatch):
    config, events = lifecycle
    def denied(**kwargs):
        raise RuntimeError('sensitive internal details')
    monkeypatch.setattr(module, 'LaunchReceiptIdentityVerifier', denied)
    with pytest.raises(module.InstalledRunnerError, match='^Installed executor unavailable$'):
        await module.run_installed_executor(config)
    assert events == ['transport', 'transport-close']


@pytest.mark.asyncio
async def test_failed_service_closes_identity_only_after_accepted_cleanup(lifecycle, monkeypatch):
    config, events = lifecycle
    async def denied(factory, **kwargs):
        with factory():
            events.append('drained-before-failure')
        raise RuntimeError('sensitive service details')
    monkeypatch.setattr(module, 'serve_owned_host_executor_unix', denied)
    with pytest.raises(module.InstalledRunnerError):
        await module.run_installed_executor(config)
    assert events[-5:] == ['drained-before-failure', 'resources-close', 'observation-close', 'identity-close', 'transport-close']


@pytest.mark.asyncio
async def test_cancelled_service_finishes_owned_lifecycle_before_identity_cleanup(lifecycle, monkeypatch):
    config, events = lifecycle
    async def cancelled(factory, **kwargs):
        with factory():
            events.append('accepted-drain-finished')
        raise asyncio.CancelledError
    monkeypatch.setattr(module, 'serve_owned_host_executor_unix', cancelled)
    with pytest.raises(asyncio.CancelledError):
        await module.run_installed_executor(config)
    assert events[-4:] == ['resources-close', 'observation-close', 'identity-close', 'transport-close']


@pytest.mark.asyncio
async def test_forked_configuration_denied_before_issuer_or_provider_use(lifecycle):
    config, events = lifecycle
    with pytest.raises(module.InstalledRunnerError):
        await module.run_installed_executor(replace(config, process_id=config.process_id + 1))
    assert events == []


@pytest.mark.parametrize('arguments', [[], ['--config', '/sentinel', '--config', '/other'],
    ['--conf', '/sentinel'], ['--config', '/sentinel', '--extra-secret', 'not-public']])
def test_cli_invalid_arguments_sanitized_before_io(monkeypatch, capsys, arguments):
    monkeypatch.setattr(module, 'load_configuration', lambda *a: pytest.fail('No configuration I/O'))
    assert module.main(arguments) == 2
    output = capsys.readouterr()
    assert output.out == '' and output.err == 'Installed executor unavailable\n'


def test_v6_baseline_authority_is_signed_config_bound_and_gate_remains_disabled():
    from hub_container_executor.configuration_commitment import configuration_commitment
    data = configuration(); data['schema'] = module.BASELINE_GATE_SCHEMA
    data['gate_socket_directory'] = '/fixture/gate-control'
    data['baseline_gate'] = dict(issuer_id='baseline-issuer', issuer_public_key_hex='b' * 64,
        receipt_public_key_hex='c' * 64, socket_source_directory='/fixture/baseline-gate',
        launch_transport=dict(socket_path='/run/fixture/baseline-issuer.sock', expected_issuer_uid=65533,
                              expected_issuer_gid=65533))
    parsed = module.parse_configuration(canonical(data))
    assert parsed.baseline_gate['source_directory'] == '/fixture/baseline-gate'
    assert parsed.baseline_gate['public_key'] == bytes.fromhex('c' * 64)
    assert parsed.gate_ledger_channel is True
    with pytest.raises(module.InstalledRunnerError): module._require_supported_gate_runtime(parsed)
    old = configuration_commitment(canonical(data)).content_digest
    data['baseline_gate']['receipt_public_key_hex'] = 'd' * 64
    assert configuration_commitment(canonical(data)).content_digest != old
    data['baseline_gate']['socket_source_directory'] = '/fixture/gate-control/nested'
    with pytest.raises(module.InstalledRunnerError): module.parse_configuration(canonical(data))
