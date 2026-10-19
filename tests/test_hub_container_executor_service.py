"""Owned loopback HTTP lifecycle with real adapter/ledger and synthetic providers.

No Docker daemon, real provenance issuer, production host, or installed service
is exercised. Factory freshness, dedication, and trustworthy provider cleanup
remain caller preconditions, not properties inferred from an exact-type check.
"""
import asyncio
from contextlib import contextmanager
import os
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

if sys.platform != 'linux':
    pytest.skip('Real Linux host-adapter ownership and loopback service', allow_module_level=True)

import httpx

from engineering_hub.container_pilot_client import ContainerPilotClient
from hub_container_executor.adapter import HostAdapter
from hub_container_executor.ledger import LedgerClosed, OwnerBusy
from tests.test_hub_container_executor_adapter import make_fixture, command


TOKEN = 'synthetic-host-service-token'
COMMANDS = '/v1/container-pilot/commands'


@pytest.fixture
def owned(tmp_path):
    from hub_container_executor import service

    state = SimpleNamespace(module=service, root=tmp_path, entered=0, exited=0,
        provider_closes=0, adapter_closes=0, events=[], adapters=[], block=None,
        failed_probe=False, cleanup_error=False, adapter_close_block=None,
        provider_exit_block=None)

    @contextmanager
    def factory():
        state.entered += 1
        directory = state.root / ('factory-' + str(state.entered))
        directory.mkdir(mode=0o700)
        adapter, backend, provisioner, manifest = make_fixture(directory)
        backend.probe_health = ['unhealthy', 'healthy'] if state.failed_probe else ['healthy', 'healthy']
        state.adapter, state.backend, state.provisioner, state.manifest = adapter, backend, provisioner, manifest
        state.path = directory / 'private/ledger.sqlite3'
        state.adapters.append(adapter)
        close = adapter.close
        def close_adapter():
            state.adapter_closes += 1
            state.events.append('adapter-close')
            if state.adapter_close_block is not None:
                state.adapter_close_block()
            close()
        adapter.close = close_adapter
        original_create = backend.create_container
        def create(*args, **kwargs):
            assert state.provider_closes == 0
            if state.block is not None:
                state.block()
            return original_create(*args, **kwargs)
        backend.create_container = create
        try:
            yield adapter
        finally:
            state.exited += 1
            assert adapter._active_operations == 0
            try:
                if not adapter._closed:
                    adapter.close()
            finally:
                if state.provider_exit_block is not None:
                    state.provider_exit_block()
                state.provider_closes += 1
                state.events.append('providers-close')
            if state.cleanup_error:
                raise RuntimeError('synthetic-private-provider-cleanup-secret')

    state.factory = factory
    return state


def options(**changes):
    values = dict(bearer_token=TOKEN, host='127.0.0.1', port=0,
        supervisor_stop_grace_seconds=2000, install_signals=False)
    values.update(changes)
    return values


async def start(state, **changes):
    stop = asyncio.Event()
    ready = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(state.module.serve_owned_host_executor(state.factory,
        **options(stop_event=stop, on_ready=lambda address: ready.set_result(address), **changes)))
    try:
        address = await asyncio.wait_for(asyncio.shield(ready), 8)
    except BaseException:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)
        raise
    return stop, task, address


def origin(address):
    return 'http://[' + address[0] + ']:' + str(address[1]) if ':' in address[0] else (
        'http://' + address[0] + ':' + str(address[1]))


def assert_closed(state):
    assert state.entered == state.exited == state.provider_closes == 1
    assert state.adapter_closes == 1
    assert state.events[-2:] == ['adapter-close', 'providers-close']
    assert state.adapter._closed
    with pytest.raises(LedgerClosed):
        state.adapter.get(str(uuid4()))


def assert_port_released(address):
    family = socket.AF_INET6 if ':' in address[0] else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(address[:2])
    finally:
        sock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('rollback', [False, True], ids=['healthy', 'rollback'])
