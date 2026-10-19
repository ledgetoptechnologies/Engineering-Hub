"""Real loopback/UDS runner composition; synthetic host effects, not deployment.

Both runner functions share this test process. This verifies listener/lifecycle
contracts, not deployment namespace or credential separation between containers.
"""
import asyncio
import sys

import httpx
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.events import NativeHubEventFeed
from engineering_hub.frontends import HubFrontends
from engineering_hub.private_browser_socket import create_private_browser_client
from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
from engineering_hub.split_server import serve_private_coordinator, serve_public_browser
from tests.test_hub_gateway_queued_pilot import signed_access, checked, BASE, ORIGIN
from tests.test_hub_queued_pilot import rig as pilot_rig, ACTION, machine_job, submit
from tests.test_hub_container_pilot import COMMIT, IMAGE, ACTIONS

pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux owner/Unix socket')]


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize('failed_smoke', [False, True], ids=['healthy', 'rollback'])
async def test_two_runner_http_review_and_native_pilot_flow(pilot_rig, tmp_path, monkeypatch, failed_smoke):
    rig = pilot_rig
    config, assertion = signed_access(monkeypatch)
    auth = AuthSettings('test-hermes', 'test-codex', claude_token='test-claude',
                        allowed_ips=frozenset({'127.0.0.1'}))
    if failed_smoke:
        rig.client.outcomes['smoke_container'] = 'failed'
    feed = NativeHubEventFeed(rig.repo.path)
    runtime = QueuedPilotRuntime(rig.bridge, feed)
    directory = tmp_path / 'socket'
    directory.mkdir(mode=0o700)
    endpoint = directory / 'browser.sock'
    holder = {}
    def pair_factory():
        core = create_app(Settings(rig.repo.path, auth, config), repository=rig.repo,
            event_feed=feed, work_queue=rig.queue, queued_pilot_runtime=runtime)
        holder['pair'] = HubFrontends(core)
        return holder['pair']
    private_stop, public_stop = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    private_ready, public_ready = loop.create_future(), loop.create_future()
    private_task = asyncio.create_task(serve_private_coordinator(pair_factory,
        socket_path=endpoint, agent_host='127.0.0.1', agent_port=0,
        stop_event=private_stop, install_signals=False,
        on_ready=lambda agent, private: private_ready.set_result(agent)))
    public_task = None
    client_holder = {}
    try:
        agent_address = await asyncio.wait_for(private_ready, 5)
        def client_factory():
            client_holder['client'] = create_private_browser_client(endpoint)
            return client_holder['client']
        public_task = asyncio.create_task(serve_public_browser(client_factory,
            browser_port=0, stop_event=public_stop, install_signals=False,
            on_ready=lambda address: public_ready.set_result(address)))
        browser_address = await asyncio.wait_for(public_ready, 5)
        assert browser_address[0] == '127.0.0.1'
        agent_url = f'http://127.0.0.1:{agent_address[1]}'
        browser_url = f'http://127.0.0.1:{browser_address[1]}'
        async with (httpx.AsyncClient(base_url=browser_url, trust_env=False,
                headers={'Host': 'hub.example', 'Cf-Access-Jwt-Assertion': assertion},
                follow_redirects=False) as browser,
                httpx.AsyncClient(base_url=agent_url, trust_env=False,
                headers={'Host': 'hub.example', 'Authorization': 'Bearer test-codex'}) as agents):
            assert (await browser.get(BASE + '/auth/login')).status_code == 303
            # The local hop represents the tunnel, not real TLS. Explicitly
            # forward the cookie a browser would send to the HTTPS public origin.
            cookie = browser.cookies.get(config.cookie_name)
            browser.headers['Cookie'] = config.cookie_name + '=' + cookie
            human = checked(await browser.get(BASE + '/auth/me'))
            browser.headers.update({'Origin': ORIGIN, 'X-Hub-CSRF': human['csrf_token']})
            task = checked(await browser.post(BASE + '/tasks', json={
                'project': 'engineering-hub', 'title': 'Split listener pilot',
                'client_request_id': 'split-listener-pilot'}), 201)
            path = BASE + '/tasks/' + task['id']
            task = checked(await agents.post(path + '/claim', json={'expected_revision': task['revision']}))
            task = checked(await agents.post(path + '/candidate',
                json={'expected_revision': task['revision'], 'commit': COMMIT}))
            task = checked(await agents.post(path + '/review',
                headers={'Authorization': 'Bearer test-claude'},
                json={'expected_revision': task['revision'], 'commit': COMMIT, 'verdict': 'approve'}))
            job = checked(await agents.post(BASE + '/work/jobs', json={
                'request_id': 'split-listener-work', 'action_key': ACTION,
                'environment': 'staging', 'target': {'task_id': task['id'],
                    'source_commit': COMMIT, 'artifact_digest': IMAGE}}), 201)
            await until(lambda: runtime.status['last_job'] is not None)
            seen = checked(await browser.get(BASE + '/work/jobs/' + job['id']))
            assert seen['state'] == ('failed' if failed_smoke else 'succeeded')
            assert [c.action for c in rig.client.submissions] == (ACTIONS if failed_smoke else ACTIONS[:2])
            assert machine_job(rig) is None
            assert (await agents.get('/')).status_code == 403
            assert (await browser.get(BASE + '/tasks',
                headers={'Authorization': 'Bearer test-codex'})).status_code == 403
        assert feed.available and runtime.status['state'] == 'active'
    finally:
        public_stop.set()
        if public_task is not None:
            await asyncio.wait_for(public_task, 5)
        private_stop.set()
        await asyncio.wait_for(private_task, 5)
    assert client_holder['client'].is_closed
    assert holder['pair'].state == 'closed'
    assert not feed.available and runtime.status['state'] == 'stopped'
    assert not endpoint.exists()
    rig.owner.assert_active(rig.repo.path)


