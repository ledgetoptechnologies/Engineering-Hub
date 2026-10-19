"""Linux HTTP-to-native-queue integration with typed synthetic remote effects.

Real owner, session, review, queue, lifecycle and native SQLite notifications.
The private browser link uses either in-process ASGI or a real protected Unix
HTTP socket. FakeClient effects are not Docker/application acceptance, and the
public/agent frontends remain in-process; no deployed isolation is claimed.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import sys
import time
import uuid

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_message_bus.auth import AuthSettings
from agent_message_bus.sqlite_events import native_event_support
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub import cloudflare_access as access
from engineering_hub.events import NativeHubEventFeed
from engineering_hub.ingress import HubIngressMiddleware
from engineering_hub.private_gateway import PrivateBrowserGateway, PublicBrowserGateway
from engineering_hub.private_browser_socket import private_browser_socket, create_private_browser_client
from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
from engineering_hub.server import _listener
from tests.test_hub_queued_pilot import rig as pilot_rig, ACTION, machine_job
from tests.test_hub_container_pilot import ACTIONS, COMMIT, IMAGE


BASE = '/api/hub/v1'
ORIGIN = 'https://hub.example'
pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux owner/native events')]


def signed_access(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid='fixture-key', alg='RS256', use='sig')
    config = AuthConfig(ORIGIN, human_auth_mode='cloudflare_access',
        cloudflare_issuer='https://fixture-team.cloudflareaccess.com',
        cloudflare_audience='fixture-hub', cloudflare_allowed_emails=('beau@example.com',))
    verifier = access.CloudflareAccessVerifier(config.cloudflare_config,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'keys': [jwk]})))
    monkeypatch.setattr(access, 'CloudflareAccessVerifier', lambda _: verifier)
    monkeypatch.setattr('engineering_hub.ingress.CloudflareAccessVerifier', lambda _: verifier)
    now = int(time.time())
    assertion = jwt.encode(dict(iss=config.cloudflare_issuer, aud=['fixture-hub'],
        sub='synthetic-human', email='beau@example.com', type='app', iat=now - 1, exp=now + 300),
        private, algorithm='RS256', headers={'kid': 'fixture-key'})
    return config, assertion


def checked(response, expected=200):
    assert response.status_code == expected, response.text
    return response.json()


@asynccontextmanager
async def private_transport(receiver, mode, directory):
    if mode == 'asgi':
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=receiver)) as client:
            yield client
        return
    directory.mkdir(mode=0o700)
    endpoint = directory / 'browser.sock'
    assert len(str(endpoint).encode('utf-8')) <= 107
    with private_browser_socket(endpoint) as listener:
        server = _listener(receiver)  # Explicitly lifespan=off, core is already live.
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done():
                        await serving
                        pytest.fail('Private listener exited before readiness')
                    await asyncio.sleep(0.001)
            async with create_private_browser_client(endpoint) as client:
                yield client
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 5)
    assert not endpoint.exists()


@pytest.mark.parametrize('browser_transport', ['asgi', 'uds'])
@pytest.mark.parametrize('failed_smoke', [False, True], ids=['healthy', 'healthy-rollback'])
async def test_public_human_agent_review_native_runtime_and_result(
        pilot_rig, monkeypatch, failed_smoke, browser_transport, tmp_path):
    rig = pilot_rig
    supported, reason = native_event_support(rig.repo.path)
    if not supported:
        pytest.skip(reason)
    if failed_smoke:
        rig.client.outcomes['smoke_container'] = 'failed'
    config, assertion = signed_access(monkeypatch)
    auth = AuthSettings('test-hermes', 'test-codex', claude_token='test-claude',
                        allowed_ips=frozenset({'192.0.2.10'}))
    feed = NativeHubEventFeed(rig.repo.path)
    runtime = QueuedPilotRuntime(rig.bridge, feed)
    core = create_app(Settings(rig.repo.path, auth, config), repository=rig.repo,
        event_feed=feed, work_queue=rig.queue, queued_pilot_runtime=runtime)
    private = PrivateBrowserGateway(core, agent_auth=auth, cloudflare_config=config.cloudflare_config)
    agent_interface = HubIngressMiddleware(core, mode='agents', agent_auth=auth)
    empty_drain, completed = asyncio.Event(), asyncio.Event()
    original = rig.bridge.run_once

    async def observed_drain():
        result = await original()
        if result is None:
            empty_drain.set()
        else:
            completed.set()
        return result
    monkeypatch.setattr(rig.bridge, 'run_once', observed_drain)

    # Lifespan is owned only by the private core, never the public gateway.
    async with core.router.lifespan_context(core):
        assert runtime.status['state'] == 'active' and feed.available
        await asyncio.wait_for(empty_drain.wait(), 5)
        async with private_transport(private, browser_transport, tmp_path / 'browser-transport') as private_client:
            gateway = PublicBrowserGateway(private_client)
            async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway),
                                         base_url=ORIGIN, follow_redirects=False) as browser,
                        httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_interface,
                                         client=('192.0.2.10', 12345)), base_url=ORIGIN) as agents,
                        httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_interface,
                                         client=('192.0.2.11', 12345)), base_url=ORIGIN) as denied_agent):
                browser.headers['Cf-Access-Jwt-Assertion'] = assertion
                assert (await browser.get(BASE + '/auth/login')).status_code == 303
                human = checked(await browser.get(BASE + '/auth/me'))
                assert human['authenticated'] and human['actor'].startswith('human:cf:')
                browser.headers.update({'Origin': ORIGIN, 'X-Hub-CSRF': human['csrf_token']})
                codex = {'Authorization': 'Bearer test-codex'}
                claude = {'Authorization': 'Bearer test-claude'}
                assert (await denied_agent.get(BASE + '/overview', headers={**codex,
                    'X-Forwarded-For': '192.0.2.10'})).status_code == 403

                # Pause admission before creating work. Later unpause must wake
                # the real native subscriber; no direct bridge/broker call.
                assert checked(await browser.post(BASE + '/work/pause', json={'paused': True}))['paused']
                task = checked(await browser.post(BASE + '/tasks', json={
                    'project': 'engineering-hub', 'title': 'HTTP queued pilot',
                    'client_request_id': 'gateway-queued-task'}), 201)
                task_id = task['id']
                task_path = BASE + '/tasks/' + task_id
                task = checked(await agents.post(task_path + '/claim', headers=codex,
                    json={'expected_revision': task['revision']}))
                assert task['owner'] == 'codex'
                task = checked(await agents.post(task_path + '/candidate', headers=codex,
                    json={'expected_revision': task['revision'], 'commit': COMMIT}))
                review = {'expected_revision': task['revision'], 'commit': COMMIT, 'verdict': 'approve'}
                assert (await agents.post(task_path + '/review', headers=codex, json=review)).status_code == 403
                assert (await agents.post(task_path + '/review', headers=claude,
                    json={**review, 'actor': 'codex'})).status_code == 422
                task = checked(await agents.post(task_path + '/review', headers=claude, json=review))
                assert task['review']['actor'] == 'claude' and task['status'] == 'verified'
                submission = {'request_id': 'gateway-queued-work', 'action_key': ACTION,
                    'environment': 'staging', 'target': {'task_id': task_id,
                    'source_commit': COMMIT, 'artifact_digest': IMAGE}}
                for injected in ('actor', 'executor'):
                    assert (await agents.post(BASE + '/work/jobs', headers=codex,
                        json={**submission, injected: 'executor:container-pilot'})).status_code == 422
                instance = {'instance_id': str(uuid.uuid4())}
                for path, payload in [('/work/claim', instance), ('/work/jobs', submission),
                                      (f'/container-pilot/runs/{uuid.uuid4()}/dispatch', {})]:
                    assert (await browser.post(BASE + path, json=payload)).status_code == 403
                assert (await browser.get(BASE + '/tasks', headers=codex)).status_code == 403
                assert (await agents.post(BASE + '/work/claim', headers=codex,
                    json={**instance, 'actor': 'executor:container-pilot'})).status_code == 422
                empty_drain.clear()
                job = checked(await agents.post(BASE + '/work/jobs', headers=codex, json=submission), 201)
                await asyncio.wait_for(empty_drain.wait(), 5)
                assert checked(await browser.get(BASE + '/work/jobs/' + job['id']))['state'] == 'queued'
                assert not completed.is_set() and rig.client.submissions == []
                assert checked(await agents.post(BASE + '/work/claim', headers=codex,
                                               json=instance))['job'] is None
                decision = checked(await agents.post(BASE + '/decisions', headers=codex, json={
                    'project': 'engineering-hub', 'title': 'Inspect result?', 'options': ['Yes', 'No']}), 201)
                assert not checked(await browser.post(BASE + '/work/pause', json={'paused': False}))['paused']
                await asyncio.wait_for(completed.wait(), 5)
                result = checked(await browser.get(BASE + '/work/jobs/' + job['id']))
                assert result['state'] == ('failed' if failed_smoke else 'succeeded')
                runs = checked(await browser.get(BASE + '/runs'))['items']
                assert len(runs) == 1 and runs[0]['task_id'] == task_id
                assert runs[0]['status'] == ('rolled_back' if failed_smoke else 'succeeded')
                assert runs[0]['artifact_digest'] == IMAGE
                assert [command.action for command in rig.client.submissions] == (ACTIONS if failed_smoke else ACTIONS[:2])
                assert rig.client.inspections == [] and machine_job(rig) is None
                assert checked(await browser.get(task_path))['status'] == 'verified'
                answer = checked(await browser.post(BASE + '/decisions/' + decision['id'] + '/answer',
                    json={'expected_revision': decision['revision'], 'answer': 'Yes'}))
                assert answer['answered_by'] == human['actor']
    assert not runtime.status['running'] and not feed.available
    rig.owner.assert_active(rig.repo.path)  # Caller ownership was not transferred.