async def test_real_loopback_auth_typed_commands_and_read_only_get_persistence(owned, rollback):
    state = owned
    state.failed_probe = rollback
    stop, serving, address = await start(state)
    commands = []
    try:
        async with httpx.AsyncClient(base_url=origin(address), trust_env=False) as anonymous:
            response = await anonymous.get(COMMANDS + '/' + str(uuid4()))
            assert response.status_code == 401 and TOKEN not in response.text
        async with ContainerPilotClient(origin(address), TOKEN) as client:
            run_id = str(uuid4())
            phases = ['deploy_container', 'smoke_container']
            if rollback:
                phases.extend(['rollback_container', 'smoke_container_rollback'])
            for phase in phases:
                item = command(state.manifest, phase, run_id=run_id)
                result = await client.submit_command(item)
                assert result.command_id == item.command_id and not result.replayed
                assert result.state == ('failed' if rollback and phase == 'smoke_container' else 'succeeded')
                commands.append((item, result))
            events = list(state.backend.events)
            for item, expected in commands:
                assert await client.get_command(item) == expected
            assert state.backend.events == events
            assert state.provider_closes == 0 and not state.adapter._closed
    finally:
        stop.set()
        await asyncio.wait_for(serving, 8)
    assert_closed(state)
    assert_port_released(address)
    # Reopen the actual ledger explicitly to inspect receipts; this does not
    # restart a workflow, issue a new command, or call any provider/backend.
    reopened = HostAdapter(state.path, state.manifest)
    try:
        for item, expected in commands:
            assert reopened.get(item.command_id) == expected
        assert state.backend.events == events
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['0.0.0.0', '192.168.50.90', 'localhost', '127.0.0.2', '::', None])
async def test_nonexact_loopback_host_rejected_before_factory_or_bind(owned, monkeypatch, host):
    state = owned
    binds = []
    def no_bind(*args):
        binds.append(args)
        raise AssertionError('Invalid host reached bind')
    monkeypatch.setattr(state.module, '_bind', no_bind)
    with pytest.raises((ValueError, RuntimeError)):
        await state.module.serve_owned_host_executor(state.factory, **options(host=host))
    assert binds == [] and state.entered == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['token-empty', 'token-whitespace', 'port-bool', 'port-negative',
    'grace-bool', 'grace-short', 'signals-nonbool', 'stop-invalid', 'ready-invalid', 'factory-async'])
async def test_invalid_options_are_inert_before_bind_or_context_entry(owned, monkeypatch, changed):
    state = owned
    values = options()
    factory = state.factory
    async def asynchronous(*args):
        return True
    if changed == 'token-empty': values['bearer_token'] = ''
    elif changed == 'token-whitespace': values['bearer_token'] = 'private secret'
    elif changed == 'port-bool': values['port'] = True
    elif changed == 'port-negative': values['port'] = -1
    elif changed == 'grace-bool': values['supervisor_stop_grace_seconds'] = True
    elif changed == 'grace-short': values['supervisor_stop_grace_seconds'] = 1119
    elif changed == 'signals-nonbool': values['install_signals'] = 1
    elif changed == 'stop-invalid': values['stop_event'] = object()
    elif changed == 'ready-invalid': values['on_ready'] = object()
    else: factory = asynchronous
    binds = []
    def no_bind(*args):
        binds.append(args)
        raise AssertionError('Invalid configuration reached bind')
    monkeypatch.setattr(state.module, '_bind', no_bind)
    with pytest.raises((ValueError, RuntimeError)):
        await state.module.serve_owned_host_executor(factory, **values)
    assert binds == [] and state.entered == 0


@pytest.mark.asyncio
async def test_preclosed_stop_never_enters_factory(owned):
    state = owned
    stop = asyncio.Event()
    stop.set()
    await state.module.serve_owned_host_executor(state.factory, **options(stop_event=stop))
    assert state.entered == 0


@pytest.mark.asyncio
async def test_real_bind_failure_never_enters_factory(owned):
    state = owned
    held = socket.socket(socket.AF_INET)
    held.bind(('127.0.0.1', 0))
    held.listen(1)
    try:
        with pytest.raises((OSError, RuntimeError)):
            await state.module.serve_owned_host_executor(state.factory,
                **options(port=held.getsockname()[1]))
    finally:
        held.close()
    assert state.entered == 0


@pytest.mark.asyncio
async def test_factory_context_enter_failure_keeps_partial_cleanup_with_its_owner(owned):
    state = owned
    operations = []
    @contextmanager
    def partial():
        operations.append('allocate')
        try:
            raise RuntimeError('synthetic-private-factory-enter-secret')
            yield
        finally:
            operations.append('cleanup')
    with pytest.raises(RuntimeError) as failed:
        await state.module.serve_owned_host_executor(partial, **options())
    assert operations == ['allocate', 'cleanup']
    assert 'synthetic-private' not in str(failed.value)


@pytest.mark.asyncio
async def test_wrong_factory_yield_is_not_adopted_and_context_is_closed(owned):
    state = owned
    events = []
    foreign = SimpleNamespace(closes=0)
    def close():
        foreign.closes += 1
    foreign.close = close
    @contextmanager
    def wrong():
        events.append('enter')
        try:
            yield foreign
        finally:
            events.append('exit')
    with pytest.raises(RuntimeError) as failed:
        await state.module.serve_owned_host_executor(wrong, **options())
    assert events == ['enter', 'exit'] and foreign.closes == 0
    assert 'synthetic' not in str(failed.value)


