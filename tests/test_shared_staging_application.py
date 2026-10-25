"""Real unified listeners and owner; remote executor replies are simulated."""
from dataclasses import replace
import asyncio
import base64
import json
import sys

import httpx
import pytest

from engineering_hub.staging_client import StagingClient
from tests.test_shared_staging_preparation import config_file
from tests.test_hub_queued_pilot_service import setup as private_setup
from tests.test_hub_unified_queued_pilot_service import (
    rig, start, connections, submit, until, closed, options, BASE, ORIGIN, service,
)

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux owner/native listeners')


async def owner_session(browser):
    response = await browser.post(BASE + '/auth/bootstrap', headers={'Origin': ORIGIN})
    assert response.status_code == 200
    browser.headers['Cookie'] = 'hub_session=' + browser.cookies.get('hub_session')
    browser.headers.update({'Origin': ORIGIN, 'X-Hub-CSRF': response.json()['csrf_token']})


@pytest.fixture
async def shared_app(rig, tmp_path, monkeypatch):
    from engineering_hub.cloudflare_access import CloudflareAccessVerifier
    identity = await CloudflareAccessVerifier(rig.settings.auth_config.cloudflare_config).verify(rig.assertion)
    rig.owner_actor = identity.actor
    path = config_file(tmp_path)
    data = json.loads(path.read_text())
    data['admin_actors'] = [rig.owner_actor]
    path.write_text(json.dumps(data))
    rig.settings = replace(rig.settings, staging_config_path=path)
    rig.modular_clients, rig.modular_requests = [], []
    rig.modular_handler = None
    original = service.prepare_shared_staging
    def factory(config):
        async def response(request):
            rig.modular_requests.append(request)
            if rig.modular_handler is not None:
                return await rig.modular_handler(request)
            raise AssertionError('No modular remote call expected in this fixture')
        client = StagingClient(config, test_transport=httpx.MockTransport(response))
        client.close_count = 0
        close = client.aclose
        async def aclose():
            client.close_count += 1
            rig.events.append('modular-client-close')
            await close()
        client.aclose = aclose
        rig.modular_clients.append(client)
        return client
    def prepare(repo, **kwargs):
        return original(repo, **kwargs, client_factory=factory)
    monkeypatch.setattr(service, 'prepare_shared_staging', prepare)
    return rig


@pytest.mark.asyncio
async def test_unified_application_reuses_one_module_and_runs_original_pilot(shared_app):
    state = shared_app
    stop, task, addresses = await start(state)
    try:
        core = state.core
        composition = core.state.shared_staging
        assert composition.status['state'] == 'active'
        assert composition.staging_module is core.state.modular_staging
        assert core.state.work_queue is composition.queue is composition.controller.queue
        assert len(state.modular_clients) == 1 and not composition.controller.background
        async with connections(addresses, state) as (agent, browser):
            _, job = await submit(agent, browser, 'shared-app-pilot')
            await until(lambda: core.state.work_queue.get_job(job['id'])['state'] == 'succeeded')
            status = await browser.get(BASE + '/staging/shared/status')
            assert status.status_code == 200 and status.json() == {'state': 'active', 'reason': None}
            assert (await agent.get(BASE + '/staging/shared/status')).status_code in {401, 403}
            assert len(state.requests) == 2 and state.modular_requests == []
    finally:
        stop.set()
        await task
    closed(state)
    assert state.modular_clients[0].close_count == 1
    assert state.events.index('modular-client-close') < state.events.index('bus-stop')


@pytest.mark.asyncio
async def test_pending_shared_start_serves_authenticated_http_without_starting_loops(shared_app):
    from tests.manual_session_fixtures import synthetic_reconciliation
    state = shared_app
    def pending(owner, target):
        return synthetic_reconciliation(owner, target, accepted=False)
    stop, task, addresses = await start(state, reconciliation_factory=pending)
    try:
        composition = state.core.state.shared_staging
        assert composition.status == {'state': 'pending', 'reason': 'operator_acceptance_required'}
        assert state.core.state.queued_pilot_runtime._task is None
        assert composition.controller.background is None
        async with connections(addresses, state) as (agent, browser):
            await owner_session(browser)
            assert (await browser.get(BASE + '/auth/me')).status_code == 200
            assert (await browser.get(BASE + '/staging/shared/status')).json()['state'] == 'pending'
            recovered = await browser.post(BASE + '/staging/shared/recover', json={})
            assert recovered.status_code == 200 and recovered.json()['state'] == 'pending'
            assert (await browser.post(BASE + '/staging/shared/recover', json={'owner': 'forged'})).status_code == 422
            browser.headers.pop('X-Hub-CSRF')
            assert (await browser.post(BASE + '/staging/shared/recover', json={})).status_code in {401, 403}
            assert (await agent.post(BASE + '/staging/shared/recover', json={})).status_code in {401, 403}
        assert state.requests == state.modular_requests == []
    finally:
        stop.set()
        await task
    closed(state)
    assert state.modular_clients[0].close_count == 1


