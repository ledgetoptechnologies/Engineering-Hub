"""In-process browser/private-owner boundary; no deployed socket or live identity."""
import asyncio
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub import cloudflare_access as access
from engineering_hub.private_gateway import PublicBrowserGateway, PrivateBrowserGateway


BASE = '/api/hub/v1'
pytestmark = pytest.mark.asyncio


async def exchange(app, path='/', *, method='GET', headers=(), body=b'', query=b'', chunks=None):
    messages = []
    sent = False
    pending = list(chunks) if chunks is not None else None
    async def receive():
        nonlocal sent
        if pending:
            chunk = pending.pop(0)
            sent = True
            return {'type': 'http.request', 'body': chunk, 'more_body': bool(pending)}
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        await asyncio.Future()
    async def send(message):
        messages.append(message)
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': method, 'scheme': 'https', 'path': path, 'raw_path': path.encode(),
             'query_string': query, 'headers': [(b'host', b'hub.example'), *headers],
             'server': ('hub.example', 443), 'client': ('127.0.0.1', 12345), 'root_path': ''}
    await app(scope, receive, send)
    return messages


def status(messages):
    return messages[0]['status']


@pytest.mark.parametrize('method,path', [
    ('POST', BASE + '/work/claim'), ('POST', BASE + '/work/jobs'),
    ('POST', BASE + '/work/jobs/x/complete'), ('POST', BASE + '/tasks/x/claim'),
    ('POST', BASE + '/tasks/x/review'), ('POST', BASE + '/runs'),
    ('POST', BASE + '/runs/x/dispatch'), ('POST', BASE + '/container-pilot/runs'),
    ('GET', '/mcp'), ('POST', '/mcp'), ('GET', '/health'),
    ('GET', BASE + '/auth/callback'), ('PUT', BASE + '/tasks'),
    ('GET', BASE + '/tasks/../work/claim'), ('GET', BASE + '//tasks'),
    ('GET', BASE + '/tasks%2fx'), ('GET', '/unknown'),
])
async def test_public_closed_route_table_never_calls_owner(method, path):
    async def run():
        calls = []
        async def upstream(request):
            calls.append(request)
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            gateway = PublicBrowserGateway(client)
            response = await exchange(gateway, path, method=method)
            assert status(response) in {400, 403}
            assert calls == []
    await run()


async def test_constructor_inert_and_original_credentials_forward_without_client_defaults():
    async def run():
        calls = []
        async def upstream(request):
            calls.append(request)
            assert str(request.url) == 'http://hub-private.invalid' + BASE + '/tasks?limit=2'
            assert request.headers['host'] == 'hub.example'
            assert request.headers['origin'] == 'https://hub.example'
            assert request.headers['cf-access-jwt-assertion'] == 'synthetic-jwt'
            assert request.headers['cookie'] == 'hub_session=original'
            assert request.headers['x-hub-csrf'] == 'original-csrf'
            assert 'authorization' not in request.headers
            assert 'x-default-secret' not in request.headers
            return httpx.Response(200, headers=[('set-cookie', 'a=1'), ('set-cookie', 'b=2')], content=b'ok')
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream),
                                    headers={'X-Default-Secret': 'never-forward'},
                                    cookies={'injected': 'never-forward'}, auth=('hidden', 'hidden')) as client:
            gateway = PublicBrowserGateway(client)
            assert calls == []
            result = await exchange(gateway, BASE + '/tasks', query=b'limit=2', headers=[
                (b'origin', b'https://hub.example'), (b'cf-access-jwt-assertion', b'synthetic-jwt'),
                (b'cookie', b'hub_session=original'), (b'x-hub-csrf', b'original-csrf')])
            assert status(result) == 200
            assert [v for k, v in result[0]['headers'] if k == b'set-cookie'] == [b'a=1', b'b=2']
            assert len(calls) == 1
    await run()


@pytest.mark.parametrize('headers', [
    [(b'authorization', b'Bearer agent')],
    [(b'cookie', b'a=1'), (b'cookie', b'b=2')],
    [(b'origin', b'https://hub.example'), (b'origin', b'https://evil.example')],
    [(b'x-hub-csrf', b'one'), (b'x-hub-csrf', b'two')],
    [(b'host', b'evil.example')],
])
async def test_raw_duplicate_or_agent_credentials_fail_before_upstream(headers):
    async def run():
        calls = []
        async def upstream(request):
            calls.append(request)
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            result = await exchange(PublicBrowserGateway(client), BASE + '/tasks', headers=headers)
            assert status(result) in {400, 403}
            assert calls == []
    await run()


