"""Real Linux private service ownership; remote effects remain synthetic.

These checks do not provision workers, contact Docker, prove host exclusivity,
or authorize a production deployment. Ephemeral owner storage is enabled only
by a constructor shim in this test module, never a service argument.
Client freshness/dedication remains an operator precondition, not a global
registry proof. Additional target vetoes are not machine-wide restore barriers.
"""
import asyncio
import json
import os
import socket
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip('fcntl', reason='Real Linux owner and Unix socket service')

import httpx

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings
from engineering_hub.container_pilot import ContainerPilotTarget
from engineering_hub.container_pilot_client import ContainerPilotClient, ContainerPilotRequest
from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError
from engineering_hub.private_browser_socket import create_private_browser_client
from engineering_hub.private_gateway import PublicBrowserGateway
from tests.test_hub_container_pilot import ACTIONS, COMMIT, IMAGE, pilot_manifest
from tests.test_hub_gateway_queued_pilot import signed_access, checked, BASE, ORIGIN
from tests.test_hub_queued_pilot import DistinctContainerClient, ACTION, MACHINE


async def until(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from engineering_hub import queued_pilot_service as service

    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    sockets = tmp_path / 'socket'
    sockets.mkdir(mode=0o700)
    config, assertion = signed_access(monkeypatch)
    state = SimpleNamespace(service=service, owners=[], clients=[], events=[],
        host=DistinctContainerClient(), requests=[], block=None,
        settings=Settings(private / 'hub.sqlite3',
            AuthSettings('test-hermes', 'test-codex', claude_token='test-claude',
                         allowed_ips=frozenset({'127.0.0.1'})), config,
            staging_machine_id=MACHINE),
        target=ContainerPilotTarget.from_manifest(pilot_manifest()),
        manifest=pilot_manifest(),
        endpoint=sockets / 'browser.sock', assertion=assertion)

    def owner_constructor(path):
        owner = PilotOwner(path, allow_ephemeral_test_storage=True)
        state.owners.append(owner)
        close = owner.close
        owner.close_count = 0

        def close_owned():
            owner.close_count += 1
            state.events.append('owner-close')
            close()

        owner.close = close_owned
        return owner

    monkeypatch.setattr(service, 'PilotOwner', owner_constructor)

    async def transport(request):
        assert request.method == 'POST'
        command = ContainerPilotRequest.model_validate(json.loads(await request.aread()))
        state.requests.append(command)
        if state.block is not None:
            await state.block(command)
        response = state.host.response(command)
        return httpx.Response(200, json=response.model_dump())

    def client_factory():
        client = ContainerPilotClient('http://127.0.0.1', 'test-private-executor',
                                      transport=httpx.MockTransport(transport))
        client.close_count = 0
        close = client.aclose

        async def close_owned():
            client.close_count += 1
            state.events.append('client-close')
            await close()

        client.aclose = close_owned
        state.clients.append(client)
        return client

    state.client_factory = client_factory
    original_create_app = service.create_app

    def create_app(*args, **kwargs):
        state.core = original_create_app(*args, **kwargs)
        return state.core

    monkeypatch.setattr(service, 'create_app', create_app)
    return state


async def human_task(state, browser, key):
    response = await browser.get(BASE + '/auth/login')
    assert response.status_code == 303
    cookie = browser.cookies.get(state.settings.auth_config.cookie_name)
    browser.headers['Cookie'] = state.settings.auth_config.cookie_name + '=' + cookie
    identity = checked(await browser.get(BASE + '/auth/me'))
    browser.headers.update({'Origin': ORIGIN, 'X-Hub-CSRF': identity['csrf_token']})
    return checked(await browser.post(BASE + '/tasks', json={
        'project': 'engineering-hub', 'title': 'Owned service synthetic pilot',
        'client_request_id': key}), 201)


async def review_task(agent, task):
    path = BASE + '/tasks/' + task['id']
    task = checked(await agent.post(path + '/claim', json={'expected_revision': task['revision']}))
    task = checked(await agent.post(path + '/candidate', json={
        'expected_revision': task['revision'], 'commit': COMMIT}))
    task = checked(await agent.post(path + '/review',
        headers={'Authorization': 'Bearer test-claude'}, json={
            'expected_revision': task['revision'], 'commit': COMMIT, 'verdict': 'approve'}))
    return task


async def reviewed_job(agent, task, key):
    task = await review_task(agent, task)
    return checked(await agent.post(BASE + '/work/jobs', json={
        'request_id': key, 'action_key': ACTION, 'environment': 'staging',
        'target': {'task_id': task['id'], 'source_commit': COMMIT, 'artifact_digest': IMAGE}}), 201)


def options(state, **changes):
    values = dict(action_key=ACTION, machine_id=MACHINE, target_verifier=lambda *_: True,
        socket_path=state.endpoint, supervisor_stop_grace_seconds=2000,
        agent_host='127.0.0.1', agent_port=0, install_signals=False)
    values.update(changes)
    return values


async def start(state, **changes):
    stop = asyncio.Event()
    ready = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(state.service.serve_owned_queued_pilot(
        state.settings, state.manifest, state.client_factory,
        **options(state, stop_event=stop, on_ready=lambda agent, _: ready.set_result(agent), **changes)))
    try:
        address = await asyncio.wait_for(asyncio.shield(ready), 8)
    except BaseException:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)
        raise
    return stop, task, address


