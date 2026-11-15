"""Synthetic local Hub app/session -> real TLS worker handler integration.

No Docker, staging credentials, external identity provider or deployed host.
"""
import asyncio
from datetime import datetime, timezone
import ipaddress
import json
import os
import threading

import httpx
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from hub_staging_service.__main__ import BoundedServer, handler
from hub_staging_service.core import Service
from tests.test_hub_staging_wire_integration import private_tls
from tests.test_staging_service_core import Backend, profile

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='private POSIX credential and worker fixture')


@pytest.mark.asyncio
async def test_real_hub_session_and_opt_in_observer_revalidate_over_real_tls(tmp_path, monkeypatch):
    import hub_staging_service.runtime_inventory as runtime

    sentinel = 'synthetic-private-container-name'
    now = datetime.now(timezone.utc).isoformat()
    snapshot = {'schema':'hub-staging-runtime.v1', 'observed_at':now,
                'sources':[{'id':'worker', 'state':'observed', 'observed_at':now}],
                'containers':[{'id':'a' * 64, 'name':sentinel, 'image':'synthetic/image:1',
                               'state':'running', 'scope':'worker', 'managed':False}],
                'services':[]}
    entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
    release.set()
    worker_calls, paths, returned = [], [], [snapshot]
    def collect(*args):
        with lock: worker_calls.append('collect')
        entered.set()
        if not release.wait(8): raise RuntimeError('synthetic collector release timed out')
        return returned[0]
    monkeypatch.setattr(runtime, 'collect_runtime', collect)

    worker_root = tmp_path / 'worker'
    service = Service(worker_root, {'demo': profile()}, Backend())
    tls_ca, context = private_tls(tmp_path)
    worker_token, scoped_token, observer_token = 'synthetic-worker-token', 'synthetic-scoped-token', 'synthetic-observer-token'
    entries = (('alice', scoped_token, frozenset({'demo'}), {'demo':frozenset({'default'})}),)
    base_handler = handler(service, worker_token, {ipaddress.ip_address('127.0.0.1')},
                           v2_credentials=entries, observer_token=observer_token)
    class Observed(base_handler):
        def do_GET(self):
            with lock: paths.append(('GET', self.path))
            return super().do_GET()
        def do_POST(self):
            with lock: paths.append(('POST', self.path))
            return super().do_POST()

    server = BoundedServer(('127.0.0.1', 0), Observed)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':0.01})
    thread.start()
    origin = 'https://127.0.0.1:' + str(server.server_port)
    token = tmp_path / 'observer-token'
    token.write_text(observer_token); token.chmod(0o600)
    invalid_token = tmp_path / 'invalid-token'
    invalid_token.write_text('synthetic-unknown-token'); invalid_token.chmod(0o600)

    def build_app(name, credential, admins, *, ca=tls_ca):
        root = tmp_path / name
        root.mkdir(mode=0o700)
        (root / 'staging-observer.json').write_text(json.dumps({'schema':1, 'enabled':True,
            'origin':origin, 'credential_file':str(credential), 'ca_file':str(ca),
            'admin_actors':admins, 'operator_v2':True}))
        return create_app(Settings(root / 'hub.sqlite3',
            AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude'),
            AuthConfig('https://hub.test', beau_github_id=1)))

    try:
        # The real receiver denies task, legacy, wrong and duplicated tokens,
        # and denies observer mutations before calling the inventory collector.
        async with httpx.AsyncClient(verify=str(tls_ca), trust_env=False) as worker:
            for credential, expected in ((scoped_token, 503), (worker_token, 404), ('synthetic-wrong-token', 401)):
                result = await worker.get(origin + '/v2/operator/runtime', headers={'Authorization':'Bearer ' + credential})
                assert result.status_code == expected and sentinel not in result.text
            duplicate = await worker.get(origin + '/v2/operator/runtime', headers=[
                ('Authorization','Bearer ' + observer_token), ('Authorization','Bearer ' + observer_token)])
            assert duplicate.status_code == 401
            for method, path in [('POST','/v2/operator/runtime'), ('GET','/v2/operator/runtime?x=1'),
                                 ('GET','/v1/runtime'), ('POST','/v2/jobs')]:
                result = await worker.request(method, origin + path, headers={'Authorization':'Bearer ' + observer_token})
                assert result.status_code != 200
            assert worker_calls == []

        # Actual create_app reads opt-in configuration from disk and the real
        # SessionStore validates the session before and after the TLS await.
        app = build_app('approved', token, ['human:1'])
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://hub.test') as hub:
                route = '/api/hub/v1/staging/runtime'
                # This is the configured, valid synthetic Codex agent token,
                # not merely an invalid-bearer check.
                assert (await hub.get(route, headers={'Authorization':'Bearer synthetic-codex'})).status_code == 403
                assert worker_calls == []
                denied = app.state.sessions.create_session(2)
                assert (await hub.get(route, cookies={'hub_session':denied['cookie_token']})).status_code == 403
                assert worker_calls == []
                revoked = app.state.sessions.create_session(1)
                entered.clear(); release.clear()
                pending = asyncio.create_task(hub.get(route, cookies={'hub_session':revoked['cookie_token']}))
                assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
                app.state.sessions.delete_session(revoked['cookie_token'])
                release.set()
                response = await asyncio.wait_for(pending, 5)
                assert response.status_code == 401 and sentinel not in response.text and 'snapshot' not in response.text
                valid = app.state.sessions.create_session(1)
                response = await hub.get(route, cookies={'hub_session':valid['cookie_token']})
                assert response.status_code == 200 and response.json()['state'] == 'ready'
                assert response.json()['snapshot'] == snapshot and sentinel in response.text

        # A signed-in human outside the observer's configured admin set never
        # triggers a worker read; no project credential becomes observer scope.
        before = len(worker_calls)
        denied_app = build_app('denied', token, ['human:2'])
        async with denied_app.router.lifespan_context(denied_app):
            session = denied_app.state.sessions.create_session(1)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=denied_app), base_url='https://hub.test') as hub:
                response = await hub.get('/api/hub/v1/staging/runtime', cookies={'hub_session':session['cookie_token']})
                assert response.status_code == 403 and sentinel not in response.text
        assert len(worker_calls) == before

        # Wrong private CA must fail before any receiver HTTP route or collector
        # is reached; no fallback to an unverified channel or v1 route.
        other_ca_root = tmp_path / 'other-ca'
        other_ca_root.mkdir(mode=0o700)
        wrong_ca, _ = private_tls(other_ca_root)
        wrong_ca_app = build_app('wrong-ca', token, ['human:1'], ca=wrong_ca)
        async with wrong_ca_app.router.lifespan_context(wrong_ca_app):
            session = wrong_ca_app.state.sessions.create_session(1)
            with lock: paths.clear()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrong_ca_app), base_url='https://hub.test') as hub:
                response = await hub.get('/api/hub/v1/staging/runtime', cookies={'hub_session':session['cookie_token']})
                assert response.status_code == 200 and response.json() == {'configured':True, 'state':'unavailable', 'snapshot':None}
            with lock: assert paths == []
        assert len(worker_calls) == before

        # An invalid opt-in token produces unavailable, with no v1 fallback.
        invalid_app = build_app('invalid', invalid_token, ['human:1'])
        async with invalid_app.router.lifespan_context(invalid_app):
            session = invalid_app.state.sessions.create_session(1)
            with lock: paths.clear()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=invalid_app), base_url='https://hub.test') as hub:
                response = await hub.get('/api/hub/v1/staging/runtime', cookies={'hub_session':session['cookie_token']})
                assert response.status_code == 200 and response.json() == {'configured':True, 'state':'unavailable', 'snapshot':None}
            with lock: assert paths == [('GET', '/v2/operator/runtime')]
        assert len(worker_calls) == before

        # A receiver body larger than the client's 128 KiB bound is unavailable
        # and cannot cause an alternate route or partial snapshot to be shown.
        returned[0] = {'synthetic_oversized_body': 'x' * (128 * 1024 + 1)}
        oversized_app = build_app('oversized', token, ['human:1'])
        async with oversized_app.router.lifespan_context(oversized_app):
            session = oversized_app.state.sessions.create_session(1)
            with lock: paths.clear()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=oversized_app), base_url='https://hub.test') as hub:
                response = await hub.get('/api/hub/v1/staging/runtime', cookies={'hub_session':session['cookie_token']})
                assert response.status_code == 200 and response.json() == {'configured':True, 'state':'unavailable', 'snapshot':None}
            with lock: assert paths == [('GET', '/v2/operator/runtime')]
        assert len(worker_calls) == before + 1
    finally:
        release.set()
        server.shutdown(); server.server_close(); thread.join(5); service.close()
        assert not thread.is_alive()