@pytest.mark.asyncio
async def test_readiness_callback_failure_closes_owned_adapter_and_providers_once(owned):
    state = owned
    address = []
    def fail(actual):
        address.append(actual)
        raise RuntimeError('synthetic-private-readiness-secret')
    with pytest.raises(RuntimeError) as failed:
        await state.module.serve_owned_host_executor(state.factory, **options(on_ready=fail))
    assert 'synthetic-private' not in str(failed.value)
    assert_closed(state)
    assert_port_released(address[0])


@pytest.mark.asyncio
async def test_provider_cleanup_failure_surfaces_sanitized_after_adapter_close(owned):
    state = owned
    state.cleanup_error = True
    stop, serving, address = await start(state)
    stop.set()
    with pytest.raises(RuntimeError) as failed:
        await asyncio.wait_for(serving, 8)
    assert 'synthetic-private' not in str(failed.value)
    assert_closed(state)
    assert_port_released(address)


@pytest.mark.asyncio
async def test_required_port_cannot_be_omitted_or_implicitly_selected(owned):
    state = owned
    values = options()
    del values['port']
    with pytest.raises(TypeError):
        await state.module.serve_owned_host_executor(state.factory, **values)
    assert state.entered == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['record_store', 'metadata_store', 'provenance_verifier',
    'backend', 'baseline_provider', 'state_provisioner', 'secret_resolver', 'driver_identity_verifier'])
async def test_missing_mandatory_provider_fails_before_readiness_and_cleans_owned_bundle(owned, provider):
    state = owned
    ready = []
    @contextmanager
    def missing():
        with state.factory() as adapter:
            setattr(adapter, provider, None)
            yield adapter
    with pytest.raises(RuntimeError):
        await state.module.serve_owned_host_executor(missing,
            **options(on_ready=lambda address: ready.append(address)))
    assert ready == []
    assert_closed(state)


@pytest.mark.asyncio
async def test_exact_ipv6_loopback_binding_is_supported_when_available(owned):
    state = owned
    if not socket.has_ipv6:
        pytest.skip('Kernel has no IPv6 support')
    probe = socket.socket(socket.AF_INET6)
    try:
        probe.bind(('::1', 0))
    except OSError:
        pytest.skip('IPv6 loopback unavailable in isolated test namespace')
    finally:
        probe.close()
    stop, serving, address = await start(state, host='::1')
    try:
        assert address[0] == '::1'
        async with httpx.AsyncClient(base_url=origin(address), trust_env=False) as client:
            assert (await client.get(COMMANDS + '/' + str(uuid4()))).status_code == 401
    finally:
        stop.set()
        await asyncio.wait_for(serving, 8)
    assert_closed(state)
    assert_port_released(address)