def agent_client(address):
    return httpx.AsyncClient(base_url='http://127.0.0.1:' + str(address[1]), trust_env=False,
        headers={'Host': 'hub.example', 'Authorization': 'Bearer test-codex'})


def assert_closed(state):
    assert len(state.clients) == len(state.owners) == 1
    assert state.clients[0].close_count == state.owners[0].close_count == 1
    assert state.events[-2:] == ['client-close', 'owner-close']
    with pytest.raises(PilotOwnershipError):
        state.owners[0].assert_active(state.settings.database)


@pytest.mark.asyncio
@pytest.mark.parametrize('rollback', [False, True], ids=['healthy', 'rollback'])
async def test_actual_private_service_http_review_native_queue_and_owned_shutdown(setup, rollback):
    state = setup
    if rollback:
        state.host.outcomes['smoke_container'] = 'failed'
    stop, serving, address = await start(state)
    try:
        async with create_private_browser_client(state.endpoint) as private:
            gateway = PublicBrowserGateway(private)
            async with (httpx.AsyncClient(transport=httpx.ASGITransport(gateway), base_url=ORIGIN,
                    follow_redirects=False, headers={'Cf-Access-Jwt-Assertion': state.assertion}) as browser,
                    agent_client(address) as agent):
                task = await human_task(state, browser, 'owned-task')
                job = await reviewed_job(agent, task, 'owned-work')
                runtime = state.core.state.queued_pilot_runtime
                await until(lambda: runtime.status['last_job'] is not None)
                seen = checked(await browser.get(BASE + '/work/jobs/' + job['id']))
                assert seen['state'] == ('failed' if rollback else 'succeeded')
                assert [command.action for command in state.requests] == (ACTIONS if rollback else ACTIONS[:2])
                repo = state.core.state.repository
                assert repo.list_runs()[0]['status'] == ('rolled_back' if rollback else 'succeeded')
                with repo._connection() as database:
                    assert database.execute('SELECT job_id FROM hub_work_machines WHERE job_id IS NOT NULL').fetchall() == []
                    assert database.execute("SELECT name FROM sqlite_master WHERE name LIKE 'hub_dispatch_%'").fetchall() == []
                assert state.clients[0].close_count == state.owners[0].close_count == 0
    finally:
        stop.set()
        await asyncio.wait_for(serving, 8)
    assert_closed(state)
    assert not state.endpoint.exists()
    assert runtime.status['state'] == 'stopped' and not state.core.state.event_feed.available


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['missing-verifier', 'async-verifier', 'async-callable',
    'async-partial', 'async-factory', 'async-generator', 'async-generator-callable',
    'machine', 'manifest', 'grace', 'settings'])
