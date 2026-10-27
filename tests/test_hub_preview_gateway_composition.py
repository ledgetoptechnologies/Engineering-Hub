"""Real capability/session/resource broker through both in-process gateways.

No sockets, worker processes, external identities or deployment acceptance.
"""
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

from engineering_hub.app import create_app
from engineering_hub.cloudflare_access import CloudflareIdentity
from engineering_hub.frontends import HubFrontends
from engineering_hub.private_gateway import PrivateBrowserGateway, PublicBrowserGateway, browser_route_allowed
from engineering_hub.staging_module import StagingModule
from engineering_hub.staging_preview import PreviewBroker
from engineering_hub.staging_resources import StagingResourceStore
from tests.test_hub_frontends import Feed
from tests.test_modular_staging_composition import settings

HUB = 'https://hub.example'
PREVIEW = 'https://preview.example'
ACTOR = 'human:cf:' + 'a' * 64
pytestmark = pytest.mark.asyncio


async def request(app, path, *, host='preview.example', method='GET', headers=(), body=b'', query=b'', raw=None):
    sent = False
    messages = []
    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        await asyncio.Future()
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': method, 'scheme': 'https', 'path': path, 'raw_path': path.encode() if raw is None else raw,
             'query_string': query, 'headers': [(b'host', host.encode()), *headers],
             'server': ('preview.example', 443), 'client': ('127.0.0.1', 1), 'root_path': ''}
    await app(scope, receive, lambda message: _append(messages, message))
    return SimpleNamespace(status=messages[0]['status'], headers=dict(messages[0]['headers']),
        body=b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body'))


async def _append(messages, message):
    messages.append(message)


@asynccontextmanager
async def composed(tmp_path, monkeypatch, *, public_enabled=True, private_enabled=True):
    monkeypatch.setattr('engineering_hub.staging_module.compose_staging',
                        lambda *a, **k: StagingModule(status={'state': 'unconfigured'}))
    config = settings(tmp_path, access=True)
    core = create_app(config, event_feed=Feed())
    now = [1800000000.0]
    sessions = core.state.sessions
    sessions.clock = lambda: now[0]
    identity = CloudflareIdentity('fixture', 'operator@example.com', ACTOR, int(now[0]) + 600)
    session = sessions.create_cloudflare_session(config.auth_config, identity)
    digest, expiry = sessions.preview_session_binding(ACTOR, session['cookie_token'])
    repo, control = core.state.repository, object()
    resources = StagingResourceStore(repo, admin_actors=frozenset({ACTOR}),
        control_principal=control, clock=lambda: now[0])
    grants = {ACTOR: ['read', 'preview', 'cleanup', 'retain']}
    resources.set_policy(ACTOR, 'engineering-hub', 0, grants)
    job = resources.bind_job(control, str(uuid4()), project='engineering-hub',
        created_by='codex', source_commit='a' * 40, artifact_digest='sha256:' + 'b' * 64)
    item = resources.register_resource(control, job['id'], 'preview')
    resources.mark_resource_ready(control, item['id'], item['revision'],
        preview_path='/staging/previews/fixture/', size_bytes=100)
    resources.update_job(control, job['id'], job['revision'], terminal=True)
    broker = PreviewBroker(repo, resources, hub_origin=HUB, preview_origin=PREVIEW,
        session_alive=sessions.preview_session_alive, clock=lambda: now[0])
    state = SimpleNamespace(now=now, sessions=sessions, cookie=session['cookie_token'],
        resources=resources, repo=repo, control=control, item=item['id'], grants=grants,
        reads=[], upstream=[], private=[], content=b'<html>preview</html>', during=None)
    async def fetch(resource_id, relative):
        state.reads.append((resource_id, relative))
        if state.during:
            state.during()
        return state.content, 'text/html'
    broker_app = broker.app(fetch)
    async def observed(scope, receive, send):
        state.private.append(list(scope['headers']))
        async def decorated(message):
            if message['type'] == 'http.response.start':
                message = dict(message, headers=[*message['headers'], *getattr(state, 'extra_headers', ())])
            await send(message)
        await broker_app(scope, receive, decorated)
    core.state.preview_app, core.state.preview_origin = observed, PREVIEW
    pair = HubFrontends(core)
    private = PrivateBrowserGateway(pair.browser, agent_auth=config.agent_auth,
        cloudflare_config=config.auth_config.cloudflare_config,
        preview_origin=PREVIEW if private_enabled else None,
        preview_app=pair.browser if private_enabled else None)
    async def upstream(scope, receive, send):
        state.upstream.append(list(scope['headers']))
        await private(scope, receive, send)
    async with pair.lifespan(), httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream)) as client:
        state.public = PublicBrowserGateway(client, preview_origin=PREVIEW if public_enabled else None)
        state.private_gateway = private
        state.path = urlsplit(broker.issue(ACTOR, item['id'], digest, expiry)['preview_url']).path
        yield state


