"""Real private HTTPX Unix transport with real adapter/ledger, synthetic providers.

No Docker, issuer, TLS, host provisioning, service installation or production
credential is exercised. Filesystem replacement below is deliberate mutation of
only a newly created private fixture; unknown cleanup must preserve it.
"""
import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
from uuid import uuid4

import pytest

if sys.platform != 'linux':
    pytest.skip('Real private Linux Unix transport and adapter ownership', allow_module_level=True)

import httpx

from hub_container_executor.adapter import HostAdapter
from hub_container_executor.ledger import OwnerBusy
from tests.test_hub_container_executor_adapter import command
from tests.test_hub_container_executor_service import owned, assert_closed, TOKEN, COMMANDS


@pytest.fixture
def endpoint():
    # Short path avoids sockaddr_un truncation under long pytest workspaces.
    with tempfile.TemporaryDirectory(prefix='eh-exec-uds-', dir='/tmp') as directory:
        yield Path(directory) / 'executor.sock'


def options(endpoint, **changes):
    values = dict(bearer_token=TOKEN, socket_path=endpoint,
                  supervisor_stop_grace_seconds=2000, install_signals=False)
    values.update(changes)
    return values


def client(endpoint, *, authorized=True):
    return httpx.AsyncClient(
        base_url='http://executor-private.invalid',
        transport=httpx.AsyncHTTPTransport(uds=str(endpoint), retries=0, trust_env=False),
        headers={'Authorization': 'Bearer ' + TOKEN} if authorized else {},
        trust_env=False, follow_redirects=False, timeout=15,
    )


async def start(state, endpoint, **changes):
    stop = asyncio.Event()
    ready = asyncio.get_running_loop().create_future()
    serving = asyncio.create_task(state.module.serve_owned_host_executor_unix(state.factory,
        **options(endpoint, stop_event=stop, on_ready=lambda actual: ready.set_result(actual), **changes)))
    try:
        address = await asyncio.wait_for(asyncio.shield(ready), 8)
    except BaseException:
        stop.set()
        await asyncio.gather(serving, return_exceptions=True)
        raise
    return stop, serving, address


@pytest.mark.asyncio
@pytest.mark.parametrize('rollback', [False, True], ids=['healthy', 'rollback'])
async def test_real_unix_http_auth_phase_receipts_and_owned_cleanup(owned, endpoint, rollback):
    state = owned
    state.failed_probe = rollback
    stop, serving, address = await start(state, endpoint)
    assert isinstance(address, str) and address
    info = endpoint.lstat()
    assert stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_uid == os.getuid() and stat.S_IMODE(endpoint.parent.stat().st_mode) == 0o700
    commands = []
    try:
        async with client(endpoint, authorized=False) as anonymous:
            result = await anonymous.get(COMMANDS + '/' + str(uuid4()))
            assert result.status_code == 401 and TOKEN not in result.text
        async with client(endpoint) as http:
            run_id = str(uuid4())
            phases = ['deploy_container', 'smoke_container']
            if rollback:
                phases.extend(['rollback_container', 'smoke_container_rollback'])
            for phase in phases:
                item = command(state.manifest, phase, run_id=run_id)
                response = await http.post(COMMANDS, json=item.model_dump())
                assert response.status_code == 200
                result = response.json()
                assert result['command_id'] == item.command_id and not result['replayed']
                assert result['state'] == ('failed' if rollback and phase == 'smoke_container' else 'succeeded')
                commands.append((item, result))
            events = list(state.backend.events)
            for item, expected in commands:
                response = await http.get(COMMANDS + '/' + item.command_id)
                assert response.status_code == 200 and response.json() == expected
            assert state.backend.events == events and state.provider_closes == 0
    finally:
        stop.set()
        await asyncio.wait_for(serving, 8)
    assert_closed(state)
    assert not endpoint.exists()
    reopened = HostAdapter(state.path, state.manifest)
    try:
        for item, expected in commands:
            assert reopened.get(item.command_id).model_dump() == expected
        assert state.backend.events == events
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['file', 'symlink', 'dangling', 'fifo', 'stale-socket', 'live-socket'])
async def test_preexisting_unix_leaf_never_enters_factory_or_unlinks(owned, endpoint, kind):
    state = owned
    foreign = None
    if kind == 'file':
        endpoint.write_bytes(b'foreign-fixture')
    elif kind in {'symlink', 'dangling'}:
        target = endpoint.parent / 'foreign-target'
        if kind == 'symlink':
            target.touch()
        endpoint.symlink_to(target)
    elif kind == 'fifo':
        os.mkfifo(endpoint, 0o600)
    else:
        foreign = socket.socket(socket.AF_UNIX)
        foreign.bind(str(endpoint))
        endpoint.chmod(0o600)
        if kind == 'stale-socket':
            foreign.close()
            foreign = None
    before = endpoint.lstat()
    try:
        with pytest.raises(RuntimeError):
            await state.module.serve_owned_host_executor_unix(state.factory, **options(endpoint))
        after = endpoint.lstat()
        assert (before.st_dev, before.st_ino, before.st_mode) == (after.st_dev, after.st_ino, after.st_mode)
        assert state.entered == 0
    finally:
        if foreign is not None:
            foreign.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', [0o755, 0o750, 0o770])