async def test_invalid_configuration_before_listener_owner_or_client(setup, monkeypatch, change):
    import functools
    state = setup
    kwargs = options(state)
    settings, manifest, factory = state.settings, state.manifest, state.client_factory
    async def asynchronous(*_):
        return True
    class AsyncCallable:
        async def __call__(self, *_):
            return True
    async def async_generator(*_):
        yield True
    class AsyncGeneratorCallable:
        async def __call__(self, *_):
            yield True
    if change == 'missing-verifier':
        kwargs['target_verifier'] = None
    elif change == 'async-verifier':
        kwargs['target_verifier'] = asynchronous
    elif change == 'async-callable':
        kwargs['target_verifier'] = AsyncCallable()
    elif change == 'async-partial':
        kwargs['target_verifier'] = functools.partial(asynchronous)
    elif change == 'async-factory':
        factory = asynchronous
    elif change == 'async-generator':
        kwargs['target_verifier'] = async_generator
    elif change == 'async-generator-callable':
        kwargs['target_verifier'] = AsyncGeneratorCallable()
    elif change == 'machine':
        kwargs['machine_id'] = 'other-machine'
    elif change == 'manifest':
        manifest = object()
    elif change == 'grace':
        kwargs['supervisor_stop_grace_seconds'] = True
    else:
        settings = object()
    runner_calls = []
    async def runner(*args, **kwargs):
        runner_calls.append(True)
        pytest.fail('Invalid configuration reached listener runner')
    monkeypatch.setattr(state.service, 'serve_private_coordinator', runner)
    with pytest.raises((ValueError, RuntimeError)):
        await state.service.serve_owned_queued_pilot(settings, manifest, factory, **kwargs)
    assert not runner_calls and not state.owners and not state.clients
    assert not state.settings.database.exists() and not state.endpoint.exists()


@pytest.mark.asyncio
async def test_preclosed_stop_does_not_construct_owner_client_or_database(setup):
    state = setup
    stop = asyncio.Event()
    stop.set()
    await state.service.serve_owned_queued_pilot(state.settings, state.manifest,
        state.client_factory, **options(state, stop_event=stop))
    assert not state.owners and not state.clients
    assert not state.settings.database.exists() and not state.endpoint.exists()


@pytest.mark.asyncio
async def test_real_bind_failure_does_not_construct_owner_or_client(setup):
    state = setup
    held = socket.socket(socket.AF_INET)
    held.bind(('127.0.0.1', 0))
    held.listen(1)
    try:
        with pytest.raises((OSError, RuntimeError)):
            await state.service.serve_owned_queued_pilot(state.settings, state.manifest,
                state.client_factory, **options(state, agent_port=held.getsockname()[1]))
    finally:
        held.close()
    assert not state.owners and not state.clients
    assert not state.settings.database.exists() and not state.endpoint.exists()


@pytest.mark.asyncio
async def test_invalid_factory_return_is_not_closed_but_accepted_owner_is(setup):
    state = setup
    foreign = SimpleNamespace(closes=0)
    async def close():
        foreign.closes += 1
    foreign.aclose = close
    with pytest.raises(RuntimeError):
        await state.service.serve_owned_queued_pilot(state.settings, state.manifest,
            lambda: foreign, **options(state))
    assert foreign.closes == 0 and state.clients == []
    assert len(state.owners) == 1 and state.owners[0].close_count == 1
    assert not state.endpoint.exists()


@pytest.mark.asyncio
async def test_factory_raises_after_owner_acquisition_closes_only_owned_owner(setup):
    state = setup
    def failed_factory():
        assert len(state.owners) == 1
        state.owners[0].assert_active(state.settings.database)
        raise RuntimeError('synthetic-private-factory-secret')
    with pytest.raises(RuntimeError) as failed:
        await state.service.serve_owned_queued_pilot(state.settings, state.manifest,
            failed_factory, **options(state))
    assert 'synthetic-private' not in str(failed.value)
    assert state.clients == []
    assert len(state.owners) == 1 and state.owners[0].close_count == 1
    assert not state.endpoint.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['workflow', 'core', 'startup'])