async def test_private_stop_vetoes_new_jobs_before_blocked_http_and_effect_drain(
        pilot_rig, tmp_path, monkeypatch):
    rig = pilot_rig
    config, _ = signed_access(monkeypatch)
    auth = AuthSettings('test-hermes', 'test-codex', claude_token='test-claude')
    _, first = submit(rig, 'first')
    _, second = submit(rig, 'second')
    effect_entered, effect_release = asyncio.Event(), asyncio.Event()
    request_entered, request_release = asyncio.Event(), asyncio.Event()
    original = rig.client.submit_command
    async def blocked_effect(command):
        effect_entered.set()
        await effect_release.wait()
        return await original(command)
    rig.client.submit_command = blocked_effect
    feed = NativeHubEventFeed(rig.repo.path)
    runtime = QueuedPilotRuntime(rig.bridge, feed)
    directory = tmp_path / 'socket'
    directory.mkdir(mode=0o700)
    endpoint = directory / 'browser.sock'
    holder = {}
    def factory():
        core = create_app(Settings(rig.repo.path, auth, config), repository=rig.repo,
            event_feed=feed, work_queue=rig.queue, queued_pilot_runtime=runtime)
        @core.middleware('http')
        async def block_request(request, call_next):
            if request.url.path == BASE + '/projects':
                request_entered.set()
                await request_release.wait()
            return await call_next(request)
        holder['pair'] = HubFrontends(core)
        return holder['pair']
    stop = asyncio.Event()
    ready = asyncio.get_running_loop().create_future()
    serving = asyncio.create_task(serve_private_coordinator(factory, socket_path=endpoint,
        agent_host='127.0.0.1', agent_port=0, stop_event=stop, install_signals=False,
        on_ready=lambda address, _: ready.set_result(address)))
    request_task = None
    try:
        address = await asyncio.wait_for(ready, 5)
        await asyncio.wait_for(effect_entered.wait(), 5)
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{address[1]}',
                trust_env=False, headers={'Host': 'hub.example',
                'Authorization': 'Bearer test-codex'}) as client:
            request_task = asyncio.create_task(client.get(BASE + '/projects'))
            await asyncio.wait_for(request_entered.wait(), 5)
            serving.cancel()
            await until(lambda: not runtime.admission_allowed())
            serving.cancel()
            assert holder['pair'].state == 'stopping'
            assert feed.available and endpoint.exists()
            assert not serving.done() and not request_task.done()
            assert rig.queue.get_job(second['id'])['state'] == 'queued'
            assert machine_job(rig) == first['id']
            rig.owner.assert_active(rig.repo.path)
            request_release.set()
            assert (await asyncio.wait_for(request_task, 5)).status_code == 200
            await asyncio.sleep(0)
            assert feed.available and not serving.done()
            effect_release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(serving, 5)
    finally:
        request_release.set()
        effect_release.set()
        stop.set()
        if request_task is not None and not request_task.done():
            await asyncio.gather(request_task, return_exceptions=True)
        if not serving.done():
            await asyncio.gather(serving, return_exceptions=True)
    assert len(rig.client.submissions) == 1
    assert rig.queue.get_job(first['id'])['state'] == 'held'
    assert rig.queue.get_job(second['id'])['state'] == 'queued'
    assert not feed.available and not endpoint.exists()
    rig.owner.assert_active(rig.repo.path)
