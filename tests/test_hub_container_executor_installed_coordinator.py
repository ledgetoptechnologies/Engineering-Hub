"""Synthetic parser/lifecycle tests, not installed PID1/mount or host approval."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from types import SimpleNamespace

import pytest
from tests.manual_session_fixtures import synthetic_reconciliation

pytest.importorskip('fcntl', reason='Optional coordinator uses Linux providers')

from hub_container_executor import installed_coordinator as module
from engineering_hub.work_queue import WorkTarget
from tests.test_hub_container_pilot_manifest import make_manifest_data


def run_owned_loop(coroutine):
    # Explicit ownership: Runner closes only its new loop, without replacing or
    # clearing pytest-asyncio's ambient policy loop (and orphaning its sockets).
    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        return runner.run(coroutine)


def test_owned_runner_preserves_ambient_policy_loop():
    previous_policy = asyncio.get_event_loop_policy()
    policy = asyncio.DefaultEventLoopPolicy()
    retained = policy.new_event_loop()
    policy.set_event_loop(retained)
    asyncio.set_event_loop_policy(policy)
    try:
        assert run_owned_loop(asyncio.sleep(0, result='done')) == 'done'
        assert policy.get_event_loop() is retained and not retained.is_closed()
    finally:
        retained.close()
        policy.set_event_loop(None)
        asyncio.set_event_loop_policy(previous_policy)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def configuration():
    return dict(schema=module.SCHEMA, manifest=make_manifest_data(),
        database_path='/pilot/controller/hub.sqlite3',
        coordinator_socket_path='/pilot/browser/hub.sock', executor_socket_path='/pilot/executor/host.sock',
        agent_port=18870, machine_id='staging-pc', action_key=module.ACTION,
        allowed_actors=['codex', 'hermes', 'claude'],
        public_auth=dict(base_url='https://pilot.example.test',
            cloudflare_issuer='https://fixture.cloudflareaccess.com', cloudflare_audience='fixture-pilot',
            cloudflare_allowed_emails=['operator@example.test']),
        secret_root='/pilot/secrets', secret_bindings={name: dict(slot_id='test-' + name,
            version=1, digest='sha256:' + str(index) * 64)
            for index, name in enumerate(('codex', 'hermes', 'claude', 'executor'), 1)},
        supervisor_stop_grace_seconds=1200)


def parse(value=None):
    return module.parse_configuration(canonical(configuration() if value is None else value))


def test_pure_parse_and_exact_additional_policy():
    config = parse()
    target = WorkTarget('1398cf6d-cebd-4f79-a913-be70321970c9',
        config.manifest.candidate.source_commit, config.manifest.candidate.image_config_id)
    assert module.target_allowed(config, 'codex', module.ACTION, 'staging', target)
    assert not module.target_allowed(config, 'admin', module.ACTION, 'staging', target)
    assert not module.target_allowed(config, 'codex', module.ACTION, 'production', target)
    assert not module.target_allowed(config, 'codex', 'different', 'staging', target)
    assert not module.target_allowed(config, 'codex', module.ACTION, 'staging',
        replace(target, source_commit='f' * 40))
    assert not module.target_allowed(config, 'codex', module.ACTION, 'staging',
        replace(target, artifact_digest='sha256:' + 'f' * 64))
    assert not module.target_allowed(config, 'codex', module.ACTION, 'staging', target.__dict__)
    with pytest.raises(TypeError):
        config.roles['admin'] = ('test-admin', 1)


@pytest.mark.parametrize('key,value', [
    ('schema', 'other'), ('machine_id', 'production'), ('action_key', 'arbitrary'),
    ('agent_port', True), ('agent_port', 8765), ('agent_port', 8770), ('agent_port', 8771),
    ('agent_port', 0), ('agent_port', 65536), ('allowed_actors', []),
    ('allowed_actors', ['codex', 'codex']), ('allowed_actors', ['admin']),
    ('database_path', '../state'), ('executor_socket_path', '/a/../x'),
    ('executor_socket_path', '/a/' + 'x' * 108), ('supervisor_stop_grace_seconds', 1119),
    ('supervisor_stop_grace_seconds', True),
])
def test_closed_scalar_policy_denials(key, value):
    data = configuration()
    data[key] = value
    with pytest.raises(module.InstalledCoordinatorError):
        parse(data)


@pytest.mark.parametrize('kind', ['unknown', 'duplicate-json', 'noncanonical', 'float',
    'unknown-auth', 'github', 'http', 'duplicate-email', 'unknown-secret', 'duplicate-slot',
    'duplicate-digest', 'non-test-slot', 'bool-version', 'path-alias', 'secret-nesting',
    'parent-alias', 'parent-ancestor', 'secret-parent-ancestor'])
def test_closed_nested_shape_and_canonical_denials(kind):
    data = configuration()
    if kind == 'unknown': data['factory'] = 'untrusted.module'
    elif kind == 'unknown-auth': data['public_auth']['cookie_name'] = 'ops'
    elif kind == 'github': data['public_auth']['github_client_secret'] = 'never-accepted'
    elif kind == 'http': data['public_auth']['base_url'] = 'http://127.0.0.1'
    elif kind == 'duplicate-email': data['public_auth']['cloudflare_allowed_emails'] *= 2
    elif kind == 'unknown-secret': data['secret_bindings']['admin'] = data['secret_bindings']['codex']
    elif kind == 'duplicate-slot': data['secret_bindings']['executor'] = data['secret_bindings']['codex']
    elif kind == 'duplicate-digest': data['secret_bindings']['executor']['digest'] = data['secret_bindings']['codex']['digest']
    elif kind == 'non-test-slot': data['secret_bindings']['executor']['slot_id'] = 'production-token'
    elif kind == 'bool-version': data['secret_bindings']['executor']['version'] = True
    elif kind == 'path-alias': data['executor_socket_path'] = data['coordinator_socket_path']
    elif kind == 'secret-nesting': data['database_path'] = data['secret_root'] + '/database'
    elif kind == 'parent-alias': data['executor_socket_path'] = '/pilot/controller/executor.sock'
    elif kind == 'parent-ancestor': data['executor_socket_path'] = '/pilot/controller/nested/executor.sock'
    elif kind == 'secret-parent-ancestor': data['secret_root'] = '/pilot'
    raw = canonical(data)
    if kind == 'duplicate-json': raw = raw[:-1] + b',"schema":"duplicate"}'
    elif kind == 'noncanonical': raw += b'\n'
    elif kind == 'float': raw = raw.replace(b'18870', b'18870.0')
    with pytest.raises(module.InstalledCoordinatorError):
        module.parse_configuration(raw)


def install_fakes(monkeypatch, events, *, failure=False, expected_acceptance=None):
    monkeypatch.setattr(module, '_process', lambda: events.append('process'))

    @contextmanager
    def secrets(root, *, bindings):
        events.append('secrets-enter')
        try:
            yield SimpleNamespace(resolve_test_secret=lambda slot, version: 'fixture-' + slot)
        finally:
            events.append('secrets-close')
    monkeypatch.setattr(module, 'ImmutableTestSecretResolver', secrets)

    class Client:
        @staticmethod
        def for_private_unix(path, token):
            events.append('client')
            assert path == '/pilot/executor/host.sock' and token == 'fixture-test-executor'
            return object()
    monkeypatch.setattr(module, 'ContainerPilotClient', Client)

    async def serve(settings, manifest, factory, **kwargs):
        events.append('serve')
        assert kwargs['session_acceptance_factory'] is expected_acceptance
        assert kwargs['reconciliation_factory'] is synthetic_reconciliation
        assert settings.work_queue_enabled and settings.agent_auth.admin_token is None
        assert settings.agent_auth.allowed_ips == frozenset({'127.0.0.1'})
        assert kwargs['agent_host'] == '127.0.0.1' and kwargs['agent_port'] == 18870
        assert kwargs['machine_id'] == 'staging-pc'
        assert settings.auth_config.human_auth_mode == 'cloudflare_access'
        assert factory() is not factory()
        if failure:
            raise RuntimeError('SECRET-PATH-MUST-NOT-ESCAPE')
        await asyncio.sleep(0)
        events.append('drained')
    monkeypatch.setattr(module, 'serve_owned_queued_pilot', serve)


@pytest.mark.parametrize('with_acceptance', [False, True])
def test_concrete_factory_and_service_lifetime(monkeypatch, with_acceptance):
    events = []
    def acceptance(*_):
        pytest.fail('selector must forward, not invoke, trusted acceptance factory')
    factory = acceptance if with_acceptance else None
    install_fakes(monkeypatch, events, expected_acceptance=factory)
    run_owned_loop(module.run_installed_coordinator(parse(), reconciliation_factory=synthetic_reconciliation,
                                                    session_acceptance_factory=factory))
    assert events == ['process', 'secrets-enter', 'secrets-close', 'serve', 'client', 'client', 'drained']


@pytest.mark.parametrize('kind', ['bool', 'object', 'async-function', 'async-callable', 'async-generator'])
def test_invalid_acceptance_factory_denied_before_secret_provider(monkeypatch, kind):
    async def async_function(*_):
        pytest.fail('async factory must not execute')
    class AsyncCallable:
        async def __call__(self, *_):
            pytest.fail('async callable must not execute')
    async def async_generator(*_):
        yield True
    factory = {'bool': True, 'object': object(), 'async-function': async_function,
               'async-callable': AsyncCallable(), 'async-generator': async_generator}[kind]
    monkeypatch.setattr(module, '_process', lambda: None)
    def forbidden(*_args, **_kwargs):
        pytest.fail('must reject factory before TEST provider or service/owner construction')
    monkeypatch.setattr(module, 'ImmutableTestSecretResolver', forbidden)
    monkeypatch.setattr(module, 'serve_owned_queued_pilot', forbidden)
    with pytest.raises(module.InstalledCoordinatorError):
        run_owned_loop(module.run_installed_coordinator(parse(),
            reconciliation_factory=synthetic_reconciliation, session_acceptance_factory=factory))


def test_failed_service_is_sanitized(monkeypatch):
    events = []
    install_fakes(monkeypatch, events, failure=True)
    with pytest.raises(module.InstalledCoordinatorError, match='^Installed pilot coordinator unavailable$'):
        run_owned_loop(module.run_installed_coordinator(parse(), reconciliation_factory=synthetic_reconciliation))


def test_fork_denied_before_secrets(monkeypatch):
    events = []
    install_fakes(monkeypatch, events)
    with pytest.raises(module.InstalledCoordinatorError):
        run_owned_loop(module.run_installed_coordinator(replace(parse(), process_id=os.getpid() + 1), reconciliation_factory=synthetic_reconciliation))
    assert events == ['process']


def test_old_core_without_private_unix_api_denied_before_secret_provider(monkeypatch):
    events = []
    monkeypatch.setattr(module, '_process', lambda: events.append('process'))
    monkeypatch.setattr(module, 'ContainerPilotClient', type('OlderCoreClient', (), {}))
    monkeypatch.setattr(module, 'ImmutableTestSecretResolver',
        lambda *args, **kwargs: pytest.fail('must not access TEST provider with incompatible core'))
    monkeypatch.setattr(module, 'serve_owned_queued_pilot',
        lambda *args, **kwargs: pytest.fail('must not construct owner with incompatible core'))
    with pytest.raises(module.InstalledCoordinatorError):
        run_owned_loop(module.run_installed_coordinator(parse(), reconciliation_factory=synthetic_reconciliation))
    assert events == ['process']


def test_validate_never_serves_or_resolves_secrets(monkeypatch):
    monkeypatch.setattr(module, 'load_configuration', lambda path: parse())
    monkeypatch.setattr(module, 'ImmutableTestSecretResolver', lambda *args, **kwargs: pytest.fail('secret effect'))
    monkeypatch.setattr(module, 'run_installed_coordinator', lambda *args: pytest.fail('service effect'))
    assert module.main(['validate', '--config', '/operator/coordinator.json']) == 0


@pytest.mark.parametrize('method,bad', [('getuid', 0), ('geteuid', 0), ('getgid', 0),
    ('getegid', 0), ('getpid', 2)])
def test_process_fence_rejects_wrong_identity_and_non_pid1(monkeypatch, method, bad):
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    for name in ('getuid', 'geteuid', 'getgid', 'getegid'):
        monkeypatch.setattr(module.os, name, lambda: 10001)
    monkeypatch.setattr(module.os, 'getpid', lambda: 1)
    module._process()
    monkeypatch.setattr(module.os, method, lambda: bad)
    with pytest.raises(module.InstalledCoordinatorError):
        module._process()


def test_cancellation_propagates_after_existing_service_drains(monkeypatch):
    events = []
    install_fakes(monkeypatch, events)

    async def service(*args, **kwargs):
        try:
            raise asyncio.CancelledError()
        finally:
            events.append('existing-service-drain')
    monkeypatch.setattr(module, 'serve_owned_queued_pilot', service)
    with pytest.raises(asyncio.CancelledError):
        run_owned_loop(module.run_installed_coordinator(parse(), reconciliation_factory=synthetic_reconciliation))
    assert events[-1] == 'existing-service-drain'


@pytest.mark.parametrize('arguments', [[], ['serve', '--config', '/SECRET', '--unknown'],
    ['validate', '--config', '/SECRET', '--config', '/OTHER'], ['repair', '--config', '/SECRET']])
def test_cli_sanitized(arguments, capsys):
    assert module.main(arguments) == 2
    assert capsys.readouterr().err == 'Installed pilot coordinator unavailable\n'


def test_readonly_file_guard_with_explicit_synthetic_readonly_flag(tmp_path, monkeypatch):
    # The file is real; ST_RDONLY is synthetic and proves no installed mount.
    path = tmp_path / 'coordinator.json'
    path.write_bytes(canonical(configuration()))
    path.chmod(0o600)
    monkeypatch.setattr(module, '_process', lambda: None)
    monkeypatch.setattr(module.os, 'fstatvfs', lambda fd: SimpleNamespace(f_flag=os.ST_RDONLY))
    assert module.load_configuration(str(path)).port == 18870
    monkeypatch.setattr(module.os, 'fstatvfs', lambda fd: SimpleNamespace(f_flag=0))
    with pytest.raises(module.InstalledCoordinatorError):
        module.load_configuration(str(path))


def test_config_is_not_inside_writable_controller_parent(tmp_path, monkeypatch):
    path = tmp_path / 'coordinator.json'
    data = configuration()
    data['database_path'] = str(tmp_path / 'hub.sqlite3')
    path.write_bytes(canonical(data))
    path.chmod(0o600)
    monkeypatch.setattr(module, '_process', lambda: None)
    monkeypatch.setattr(module.os, 'fstatvfs', lambda fd: SimpleNamespace(f_flag=os.ST_RDONLY))
    with pytest.raises(module.InstalledCoordinatorError):
        module.load_configuration(str(path))


def test_actual_mcp_transport_accepts_configured_loopback_port_without_host_override(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from engineering_hub.app import create_app
    events, captured = [], []
    install_fakes(monkeypatch, events)

    async def capture(settings, *args, **kwargs):
        captured.append(settings)
    monkeypatch.setattr(module, 'serve_owned_queued_pilot', capture)
    run_owned_loop(module.run_installed_coordinator(parse(), reconciliation_factory=synthetic_reconciliation))
    settings = replace(captured[0], database=tmp_path / 'hub.sqlite3')
    app = create_app(settings)
    headers = {'Authorization': 'Bearer fixture-test-codex',
               'Accept': 'application/json, text/event-stream',
               'MCP-Protocol-Version': '2025-06-18'}
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-06-18', 'capabilities': {},
        'clientInfo': {'name': 'coordinator-test', 'version': '1'}}}
    # Real MCP middleware and actual URL-derived Host, not a custom Host header.
    with TestClient(app, base_url='http://127.0.0.1:18870', client=('127.0.0.1', 31000)) as client:
        assert client.post('/mcp/', headers=headers, json=body).status_code == 200
        assert client.post('http://127.0.0.1:18871/mcp/', headers=headers, json=body).status_code == 421


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'mode', 'empty', 'directory', 'oversize'])
def test_file_denials_without_providers(tmp_path, monkeypatch, kind):
    path = tmp_path / 'coordinator.json'
    path.write_bytes(canonical(configuration()))
    path.chmod(0o600)
    monkeypatch.setattr(module, '_process', lambda: None)
    monkeypatch.setattr(module.os, 'fstatvfs', lambda fd: SimpleNamespace(f_flag=os.ST_RDONLY))
    if kind == 'symlink':
        link = tmp_path / 'alias'
        link.symlink_to(path)
        path = link
    elif kind == 'hardlink': os.link(path, tmp_path / 'linked')
    elif kind == 'mode': path.chmod(0o640)
    elif kind == 'empty': path.write_bytes(b'')
    elif kind == 'directory': path = tmp_path
    elif kind == 'oversize': path.write_bytes(b'x' * (module.MAX_CONFIG_BYTES + 1))
    with pytest.raises(module.InstalledCoordinatorError):
        module.load_configuration(str(path))