@pytest.mark.asyncio
async def test_existing_human_acceptance_opens_shared_barrier_without_replacing_review(shared_app):
    from tests.test_hub_queued_session_acceptance_services import factories
    state = shared_app
    reconciliation, acceptance = factories(state)
    stop, task, addresses = await start(state, reconciliation_factory=reconciliation,
                                       session_acceptance_factory=acceptance)
    try:
        async with connections(addresses, state) as (agent, browser):
            await owner_session(browser)
            path = BASE + '/staging/session-reconciliation'
            snapshot = (await browser.get(path)).json()
            assert snapshot['state'] == state.core.state.shared_staging.status['state'] == 'pending'
            accepted = await browser.post(path, json={'request_id': snapshot['request_id'],
                'expected_revision': snapshot['revision'], 'action': 'accept'})
            assert accepted.status_code == 200 and accepted.json()['state'] == 'accepted'
            assert accepted.json()['execution_authorization'] is False
            assert state.core.state.shared_staging.status['state'] == 'active'
            replay = await browser.post(path, json={'request_id': snapshot['request_id'],
                'expected_revision': snapshot['revision'], 'action': 'accept'})
            assert replay.status_code >= 400 and state.requests == []
            _, job = await submit(agent, browser, 'shared-after-human-acceptance')
            await until(lambda: state.core.state.work_queue.get_job(job['id'])['state'] == 'succeeded')
    finally:
        stop.set()
        await task
    assert state.owner.close_count == state.clients[0].close_count == state.modular_clients[0].close_count == 1


@pytest.mark.asyncio
async def test_signed_in_non_admin_cannot_read_or_drive_shared_barrier(shared_app):
    state = shared_app
    data = json.loads(state.settings.staging_config_path.read_text())
    data['admin_actors'] = ['human:999']
    state.settings.staging_config_path.write_text(json.dumps(data))
    stop, task, addresses = await start(state)
    try:
        async with connections(addresses, state) as (_, browser):
            await owner_session(browser)
            assert (await browser.get(BASE + '/staging/shared/status')).status_code == 403
            assert (await browser.post(BASE + '/staging/shared/recover', json={})).status_code == 403
    finally:
        stop.set()
        await task


@pytest.mark.asyncio
async def test_partial_app_construction_failure_closes_prepared_clients_and_owner(shared_app, monkeypatch):
    state = shared_app
    def failed(*args, **kwargs):
        assert kwargs['shared_staging'] is not None
        raise ValueError('Synthetic app constructor failure')
    monkeypatch.setattr(service, 'create_app', failed)
    with pytest.raises(service.QueuedPilotServiceError):
        await service.serve_owned_unified_queued_pilot(state.settings, state.manifest, state.client_factory,
            **options(state))
    assert state.modular_clients[0].close_count == state.clients[0].close_count == state.owner.close_count == 1
    assert state.events[-1] == 'owner-close'


@pytest.mark.asyncio
async def test_rejected_factory_task_drains_before_prepared_transport_closes(shared_app):
    state = shared_app
    ready, cleanup_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def invalid_dependency():
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert not state.modular_clients[0]._client.is_closed
            state.owner.assert_active(state.settings.database)
            cleanup_entered.set()
            await release.wait()
            state.events.append('rejected-task-drained')
    dependency = asyncio.create_task(invalid_dependency())
    await ready.wait()
    serving = asyncio.create_task(service.serve_owned_unified_queued_pilot(
        state.settings, state.manifest, state.client_factory,
        **options(state, session_acceptance_factory=lambda *args: dependency)))
    try:
        await asyncio.wait_for(cleanup_entered.wait(), 5)
        assert not serving.done() and state.modular_clients[0].close_count == 0
        assert state.owner.close_count == 0
    finally:
        release.set()
        await asyncio.gather(dependency, return_exceptions=True)
    with pytest.raises(service.QueuedPilotServiceError):
        await serving
    assert state.modular_clients[0].close_count == state.clients[0].close_count == state.owner.close_count == 1
    assert state.events.index('rejected-task-drained') < state.events.index('modular-client-close')
    assert state.events[-1] == 'owner-close'