async def test_accepted_client_partial_construction_or_startup_failure_closes_owned_resources(setup, monkeypatch, failure):
    state = setup
    def fail(*_, **__):
        raise RuntimeError('synthetic-private-constructor-secret')
    if failure == 'workflow':
        monkeypatch.setattr(state.service, 'ContainerPilotWorkflow', fail)
    elif failure == 'core':
        monkeypatch.setattr(state.service, 'create_app', fail)
    else:
        original_feed = state.service.NativeHubEventFeed
        def unsupported(path):
            feed = original_feed(path)
            feed._support_check = lambda _: (False, 'synthetic-private-native-secret')
            return feed
        monkeypatch.setattr(state.service, 'NativeHubEventFeed', unsupported)
    with pytest.raises(RuntimeError) as failed:
        await state.service.serve_owned_queued_pilot(state.settings, state.manifest,
            state.client_factory, **options(state))
    assert 'synthetic-private' not in str(failed.value)
    assert_closed(state)
    assert not state.endpoint.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict', ['false', 'truthy-integer', 'sync-returns-coroutine'])
async def test_additional_veto_rejects_job_before_any_host_effect(setup, verdict):
    state = setup
    async def asynchronous_result():
        return True
    verifier = {'false': lambda *_: False, 'truthy-integer': lambda *_: 1,
                'sync-returns-coroutine': lambda *_: asynchronous_result()}[verdict]
    stop, serving, address = await start(state, target_verifier=verifier)
    try:
        async with create_private_browser_client(state.endpoint) as private:
            async with (httpx.AsyncClient(transport=httpx.ASGITransport(PublicBrowserGateway(private)),
                    base_url=ORIGIN, follow_redirects=False,
                    headers={'Cf-Access-Jwt-Assertion': state.assertion}) as browser,
                    agent_client(address) as agent):
                task = await human_task(state, browser, 'veto-task')
                task = await review_task(agent, task)
                response = await agent.post(BASE + '/work/jobs', json={
                    'request_id': 'veto-work', 'action_key': ACTION, 'environment': 'staging',
                    'target': {'task_id': task['id'], 'source_commit': COMMIT, 'artifact_digest': IMAGE}})
                assert response.status_code == 403
                assert response.json()['detail']['code'] == 'work_target_not_verified'
                with state.core.state.repository._connection() as db:
                    assert db.execute('SELECT COUNT(*) FROM hub_work_jobs').fetchone()[0] == 0
                assert state.requests == []
    finally:
        stop.set()
        await asyncio.wait_for(serving, 8)
    assert_closed(state)


@pytest.mark.asyncio
@pytest.mark.parametrize('returned', ['started-coroutine', 'future'])
async def test_misdeclared_veto_does_not_close_or_cancel_another_owners_started_awaitable(setup, returned):
    state = setup
    entered, release = asyncio.Event(), asyncio.Event()
    async def independent_owner():
        entered.set()
        await release.wait()
        return 'independently-completed'
    worker = asyncio.create_task(independent_owner())
    await asyncio.wait_for(entered.wait(), 8)
    future = asyncio.get_running_loop().create_future()
    value = worker.get_coro() if returned == 'started-coroutine' else future
    stop = serving = None
    try:
        stop, serving, address = await start(state, target_verifier=lambda *_: value)
        async with create_private_browser_client(state.endpoint) as private:
            async with (httpx.AsyncClient(transport=httpx.ASGITransport(PublicBrowserGateway(private)),
                    base_url=ORIGIN, follow_redirects=False,
                    headers={'Cf-Access-Jwt-Assertion': state.assertion}) as browser,
                    agent_client(address) as agent):
                task = await human_task(state, browser, 'owned-awaitable-task')
                task = await review_task(agent, task)
                response = await agent.post(BASE + '/work/jobs', json={
                    'request_id': 'owned-awaitable-work', 'action_key': ACTION, 'environment': 'staging',
                    'target': {'task_id': task['id'], 'source_commit': COMMIT, 'artifact_digest': IMAGE}})
                assert response.status_code == 403
                assert not worker.done() and worker.get_coro().cr_frame is not None
                assert not future.done() and not future.cancelled()
                assert state.requests == []
                future.set_result('future-owned-completion')
                release.set()
                assert await asyncio.wait_for(worker, 8) == 'independently-completed'
                assert await future == 'future-owned-completion'
    finally:
        release.set()
        if not future.done():
            future.set_result('cleanup')
        await asyncio.gather(worker, return_exceptions=True)
        if stop is not None:
            stop.set()
        if serving is not None:
            await asyncio.wait_for(serving, 8)
    assert_closed(state)


