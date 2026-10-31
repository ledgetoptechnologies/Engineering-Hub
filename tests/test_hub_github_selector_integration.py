"""Actual trusted selector to owned service; host custody and executor are synthetic."""
import asyncio
import hashlib
import json
import queue
import sys
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
    original_owned = owned_service._owned_service
    failures = []
    async def capture_owned(operation, stop):
        try: return await original_owned(operation, stop)
        except Exception as error:
            import traceback
            failures.append(''.join(traceback.format_exception(error)))
            raise
    monkeypatch.setattr(owned_service, '_owned_service', capture_owned)
    ready = queue.Queue(maxsize=1); control = {}
    async def bounded_service(*args, **kwargs):
        control['loop'] = asyncio.get_running_loop(); control['stop'] = asyncio.Event()
        kwargs.update(agent_host='127.0.0.1', agent_port=0, browser_host='127.0.0.1', browser_port=0,
                      install_signals=False, stop_event=control['stop'], on_ready=lambda *a: ready.put_nowait(a))
        return await original_service(*args, **kwargs)
    monkeypatch.setattr(owned_service, 'serve_owned_unified_queued_pilot', bounded_service)
    startup.validate_configuration(config, profile)
    def run_selector():
        try:
            return selector.main(['--config', str(config), '--pilot-config', str(profile), '--session-config', str(enrollment)])
        except SystemExit as error:
            return error.code
    selector_task = asyncio.create_task(asyncio.to_thread(run_selector))
    try:
        async def await_ready():
            while ready.empty():
                if selector_task.done(): pytest.fail('selector exited early: ' + str(selector_task.result()) + ' '.join(failures))
                await asyncio.sleep(.02)
            return ready.get_nowait()
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
            assert [item.action for item in requests] == ['deploy_container', 'smoke_container']
            assert dependencies.events.count('observe') >= 3
    finally:
        if 'loop' in control and not control['loop'].is_closed(): control['loop'].call_soon_threadsafe(control['stop'].set)
        result = await asyncio.wait_for(asyncio.shield(selector_task), 12)
        if sys.exc_info()[1] is None: assert result == 0