async def test_body_and_response_bounds_and_unknown_mutation_are_not_retried():
    async def run():
        calls = []
        async def upstream(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, content=b'x' * 33)
            raise httpx.ReadError('synthetic response lost after commit')
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            gateway = PublicBrowserGateway(client, max_body_bytes=16, max_response_bytes=32)
            assert status(await exchange(gateway, BASE + '/tasks', method='POST', body=b'x' * 17)) == 413
            assert not calls
            assert status(await exchange(gateway, BASE + '/tasks')) == 502
            assert status(await exchange(gateway, BASE + '/tasks', method='POST', body=b'{}')) == 502
            assert len(calls) == 2
    await run()


async def test_redirect_is_returned_without_following():
    async def run():
        calls = []
        async def upstream(request):
            calls.append(request)
            return httpx.Response(303, headers={'location': '/'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream), follow_redirects=True) as client:
            result = await exchange(PublicBrowserGateway(client), BASE + '/auth/login')
            assert status(result) == 303
            assert len(calls) == 1
    await run()


async def test_sse_streams_first_event_before_next_event_and_cancellation_closes_owner():
    async def run():
        opened = asyncio.Event()
        next_chunk = asyncio.Event()
        closed = asyncio.Event()
        first_sent = asyncio.Event()
        calls = []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                opened.set()
                yield b'event: change\ndata: {"id":1}\n\n'
                await next_chunk.wait()
                yield b'data: second\n\n'
            async def aclose(self):
                closed.set()
        async def upstream(request):
            calls.append(request)
            if request.url.path.endswith('/events'):
                assert request.extensions['timeout']['read'] >= 45
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Stream())
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            gateway = PublicBrowserGateway(client, max_concurrency=1)
            received = False
            async def receive():
                nonlocal received
                if not received:
                    received = True
                    return {'type': 'http.request', 'body': b'', 'more_body': False}
                await asyncio.Future()
            async def send(message):
                if message['type'] == 'http.response.body' and b'"id":1' in message.get('body', b''):
                    first_sent.set()
            scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                     'method': 'GET', 'scheme': 'https', 'path': BASE + '/events',
                     'raw_path': (BASE + '/events').encode(), 'query_string': b'',
                     'headers': [(b'host', b'hub.example')], 'client': ('127.0.0.1', 1)}
            task = asyncio.create_task(gateway(scope, receive, send))
            await asyncio.wait_for(opened.wait(), 2)
            await asyncio.wait_for(first_sent.wait(), 2)
            assert not next_chunk.is_set() and len(calls) == 1
            assert status(await exchange(gateway, BASE + '/tasks')) == 503
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set()
            # Released request admission, not a lingering worker or subscription.
            next_chunk.set()
            assert status(await exchange(gateway, BASE + '/tasks')) == 200
            assert len(calls) == 2
    await run()


async def test_response_cookie_jar_never_crosses_independent_browser_requests():
    async def run():
        calls = []
        async def upstream(request):
            calls.append(request)
            if len(calls) == 1:
                assert request.headers['cookie'] == 'hub_session=browser-a'
                return httpx.Response(200, headers={'set-cookie': 'hub_session=owner-response; Path=/'})
            assert 'cookie' not in request.headers
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            gateway = PublicBrowserGateway(client)
            assert status(await exchange(gateway, BASE + '/tasks',
                headers=[(b'cookie', b'hub_session=browser-a')])) == 200
            # The shared private client's real jar has learned browser A's
            # cookie. Browser B still supplies no cookie on its own request.
            assert client.cookies.get('hub_session') == 'owner-response'
            assert status(await exchange(gateway, BASE + '/tasks')) == 200
            assert len(calls) == 2
    await run()


async def test_late_sse_error_closes_stream_without_second_response_or_retry():
    async def run():
        closed = asyncio.Event()
        calls = []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: first\n\n'
                raise httpx.ReadError('synthetic secret upstream details')
            async def aclose(self):
                closed.set()
        async def upstream(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Stream())
            return httpx.Response(200, content=b'ok')
        messages = []
        received = False
        async def receive():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            await asyncio.Future()
        async def send(message):
            messages.append(message)
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                 'http_version': '1.1', 'method': 'GET', 'scheme': 'https',
                 'path': BASE + '/events', 'raw_path': (BASE + '/events').encode(),
                 'query_string': b'', 'headers': [(b'host', b'hub.example')],
                 'client': ('127.0.0.1', 1)}
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            gateway = PublicBrowserGateway(client, max_concurrency=1)
            with pytest.raises(httpx.ReadError):
                await gateway(scope, receive, send)
            assert closed.is_set() and len(calls) == 1
            assert [m['status'] for m in messages if m['type'] == 'http.response.start'] == [200]
            assert b''.join(m.get('body', b'') for m in messages) == b'data: first\n\n'
            assert status(await exchange(gateway, BASE + '/tasks')) == 200
            assert len(calls) == 2
    await run()


