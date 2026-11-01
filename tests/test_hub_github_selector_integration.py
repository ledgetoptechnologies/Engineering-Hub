"""Actual trusted selector to owned service; host custody and executor are synthetic."""
import asyncio
import hashlib
import json
import queue
import sys
import multiprocessing
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from engineering_hub import protected_layout, trusted_session_startup as selector
from engineering_hub import unified_pilot_startup as startup
from engineering_hub import unified_queued_pilot_service as owned_service
from engineering_hub.auth import GitHubIdentityProvider
from engineering_hub.container_pilot_client import ContainerPilotClient, ContainerPilotRequest
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubRepository
from tests.test_hub_container_pilot import reviewed_task
from tests.test_hub_queued_pilot import DistinctContainerClient, COMMIT, IMAGE
from tests.test_hub_trusted_queued_session import v2_document, encode, synthetic_dependencies
from tests.test_hub_unified_pilot_startup import profile_document, certificate
from tests.test_hub_unified_queued_pilot_service import url


async def stop_owned_process(process, stop_requested, *, grace=12, forced=3):
    """Stop only this test's child; bounded termination covers pre-service hangs."""
    stop_requested.set()
    await asyncio.to_thread(process.join, grace)
    forced_stop = process.is_alive()
    if forced_stop:
        process.terminate()
        await asyncio.to_thread(process.join, forced)
    if process.is_alive():
        process.kill()
        await asyncio.to_thread(process.join, forced)
    assert not process.is_alive(), 'task-owned selector process did not exit'
    return forced_stop