@pytest.mark.asyncio
async def test_listener_stop_drains_accepted_http_upload_before_transport_and_owner_close(shared_app):
    from tests.test_hub_staging_controller import payload
    state = shared_app
    entered, release = asyncio.Event(), asyncio.Event()
    async def upload(request):
        assert request.url.path == '/v1/sources'
        value = json.loads(request.content)
        entered.set()
        await release.wait()
        state.events.append('upload-remote-returned')
        return httpx.Response(200, json={key: value[key] for key in ('source_id', 'source_digest', 'project')} |
                             {'size_bytes': len(base64.b64decode(value['source_zip_base64']))})
    state.modular_handler = upload
    stop, task, addresses = await start(state)
    request_task = None
    try:
        controller = state.core.state.modular_staging.controller
        controller.resources.set_policy(state.owner_actor, 'engineering-hub', 0,
                                        {'codex': ['read', 'build', 'test']})
        async with connections(addresses, state) as (agent, _):
            request_task = asyncio.create_task(agent.post(BASE + '/staging/jobs', json=payload()))
            await asyncio.wait_for(entered.wait(), 3)
            stop.set()
            await until(lambda: state.core.state.shared_staging._closing)
            assert not task.done() and state.modular_clients[0].close_count == 0
            assert state.owner.close_count == 0
            release.set()
            assert (await request_task).status_code == 409
        await task
    finally:
        release.set(); stop.set()
        if request_task is not None:
            await asyncio.gather(request_task, return_exceptions=True)
        await asyncio.gather(task, return_exceptions=True)
    closed(state)
    assert state.modular_clients[0].close_count == 1
    assert state.events.index('upload-remote-returned') < state.events.index('modular-client-close')
    assert all(request.url.path != '/v1/jobs' for request in state.modular_requests)


def test_browser_route_allowlist_exposes_only_explicit_shared_paths():
    from engineering_hub.private_gateway import browser_route_allowed
    assert browser_route_allowed('GET', BASE + '/staging/shared/status')
    assert browser_route_allowed('POST', BASE + '/staging/shared/recover')
    for method, suffix in [('GET', '/recover'), ('POST', '/status'), ('DELETE', '/recover'), ('POST', '/anything')]:
        assert not browser_route_allowed(method, BASE + '/staging/shared' + suffix)


@pytest.mark.asyncio
async def test_private_socket_service_uses_same_shared_queue_and_owned_shutdown(private_setup, tmp_path, monkeypatch):
    from tests import test_hub_queued_pilot_service as private
    state = private_setup
    state.settings = replace(state.settings, staging_config_path=config_file(tmp_path))
    clients = []
    original = state.service.prepare_shared_staging
    def factory(config):
        def unexpected(request):
            raise AssertionError('No modular remote effect expected')
        client = StagingClient(config, test_transport=httpx.MockTransport(unexpected))
        clients.append(client)
        return client
    def prepare(repo, **kwargs):
        return original(repo, **kwargs, client_factory=factory)
    monkeypatch.setattr(state.service, 'prepare_shared_staging', prepare)
    stop, serving, address = await private.start(state)
    try:
        composition = state.core.state.shared_staging
        assert composition.status['state'] == 'active'
        assert composition.queue is state.core.state.work_queue is composition.controller.queue
        async with private.create_private_browser_client(state.endpoint) as transport:
            gateway = private.PublicBrowserGateway(transport)
            async with (httpx.AsyncClient(transport=httpx.ASGITransport(gateway), base_url=ORIGIN,
                    headers={'Cf-Access-Jwt-Assertion': state.assertion}) as browser,
                    private.agent_client(address) as agent):
                task = await private.human_task(state, browser, 'private-shared-task')
                job = await private.reviewed_job(agent, task, 'private-shared-work')
                await private.until(lambda: composition.queue.get_job(job['id'])['state'] == 'succeeded')
    finally:
        stop.set()
        await serving
    private.assert_closed(state)
    assert len(clients) == 1 and clients[0]._client.is_closed and composition.is_drained
