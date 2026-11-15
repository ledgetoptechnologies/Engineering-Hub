"""Synthetic TLS operator observer checks; no deployed credentials or host."""
import ipaddress
import os
import threading
from datetime import datetime, timezone

import httpx
import pytest

from engineering_hub.staging_client import StagingConnectionConfig
from engineering_hub.staging_observer import StagingObserver
from hub_staging_service.__main__ import BoundedServer, handler, observer_credential
from hub_staging_service.core import Rejected
from tests.test_hub_staging_wire_integration import private_tls

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='private POSIX fixture')


class Stub:
    backend = object()
    projects = {'demo': {'static': {}}}
    active = None
    def legacy_active(self, **kwargs):
        return None
    def legacy_runtime_allowed(self):
        return True


def test_credential_is_explicit_and_distinct(tmp_path):
    token = tmp_path / 'token'
    token.write_text('synthetic-observer')
    token.chmod(0o600)
    projects = (('alice', 'synthetic-project', frozenset({'demo'}), {}),)
    assert observer_credential({'token_file': str(token)}, 'synthetic-legacy', projects) == 'synthetic-observer'
    for config in ({}, {'token_file': str(token), 'extra': True}, {'token_file': None}):
        with pytest.raises(Rejected):
            observer_credential(config, 'synthetic-legacy', projects)
    for duplicate in ('synthetic-legacy', 'synthetic-project'):
        token.write_text(duplicate)
        with pytest.raises(Rejected):
            observer_credential({'token_file': str(token)}, 'synthetic-legacy', projects)


@pytest.mark.asyncio
async def test_tls_observer_exact_route_and_role_boundaries(tmp_path, monkeypatch):
    import hub_staging_service.runtime_inventory as runtime
    calls = []
    now = datetime.now(timezone.utc).isoformat()
    snapshot = {'schema':'hub-staging-runtime.v1', 'observed_at':now,
                'sources':[{'id':'worker','state':'not_configured','observed_at':None}],
                'containers':[], 'services':[]}
    monkeypatch.setattr(runtime, 'collect_runtime', lambda *args: calls.append(args) or snapshot)
    ca, context = private_tls(tmp_path)
    entries = (('alice', 'synthetic-project', frozenset({'demo'}), {'demo':frozenset({'static'})}),)
    server = BoundedServer(('127.0.0.1', 0), handler(Stub(), 'synthetic-legacy',
        {ipaddress.ip_address('127.0.0.1')}, v2_credentials=entries, observer_token='synthetic-observer'))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':0.01})
    thread.start()
    origin = 'https://127.0.0.1:' + str(server.server_port)
    try:
        async with httpx.AsyncClient(verify=str(ca), trust_env=False) as client:
            auth = lambda value: {'Authorization':'Bearer ' + value}
            for credential in ('synthetic-project', 'synthetic-legacy', 'incorrect'):
                response = await client.get(origin + '/v2/operator/runtime', headers=auth(credential))
                assert response.status_code != 200
            assert calls == []
            response = await client.get(origin + '/v2/operator/runtime', headers=[
                ('Authorization','Bearer synthetic-observer'), ('Authorization','Bearer synthetic-observer')])
            assert response.status_code == 401 and calls == []
            for method, path in [('POST','/v2/operator/runtime'), ('GET','/v2/operator/runtime?x=1'),
                                 ('GET','/v2/operator/runtime/'), ('GET','/v2/operator/%72untime'),
                                 ('GET','/v1/runtime'), ('GET','/v2/status'), ('POST','/v2/jobs')]:
                response = await client.request(method, origin + path, headers=auth('synthetic-observer'))
                assert response.status_code != 200
            assert calls == []
            response = await client.get(origin + '/v2/operator/runtime', headers=auth('synthetic-observer'))
            assert response.status_code == 200 and response.json() == snapshot and len(calls) == 1
            assert response.headers['cache-control'] == 'no-store'
        token = tmp_path / 'observer-token'
        token.write_text('synthetic-observer'); token.chmod(0o600)
        config = StagingConnectionConfig(origin=origin, credential_file=token, ca_file=ca,
                                         admin_actors=frozenset({'human:1'}), enabled=True)
        observer = StagingObserver(config, operator_v2=True)
        try:
            from engineering_hub.repository import HubError
            with pytest.raises(HubError):
                await observer.runtime('human:2')
            assert len(calls) == 1
            assert (await observer.runtime('human:1'))['state'] == 'ready'
            assert len(calls) == 2
        finally:
            await observer.close()
    finally:
        server.shutdown(); server.server_close(); thread.join()