@pytest.mark.asyncio
async def test_repeated_cancel_retains_adapter_and_providers_until_real_backend_thread_finishes(owned, monkeypatch):
    from engineering_hub import split_server
    state = owned
    entered, finished = asyncio.Event(), threading.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    captured_apps = []
    original_listener = split_server._listener
    def listener(app):
        captured_apps.append(app)
        return original_listener(app)
    monkeypatch.setattr(split_server, '_listener', listener)
    def block():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), 'Test backend release was not signalled'
        finished.set()
    state.block = block
    stop, serving, address = await start(state)
    response_task = None
    item = command(state.manifest, 'deploy_container')
    try:
        async with httpx.AsyncClient(base_url=origin(address), trust_env=False, timeout=15) as client:
            response_task = asyncio.create_task(client.post(COMMANDS,
                headers={'Authorization': 'Bearer ' + TOKEN}, json=item.model_dump()))
            await asyncio.wait_for(entered.wait(), 8)
            assert state.adapter._active_operations == 1
            serving.cancel()
            await asyncio.sleep(0)
            serving.cancel()
            await asyncio.sleep(0)
            assert stop.is_set()
            assert not serving.done() and not response_task.done() and not finished.is_set()
            assert state.provider_closes == state.adapter_closes == state.exited == 0
            with pytest.raises(OwnerBusy):
                HostAdapter(state.path, state.manifest)

            # Observe admission without guessing whether a new TCP connection
            # wins a race against listener close. The real accepted HTTP
            # request above remains blocked in its owned backend thread.
            messages = []
            async def receive():
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            async def send(message):
                messages.append(message)
            await captured_apps[0]({'type': 'http', 'method': 'GET',
                'path': COMMANDS + '/' + item.command_id, 'query_string': b'',
                'headers': [(b'authorization', ('Bearer ' + TOKEN).encode())]}, receive, send)
            assert messages[0]['status'] == 503
            assert not state.adapter._closed and state.provider_closes == 0
            release.set()
            result = await asyncio.wait_for(response_task, 8)
            assert result.status_code == 200 and result.json()['state'] == 'succeeded'
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(serving, 8)
    finally:
        release.set()
        stop.set()
        if response_task is not None:
            await asyncio.gather(response_task, return_exceptions=True)
        await asyncio.gather(serving, return_exceptions=True)
    assert finished.is_set()
    assert_closed(state)
    assert_port_released(address)
    before = list(state.backend.events)
    reopened = HostAdapter(state.path, state.manifest)
    try:
        receipt = reopened.get(item.command_id)
        assert receipt.state == 'succeeded' and receipt.command_id == item.command_id
        assert state.backend.events == before and before.count('create') == 1
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_paused_held_receipt_survives_explicit_service_restart_without_replay(owned):
    state = owned
    stop, serving, address = await start(state)
    state.adapter.set_paused(True)
    item = command(state.manifest, 'deploy_container')
    try:
        async with ContainerPilotClient(origin(address), TOKEN) as client:
            held = await client.submit_command(item)
            assert held.state == 'held' and held.reason == 'paused'
            assert 'create' not in state.backend.events and state.provisioner.calls == []
    finally:
        stop.set()
        await asyncio.wait_for(serving, 8)
    assert_closed(state)
    previous_events = list(state.backend.events)
    fresh_root = state.root / 'fresh-providers'
    fresh_root.mkdir(mode=0o700)
    seed, fresh_backend, _, fresh_manifest = make_fixture(fresh_root)
    assert fresh_manifest.digest() == state.manifest.digest()
    provider_names = ('record_store', 'metadata_store', 'provenance_verifier', 'backend',
        'baseline_provider', 'state_provisioner', 'secret_resolver', 'driver_identity_verifier')
    providers = {name: getattr(seed, name) for name in provider_names}
    seed.close()
    entered, exited = [], []
    @contextmanager
    def reopened_factory():
        adapter = HostAdapter(state.path, state.manifest, **providers)
        entered.append(adapter)
        try:
            yield adapter
        finally:
            if not adapter._closed:
                adapter.close()
            exited.append(True)
    stopped = asyncio.Event()
    ready = asyncio.get_running_loop().create_future()
    restarted = asyncio.create_task(state.module.serve_owned_host_executor(reopened_factory,
        **options(stop_event=stopped, on_ready=lambda actual: ready.set_result(actual))))
    try:
        replacement = await asyncio.wait_for(ready, 8)
        async with ContainerPilotClient(origin(replacement), TOKEN) as client:
            assert await client.get_command(item) == held
            assert await client.get_command(item) == held
        assert entered[0].ledger.is_paused()
        assert fresh_backend.events == [] and state.backend.events == previous_events
    finally:
        stopped.set()
        await asyncio.wait_for(restarted, 8)
    assert exited == [True] and entered[0]._closed
    assert_port_released(replacement)


@pytest.mark.asyncio
async def test_async_readiness_callback_is_awaited_and_failure_is_sanitized(owned):
    state = owned
    entered, release = asyncio.Event(), asyncio.Event()
    ready_address = []
    async def on_ready(address):
        ready_address.append(address)
        entered.set()
        await release.wait()
        raise RuntimeError('synthetic-private-async-ready-secret')
    stop = asyncio.Event()
    serving = asyncio.create_task(state.module.serve_owned_host_executor(state.factory,
        **options(stop_event=stop, on_ready=on_ready)))
    try:
        await asyncio.wait_for(entered.wait(), 8)
        assert state.provider_closes == state.adapter_closes == 0
        assert not serving.done()
        release.set()
        with pytest.raises(RuntimeError) as failed:
            await asyncio.wait_for(serving, 8)
        assert 'synthetic-private' not in str(failed.value)
    finally:
        release.set()
        stop.set()
        await asyncio.gather(serving, return_exceptions=True)
    assert_closed(state)
    assert_port_released(ready_address[0])