@pytest.mark.asyncio
async def test_live_additional_veto_is_rechecked_between_phases_and_keeps_machine(setup):
    state = setup
    allowed = [True]
    async def flip_after_deploy(command):
        if command.action == 'deploy_container':
            allowed[0] = False
    state.block = flip_after_deploy
    stop, serving, address = await start(state, target_verifier=lambda *_: allowed[0])
    try:
        async with create_private_browser_client(state.endpoint) as private:
            async with (httpx.AsyncClient(transport=httpx.ASGITransport(PublicBrowserGateway(private)),
                    base_url=ORIGIN, follow_redirects=False,
                    headers={'Cf-Access-Jwt-Assertion': state.assertion}) as browser,
                    agent_client(address) as agent):
                task = await human_task(state, browser, 'changing-veto-task')
                job = await reviewed_job(agent, task, 'changing-veto-work')
                runtime = state.core.state.queued_pilot_runtime
                await until(lambda: runtime.status['last_job'] is not None)
                assert state.core.state.work_queue.get_job(job['id'])['state'] == 'held'
                with state.core.state.repository._connection() as db:
                    assert db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?',
                                      (MACHINE,)).fetchone()[0] == job['id']
                assert [command.action for command in state.requests] == ['deploy_container']
    finally:
        stop.set()
        await asyncio.wait_for(serving, 8)
    assert_closed(state)


@pytest.mark.asyncio
async def test_repeated_cancel_drains_accepted_http_and_phase_before_client_and_owner_close(setup, monkeypatch):
    state = setup
    phase_entered, phase_release = asyncio.Event(), asyncio.Event()
    request_entered, request_release = asyncio.Event(), asyncio.Event()
    async def block_phase(command):
        phase_entered.set()
        await phase_release.wait()
    state.block = block_phase
    create_app = state.service.create_app
    def blocking_core(*args, **kwargs):
        core = create_app(*args, **kwargs)
        @core.middleware('http')
        async def block(request, call_next):
            if request.url.path == BASE + '/projects':
                request_entered.set()
                await request_release.wait()
            return await call_next(request)
        return core
    monkeypatch.setattr(state.service, 'create_app', blocking_core)
    stop, serving, address = await start(state)
    incoming = None
    try:
        async with create_private_browser_client(state.endpoint) as private:
            async with (httpx.AsyncClient(transport=httpx.ASGITransport(PublicBrowserGateway(private)),
                    base_url=ORIGIN, follow_redirects=False,
                    headers={'Cf-Access-Jwt-Assertion': state.assertion}) as browser,
                    agent_client(address) as agent):
                task = await human_task(state, browser, 'drain-task')
                job = await reviewed_job(agent, task, 'drain-work')
                await asyncio.wait_for(phase_entered.wait(), 8)
                incoming = asyncio.create_task(agent.get(BASE + '/projects'))
                await asyncio.wait_for(request_entered.wait(), 8)
                serving.cancel()
                await until(lambda: not state.core.state.queued_pilot_runtime.admission_allowed())
                serving.cancel()
                assert not serving.done() and not incoming.done()
                assert state.clients[0].close_count == state.owners[0].close_count == 0
                state.owners[0].assert_active(state.settings.database)
                assert state.core.state.event_feed.available
                request_release.set()
                assert (await asyncio.wait_for(incoming, 8)).status_code == 200
                assert not serving.done() and state.clients[0].close_count == 0
                phase_release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(serving, 8)
    finally:
        request_release.set()
        phase_release.set()
        stop.set()
        if incoming is not None:
            await asyncio.gather(incoming, return_exceptions=True)
        await asyncio.gather(serving, return_exceptions=True)
    assert_closed(state)
    assert [command.action for command in state.requests] == ['deploy_container']
    db = sqlite3.connect('file:' + str(state.settings.database) + '?mode=ro', uri=True)
    try:
        assert db.execute('SELECT state FROM hub_work_jobs WHERE id=?', (job['id'],)).fetchone()[0] == 'held'
        assert db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?', (MACHINE,)).fetchone()[0] == job['id']
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_close', ['client', 'owner'])
async def test_cleanup_failure_is_sanitized_and_still_closes_other_resource(setup, failed_close):
    state = setup
    stop, serving, _ = await start(state)
    if failed_close == 'client':
        original = state.clients[0].aclose
        async def broken_close():
            await original()
            raise RuntimeError('synthetic-private-close-secret')
        state.clients[0].aclose = broken_close
    else:
        original = state.owners[0].close
        def broken_close():
            original()
            raise RuntimeError('synthetic-private-close-secret')
        state.owners[0].close = broken_close
    stop.set()
    with pytest.raises(RuntimeError) as failed:
        await asyncio.wait_for(serving, 8)
    assert 'synthetic-private' not in str(failed.value)
    assert_closed(state)
    assert not state.endpoint.exists()