async def test_unix_parent_permissions_rejected_before_factory_without_repair(owned, endpoint, mode):
    endpoint.parent.chmod(mode)
    with pytest.raises(RuntimeError):
        await owned.module.serve_owned_host_executor_unix(owned.factory, **options(endpoint))
    assert owned.entered == 0 and not endpoint.exists()
    assert stat.S_IMODE(endpoint.parent.stat().st_mode) == mode


@pytest.mark.asyncio
async def test_symlink_parent_and_absent_parent_are_not_followed_or_created(owned, endpoint):
    alias = endpoint.parent / 'alias'
    alias.symlink_to(endpoint.parent, target_is_directory=True)
    for selected in (alias / endpoint.name, endpoint.parent / 'missing' / endpoint.name):
        with pytest.raises(RuntimeError):
            await owned.module.serve_owned_host_executor_unix(owned.factory, **options(selected))
    assert owned.entered == 0 and not endpoint.exists()
    assert not (endpoint.parent / 'missing').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['file', 'symlink', 'socket', 'removed'])
async def test_foreign_unix_replacement_is_preserved_and_cleanup_fails_after_owned_drain(owned, endpoint, kind):
    state = owned
    stop, serving, _ = await start(state, endpoint)
    replacement = None
    identity = None
    try:
        endpoint.unlink()  # Deliberate replacement of this test's own endpoint.
        if kind == 'file':
            endpoint.write_bytes(b'foreign-replacement')
        elif kind == 'symlink':
            endpoint.symlink_to(endpoint.parent / 'foreign-target')
        elif kind == 'socket':
            replacement = socket.socket(socket.AF_UNIX)
            replacement.bind(str(endpoint))
            endpoint.chmod(0o600)
        if kind != 'removed':
            info = endpoint.lstat()
            identity = (info.st_dev, info.st_ino, info.st_mode)
        stop.set()
        with pytest.raises(RuntimeError) as failed:
            await asyncio.wait_for(serving, 8)
        assert TOKEN not in str(failed.value) and str(endpoint) not in str(failed.value)
        assert_closed(state)
        if identity is None:
            assert not endpoint.exists()
        else:
            info = endpoint.lstat()
            assert (info.st_dev, info.st_ino, info.st_mode) == identity
    finally:
        stop.set()
        await asyncio.gather(serving, return_exceptions=True)
        if replacement is not None:
            replacement.close()


@pytest.mark.asyncio
async def test_unix_bind_precedes_factory_and_partial_factory_failure_cleans_only_owned_socket(owned, endpoint):
    state = owned
    events = []
    @contextmanager
    def failing_factory():
        events.append(('enter', stat.S_IMODE(endpoint.lstat().st_mode)))
        try:
            raise RuntimeError('synthetic-private-factory-detail')
            yield
        finally:
            events.append(('cleanup', endpoint.exists()))
    with pytest.raises(RuntimeError) as failed:
        await state.module.serve_owned_host_executor_unix(failing_factory, **options(endpoint))
    assert 'synthetic-private' not in str(failed.value)
    assert events == [('enter', 0o600), ('cleanup', True)]
    assert not endpoint.exists() and state.entered == 0


@pytest.mark.asyncio
async def test_unix_repeated_cancel_retains_raw_effect_ledger_and_providers_until_completion(owned, endpoint):
    state = owned
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    def block():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), 'Synthetic effect was not released'
    state.block = block
    stop, serving, _ = await start(state, endpoint)
    pending = None
    item = command(state.manifest, 'deploy_container')
    try:
        async with client(endpoint) as http:
            pending = asyncio.create_task(http.post(COMMANDS, json=item.model_dump()))
            await asyncio.wait_for(entered.wait(), 8)
            serving.cancel()
            await asyncio.sleep(0)
            serving.cancel()
            await asyncio.sleep(0)
            assert stop.is_set() and not serving.done() and not pending.done()
            assert state.adapter_closes == state.provider_closes == 0
            assert state.adapter._active_operations == 1
            with pytest.raises(OwnerBusy):
                HostAdapter(state.path, state.manifest)
            release.set()
            response = await asyncio.wait_for(pending, 8)
            assert response.status_code == 200 and response.json()['state'] == 'succeeded'
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(serving, 8)
    finally:
        release.set()
        stop.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await asyncio.gather(serving, return_exceptions=True)
    assert_closed(state)
    assert not endpoint.exists() and state.backend.events.count('create') == 1


@pytest.mark.asyncio
async def test_unix_preclosed_stop_is_inert_and_endpoint_is_explicitly_required(owned, endpoint):
    state = owned
    stop = asyncio.Event()
    stop.set()
    await state.module.serve_owned_host_executor_unix(state.factory, **options(endpoint, stop_event=stop))
    assert state.entered == 0 and not endpoint.exists()
    values = options(endpoint)
    del values['socket_path']
    with pytest.raises(TypeError):
        await state.module.serve_owned_host_executor_unix(state.factory, **values)
    assert state.entered == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [dict(bearer_token=''), dict(bearer_token='private secret'),
    dict(supervisor_stop_grace_seconds=1119), dict(socket_path=None),
    dict(stop_event=object()), dict(on_ready=object()), dict(install_signals=1)])
async def test_unix_invalid_options_do_not_construct_or_bind(owned, endpoint, change):
    with pytest.raises(ValueError):
        await owned.module.serve_owned_host_executor_unix(owned.factory, **options(endpoint, **change))
    assert owned.entered == 0 and not endpoint.exists()