@pytest.mark.asyncio
async def test_adapter_close_failure_still_exits_provider_context_and_is_sanitized(owned):
    state = owned
    stop, serving, address = await start(state)
    original = state.adapter.close
    def fail_after_close():
        original()
        raise RuntimeError('synthetic-private-adapter-close-secret')
    state.adapter.close = fail_after_close
    stop.set()
    with pytest.raises(RuntimeError) as failed:
        await asyncio.wait_for(serving, 8)
    assert 'synthetic-private' not in str(failed.value)
    assert_closed(state)
    assert_port_released(address)


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['adapter-close', 'provider-exit'])
async def test_repeated_cancel_retains_off_loop_cleanup_and_keeps_loop_responsive(owned, stage):
    state = owned
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    thread_ids = []
    def block_cleanup():
        thread_ids.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), 'Test cleanup release was not signalled'
    stop, serving, address = await start(state)
    if stage == 'adapter-close':
        state.adapter_close_block = block_cleanup
    else:
        state.provider_exit_block = block_cleanup
    try:
        stop.set()
        await asyncio.wait_for(entered.wait(), 8)
        assert thread_ids == [thread_ids[0]] and thread_ids[0] != threading.get_ident()
        serving.cancel()
        await asyncio.sleep(0)
        serving.cancel()
        await asyncio.sleep(0)
        assert not serving.done() and state.provider_closes == 0
        assert state.adapter._closed is (stage == 'provider-exit')
        tick = loop.create_future()
        loop.call_soon(tick.set_result, True)
        assert await asyncio.wait_for(tick, 1)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serving, 8)
    finally:
        release.set()
        stop.set()
        await asyncio.gather(serving, return_exceptions=True)
    assert_closed(state)
    assert_port_released(address)


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['closed', 'closing', 'finalizing', 'inherited-pid',
    'active-operations', 'active-threads'])
async def test_known_invalid_adapter_snapshot_is_rejected_before_ownership_transfer(owned, invalid):
    state = owned
    attempted_foreign_close = []
    ready = []
    @contextmanager
    def invalid_factory():
        with state.factory() as adapter:
            fields = ('_closed', '_closing', '_finalizing_close', '_owner_pid',
                      '_active_operations', '_active_threads')
            before = {name: getattr(adapter, name) for name in fields}
            original_close = adapter.close
            def forbidden_close():
                # Fail immediately if the service tries to own this known
                # invalid snapshot. Never deadlock a fake active operation.
                attempted_foreign_close.append(True)
                raise AssertionError('Unaccepted adapter closed by service')
            adapter.close = forbidden_close
            if invalid == 'closed': adapter._closed = True
            elif invalid == 'closing': adapter._closing = True
            elif invalid == 'finalizing': adapter._finalizing_close = True
            elif invalid == 'inherited-pid': adapter._owner_pid = os.getpid() + 1
            elif invalid == 'active-operations': adapter._active_operations = 1
            else: adapter._active_threads = {threading.get_ident(): 1}
            try:
                yield adapter
            finally:
                for name, value in before.items():
                    setattr(adapter, name, value)
                adapter.close = original_close
                # The outer factory remains responsible for its unaccepted
                # resource, after restoring test-only synthetic mutations.
    with pytest.raises(RuntimeError):
        await state.module.serve_owned_host_executor(invalid_factory,
            **options(on_ready=lambda address: ready.append(address)))
    assert ready == [] and attempted_foreign_close == []
    assert_closed(state)


def test_service_module_import_is_inert_in_an_isolated_process():
    script = '''
import asyncio, httpx, json, os, socket, sqlite3, subprocess, threading, uvicorn
def denied(*args, **kwargs):
    raise AssertionError('Unexpected service import effect')
class DeniedSocket(socket.socket):
    def __new__(cls, *args, **kwargs):
        return denied(*args, **kwargs)
socket.socket = DeniedSocket
sqlite3.connect = denied
subprocess.Popen = denied
threading.Thread.start = denied
os.mkdir = denied
import hub_container_executor.service as service
assert callable(service.serve_owned_host_executor)
print(json.dumps({'imported': True, 'effects': 0}))
'''
    result = subprocess.run([sys.executable, '-B', '-c', script],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '{"imported": true, "effects": 0}'


@pytest.mark.asyncio
async def test_provider_context_cannot_suppress_listener_failure(owned):
    state = owned

    class SuppressingContext:
        def __enter__(self):
            self.inner = state.factory()
            return self.inner.__enter__()

        def __exit__(self, *error):
            self.inner.__exit__(*error)
            return True

    def fail(_address):
        raise RuntimeError('synthetic-private-suppressed-listener-secret')

    with pytest.raises(RuntimeError) as failed:
        await state.module.serve_owned_host_executor(SuppressingContext,
            **options(on_ready=fail))
    assert 'synthetic-private' not in str(failed.value)
    assert_closed(state)
