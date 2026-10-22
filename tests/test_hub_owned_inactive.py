"""Real isolated Linux ledger/frontends; no executor or host actions.

The seed fixture simulates a previous pilot. The inactive service must preserve
its history without constructing or contacting an executor of its own.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import socket
import sqlite3
import sys
from types import SimpleNamespace

import httpx
import pytest

if sys.platform != 'linux':
    pytest.skip('Real Linux owner and native event feeds', allow_module_level=True)

from agent_message_bus.auth import AuthSettings
from agent_message_bus.database import MessageRepository
from engineering_hub.app import Settings
from engineering_hub import owned_inactive_hub as service
from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError
from engineering_hub.repository import HubError, HubRepository
from tests.test_hub_gateway_queued_pilot import signed_access, checked, BASE, ORIGIN
from tests.test_hub_queued_pilot import rig as pilot_rig, submit


@pytest.fixture
def rig(pilot_rig, tmp_path, monkeypatch):
    config, assertion = signed_access(monkeypatch)
    settings = Settings(Path(pilot_rig.repo.path), AuthSettings('test-hermes', 'test-codex',
        claude_token='test-claude', allowed_ips=frozenset({'127.0.0.1'})), config)
    root = tmp_path / 'bus'
    root.mkdir()
    repository = MessageRepository(root / 'messages.db')
    message = repository.insert(sender='hermes', recipient='codex', message_type='info',
                                body={'purpose': 'preserved synthetic message'})
    old_ack = repository.insert(sender='claude', recipient='codex', message_type='info',
                                body={'purpose': 'aged acknowledged history must survive'})
    repository.claim(old_ack.id, 'codex')
    repository.acknowledge(old_ack.id, 'codex')
    with repository.connect() as db:
        db.execute("UPDATE messages SET acknowledged_at='2000-01-01T00:00:00Z' WHERE id=?", (old_ack.id,))
    (root / 'audit.jsonl').touch()
    (root / 'files').mkdir()
    state = SimpleNamespace(settings=settings, assertion=assertion, pilot=pilot_rig,
        paths={'AGENT_BUS_DB': root / 'messages.db', 'AGENT_BUS_AUDIT': root / 'audit.jsonl',
               'AGENT_BUS_FILE_ROOT': root / 'files'}, events=[], owners=[], core=None,
        message_id=message.id, old_ack_id=old_ack.id, bus_repository=repository,
        release=None, draining=None)
    state.block_stage = 'bus'
    monkeypatch.delenv('HUB_BUS_STARTUP_CLEANUP', raising=False)
    actual_owner = service.PilotOwner
    def acquire(path, *, require_existing):
        assert require_existing is True
        owner = actual_owner(path, require_existing=True, allow_ephemeral_test_storage=True)
        state.owners.append(owner)
        original_close = owner.close
        def close():
            state.events.append('owner-close')
            original_close()
        owner.close = close
        return owner
    monkeypatch.setattr(service, 'PilotOwner', acquire)
    actual_bus = service._create_unified_bus
    def bus(*args, **kwargs):
        result = actual_bus(*args, **kwargs)
        original_lifespan = result.router.lifespan_context
        @asynccontextmanager
        async def lifespan(app):
            async with original_lifespan(app):
                state.events.append('bus-start')
                try:
                    yield
                finally:
                    if state.draining is not None and state.block_stage == 'bus':
                        state.draining.set()
                        await state.release.wait()
                    state.events.append('bus-stop')
        result.router.lifespan_context = lifespan
        return result
    monkeypatch.setattr(service, '_create_unified_bus', bus)
    actual_app = service.create_app
    def app(*args, **kwargs):
        assert set(kwargs) == {'repository', 'bus_app'}
        assert kwargs['repository'].execution_principal is None
        state.core = actual_app(*args, **kwargs)
        actual_stop = state.core.state.event_feed.stop
        async def stop_feed():
            if state.draining is not None and state.block_stage == 'feed':
                state.draining.set()
                await state.release.wait()
            await actual_stop()
        state.core.state.event_feed.stop = stop_feed
        return state.core
    monkeypatch.setattr(service, 'create_app', app)
    return state


def options(rig, **changes):
    result = dict(bus_paths=rig.paths, agent_host='127.0.0.1', agent_port=0,
                  browser_host='127.0.0.1', browser_port=0, install_signals=False)
    result.update(changes)
    return result


async def start(rig):
    rig.pilot.owner.close()
    stop = asyncio.Event()
    ready = asyncio.get_running_loop().create_future()
    serving = asyncio.create_task(service.serve_owned_inactive_hub(rig.settings,
        **options(rig, stop_event=stop, on_ready=lambda *addresses: ready.set_result(addresses))))
    try:
        done, _ = await asyncio.wait((ready, serving), timeout=8, return_when=asyncio.FIRST_COMPLETED)
        if serving in done:
            await serving
        assert ready in done, 'Inactive Hub did not become ready'
        addresses = ready.result()
    except BaseException:
        stop.set()
        await asyncio.gather(serving, return_exceptions=True)
        raise
    return stop, serving, addresses


@asynccontextmanager
async def connections(rig, addresses):
    origin = lambda address: 'http://' + address[0] + ':' + str(address[1])
    async with (httpx.AsyncClient(base_url=origin(addresses[0]), trust_env=False,
        headers={'Authorization': 'Bearer test-codex', 'Host': 'hub.example'}, timeout=8) as agent,
        httpx.AsyncClient(base_url=origin(addresses[1]), trust_env=False,
        headers={'Host': 'hub.example', 'CF-Access-Jwt-Assertion': rig.assertion}, timeout=8) as human):
        yield agent, human


@pytest.mark.asyncio
async def test_pilot_to_inactive_preserves_history_and_ordinary_hub_across_reopen(rig, monkeypatch):
    previous, completed = submit(rig.pilot, 'completed-before-inactive')
    await rig.pilot.bridge.run_once()
    pending_task, pending = submit(rig.pilot, 'pending-remains-pending')
    remote_count = len(rig.pilot.client.submissions)
    existing_runs = rig.pilot.repo.list_runs()
    with rig.pilot.repo._connection() as db:
        existing_jobs = [dict(row) for row in db.execute('SELECT * FROM hub_work_jobs ORDER BY id')]
    def forbidden(*_args, **_kwargs):
        pytest.fail('Inactive service constructed an executor or runtime')
    monkeypatch.setattr('engineering_hub.container_pilot_client.ContainerPilotClient.__init__', forbidden)
    monkeypatch.setattr('engineering_hub.queued_pilot_runtime.QueuedPilotRuntime.__init__', forbidden)
    monkeypatch.setattr('engineering_hub.work_queue.WorkQueue.__init__', forbidden)
    ids = []
    for iteration in range(2):
        stop, serving, addresses = await start(rig)
        try:
            assert rig.core.state.work_queue is None
            assert rig.core.state.repository.execution_principal is None
            assert rig.core.state.repository.queued_pilot_enrolled()
            async with connections(rig, addresses) as (agent, human):
                overview = checked(await agent.get(BASE + '/overview'))
                assert overview['autonomy'] == {'configured': False, 'state': 'disabled',
                                               'running': False, 'receipts': []}
                assert overview['runs'][0]['task_id'] == previous['id']
                assert overview['runs'][0]['status'] == 'succeeded'
                payload = {
                    'task_id': pending_task['id'], 'expected_revision': pending_task['revision'],
                    'artifact_digest': 'sha256:' + 'a' * 64, 'target': 'engineering-hub:staging'}
                assert checked(await agent.get(BASE + '/work/jobs'), 423)['detail']['code'] == 'work_queue_not_configured'
                assert checked(await agent.post(BASE + '/runs', json=payload), 423)['detail']['code'] == 'executor_not_enabled'
                assert checked(await agent.post(BASE + '/runs/' + existing_runs[0]['id'] + '/dispatch'), 423)['detail']['code'] == 'executor_not_enabled'
                for suffix, body in (('', payload), ('/' + existing_runs[0]['id'] + '/dispatch', None),
                                     ('/' + existing_runs[0]['id'] + '/recover', None)):
                    assert checked(await agent.post(BASE + '/container-pilot/runs' + suffix, json=body),
                        423)['detail']['code'] == 'queued_pilot_direct_route_disabled'
                assert checked(await agent.post(BASE + '/work/claim', json={
                    'instance_id': '00000000-0000-4000-8000-000000000001'}),
                    423)['detail']['code'] == 'work_queue_not_configured'
                assert checked(await agent.post(BASE + '/work/jobs', json={
                    'request_id': 'inactive-must-not-enqueue', 'action_key': 'engineering-hub.container-pilot',
                    'environment': 'staging', 'target': {'task_id': pending_task['id'],
                    'source_commit': pending_task['candidate_commit'], 'artifact_digest': 'sha256:' + 'a' * 64}}),
                    423)['detail']['code'] == 'work_queue_not_configured'
                session = checked(await human.post(BASE + '/auth/bootstrap', headers={'Origin': ORIGIN}))
                human.headers.update({'Cookie': 'hub_session=' + human.cookies.get('hub_session'),
                    'Origin': ORIGIN, 'X-Hub-CSRF': session['csrf_token']})
                task = checked(await human.post(BASE + '/tasks', json={
                    'project': 'engineering-hub', 'title': 'Ordinary inactive task',
                    'client_request_id': 'inactive-task-' + str(iteration)}), 201)
                ids.append(task['id'])
                for identifier in ids:
                    assert checked(await agent.get(BASE + '/tasks/' + identifier))['id'] == identifier
                note = checked(await human.post(BASE + '/notes', json={
                    'project': 'engineering-hub', 'path': 'inactive/continuation.md',
                    'title': 'Ordinary note', 'content': 'Preserved content ' + str(iteration),
                    'expected_version': iteration}))
                assert note['version'] == iteration + 1
                assert checked(await agent.get(BASE + '/notes/document', params={
                    'project': 'engineering-hub', 'path': 'inactive/continuation.md'}))['content'] == note['content']
                assert rig.message_id in json.dumps(checked(await agent.get('/api/messages')))
                assert rig.bus_repository.get(rig.old_ack_id).acknowledged_at == '2000-01-01T00:00:00Z'
                with rig.core.state.repository._connection() as db:
                    rows = dict(db.execute('SELECT id,state FROM hub_work_jobs'))
                    assert [dict(row) for row in db.execute('SELECT * FROM hub_work_jobs ORDER BY id')] == existing_jobs
                assert rig.core.state.repository.list_runs() == existing_runs
                assert rows[completed['id']] == 'succeeded'
                assert rows[pending['id']] == 'queued'
                assert len(rig.pilot.client.submissions) == remote_count
        finally:
            stop.set()
            await asyncio.wait_for(serving, 8)
        assert rig.events[-2:] == ['bus-stop', 'owner-close']
        with pytest.raises(PilotOwnershipError):
            rig.owners[-1].assert_active(rig.settings.database)
    assert rig.owners[1].previous_epoch == rig.owners[0].epoch


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['bus', 'feed'])
async def test_repeated_cancellation_keeps_owner_until_bus_and_feed_drain(rig, stage):
    rig.draining, rig.release = asyncio.Event(), asyncio.Event()
    rig.block_stage = stage
    stop, serving, _ = await start(rig)
    serving.cancel()
    await asyncio.wait_for(rig.draining.wait(), 8)
    serving.cancel()
    await asyncio.sleep(0)
    assert not serving.done()
    assert 'owner-close' not in rig.events
    rig.owners[-1].assert_active(rig.settings.database)
    with pytest.raises(PilotOwnershipError):
        PilotOwner(rig.settings.database, require_existing=True, allow_ephemeral_test_storage=True)
    rig.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(serving, 8)
    assert rig.events[-2:] == ['bus-stop', 'owner-close']
    assert not rig.core.state.event_feed.available


@pytest.mark.asyncio
async def test_accepted_http_mutation_drains_before_owner_release(rig, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = []
    listeners = []
    from engineering_hub import split_server
    original_listener = split_server._listener
    def listener(app):
        result = original_listener(app)
        listeners.append(result)
        return result
    monkeypatch.setattr(split_server, '_listener', listener)
    original_app = service.create_app
    def app(*args, **kwargs):
        core = original_app(*args, **kwargs)
        @core.post(BASE + '/synthetic-held-mutation')
        async def held():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return core.state.repository.create_task('human:42', 'engineering-hub',
                                                     'Accepted before stop', 'held-mutation')
        return core
    monkeypatch.setattr(service, 'create_app', app)
    stop, serving, addresses = await start(rig)
    request = None
    try:
        async with connections(rig, addresses) as (agent, _):
            request = asyncio.create_task(agent.post(BASE + '/synthetic-held-mutation'))
            await asyncio.wait_for(entered.wait(), 8)
            assert all(item.config.timeout_graceful_shutdown is None for item in listeners)
            serving.cancel()
            async with asyncio.timeout(8):
                while rig.core.state.hub_frontends.state != 'stopping':
                    await asyncio.sleep(0.001)
            serving.cancel()
            await asyncio.sleep(0)
            assert not serving.done() and not request.done()
            assert not cancelled and 'owner-close' not in rig.events
            rig.owners[-1].assert_active(rig.settings.database)
            with pytest.raises(PilotOwnershipError):
                PilotOwner(rig.settings.database, require_existing=True, allow_ephemeral_test_storage=True)
            release.set()
            result = checked(await request)
            assert result['title'] == 'Accepted before stop'
    finally:
        release.set()
        stop.set()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serving, 8)
    assert not cancelled
    assert rig.events[-2:] == ['bus-stop', 'owner-close']
    with PilotOwner(rig.settings.database, require_existing=True, allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(rig.settings.database, pilot_owner=owner)
        assert any(task['title'] == 'Accepted before stop' for task in repo.list_tasks())


@pytest.mark.asyncio
async def test_bind_failure_happens_before_any_owner_or_state_open(rig, monkeypatch):
    def forbidden(*_a, **_k):
        pytest.fail('Owner constructed before successful listener binds')
    monkeypatch.setattr(service, 'PilotOwner', forbidden)
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        with pytest.raises(service.OwnedInactiveHubError):
            await service.serve_owned_inactive_hub(rig.settings,
                **options(rig, browser_port=occupied.getsockname()[1]))
    assert rig.events == []


@pytest.mark.asyncio
async def test_core_construction_failure_releases_acquired_owner(rig, monkeypatch):
    rig.pilot.owner.close()
    monkeypatch.setattr(service, '_create_unified_bus', lambda *_a, **_k: (_ for _ in ()).throw(ValueError()))
    with pytest.raises(service.OwnedInactiveHubError):
        await service.serve_owned_inactive_hub(rig.settings, **options(rig))
    assert rig.events == ['owner-close']
    with PilotOwner(rig.settings.database, require_existing=True, allow_ephemeral_test_storage=True):
        pass


@pytest.mark.asyncio
async def test_contention_refuses_to_start_without_fallback(rig):
    with pytest.raises(service.OwnedInactiveHubError):
        await service.serve_owned_inactive_hub(rig.settings, **options(rig))
    assert rig.owners == []
    rig.pilot.owner.assert_active(rig.settings.database)


@pytest.mark.asyncio
async def test_queue_flag_rejected_before_any_owner_or_listener(rig, monkeypatch):
    monkeypatch.setattr(service, '_bind', lambda *_a, **_k: pytest.fail('Listeners constructed'))
    with pytest.raises(service.OwnedInactiveHubError):
        await service.serve_owned_inactive_hub(replace(rig.settings, work_queue_enabled=True), **options(rig))
    assert rig.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize('flag', ['1', 'unexpected'])
async def test_retention_cleanup_flag_rejected_before_any_state_or_listener(rig, monkeypatch, flag):
    monkeypatch.setenv('HUB_BUS_STARTUP_CLEANUP', flag)
    monkeypatch.setattr(service, '_bind', lambda *_a, **_k: pytest.fail('Listeners constructed'))
    with pytest.raises(service.OwnedInactiveHubError):
        await service.serve_owned_inactive_hub(rig.settings, **options(rig))
    assert rig.events == []
    assert rig.bus_repository.get(rig.old_ack_id).status == 'acknowledged'


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['agent-only-ingress', 'github-auth', 'missing-access-config'])
async def test_invalid_ingress_or_auth_refused_before_owner_epoch_mutation(rig, monkeypatch, case):
    from engineering_hub.auth import AuthConfig
    settings = (replace(rig.settings, ingress_mode='agents') if case == 'agent-only-ingress' else
        replace(rig.settings, auth_config=AuthConfig(ORIGIN) if case == 'github-auth' else
            AuthConfig(ORIGIN, human_auth_mode='cloudflare_access')))
    monkeypatch.setattr(service, '_bind', lambda *_a, **_k: pytest.fail('Listeners constructed'))
    with pytest.raises(service.OwnedInactiveHubError):
        await service.serve_owned_inactive_hub(settings, **options(rig))
    assert rig.owners == []
    rig.pilot.owner.assert_active(rig.settings.database)


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['database', 'lock'])
async def test_missing_enrolled_artifact_is_not_recreated(rig, missing):
    from pathlib import Path
    rig.pilot.owner.close()
    path = Path(rig.settings.database)
    selected = path if missing == 'database' else path.with_name(path.name + '.pilot-owner.lock')
    selected.unlink()
    with pytest.raises(service.OwnedInactiveHubError):
        await service.serve_owned_inactive_hub(rig.settings, **options(rig))
    assert not selected.exists()
    assert rig.events == []


@pytest.mark.asyncio
async def test_malformed_enrollment_is_preserved_not_repaired(rig):
    rig.pilot.owner.close()
    with sqlite3.connect(rig.settings.database) as db:
        db.execute('UPDATE hub_pilot_owner SET version=99')
    with pytest.raises(service.OwnedInactiveHubError):
        await service.serve_owned_inactive_hub(rig.settings, **options(rig))
    with sqlite3.connect(rig.settings.database) as db:
        assert db.execute('SELECT version FROM hub_pilot_owner').fetchone() == (99,)
    assert rig.owners == []


@pytest.mark.asyncio
async def test_unenrolled_existing_ledger_is_not_enrolled_or_replaced(rig, tmp_path):
    private = tmp_path / 'unenrolled'
    private.mkdir(mode=0o700)
    path = private / 'hub.db'
    repo = HubRepository(path)
    item = repo.create_task('human:42', 'engineering-hub', 'Do not adopt', 'unchanged')
    path.chmod(0o600)
    with pytest.raises(service.OwnedInactiveHubError):
        await service.serve_owned_inactive_hub(replace(rig.settings, database=path), **options(rig))
    assert HubRepository(path).get_task(item['id']) == item
    assert not path.with_name(path.name + '.pilot-owner.lock').exists()


def test_default_unowned_factory_still_rejects_enrolled_ledger(rig):
    from engineering_hub.unified_app import create_unified_app
    with pytest.raises(HubError, match='pilot_ownership_required'):
        create_unified_app(rig.settings, bus_paths=rig.paths)


@pytest.mark.asyncio
async def test_pre_stopped_service_does_not_open_or_advance_owner(rig):
    stop = asyncio.Event()
    stop.set()
    await service.serve_owned_inactive_hub(rig.settings, **options(rig, stop_event=stop))
    assert rig.owners == [] and rig.events == []


def test_cli_requires_explicit_config_and_rejects_executor_switches():
    for args in ([], ['--config', '/fixture/settings.json', '--pilot-config', '/fixture/pilot.json']):
        with pytest.raises(SystemExit) as error:
            service.main(args)
        assert error.value.code == 2


@pytest.mark.parametrize('switch', ['--executor-token', '--pilot-config', '--runtime'])
def test_cli_parse_errors_do_not_echo_accidental_credential_or_runtime_values(monkeypatch, capsys, switch):
    monkeypatch.setattr(service, 'serve_configuration', lambda *_a, **_k: pytest.fail('Configuration opened'))
    with pytest.raises(SystemExit) as error:
        service.main(['--config', '/fixture/settings.json', switch, 'private-canary-never-log'])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == str(service.OwnedInactiveHubError()) + '\n'
    assert 'private-canary-never-log' not in output.err
