"""Owned unified service, protected v3 GitHub enrollment, synthetic executor evidence."""
import asyncio
import sys
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from engineering_hub.auth import AuthConfig, GitHubIdentityProvider
from engineering_hub.trusted_queued_session import SCHEMA_V3, TrustedQueuedSessionComposition, parse_enrollment
from engineering_hub.unified_pilot_startup import parse_profile, read_protected_file
from tests.test_hub_unified_pilot_startup import profile_document, encode
from tests.test_hub_trusted_queued_session import v2_document, synthetic_dependencies
from tests.test_hub_unified_queued_pilot_service import rig, options, url, until, ACTION, COMMIT, IMAGE
from engineering_hub import unified_queued_pilot_service as service


@pytest.mark.asyncio
async def test_protected_github_enrollment_owned_service_queue(rig, monkeypatch):
    from engineering_hub import trusted_queued_session as trusted
    data = v2_document(); data.update(schema=SCHEMA_V3, operator_actor='human:123')
    path = rig.settings.database.parent / 'operator-enrollment.json'
    path.write_bytes(encode(data)); path.chmod(0o600)
    enrollment = parse_enrollment(read_protected_file(path, maximum_bytes=trusted.MAX_ENROLLMENT_BYTES))
    profile_data = profile_document()
    profile_data.update(schema='engineering-hub.unified-pilot.v2', profile_mode='disjoint-v3')
    profile = parse_profile(encode(profile_data))
    assert profile.manifest.digest() == enrollment.manifest_digest
    rig.manifest = profile.manifest
    rig.settings = replace(rig.settings, staging_machine_id='staging-pc', auth_config=AuthConfig('https://hub.example',
        github_client_id='fixture-client', github_client_secret='fixture-secret', beau_github_id=123))
    dependencies = synthetic_dependencies(monkeypatch)
    async def exchange(self, code, verifier):
        assert code == 'synthetic-code' and verifier
        return 123
    monkeypatch.setattr(GitHubIdentityProvider, 'exchange', exchange)
    composition = TrustedQueuedSessionComposition(profile=profile, database=rig.settings.database,
        enrollment=enrollment, ca_pem=b'synthetic-ca', credential=b'synthetic-token')
    diagnostics = []
    original_owned = service._owned_service
    async def capture_owned(operation, stop):
        try: return await original_owned(operation, stop)
        except Exception as error:
            import traceback
            diagnostics.append(''.join(traceback.format_exception(error)))
            raise
    monkeypatch.setattr(service, '_owned_service', capture_owned)
    stop = serving = None
    try:
        stop = asyncio.Event()
        ready = asyncio.get_running_loop().create_future()
        serving = asyncio.create_task(service.serve_owned_unified_queued_pilot(rig.settings, rig.manifest,
            rig.client_factory, **options(rig, machine_id='staging-pc', stop_event=stop, on_ready=lambda *a: ready.set_result(a),
                allow_github=True, reconciliation_factory=composition.reconciliation_factory,
                session_acceptance_factory=composition.session_acceptance_factory)))
        done, _ = await asyncio.wait({ready, serving}, timeout=8, return_when=asyncio.FIRST_COMPLETED)
        if serving in done:
            try: await serving
            except Exception: pytest.fail('synthetic service startup: ' + '\n'.join(diagnostics))
        assert ready in done
        addresses = ready.result()
        async with (httpx.AsyncClient(base_url=url(addresses[0]), trust_env=False,
                    headers={'Authorization': 'Bearer test-codex', 'Host': 'hub.example'}, timeout=8) as agent,
                    httpx.AsyncClient(base_url=url(addresses[1]), trust_env=False,
                    headers={'Host': 'hub.example'}, follow_redirects=False, timeout=8) as browser):
            login = await browser.get('/api/hub/v1/auth/login')
            assert login.status_code == 302
            state = parse_qs(urlsplit(login.headers['location']).query)['state'][0]
            oauth_cookie = browser.cookies.get('hub_session_oauth')
            assert oauth_cookie
            callback = await browser.get('/api/hub/v1/auth/callback', params={'state': state, 'code': 'synthetic-code'},
                                         headers={'Cookie': 'hub_session_oauth=' + oauth_cookie})
            assert callback.status_code == 303
            cookie = browser.cookies.get('hub_session'); assert cookie
            browser.headers['Cookie'] = 'hub_session=' + cookie
            me = (await browser.get('/api/hub/v1/auth/me')).json()
            assert me['authenticated'] and me['actor'] == 'human:123'
            headers = {'Origin': 'https://hub.example', 'X-Hub-CSRF': me['csrf_token']}
            snapshot = await browser.get('/api/hub/v1/staging/session-reconciliation')
            assert snapshot.status_code == 200
            decision = {'request_id': snapshot.json()['request_id'], 'expected_revision': snapshot.json()['revision'],
                        'action': 'accept'}
            assert (await browser.post('/api/hub/v1/staging/session-reconciliation', json=decision)).status_code == 403
            assert (await browser.post('/api/hub/v1/staging/session-reconciliation', headers=headers, json=decision)).status_code == 200
            task_response = await browser.post('/api/hub/v1/tasks', headers=headers,
                json={'project': 'engineering-hub', 'title': 'Synthetic GitHub pilot', 'client_request_id': 'github-v3'})
            assert task_response.status_code == 201
            task = task_response.json(); route = '/api/hub/v1/tasks/' + task['id']
            claimed = await agent.post(route + '/claim', json={'expected_revision': task['revision']}); assert claimed.status_code == 200
            candidate = await agent.post(route + '/candidate', json={'expected_revision': claimed.json()['revision'], 'commit': COMMIT}); assert candidate.status_code == 200
            reviewed = await agent.post(route + '/review', headers={'Authorization': 'Bearer test-claude'},
                json={'expected_revision': candidate.json()['revision'], 'commit': COMMIT, 'verdict': 'approve'})
            assert reviewed.status_code == 200
            job = await agent.post('/api/hub/v1/work/jobs', json={'request_id': 'github-v3', 'action_key': ACTION,
                'environment': 'staging', 'target': {'task_id': task['id'], 'source_commit': COMMIT, 'artifact_digest': IMAGE}})
            assert job.status_code == 201
            job_id = job.json()['id']
            await until(lambda: rig.core.state.work_queue.get_job(job_id)['state'] in {'succeeded', 'failed', 'held'})
            assert rig.core.state.work_queue.get_job(job_id)['state'] == 'succeeded'
            assert [command.action for command in rig.requests] == ['deploy_container', 'smoke_container']
            assert dependencies.events.count('observe') >= 3
    finally:
        try:
            if stop is not None: stop.set()
            if serving is not None:
                active_error = sys.exc_info()[1]
                outcome = await asyncio.wait_for(asyncio.gather(serving, return_exceptions=True), 10)
                if active_error is None and isinstance(outcome[0], BaseException):
                    raise outcome[0]
        finally:
            composition.close()