async def test_dripping_ordinary_response_total_deadline_closes_without_retry():
    async def run():
        closed = asyncio.Event()
        calls = []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    yield b'x'
                    await asyncio.sleep(0.005)
            async def aclose(self):
                closed.set()
        async def upstream(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, stream=Stream())
            return httpx.Response(200, content=b'ok')
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            gateway = PublicBrowserGateway(client, max_concurrency=1, request_timeout=0.05)
            # wait_for is a test guard, not the timeout mechanism being proved.
            result = await asyncio.wait_for(exchange(gateway, BASE + '/tasks'), 2)
            assert status(result) == 502
            assert closed.is_set() and len(calls) == 1
            assert status(await exchange(gateway, BASE + '/tasks')) == 200
            assert len(calls) == 2
    await run()


@pytest.mark.parametrize('headers,query', [
    ([(b'x-large', b'x' * 40000)], b''),
    ([], b'x=' + b'x' * 40000),
], ids=['header-limit', 'query-limit'])
async def test_header_and_query_envelopes_are_bounded(headers, query):
    async def run():
        calls = []
        async def upstream(request):
            calls.append(request)
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            result = await exchange(PublicBrowserGateway(client), BASE + '/tasks', headers=headers, query=query)
            assert status(result) in {400, 414, 431}
            assert calls == []
    await run()


async def test_private_receiver_independently_rejects_executor_paths_and_bearer():
    async def run():
        calls = []
        async def core(scope, receive, send):
            calls.append(scope)
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})
        config = AuthConfig('https://hub.example', human_auth_mode='cloudflare_access',
            cloudflare_issuer='https://fixture-team.cloudflareaccess.com',
            cloudflare_audience='fixture-hub', cloudflare_allowed_emails=('beau@example.com',))
        receiver = PrivateBrowserGateway(core, agent_auth=AuthSettings('hermes', 'codex'),
                                         cloudflare_config=config.cloudflare_config)
        for path in [BASE + '/work/claim', BASE + '/runs', '/mcp']:
            result = await exchange(receiver, path, method='POST')
            assert status(result) == 403
        assert status(await exchange(receiver, BASE + '/tasks',
            headers=[(b'authorization', b'Bearer codex')])) == 403
        assert status(await exchange(receiver, BASE + '/tasks')) == 401
        assert calls == []
    await run()


@pytest.mark.parametrize('kwargs', [
    {'max_body_bytes': True}, {'max_response_bytes': 0}, {'max_concurrency': 65},
    {'request_timeout': float('nan')}, {'request_timeout': 0},
])
async def test_invalid_constructor_bounds_never_create_work(kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
            with pytest.raises(ValueError):
                PublicBrowserGateway(client, **kwargs)
    await run()


async def test_bodyless_methods_and_length_mismatch_never_reach_owner():
    async def run():
        calls = []
        async def upstream(request):
            calls.append(request)
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            gateway = PublicBrowserGateway(client)
            assert status(await exchange(gateway, BASE + '/tasks', body=b'x')) == 400
            assert status(await exchange(gateway, BASE + '/tasks', method='POST', body=b'{}',
                headers=[(b'content-length', b'3')])) == 400
            assert calls == []
    await run()


@pytest.mark.parametrize('headers,body,chunks,expected', [
    ([(b'content-length', b'65537')], b'', None, 413),
    ([], b'', [b'a' * 32768, b'b' * 32769], 413),
    ([(b'content-length', b'3')], b'{}', None, 400),
], ids=['declared-too-large', 'streamed-too-large', 'length-mismatch'])
async def test_private_receiver_bounds_before_auth_or_core(headers, body, chunks, expected, monkeypatch):
    calls = []
    async def core(scope, receive, send):
        calls.append('core')
        raise AssertionError('Invalid body must never reach core')
    class Checker:
        async def verify(self, token):
            calls.append('verify')
            raise AssertionError('Invalid body must be rejected before authentication')
    monkeypatch.setattr('engineering_hub.ingress.CloudflareAccessVerifier', lambda _: Checker())
    config = AuthConfig('https://hub.example', human_auth_mode='cloudflare_access',
        cloudflare_issuer='https://fixture-team.cloudflareaccess.com',
        cloudflare_audience='fixture-hub', cloudflare_allowed_emails=('beau@example.com',))
    receiver = PrivateBrowserGateway(core, agent_auth=AuthSettings('hermes', 'codex'),
                                     cloudflare_config=config.cloudflare_config)
    result = await exchange(receiver, BASE + '/tasks', method='POST', body=body, chunks=chunks,
        headers=[*headers, (b'cf-access-jwt-assertion', b'never-verified')])
    assert status(result) == expected
    assert calls == []


async def test_private_receiver_body_timeout_is_408_before_core(monkeypatch):
    from engineering_hub import private_gateway as gateway_module
    original = gateway_module._bounded_body
    calls = []
    async def shortened(scope, receive, length, *, maximum, timeout):
        assert maximum == 65536 and timeout == 15.0
        return await original(scope, receive, length, maximum=maximum, timeout=0.01)
    monkeypatch.setattr(gateway_module, '_bounded_body', shortened)
    async def core(scope, receive, send):
        calls.append('core')
        raise AssertionError('Timed-out body must never reach core')
    config = AuthConfig('https://hub.example', human_auth_mode='cloudflare_access',
        cloudflare_issuer='https://fixture-team.cloudflareaccess.com',
        cloudflare_audience='fixture-hub', cloudflare_allowed_emails=('beau@example.com',))
    receiver = PrivateBrowserGateway(core, agent_auth=AuthSettings('hermes', 'codex'),
                                     cloudflare_config=config.cloudflare_config)
    async def receive():
        await asyncio.Future()
    messages = []
    async def send(message):
        messages.append(message)
    path = BASE + '/tasks'
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
        'method': 'POST', 'scheme': 'https', 'path': path, 'raw_path': path.encode(),
        'query_string': b'', 'headers': [(b'host', b'hub.example')]}
    await asyncio.wait_for(receiver(scope, receive, send), 2)
    assert status(messages) == 408
    assert calls == []