async def test_real_broker_crosses_both_gateways_with_get_head_and_scrubbed_credentials(tmp_path, monkeypatch):
    async with composed(tmp_path, monkeypatch) as state:
        assert browser_route_allowed('GET', state.path) is False
        headers = [(b'cookie', b'hub_session=secret'), (b'cf-access-jwt-assertion', b'access-secret'),
                   (b'authorization', b'Bearer agent-secret'), (b'x-hub-csrf', b'csrf-secret'),
                   (b'x-hub-agent', b'agent-secret'), (b'origin', b'https://hub.example')]
        for method in ('GET', 'HEAD'):
            response = await request(state.public, state.path, method=method, headers=headers)
            assert response.status == 200
            assert response.body == (state.content if method == 'GET' else b'')
            assert response.headers[b'content-length'] == str(len(state.content)).encode()
            assert response.headers[b'access-control-allow-origin'] == b'*'
            assert response.headers[b'content-security-policy'].startswith(b'sandbox allow-scripts;')
            assert b'frame-ancestors https://hub.example' in response.headers[b'content-security-policy']
            assert b'set-cookie' not in response.headers and b'x-hub-csrf' not in response.headers
        for received in state.upstream + state.private:
            assert all(k in {b'host', b'accept', b'accept-encoding'} for k, _ in received)
            assert not any(b'secret' in v for _, v in received)
        assert state.public._active == 0
        # The receiver independently strips even if called without public proxy.
        assert (await request(state.private_gateway, state.path, headers=headers)).status == 200
        assert all(k in {b'host', b'accept'} for k, _ in state.private[-1])


@pytest.mark.parametrize('public_enabled,private_enabled', [(False, True), (True, False), (False, False)])
async def test_each_gateway_defaults_closed_without_explicit_binding(tmp_path, monkeypatch, public_enabled, private_enabled):
    async with composed(tmp_path, monkeypatch, public_enabled=public_enabled, private_enabled=private_enabled) as state:
        assert (await request(state.public, state.path)).status == 403
        assert state.reads == []


@pytest.mark.parametrize('case', ['dashboard', 'forwarded', 'host-suffix', 'duplicate-host', 'query',
    'encoded', 'double-slash', 'dot', 'post', 'body', 'length', 'duplicate-header', 'hub-api'])
async def test_preview_path_is_exact_and_separate_from_dashboard(tmp_path, monkeypatch, case):
    async with composed(tmp_path, monkeypatch) as state:
        args = {}
        path = state.path
        if case == 'dashboard': args['host'] = 'hub.example'
        elif case == 'forwarded': args.update(host='hub.example', headers=[(b'x-forwarded-host', b'preview.example')])
        elif case == 'host-suffix': args['host'] = 'preview.example.evil'
        elif case == 'duplicate-host': args['headers'] = [(b'host', b'preview.example')]
        elif case == 'query': args['query'] = b'x=1'
        elif case == 'encoded': args['raw'] = path.replace('index', '%69ndex').encode()
        elif case == 'double-slash': path = path.replace('/index', '//index')
        elif case == 'dot': path = path.replace('/index', '/./index')
        elif case == 'post': args['method'] = 'POST'
        elif case == 'body': args['body'] = b'x'
        elif case == 'length': args['headers'] = [(b'content-length', b'1')]
        elif case == 'duplicate-header': args['headers'] = [(b'cookie', b'a'), (b'cookie', b'b')]
        elif case == 'hub-api': path = '/api/hub/v1/overview'
        assert (await request(state.public, path, **args)).status in {400, 403, 413}
        assert state.reads == []
        assert state.public._active == 0
        # Direct private hop has the same validation rather than trusting public.
        assert (await request(state.private_gateway, path, **args)).status in {400, 403, 413}