@pytest.mark.asyncio
async def test_actual_selector_v3_to_github_owned_queue(tmp_path, monkeypatch):
    from engineering_hub import pilot_ownership
    from engineering_hub.trusted_queued_session import SCHEMA_V3
    private = tmp_path / 'private'; private.mkdir(mode=0o700)
    shared = tmp_path / 'shared'; shared.mkdir(mode=0o700)
    bus = shared / 'agent-message-bus'; bus.mkdir(mode=0o700)
    (bus / 'files').mkdir(mode=0o700)
    from agent_message_bus.database import MessageRepository
    MessageRepository(bus / 'messages.sqlite3')
    (bus / 'audit.jsonl').touch()
    monkeypatch.setattr(protected_layout, 'ROOT', private)
    # No host mount in this fixture: only this host-custody check is simulated.
    monkeypatch.setattr(protected_layout, 'validate_existing_boundary', lambda *_a, **_k: None)
    native_fs = pilot_ownership._local_filesystem
    monkeypatch.setattr(pilot_ownership, '_local_filesystem',
                        lambda fd, **_: native_fs(fd, allow_ephemeral_test_storage=True))
    db = private / 'hub.sqlite3'
    with PilotOwner(db, allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(db, execution_principal='executor:container-pilot', pilot_owner=owner)
        task = reviewed_task(repo, 'selector-github-v3')
    config = shared / 'settings.json'
    config.write_text(json.dumps({'schema_version': 3, 'base_url': 'https://hub.example',
        'github': {'client_id': 'fixture-client', 'allowed_user_id': 123},
        'agent_allowed_hosts': ['hub.example'], 'agent_allowed_ips': ['127.0.0.1'],
        'storage': {'layout': 'protected-ledger-v1'}}))
    ca, _ = certificate('localhost'); token = b'synthetic-selector-executor'
    ca_path, token_path = private / 'staging-ca.pem', private / 'executor.token'
    ca_path.write_bytes(ca); ca_path.chmod(0o644)
    token_path.write_bytes(token); token_path.chmod(0o600)
    document = profile_document()
    document.update(schema='engineering-hub.unified-pilot.v2', profile_mode='disjoint-v3', allowed_task_ids=[task['id']])
    document['executor'].update(https_origin='https://127.0.0.1:9443', ca_file=str(ca_path),
        ca_sha256='sha256:' + hashlib.sha256(ca).hexdigest(), credential_file=str(token_path),
        credential_sha256='sha256:' + hashlib.sha256(token).hexdigest())
    profile = private / 'pilot.json'; profile.write_bytes(encode(document)); profile.chmod(0o600)
    data = v2_document(); data.update(schema=SCHEMA_V3, operator_actor='human:123')
    enrollment = private / 'manual-session.json'; enrollment.write_bytes(encode(data)); enrollment.chmod(0o600)
    for name, value in {'CODEX_TOKEN': 'synthetic-codex', 'HERMES_TOKEN': 'synthetic-hermes',
                        'CLAUDE_TOKEN': 'synthetic-claude', 'HUB_GITHUB_CLIENT_SECRET': 'synthetic-oauth-secret'}.items():
        monkeypatch.setenv(name, value)
    dependencies = synthetic_dependencies(monkeypatch)
    async def oauth(self, code, verifier):
        assert code == 'synthetic-code' and verifier
        return 123
    monkeypatch.setattr(GitHubIdentityProvider, 'exchange', oauth)
    host = DistinctContainerClient(); requests = []
    async def executor(request):
        command = ContainerPilotRequest.model_validate(json.loads(await request.aread()))
        requests.append(command)
        return httpx.Response(200, json=host.response(command).model_dump())
    def make_client(cls, origin, credential, ca_pem, **kwargs):
        assert origin == document['executor']['https_origin'] and credential.encode() == token and ca_pem == ca
        return ContainerPilotClient('http://127.0.0.1', credential, transport=httpx.MockTransport(executor))
    monkeypatch.setattr(ContainerPilotClient, 'for_verified_https', classmethod(make_client))
    original_service = owned_service.serve_owned_unified_queued_pilot
    context = multiprocessing.get_context('fork')
    ready = context.Queue(maxsize=1)
    finished = context.Queue(maxsize=1)
    stop_requested = context.Event()
    async def bounded_service(*args, **kwargs):
        stop = asyncio.Event()
        async def watch_stop():
            await asyncio.to_thread(stop_requested.wait)
            stop.set()
        watcher = asyncio.create_task(watch_stop())
        kwargs.update(agent_host='127.0.0.1', agent_port=0, browser_host='127.0.0.1', browser_port=0,
                      install_signals=False, stop_event=stop, on_ready=lambda *a: ready.put_nowait(a))
        try:
            return await original_service(*args, **kwargs)
        finally:
            stop_requested.set()
            await watcher
    monkeypatch.setattr(owned_service, 'serve_owned_unified_queued_pilot', bounded_service)
    startup.validate_configuration(config, profile)
    def run_selector():
        try:
            code = selector.main(['--config', str(config), '--pilot-config', str(profile), '--session-config', str(enrollment)])
        except SystemExit as error:
            code = error.code
        except BaseException:
            code = -1
        finished.put({'code': code, 'actions': [item.action for item in requests],
                      'observations': dependencies.events.count('observe')})
    process = context.Process(target=run_selector, name='synthetic-github-selector')
    process.start()
    try:
        async def await_ready():
            while True:
                try: return ready.get_nowait()
                except queue.Empty:
                    if not process.is_alive(): pytest.fail('selector exited before readiness')
                    await asyncio.sleep(.02)
        addresses = await asyncio.wait_for(await_ready(), 8)
        async with (httpx.AsyncClient(base_url=url(addresses[0]), trust_env=False,
                    headers={'Authorization': 'Bearer synthetic-codex', 'Host': 'hub.example'}, timeout=8) as agent,
                    httpx.AsyncClient(base_url=url(addresses[1]), trust_env=False,
                    headers={'Host': 'hub.example'}, follow_redirects=False, timeout=8) as browser):
            login = await browser.get('/api/hub/v1/auth/login'); assert login.status_code == 302
            state = parse_qs(urlsplit(login.headers['location']).query)['state'][0]
            oauth_cookie = browser.cookies.get('hub_session_oauth'); assert oauth_cookie
            callback = await browser.get('/api/hub/v1/auth/callback', params={'state': state, 'code': 'synthetic-code'},
                                         headers={'Cookie': 'hub_session_oauth=' + oauth_cookie})
            assert callback.status_code == 303
            session = browser.cookies.get('hub_session'); assert session
            browser.headers['Cookie'] = 'hub_session=' + session
            me = (await browser.get('/api/hub/v1/auth/me')).json(); assert me['actor'] == 'human:123'
            headers = {'Origin': 'https://hub.example', 'X-Hub-CSRF': me['csrf_token']}
            snapshot = await browser.get('/api/hub/v1/staging/session-reconciliation'); assert snapshot.status_code == 200
            decision = {'request_id': snapshot.json()['request_id'], 'expected_revision': snapshot.json()['revision'], 'action': 'accept'}
            assert (await browser.post('/api/hub/v1/staging/session-reconciliation', json=decision)).status_code == 403
            assert (await browser.post('/api/hub/v1/staging/session-reconciliation', headers=headers, json=decision)).status_code == 200
            job = await agent.post('/api/hub/v1/work/jobs', json={'request_id': 'selector-github-v3', 'action_key': startup.ACTION,
                'environment': 'staging', 'target': {'task_id': task['id'], 'source_commit': COMMIT, 'artifact_digest': IMAGE}})
            assert job.status_code == 201, job.text
            job_id = job.json()['id']
            async def completed():
                while True:
                    result = await agent.get('/api/hub/v1/work/jobs/' + job_id)
                    assert result.status_code == 200
                    if result.json()['state'] in {'succeeded', 'failed', 'held'}: return result.json()['state']
                    await asyncio.sleep(.01)
            assert await asyncio.wait_for(completed(), 8) == 'succeeded'
    finally:
        active_error = sys.exc_info()[1]
        try:
            forced_stop = await stop_owned_process(process, stop_requested)
            if active_error is None:
                assert not forced_stop and process.exitcode == 0
                report = finished.get(timeout=1)
                assert report['code'] == 0
                assert report['actions'] == ['deploy_container', 'smoke_container']
                assert report['observations'] >= 3
        finally:
            ready.close(); finished.close()
            ready.join_thread(); finished.join_thread()


@pytest.mark.asyncio
async def test_pre_service_hang_terminates_only_task_owned_child():
    import time
    context = multiprocessing.get_context('fork')
    stop_requested = context.Event()
    process = context.Process(target=time.sleep, args=(60,), name='synthetic-selector-stall')
    process.start()
    try:
        assert await stop_owned_process(process, stop_requested, grace=.1, forced=2)
        assert process.exitcode is not None
    finally:
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, 2)
