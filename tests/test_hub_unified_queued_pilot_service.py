"""One real unified TCP/MCP/queue lifecycle; executor responses are synthetic.

Not host enrollment, Docker processing, production cutover or reboot evidence.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import socket
import sys
from types import SimpleNamespace

import pytest

if sys.platform != 'linux':
    pytest.skip('Real Linux private PilotOwner and native SQLite feeds', allow_module_level=True)

import httpx

from agent_message_bus.auth import AuthSettings
from agent_message_bus.database import MessageRepository
from engineering_hub.app import Settings
from engineering_hub.container_pilot_client import ContainerPilotClient, ContainerPilotRequest
from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError
from engineering_hub.repository import HubRepository
from engineering_hub import unified_queued_pilot_service as service
from tests.test_hub_gateway_queued_pilot import signed_access, checked, BASE, ORIGIN
from tests.test_hub_container_pilot import COMMIT, IMAGE
from tests.test_hub_queued_pilot import DistinctContainerClient, ACTION, MACHINE
from tests.test_hub_container_pilot import pilot_manifest


@pytest.fixture
def rig(tmp_path, monkeypatch):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    owner = PilotOwner(private / 'hub.db', allow_ephemeral_test_storage=True)
    config, assertion = signed_access(monkeypatch)
    settings = Settings(private / 'hub.db', AuthSettings('test-hermes', 'test-codex',
        claude_token='test-claude', allowed_ips=frozenset({'127.0.0.1'})), config,
        staging_machine_id=MACHINE)
    bus_root = tmp_path / 'bus'
    bus_root.mkdir()
    bus_repository = MessageRepository(bus_root / 'messages.db')
    original = bus_repository.insert(sender='hermes', recipient='codex',
        message_type='info', body={'purpose': 'preserved-synthetic-history'})
    audit = bus_root / 'audit.jsonl'
    audit.touch()
    files = bus_root / 'files'
    files.mkdir()
    state = SimpleNamespace(settings=settings, owner=owner, manifest=pilot_manifest(),
        paths={'AGENT_BUS_DB': bus_root / 'messages.db', 'AGENT_BUS_AUDIT': audit,
               'AGENT_BUS_FILE_ROOT': files},
        assertion=assertion, clients=[], events=[], requests=[], block=None,
        allowed=True, original=original.id, host=DistinctContainerClient(), core=None)
    monkeypatch.delenv('HUB_BUS_STARTUP_CLEANUP', raising=False)
    close_owner = owner.close
    owner.close_count = 0
    def close():
        owner.close_count += 1
        state.events.append('owner-close')
        close_owner()
    owner.close = close

    async def transport(request):
        command = ContainerPilotRequest.model_validate(json.loads(await request.aread()))
        state.requests.append(command)
        if state.block is not None:
            await state.block(command)
        return httpx.Response(200, json=state.host.response(command).model_dump())
    def client_factory():
        client = ContainerPilotClient('http://127.0.0.1', 'test-only-executor',
                                     transport=httpx.MockTransport(transport))
        original_close = client.aclose
        client.close_count = 0
        async def aclose():
            client.close_count += 1
            state.events.append('client-close')
            try:
                if getattr(state, 'close_block', None) is not None:
                    await state.close_block()
            finally:
                await original_close()
        client.aclose = aclose
        state.clients.append(client)
        return client
    state.client_factory = client_factory
    original_bus = service._create_unified_bus
    def create_bus(*args, **kwargs):
        bus = original_bus(*args, **kwargs)
        original_lifespan = bus.router.lifespan_context
        @asynccontextmanager
        async def lifespan(app):
            state.events.append('bus-start')
            try:
                async with original_lifespan(app):
                    yield
            finally:
                state.events.append('bus-stop')
        bus.router.lifespan_context = lifespan
        return bus
    monkeypatch.setattr(service, '_create_unified_bus', create_bus)
    original_app = service.create_app
    def create_app(*args, **kwargs):
        state.core = original_app(*args, **kwargs)
        return state.core
    monkeypatch.setattr(service, 'create_app', create_app)
    try:
        yield state
    finally:
        close_owner()


def options(rig, **changes):
    result = dict(owner=rig.owner, bus_paths=rig.paths, action_key=ACTION,
        machine_id=MACHINE, target_verifier=lambda *_: rig.allowed,
        supervisor_stop_grace_seconds=2000, agent_host='127.0.0.1', agent_port=0,
        browser_host='127.0.0.1', browser_port=0, install_signals=False)
    result.update(changes)
    return result


async def start(rig, **changes):
    ready = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    task = asyncio.create_task(service.serve_owned_unified_queued_pilot(
        rig.settings, rig.manifest, rig.client_factory,
        **options(rig, stop_event=stop, on_ready=lambda *args: ready.set_result(args), **changes)))
    try:
        addresses = await asyncio.wait_for(asyncio.shield(ready), 8)
    except BaseException:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)
        raise
    return stop, task, addresses


def url(address):
    return 'http://' + address[0] + ':' + str(address[1])


@asynccontextmanager
async def connections(addresses, rig):
    async with (httpx.AsyncClient(base_url=url(addresses[0]), trust_env=False,
            headers={'Authorization': 'Bearer test-codex', 'Host': 'hub.example'}, timeout=8) as agent,
        httpx.AsyncClient(base_url=url(addresses[1]), trust_env=False,
            headers={'Host': 'hub.example', 'CF-Access-Jwt-Assertion': rig.assertion}, timeout=8) as browser):
        yield agent, browser


async def submit(agent, browser, key):
    session = checked(await browser.post(BASE + '/auth/bootstrap', headers={'Origin': ORIGIN}))
    # The synthetic listener is plain loopback HTTP; preserve the production
    # Secure cookie rather than weakening AuthConfig for this transport fixture.
    browser.headers['Cookie'] = 'hub_session=' + browser.cookies.get('hub_session')
    browser.headers.update({'Origin': ORIGIN, 'X-Hub-CSRF': session['csrf_token']})
    task = checked(await browser.post(BASE + '/tasks', json={
        'project': 'engineering-hub', 'title': 'Unified synthetic pilot', 'client_request_id': key}), 201)
    path = BASE + '/tasks/' + task['id']
    task = checked(await agent.post(path + '/claim', json={'expected_revision': task['revision']}))
    task = checked(await agent.post(path + '/candidate', json={'expected_revision': task['revision'], 'commit': COMMIT}))
    assert (await agent.post(path + '/review', json={
        'expected_revision': task['revision'], 'commit': COMMIT, 'verdict': 'approve'})).status_code == 403
    task = checked(await agent.post(path + '/review', headers={'Authorization': 'Bearer test-claude'},
        json={'expected_revision': task['revision'], 'commit': COMMIT, 'verdict': 'approve'}))
    job = checked(await agent.post(BASE + '/work/jobs', json={
        'request_id': key, 'action_key': ACTION, 'environment': 'staging',
        'target': {'task_id': task['id'], 'source_commit': COMMIT, 'artifact_digest': IMAGE}}), 201)
    return task, job


async def until(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.001)  # Bounded test observation only.


def closed(rig):
    assert rig.owner.close_count == 1 and rig.clients[0].close_count == 1
    assert rig.events.count('bus-start') == rig.events.count('bus-stop') == 1
    assert rig.events[-3:] == ['bus-stop', 'client-close', 'owner-close']
    assert not rig.core.state.event_feed.available
    assert not rig.core.state.queued_pilot_runtime.status['running']
    with pytest.raises(PilotOwnershipError):
        rig.owner.assert_active(rig.settings.database)


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_smoke', [False, True])
async def test_one_unified_bus_mcp_hub_queue_lifecycle_and_healthy_or_rollback(rig, failed_smoke):
    if failed_smoke:
        rig.host.outcomes['smoke_container'] = 'failed'
    stop, serving, addresses = await start(rig)
    try:
        core = rig.core
        assert core.state.embedded_bus.state.hub_owner is core
        assert core.state.mcp is core.state.embedded_bus.state.mcp
        assert core.state.embedded_bus.state.auth is rig.settings.agent_auth
        assert core.state.queued_pilot_runtime.bridge.repo is core.state.repository
        assert core.state.work_queue.repo is core.state.repository
        async with connections(addresses, rig) as (agent, browser):
            rpc_headers = {'Accept': 'application/json, text/event-stream'}
            initialized = await agent.post('/mcp', headers=rpc_headers, json={
                'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                    'protocolVersion': '2025-06-18', 'capabilities': {},
                    'clientInfo': {'name': 'unified-pilot-test', 'version': '1'}}})
            assert initialized.status_code == 200
            session = initialized.headers['mcp-session-id']
            rpc_headers.update({'Mcp-Session-Id': session, 'MCP-Protocol-Version': '2025-06-18'})
            assert (await agent.post('/mcp', headers=rpc_headers, json={
                'jsonrpc': '2.0', 'method': 'notifications/initialized'})).status_code == 202
            listed = checked(await agent.post('/mcp', headers=rpc_headers, json={
                'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}))
            names = {item['name'] for item in listed['result']['tools']}
            assert {'send_message', 'receive_messages', 'hub_staging_task_propose',
                    'hub_work_submit', 'hub_work_claim'} <= names
            existing = checked(await agent.get('/api/messages'))
            assert rig.original in json.dumps(existing)
            task, job = await submit(agent, browser, 'unified-pilot')
            runtime = core.state.queued_pilot_runtime
            await until(lambda: runtime.status['last_job'] is not None)
            expected = 'failed' if failed_smoke else 'succeeded'
            assert runtime.status['last_job'] == {'id': job['id'], 'state': expected}
            assert checked(await agent.get(BASE + '/work/jobs/' + job['id']))['state'] == expected
            visible = checked(await browser.get(BASE + '/overview'))
            assert visible['tasks'][0]['id'] == task['id']
            assert visible['runs'][0]['task_id'] == task['id']
            assert visible['runs'][0]['status'] == ('rolled_back' if failed_smoke else 'succeeded')
            assert visible['capabilities']['production'] is False
            assert len(rig.requests) == (4 if failed_smoke else 2)
            assert checked(await agent.get('/health'))['bus']['ok'] is True
    finally:
        stop.set()
        await asyncio.wait_for(serving, 8)
    closed(rig)


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_accepted_phase_then_bus_client_owner(rig):
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = []
    async def block(_):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    rig.block = block
    stop, serving, addresses = await start(rig)
    try:
        async with connections(addresses, rig) as (agent, browser):
            _, job = await submit(agent, browser, 'drain')
            await asyncio.wait_for(entered.wait(), 8)
            serving.cancel()
            await until(lambda: not rig.core.state.queued_pilot_runtime.admission_allowed())
            serving.cancel()
            assert not serving.done()
            assert rig.clients[0].close_count == rig.owner.close_count == 0
            assert rig.events.count('bus-stop') == 0
            rig.owner.assert_active(rig.settings.database)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(serving, 8)
            assert not cancelled and len(rig.requests) == 1
            assert rig.core.state.work_queue.get_job(job['id'])['state'] == 'held'
    finally:
        release.set()
        stop.set()
        await asyncio.gather(serving, return_exceptions=True)
    closed(rig)


@pytest.mark.asyncio
async def test_bind_failure_or_preset_stop_does_not_transfer_owner(rig):
    stopping = asyncio.Event()
    stopping.set()
    await service.serve_owned_unified_queued_pilot(rig.settings, rig.manifest, rig.client_factory,
        **options(rig, stop_event=stopping))
    assert not rig.clients and rig.owner.close_count == 0
    rig.owner.assert_active(rig.settings.database)
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        with pytest.raises(service.QueuedPilotServiceError):
            await service.serve_owned_unified_queued_pilot(rig.settings, rig.manifest, rig.client_factory,
                **options(rig, agent_port=occupied.getsockname()[1]))
    assert not rig.clients and rig.owner.close_count == 0
    rig.owner.assert_active(rig.settings.database)


@pytest.mark.asyncio
async def test_duplicate_owner_transfer_cannot_close_first_service(rig):
    stop, serving, _ = await start(rig)
    try:
        with pytest.raises(service.QueuedPilotServiceError):
            await service.serve_owned_unified_queued_pilot(rig.settings, rig.manifest, rig.client_factory,
                **options(rig))
        assert len(rig.clients) == 1 and rig.owner.close_count == 0
        rig.owner.assert_active(rig.settings.database)
    finally:
        stop.set()
        await asyncio.wait_for(serving, 8)
    closed(rig)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['factory-raises', 'wrong-client', 'bad-bus-paths', 'core-construction'])
async def test_partial_construction_closes_only_transferred_resources(rig, monkeypatch, failure):
    factory = rig.client_factory
    changes = {}
    if failure == 'factory-raises':
        def factory():
            raise RuntimeError('private-detail')
    elif failure == 'wrong-client':
        factory = lambda: object()
    elif failure == 'bad-bus-paths':
        changes['bus_paths'] = {}
    else:
        def fail(*args, **kwargs):
            raise RuntimeError('private-detail')
        monkeypatch.setattr(service, 'create_app', fail)
    with pytest.raises(service.QueuedPilotServiceError) as error:
        await service.serve_owned_unified_queued_pilot(rig.settings, rig.manifest, factory,
            **options(rig, **changes))
    assert 'private-detail' not in str(error.value)
    assert rig.owner.close_count == 1
    if rig.clients:
        assert rig.clients[0].close_count == 1
    assert 'bus-start' not in rig.events


@pytest.mark.asyncio
async def test_existing_unenrolled_database_cannot_be_adopted(rig, tmp_path):
    legacy = tmp_path / 'legacy'
    legacy.mkdir(mode=0o700)
    path = legacy / 'hub.db'
    repo = HubRepository(path)
    record = repo.create_task('human:42', 'engineering-hub', 'Preserve legacy task', 'old')
    before = path.read_bytes()
    with pytest.raises(PilotOwnershipError):
        PilotOwner(path, allow_ephemeral_test_storage=True)
    with pytest.raises(ValueError):
        await service.serve_owned_unified_queued_pilot(rig.settings, rig.manifest, rig.client_factory,
            **options(rig, owner=None))
    assert path.read_bytes() == before
    assert repo.get_task(record['id']) == record
    assert not rig.clients and rig.owner.close_count == 0


@pytest.mark.asyncio
async def test_cancellation_during_client_close_keeps_owner_until_close_finishes(rig):
    entered, release = asyncio.Event(), asyncio.Event()
    async def close_block():
        entered.set()
        await release.wait()
    rig.close_block = close_block
    stop, serving, _ = await start(rig)
    try:
        stop.set()
        await asyncio.wait_for(entered.wait(), 8)
        assert rig.events[-2:] == ['bus-stop', 'client-close']
        assert rig.owner.close_count == 0
        serving.cancel()
        serving.cancel()
        await asyncio.sleep(0)
        assert not serving.done()
        rig.owner.assert_active(rig.settings.database)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serving, 8)
    finally:
        release.set()
        await asyncio.gather(serving, return_exceptions=True)
    closed(rig)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['feed-start', 'bus-start', 'client-close'])
async def test_startup_and_cleanup_failures_still_close_owned_resources(rig, monkeypatch, failure):
    if failure == 'feed-start':
        async def fail_start(_):
            raise RuntimeError('private-startup-detail')
        monkeypatch.setattr(service.NativeHubEventFeed, 'start', fail_start)
    elif failure == 'bus-start':
        original = service._create_unified_bus
        def failed_bus(*args, **kwargs):
            bus = original(*args, **kwargs)
            @asynccontextmanager
            async def fail_lifespan(_):
                raise RuntimeError('private-startup-detail')
                yield
            bus.router.lifespan_context = fail_lifespan
            return bus
        monkeypatch.setattr(service, '_create_unified_bus', failed_bus)
    else:
        async def fail_close():
            raise RuntimeError('private-close-detail')
        rig.close_block = fail_close
    if failure == 'client-close':
        stop, serving, _ = await start(rig)
        stop.set()
        with pytest.raises(service.QueuedPilotServiceError) as error:
            await asyncio.wait_for(serving, 8)
    else:
        with pytest.raises(service.QueuedPilotServiceError) as error:
            await service.serve_owned_unified_queued_pilot(rig.settings, rig.manifest,
                rig.client_factory, **options(rig))
    assert 'private-' not in str(error.value)
    assert rig.owner.close_count == rig.clients[0].close_count == 1
    assert not rig.core.state.event_feed.available
    assert not rig.core.state.queued_pilot_runtime.status['running']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['owner', 'async-veto', 'async-factory', 'grace', 'stop-type'])
async def test_invalid_preconditions_leave_caller_owner_untouched(rig, change):
    kwargs = {}
    factory = rig.client_factory
    if change == 'owner':
        kwargs['owner'] = None
    elif change == 'async-veto':
        async def veto(*_):
            return True
        kwargs['target_verifier'] = veto
    elif change == 'async-factory':
        async def factory():
            return rig.client_factory()
    elif change == 'grace':
        kwargs['supervisor_stop_grace_seconds'] = 1
    else:
        kwargs['stop_event'] = object()
    with pytest.raises(ValueError):
        await service.serve_owned_unified_queued_pilot(rig.settings, rig.manifest,
            factory, **options(rig, **kwargs))
    assert rig.owner.close_count == 0 and not rig.clients
    rig.owner.assert_active(rig.settings.database)