@pytest.mark.asyncio
async def test_observer_mode_has_no_legacy_fallback(tmp_path):
    token = tmp_path / 'token'
    token.write_text('synthetic-observer'); token.chmod(0o600)
    paths = []
    def deny(request):
        paths.append(request.url.path)
        return httpx.Response(404, json={'error':'not_found'})
    config = StagingConnectionConfig(origin='http://127.0.0.1', credential_file=token,
                                     admin_actors=frozenset({'human:1'}), enabled=True)
    observer = StagingObserver(config, operator_v2=True, test_transport=httpx.MockTransport(deny))
    try:
        assert (await observer.runtime('human:1'))['state'] == 'unavailable'
        assert paths == ['/v2/operator/runtime']
    finally:
        await observer.close()


def test_observer_config_opt_in_is_strict(tmp_path):
    import json
    from engineering_hub.staging_observer import compose_observer
    path = tmp_path / 'config.json'
    data = {'schema': 1, 'enabled': True, 'origin': 'https://127.0.0.1',
            'credential_file': str(tmp_path / 'token'), 'ca_file': str(tmp_path / 'ca'),
            'admin_actors': ['human:1'], 'operator_v2': 'true'}
    path.write_text(json.dumps(data))
    assert compose_observer(path) is False



@pytest.mark.asyncio
async def test_observer_config_opt_in_composes_and_closes(tmp_path):
    import json
    from engineering_hub.staging_observer import compose_observer
    ca, _ = private_tls(tmp_path)
    token = tmp_path / 'token'
    token.write_text('synthetic-observer'); token.chmod(0o600)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'schema': 1, 'enabled': True, 'origin': 'https://127.0.0.1',
        'credential_file': str(token), 'ca_file': str(ca),
        'admin_actors': ['human:1'], 'operator_v2': True}))
    observer = compose_observer(path)
    assert observer is not False and observer is not None and observer.operator_v2 is True
    await observer.close()


@pytest.mark.asyncio
async def test_hub_http_denies_agent_and_rechecks_revoked_session_after_fetch(tmp_path):
    import asyncio
    from fastapi import FastAPI
    from engineering_hub.auth import AuthConfig, SessionStore, require_human
    from engineering_hub.staging_api import attach_staging_routes
    entered, release = asyncio.Event(), asyncio.Event()
    sentinel = 'synthetic-private-container-name'
    now = datetime.now(timezone.utc).isoformat()
    snapshot = {'schema':'hub-staging-runtime.v1', 'observed_at':now,
                'sources':[{'id':'worker','state':'observed','observed_at':now}],
                'containers':[{'id':'a' * 64, 'name':sentinel, 'image':'synthetic/image:1',
                               'state':'running', 'scope':'worker', 'managed':False}],
                'services':[]}
    class DelayedObserver:
        admin_actors = frozenset({'human:1'})
        async def runtime(self, actor):
            entered.set()
            await release.wait()
            return {'configured': True, 'state': 'ready', 'snapshot': snapshot}
    config = AuthConfig('https://hub.test', beau_github_id=1)
    store = SessionStore(tmp_path / 'auth.sqlite3')
    human = lambda request, mutation=False: require_human(request, config, store, mutation=mutation)
    app = FastAPI()
    attach_staging_routes(app, None, human, human, observer=DelayedObserver())
    session = store.create_session(1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://hub.test') as client:
        path = '/api/hub/v1/staging/runtime'
        response = await client.get(path, headers={'Authorization':'Bearer synthetic-agent'})
        assert response.status_code == 401 and not entered.is_set()
        pending = asyncio.create_task(client.get(path, cookies={'hub_session':session['cookie_token']}))
        await asyncio.wait_for(entered.wait(), 2)
        store.delete_session(session['cookie_token'])
        release.set()
        response = await asyncio.wait_for(pending, 2)
        assert response.status_code == 401 and 'snapshot' not in response.text and sentinel not in response.text
        valid = store.create_session(1)
        response = await client.get(path, cookies={'hub_session':valid['cookie_token']})
        assert response.status_code == 200 and response.json()['snapshot'] == snapshot
        assert sentinel in response.text


@pytest.mark.asyncio
async def test_operator_snapshot_limit_has_no_fallback(tmp_path):
    token = tmp_path / 'token'
    token.write_text('synthetic-observer'); token.chmod(0o600)
    paths = []
    def huge(request):
        paths.append(request.url.path)
        return httpx.Response(200, content=b'x' * (128 * 1024 + 1), headers={'content-type':'application/json'})
    config = StagingConnectionConfig(origin='http://127.0.0.1', credential_file=token,
                                     admin_actors=frozenset({'human:1'}), enabled=True)
    observer = StagingObserver(config, operator_v2=True, test_transport=httpx.MockTransport(huge))
    try:
        assert (await observer.runtime('human:1'))['state'] == 'unavailable'
        assert paths == ['/v2/operator/runtime']
    finally:
        await observer.close()