async def test_real_owner_human_session_tasks_decisions_and_csrf(tmp_path, monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid='fixture-key', alg='RS256', use='sig')
    config = AuthConfig('https://hub.example', human_auth_mode='cloudflare_access',
                        cloudflare_issuer='https://fixture-team.cloudflareaccess.com',
                        cloudflare_audience='fixture-hub', cloudflare_allowed_emails=('beau@example.com',))
    verifier = access.CloudflareAccessVerifier(config.cloudflare_config,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'keys': [jwk]})))
    monkeypatch.setattr(access, 'CloudflareAccessVerifier', lambda _: verifier)
    monkeypatch.setattr('engineering_hub.ingress.CloudflareAccessVerifier', lambda _: verifier)
    auth = AuthSettings('test-hermes', 'test-codex', claude_token='test-claude')
    core = create_app(Settings(tmp_path / 'hub.sqlite', auth, config))
    now = int(time.time())
    assertion = jwt.encode(dict(iss=config.cloudflare_issuer, aud=['fixture-hub'],
        sub='synthetic-human', email='beau@example.com', type='app', iat=now - 1, exp=now + 300),
        private, algorithm='RS256', headers={'kid': 'fixture-key'})

    async def run():
        owner = PrivateBrowserGateway(core, agent_auth=auth, cloudflare_config=config.cloudflare_config)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner)) as private_client:
            gateway = PublicBrowserGateway(private_client)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway),
                                        base_url=config.origin, follow_redirects=False) as browser:
                browser.headers['Cf-Access-Jwt-Assertion'] = assertion
                assert (await browser.get(BASE + '/auth/login')).status_code == 303
                me = (await browser.get(BASE + '/auth/me')).json()
                assert me['authenticated'] and me['actor'].startswith('human:cf:')
                payload = {'project': 'engineering-hub', 'title': 'Gateway task', 'client_request_id': 'gateway-task'}
                assert (await browser.post(BASE + '/tasks', json=payload)).status_code == 403
                browser.headers.update({'Origin': config.origin, 'X-Hub-CSRF': me['csrf_token']})
                result = await browser.post(BASE + '/tasks', json=payload)
                assert result.status_code == 201, result.text
                decision = core.state.repository.create_decision('codex', 'engineering-hub', 'Choose', ['A', 'B'])
                answer = await browser.post(BASE + f"/decisions/{decision['id']}/answer",
                    json={'expected_revision': decision['revision'], 'answer': 'A'})
                assert answer.status_code == 200, answer.text
                assert answer.json()['answered_by'] == me['actor']
                browser.headers.pop('Cf-Access-Jwt-Assertion')
                assert (await browser.get(BASE + '/tasks')).status_code == 401
                duplicate = await exchange(gateway, BASE + '/tasks', headers=[
                    (b'cf-access-jwt-assertion', assertion.encode()),
                    (b'cf-access-jwt-assertion', assertion.encode())])
                assert status(duplicate) == 401
    await run()