@pytest.mark.parametrize('change', ['logout', 'expiry', 'policy', 'resource'])
@pytest.mark.parametrize('during', [False, True])
async def test_real_store_revocation_before_and_during_reader_is_not_cached(tmp_path, monkeypatch, change, during):
    async with composed(tmp_path, monkeypatch) as state:
        def revoke():
            if change == 'logout': state.sessions.delete_cloudflare_session(state.cookie)
            elif change == 'expiry': state.now[0] += 301
            elif change == 'policy': state.resources.set_policy(ACTOR, 'engineering-hub', 1, {ACTOR: ['read']})
            else:
                with state.repo._transaction() as db:
                    row = db.execute('SELECT data FROM hub_entities WHERE id=?', (state.item,)).fetchone()
                    item = json.loads(row['data']); item['revision'] += 1
                    db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(item), state.item))
        if during: state.during = revoke
        else: revoke()
        response = await request(state.public, state.path)
        unknown = await request(state.public, '/p/' + 'a' * 43 + '/index.html')
        assert response.status == unknown.status == 404
        assert response.body == unknown.body == b'{"detail":{"code":"preview_unavailable"}}'
        assert state.content not in response.body
        assert len(state.reads) == (1 if during else 0)
        assert state.public._active == 0


async def test_preview_8mib_limit_does_not_expand_general_response_limit(tmp_path, monkeypatch):
    async with composed(tmp_path, monkeypatch) as state:
        state.content = b'x' * (3 * 1024 * 1024)
        response = await request(state.public, state.path)
        assert response.status == 200 and response.body == state.content
        state.content = b'x' * (8 * 1024 * 1024 + 1)
        assert (await request(state.public, state.path)).status == 404
        assert state.public.max_response_bytes == 2097152


async def test_dashboard_auth_stays_required_and_preview_cannot_satisfy_it(tmp_path, monkeypatch):
    async with composed(tmp_path, monkeypatch) as state:
        assert (await request(state.public, '/api/hub/v1/overview', host='hub.example')).status == 401
        assert (await request(state.public, '/api/hub/v1/overview')).status == 403
        assert (await request(state.public, '/mcp', host='hub.example')).status == 403
        assert state.reads == []


async def test_private_and_public_hops_cannot_set_preview_cookies_or_csrf(tmp_path, monkeypatch):
    async with composed(tmp_path, monkeypatch) as state:
        state.extra_headers = [(b'set-cookie', b'hub_session=bad'), (b'x-hub-csrf', b'bad'),
                               (b'cf-access-jwt-assertion', b'bad')]
        for app in (state.private_gateway, state.public):
            response = await request(app, state.path)
            assert response.status == 200
            assert not any(k in response.headers for k in (b'set-cookie', b'x-hub-csrf', b'cf-access-jwt-assertion'))


async def test_public_preview_hop_is_single_attempt_and_closes_owned_response():
    calls = []
    closed = []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'asset'
        async def aclose(self):
            closed.append(True)
    async def upstream(req):
        calls.append(req)
        assert not any(h in req.headers for h in ('cookie', 'authorization', 'cf-access-jwt-assertion'))
        return httpx.Response(200, headers={'content-type': 'text/plain', 'set-cookie': 'bad=1'}, stream=Body())
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream),
            headers={'Authorization': 'Bearer default-secret'}, cookies={'hub': 'default-secret'}) as client:
        gateway = PublicBrowserGateway(client, preview_origin=PREVIEW)
        response = await request(gateway, '/p/' + 'a' * 43 + '/file.txt')
        assert response.status == 200 and response.body == b'asset' and b'set-cookie' not in response.headers
        assert len(calls) == len(closed) == 1 and gateway._active == 0


async def test_public_preview_transport_failure_has_no_retry_or_secret_url_echo():
    calls = []
    async def upstream(req):
        calls.append(req)
        raise httpx.ConnectError('SECRET ' + str(req.url), request=req)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        gateway = PublicBrowserGateway(client, preview_origin=PREVIEW)
        response = await request(gateway, '/p/' + 'a' * 43 + '/index.html')
        assert response.status == 502 and b'SECRET' not in response.body
        assert len(calls) == 1 and gateway._active == 0