@pytest.mark.asyncio
async def test_repeated_cancel_during_accepted_client_close_keeps_owner_until_actual_close_finishes(setup):
    state = setup
    stop, serving, _ = await start(state)
    entered, release = asyncio.Event(), asyncio.Event()
    original_close = state.clients[0].aclose
    async def blocked_close():
        entered.set()
        await release.wait()
        await original_close()
    state.clients[0].aclose = blocked_close
    try:
        stop.set()
        await asyncio.wait_for(entered.wait(), 8)
        serving.cancel()
        await asyncio.sleep(0)
        serving.cancel()
        await asyncio.sleep(0)
        assert not serving.done()
        assert state.clients[0].close_count == state.owners[0].close_count == 0
        state.owners[0].assert_active(state.settings.database)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serving, 8)
    finally:
        release.set()
        await asyncio.gather(serving, return_exceptions=True)
    assert_closed(state)


@pytest.mark.asyncio
async def test_service_exposes_no_ephemeral_override_and_real_default_refuses_test_filesystem(setup, monkeypatch):
    import inspect
    from engineering_hub.pilot_ownership import _local_filesystem
    state = setup
    parameters = inspect.signature(state.service.serve_owned_queued_pilot).parameters
    assert 'allow_ephemeral_test_storage' not in parameters and 'pilot_owner' not in parameters
    fd = os.open(state.settings.database.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            _local_filesystem(fd)
        except PilotOwnershipError:
            pass
        else:
            pytest.skip('Default-refusal proof requires the isolated ephemeral validation filesystem')
    finally:
        os.close(fd)
    monkeypatch.setattr(state.service, 'PilotOwner', PilotOwner)
    with pytest.raises(RuntimeError):
        await state.service.serve_owned_queued_pilot(state.settings, state.manifest,
            state.client_factory, **options(state))
    assert not state.clients and not state.owners
    assert not state.endpoint.exists()


@pytest.mark.asyncio
async def test_existing_legacy_dispatch_records_are_not_adopted_or_deleted(setup):
    from engineering_hub.repository import HubRepository
    state = setup
    with PilotOwner(state.settings.database, allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(state.settings.database,
            execution_principal='executor:container-pilot', pilot_owner=owner)
        with repo._transaction() as db:
            db.execute('CREATE TABLE hub_dispatch_cursor(marker TEXT NOT NULL)')
            db.execute("INSERT INTO hub_dispatch_cursor VALUES('retained-legacy-marker')")
    with pytest.raises(RuntimeError):
        await state.service.serve_owned_queued_pilot(state.settings, state.manifest,
            state.client_factory, **options(state))
    assert state.requests == []
    assert_closed(state)
    db = sqlite3.connect('file:' + str(state.settings.database) + '?mode=ro', uri=True)
    try:
        assert db.execute('SELECT marker FROM hub_dispatch_cursor').fetchall() == [('retained-legacy-marker',)]
        assert db.execute('SELECT COUNT(*) FROM hub_work_jobs').fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE name='hub_queued_pilot_policy'").fetchall() == []
    finally:
        db.close()
