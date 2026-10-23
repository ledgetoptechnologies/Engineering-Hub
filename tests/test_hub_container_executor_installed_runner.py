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
