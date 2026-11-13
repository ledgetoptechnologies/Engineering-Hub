"""Real isolated transports for staging installation identities; no live state."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import secrets
import socket

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import TransportSecuritySettings

from agent_message_bus.app import create_app
from agent_message_bus.auth import AuthSettings
from agent_message_bus.agent_sessions_config import AgentSessionsConfig
from engineering_hub.app import Settings, create_app as create_hub
from engineering_hub.auth import AuthConfig
from engineering_hub.frontends import HubFrontends


OWNER = 'human:42'
PREFIX = '/api/agent-sessions/v1'
AUTH = AuthSettings('transport-hermes', 'transport-codex', claude_token='transport-claude')
ORIGIN = 'http://127.0.0.1'


def bus(tmp_path, *, enabled=True, origin=ORIGIN, auth=AUTH):
    config = AgentSessionsConfig(tmp_path / 'connections.sqlite3', OWNER, origin) if enabled else None
    return create_app(db_path=tmp_path / 'legacy.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=auth, startup_cleanup=False, push_events_operational=False,
        agent_sessions_config=config, transport_security_settings=TransportSecuritySettings(
            allowed_hosts=['127.0.0.1'] if origin == ORIGIN else ['hub.test'], allowed_origins=[origin]))


def sdk_data(result):
    assert not result.isError
    return result.structuredContent or json.loads(result.content[0].text)


def enrollment(secret, name='Codex fixture'):
    return {'family': 'codex', 'name': name, 'host_label': 'synthetic',
            'credential_sha256': hashlib.sha256(secret.encode()).hexdigest()}


def secret():
    return 'ehi_' + secrets.token_urlsafe(32)


def credential(repo, name='Codex fixture'):
    value = secret()
    pending = repo.request_enrollment(**enrollment(value, name))
    repo.approve(OWNER, pending['id'], pending['revision'])
    return value


def headers(token=None, sid=None, host='127.0.0.1'):
    h = {'Host': host, 'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'}
    if token is not None:
        h['Authorization'] = 'Bearer ' + token
    if sid:
        h['Mcp-Session-Id'] = sid
    return h


@asynccontextmanager
async def listening(app):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.setblocking(False)
    ready = asyncio.Event()
    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()
    server = Server(uvicorn.Config(app, log_level='critical', access_log=False, proxy_headers=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        yield 'http://127.0.0.1:' + str(listener.getsockname()[1])
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            listener.close()
        # sse-starlette owns a loop-scoped shutdown notifier, also cleaned by
        # the existing native MCP test fixture after its server has drained.
        current = asyncio.current_task()
        watchers = [pending for pending in asyncio.all_tasks() if pending is not current
                    and not pending.done() and pending.get_coro().__qualname__ == '_shutdown_watcher']
        for watcher in watchers:
            watcher.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)


async def initialize(client, token):
    response = await client.post('/mcp', headers=headers(token), json={'jsonrpc': '2.0', 'id': 1,
        'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
            'clientInfo': {'name': 'synthetic-installation', 'version': '1'}}})
    assert response.status_code == 200, response.text
    sid = response.headers['Mcp-Session-Id']
    notified = await client.post('/mcp', headers=headers(token, sid),
        json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    assert notified.status_code == 202
    return sid


async def rpc(client, token, sid, method, params=None):
    return await client.post('/mcp', headers=headers(token, sid),
        json={'jsonrpc': '2.0', 'id': 2, 'method': method, 'params': params or {}})


def test_default_off_and_separate_database_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_AGENT_SESSIONS_MODE', 'disabled')
    app = bus(tmp_path, enabled=False)
    assert app.state.agent_sessions is None and app.state.agent_sessions_config is None
    assert not (tmp_path / 'connections.sqlite3').exists()
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.post(PREFIX + '/enrollments', json=enrollment(secret())).status_code == 404
        response = client.post('/mcp', headers=headers(AUTH.codex_token), json={'jsonrpc': '2.0', 'id': 1,
            'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                'clientInfo': {'name': 'legacy', 'version': '1'}}})
        sid = response.headers['Mcp-Session-Id']
        result = client.post('/mcp', headers=headers(AUTH.codex_token, sid), json={
            'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}).json()['result']
        assert not any(t['name'].startswith('session_') for t in result['tools'])
    for selected in (tmp_path / 'legacy.sqlite3', tmp_path / 'audit.jsonl', tmp_path / 'files/connections.sqlite3'):
        with pytest.raises(ValueError, match='separate_agent_sessions_database_required'):
            create_app(db_path=tmp_path / 'legacy.sqlite3', audit_path=tmp_path / 'audit.jsonl',
                file_root=tmp_path / 'files', auth=AUTH,
                agent_sessions_config=AgentSessionsConfig(selected, OWNER, ORIGIN))


def test_namespace_pairing_validation_and_trace_redaction(tmp_path, caplog):
    app = bus(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        value = secret()
        wrong_host = client.post(PREFIX + '/enrollments', headers={'Host': 'other.test'}, json=enrollment(value))
        assert wrong_host.status_code == 403
        for authorization in ('Bearer invalid', 'Bearer ehi_invalid', 'Basic invalid'):
            assert client.post(PREFIX + '/enrollments', headers={'Authorization': authorization}, json=enrollment(value)).status_code == 401
        invalid = client.post(PREFIX + '/enrollments', json={**enrollment(value), 'secret': value})
        assert invalid.status_code == 422 and value not in invalid.text
        assert invalid.json() == {'detail': {'code': 'invalid_agent_session_input'}}
        pending = client.post(PREFIX + '/enrollments', json=enrollment(value)).json()
        assert client.post(PREFIX + '/exchange', headers=headers(value), json={}).status_code == 401
        app.state.agent_sessions.approve(OWNER, pending['id'], 1)
        issued = client.post(PREFIX + '/exchange', headers=headers(value), json={}).json()
        token = issued['token']
        assert client.get(PREFIX + '/identity', headers=headers(token)).json()['installation_id'] == pending['id']
        for tokenlike in ('ehs_expired_or_malformed', 'ehi_invalid', 'ehc_unusable'):
            response = client.get('/api/messages', headers={**headers(AUTH.codex_token),
                'X-Request-ID': tokenlike, 'X-Correlation-ID': tokenlike})
            assert response.status_code == 200
            assert response.headers['X-Request-ID'] != tokenlike
            assert response.headers['X-Correlation-ID'] != tokenlike
        for tokenlike in (value, token, 'ehs_expired_or_malformed', 'ehi_invalid', 'ehc_unusable'):
            # Mis-pasted secrets must not leak through either new or unrelated URL paths.
            client.get(PREFIX + '/messages/' + tokenlike, headers=headers(token))
            client.get('/unregistered/' + tokenlike, headers=headers(AUTH.codex_token))
        records = [json.loads(record.message) for record in caplog.records
                   if record.name == 'agent_message_bus.http' and record.message.startswith('{')]
        assert any(record['path'] == '/api/agent-sessions/v1/{path}' for record in records)
        assert any(record['path'] == '/{redacted}' for record in records)
        assert not any(tokenlike in caplog.text for tokenlike in (value, token, 'ehs_expired_or_malformed', 'ehi_invalid', 'ehc_unusable'))
        assert client.get('/api/messages', headers=headers(token)).status_code == 401
        assert client.get(PREFIX + '/messages', headers=headers(AUTH.codex_token)).status_code == 401
        duplicate = [('Host', '127.0.0.1'), ('Authorization', 'Bearer ' + token), ('Authorization', 'Bearer ' + token)]
        assert client.get(PREFIX + '/identity', headers=duplicate).status_code == 401


@pytest.mark.asyncio
async def test_native_mcp_two_same_family_installations_and_claim_ownership(tmp_path):
    app = bus(tmp_path)
    repo = app.state.agent_sessions
    installation_a, installation_b = credential(repo, 'A'), credential(repo, 'B')
    a, b = repo.exchange(installation_a), repo.exchange(installation_b)
    sibling = repo.exchange(installation_b)
    async with listening(app) as origin:
        async with httpx.AsyncClient(base_url=origin, headers={'Host': '127.0.0.1'}, timeout=5) as http:
            async with httpx.AsyncClient(headers=headers(a['token']), timeout=httpx.Timeout(5, read=None)) as ha:
                async with streamable_http_client(origin + '/mcp', http_client=ha) as (ra, wa, _):
                    async with ClientSession(ra, wa) as ca:
                        await ca.initialize()
                        async with httpx.AsyncClient(headers=headers(b['token']), timeout=httpx.Timeout(5, read=None)) as hb:
                            async with streamable_http_client(origin + '/mcp', http_client=hb) as (rb, wb, _):
                                async with ClientSession(rb, wb) as cb:
                                    await cb.initialize()
                                    ia = await ca.call_tool('session_identity', {})
                                    ib = await cb.call_tool('session_identity', {})
                                    assert not ia.isError and not ib.isError
                                    da, db = sdk_data(ia), sdk_data(ib)
                                    assert da['family'] == db['family'] == 'codex'
                                    assert da['installation_id'] != db['installation_id']
                                    legacy = await ca.call_tool('receive_messages', {})
                                    assert legacy.isError
                                    sent = await ca.call_tool('session_message_send', {'payload': {
                                        'recipient': b['installation']['id'], 'body': 'synthetic', 'client_request_id': 'send-1'}})
                                    assert not sent.isError
                                    message_id = sdk_data(sent)['id']
                                    claimed = await cb.call_tool('session_message_claim', {'message_id': message_id, 'payload': {}})
                                    assert not claimed.isError
                                    claim = sdk_data(claimed)
                                    result = await http.post(PREFIX + '/messages/' + message_id + '/acknowledge',
                                        headers=headers(sibling['token']), json={'claim_handle': claim['claim_handle'], 'generation': claim['generation']})
                                    assert result.status_code == 409
                                    assert (await http.get(PREFIX + '/messages/' + message_id, headers=headers(a['token']))).status_code == 404
                                    ack = await cb.call_tool('session_message_acknowledge', {'message_id': message_id,
                                        'payload': {'claim_handle': claim['claim_handle'], 'generation': claim['generation']}})
                                    assert not ack.isError
    assert app.state.push_publisher.subscription_count == 0
    assert repo._listeners == {}


@pytest.mark.asyncio
async def test_same_family_cannot_reuse_foreign_mcp_session_or_legacy_resources(tmp_path):
    app = bus(tmp_path)
    repo = app.state.agent_sessions
    a, b = repo.exchange(credential(repo, 'A')), repo.exchange(credential(repo, 'B'))
    async with listening(app) as origin:
        async with httpx.AsyncClient(base_url=origin, timeout=3) as client:
            sid = await initialize(client, a['token'])
            for method in ('GET', 'POST', 'DELETE'):
                response = await client.request(method, '/mcp', headers=headers(b['token'], sid),
                    **({'json': {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}} if method == 'POST' else {}))
                assert response.status_code == 404
            resources = await rpc(client, a['token'], sid, 'resources/list')
            envelope = resources.json()
            assert 'error' in envelope or envelope.get('result', {}).get('resources') == []
            legacy_sid = await initialize(client, AUTH.codex_token)
            denied = await rpc(client, AUTH.codex_token, legacy_sid, 'tools/call', {'name': 'session_messages', 'arguments': {}})
            assert denied.json()['result']['isError']
            duplicate = [*headers(a['token'], sid).items(), ('Authorization', 'Bearer ' + b['token'])]
            assert (await client.post('/mcp', headers=duplicate, json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})).status_code == 401
            assert (await rpc(client, a['token'], sid, 'ping')).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['session', 'installation', 'expiry'])
async def test_existing_get_stream_ends_on_revocation_or_expiry_only_its_owner(tmp_path, monkeypatch, ending):
    if ending == 'expiry':
        monkeypatch.setattr('agent_message_bus.agent_sessions.SESSION_SECONDS', 2)
    app = bus(tmp_path)
    repo = app.state.agent_sessions
    ia, ib = credential(repo, 'A'), credential(repo, 'B')
    a, b = repo.exchange(ia), repo.exchange(ib)
    sibling = repo.exchange(ia)
    async with listening(app) as origin:
        async with httpx.AsyncClient(base_url=origin, timeout=httpx.Timeout(5, read=None)) as client:
            sid = await initialize(client, a['token'])
            async with client.stream('GET', '/mcp', headers=headers(a['token'], sid)) as response:
                assert response.status_code == 200
                consume = asyncio.create_task(response.aread())
                if ending == 'session':
                    repo.revoke_session(OWNER, a['session']['id'], 1)
                elif ending == 'installation':
                    repo.revoke_installation(OWNER, a['installation']['id'], 2)
                await asyncio.wait_for(consume, 4)
            for method in ('GET', 'POST', 'DELETE'):
                rejected = await client.request(method, '/mcp', headers=headers(a['token'], sid),
                    **({'json': {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}} if method == 'POST' else {}))
                assert rejected.status_code == 401
            if ending != 'expiry':
                assert (await client.get(PREFIX + '/identity', headers=headers(b['token']))).status_code == 200
                assert (await client.get(PREFIX + '/identity', headers=headers(sibling['token']))).status_code == (200 if ending == 'session' else 401)
            assert a['session']['id'] not in repo._listeners
    assert repo._listeners == {}


def test_exact_embedded_repository_frontdoors_and_browser_denial(tmp_path):
    origin = 'https://hub.test'
    embedded = bus(tmp_path, origin=origin)
    config = embedded.state.agent_sessions_config
    auth = AuthConfig(origin, human_auth_mode='cloudflare_access', cloudflare_issuer='https://hub-test.cloudflareaccess.com',
        cloudflare_audience='test', cloudflare_allowed_emails=('owner@example.test',))
    core = create_hub(Settings(tmp_path / 'hub.sqlite3', AUTH, auth, agent_allowed_hosts=('hub.test',)), bus_app=embedded)
    pair = HubFrontends(core)
    assert pair.agents.ingress.agent_sessions is embedded.state.agent_sessions
    assert pair.agents.ingress.agent_sessions_config is config
    async def exercise():
        async with pair.lifespan():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.agents), base_url=origin) as client:
                pending = await client.post(PREFIX + '/enrollments', json=enrollment(secret()))
                assert pending.status_code == 200
                repo = embedded.state.agent_sessions
                issued = repo.exchange(credential(repo))
                token = issued['token']
                assert (await client.get(PREFIX + '/identity', headers={'Authorization': 'Bearer ' + token})).status_code == 200
                assert (await client.get('/api/hub/v1/overview', headers={'Authorization': 'Bearer ' + token})).status_code == 401
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.browser), base_url=origin) as browser:
                assert (await browser.post(PREFIX + '/enrollments', json=enrollment(secret()))).status_code == 403
    asyncio.run(exercise())


@pytest.mark.asyncio
async def test_source_ip_and_transport_caps_fail_closed(tmp_path, monkeypatch):
    from dataclasses import replace
    denied_app = bus(tmp_path / 'denied', auth=replace(AUTH, allowed_ips=frozenset({'192.0.2.1'})))
    repo = denied_app.state.agent_sessions
    issued = repo.exchange(credential(repo))
    async with listening(denied_app) as origin:
        async with httpx.AsyncClient(base_url=origin, timeout=3) as client:
            forged = {**headers(issued['token']), 'X-Forwarded-For': '192.0.2.1', 'X-Real-IP': '192.0.2.1'}
            assert (await client.get(PREFIX + '/identity', headers=forged)).status_code == 403
            assert (await client.post('/mcp', headers=forged, json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})).status_code == 403
    monkeypatch.setattr('agent_message_bus.agent_sessions_transport.MAX_NEW_TRANSPORTS', 1)
    app = bus(tmp_path / 'bounded')
    repo = app.state.agent_sessions
    a, b = repo.exchange(credential(repo, 'A')), repo.exchange(credential(repo, 'B'))
    async with listening(app) as origin:
        async with httpx.AsyncClient(base_url=origin, timeout=3) as client:
            slash = await client.post('/mcp/', headers=headers(a['token']), json={
                'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
            assert slash.status_code == 404
            sid = await initialize(client, a['token'])
            attempted = await rpc(client, b['token'], None, 'initialize', {
                'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'bounded', 'version': '1'}})
            assert attempted.status_code == 429
            assert (await rpc(client, a['token'], sid, 'ping')).status_code == 200
            legacy = await initialize(client, AUTH.codex_token)
            assert (await rpc(client, AUTH.codex_token, legacy, 'ping')).status_code == 200
            assert (await rpc(client, b['token'], legacy, 'ping')).status_code == 404
            assert (await rpc(client, AUTH.codex_token, sid, 'ping')).status_code == 404
            assert (await client.delete('/mcp', headers=headers(a['token'], sid))).status_code == 200
            # The SDK has actually ended the old transport. Its isolated slot
            # is reusable, even after more creations than the capacity.
            for _ in range(4):
                current = await initialize(client, b['token'])
                assert (await client.delete('/mcp', headers=headers(b['token'], current))).status_code == 200
            assert (await rpc(client, AUTH.codex_token, legacy, 'ping')).status_code == 200
            assert (await client.delete('/mcp', headers=headers(AUTH.codex_token, legacy))).status_code == 200
    assert repo._listeners == {}


@pytest.mark.asyncio
async def test_failed_openings_and_revoke_reap_only_isolated_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr('agent_message_bus.agent_sessions_transport.MAX_NEW_TRANSPORTS', 1)
    app = bus(tmp_path)
    # Bound the real SDK idle cleanup for the test; no SDK private lifecycle
    # hooks are used by the application or needed by this test.
    app.state.agent_session_mcp_manager.session_idle_timeout = 0.2
    repo = app.state.agent_sessions
    a, b = repo.exchange(credential(repo, 'A')), repo.exchange(credential(repo, 'B'))
    async with listening(app) as origin:
        async with httpx.AsyncClient(base_url=origin, timeout=3) as client:
            legacy = await initialize(client, AUTH.codex_token)
            for _ in range(4):
                response = await rpc(client, a['token'], None, 'ping')
                assert response.status_code == 400
            old = await initialize(client, a['token'])
            repo.revoke_session(OWNER, a['session']['id'], 1)
            await asyncio.sleep(0.6)
            assert (await rpc(client, a['token'], old, 'ping')).status_code == 401
            assert (await rpc(client, AUTH.codex_token, legacy, 'ping')).status_code == 200
            current = await initialize(client, b['token'])
            assert (await rpc(client, b['token'], current, 'ping')).status_code == 200
            assert (await client.delete('/mcp', headers=headers(b['token'], current))).status_code == 200
            assert (await client.delete('/mcp', headers=headers(AUTH.codex_token, legacy))).status_code == 200
    assert repo._listeners == {}


@pytest.mark.asyncio
async def test_native_session_messages_requires_strict_wire_integers(tmp_path, monkeypatch):
    app = bus(tmp_path)
    repo = app.state.agent_sessions
    issued = repo.exchange(credential(repo))
    calls = []
    original = repo.messages
    def observed(token, *, after=0, limit=50):
        calls.append((after, limit))
        return original(token, after=after, limit=limit)
    monkeypatch.setattr(repo, 'messages', observed)
    violations = []
    async with listening(app) as origin:
        async with httpx.AsyncClient(headers=headers(issued['token']), timeout=httpx.Timeout(5, read=None)) as http:
            async with streamable_http_client(origin + '/mcp', http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for field in ('after', 'limit'):
                        for value in (True, 1.0, '1'):
                            before = len(calls)
                            result = await session.call_tool('session_messages', {'after': 0, 'limit': 1, field: value})
                            if not result.isError or len(calls) != before:
                                violations.append(field + ':' + type(value).__name__)
                    positive = await session.call_tool('session_messages', {'after': 0, 'limit': 1})
                    assert sdk_data(positive)['messages'] == []
                    assert calls[-1] == (0, 1) and all(type(v) is int for v in calls[-1])
    assert violations == [], 'wire coercion reached the core: ' + ','.join(violations)


@pytest.mark.asyncio
async def test_direct_agents_ingress_uses_exact_session_service_and_keeps_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr('agent_message_bus.agent_sessions.MAX_SUBSCRIPTIONS', 3)
    embedded = bus(tmp_path)
    core = create_hub(Settings(tmp_path / 'hub.sqlite3', AUTH, AuthConfig(ORIGIN, beau_github_id=42),
        ingress_mode='agents', agent_allowed_hosts=('127.0.0.1',)), bus_app=embedded)
    repo = embedded.state.agent_sessions
    value = secret()
    async with listening(core) as origin:
        async with httpx.AsyncClient(base_url=origin, timeout=5) as http:
            response = await http.post(PREFIX + '/enrollments', headers=headers(), json=enrollment(value))
            assert response.status_code == 200
            pending = response.json()
            repo.approve(OWNER, pending['id'], pending['revision'])
            exchanged = await http.post(PREFIX + '/exchange', headers=headers(value), json={})
            assert exchanged.status_code == 200
            issued = exchanged.json()
            token = issued['token']
            identity = await http.get(PREFIX + '/identity', headers=headers(token))
            assert identity.status_code == 200 and identity.json()['installation_id'] == pending['id']
            async with httpx.AsyncClient(headers=headers(token), timeout=httpx.Timeout(5, read=None)) as client:
                async with streamable_http_client(origin + '/mcp', http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        assert sdk_data(await session.call_tool('session_identity', {}))['installation_id'] == pending['id']
                        assert (await session.call_tool('hub_overview', {})).isError
                        assert (await session.call_tool('hub_overview', {'active_only': True, 'project': 'treedar'})).isError
            assert (await http.get('/api/hub/v1/overview', headers=headers(token))).status_code == 401
            assert (await http.get('/api/hub/v1/overview', headers=headers(AUTH.codex_token))).status_code == 200
            assert (await http.get('/api/messages', headers=headers(AUTH.codex_token))).status_code == 200
            sid = await initialize(http, AUTH.codex_token)
            legacy = await rpc(http, AUTH.codex_token, sid, 'tools/call', {'name': 'hub_overview', 'arguments': {}})
            assert legacy.status_code == 200 and not legacy.json()['result'].get('isError')
    assert repo._listeners == {}


@pytest.mark.parametrize('prefix', ['ehs_', 'ehi_', 'ehc_', 'EHS_', 'eHi_', 'EhC_'])
def test_disabled_feature_preserves_legacy_prefixed_credentials(tmp_path, monkeypatch, prefix):
    monkeypatch.setenv('HUB_AGENT_SESSIONS_MODE', 'disabled')
    token = prefix + 'synthetic-legacy-codex'
    auth = AuthSettings('ordinary-hermes', token, claude_token='ordinary-claude')
    embedded = bus(tmp_path, enabled=False, auth=auth)
    core = create_hub(Settings(tmp_path / 'hub.sqlite3', auth, AuthConfig(ORIGIN, beau_github_id=42),
        ingress_mode='agents', agent_allowed_hosts=('127.0.0.1',)), bus_app=embedded)
    with TestClient(core, base_url=ORIGIN) as client:
        assert client.get('/api/messages', headers=headers(token)).status_code == 200
        assert client.get('/api/hub/v1/overview', headers=headers(token)).status_code == 200
        response = client.post('/mcp', headers=headers(token), json={'jsonrpc': '2.0', 'id': 1,
            'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                'clientInfo': {'name': 'disabled-prefix-parity', 'version': '1'}}})
        assert response.status_code == 200
        sid = response.headers['Mcp-Session-Id']
        listed = client.post('/mcp', headers=headers(token, sid), json={'jsonrpc': '2.0', 'id': 2,
            'method': 'tools/list', 'params': {}})
        assert listed.status_code == 200 and not any(t['name'].startswith('session_') for t in listed.json()['result']['tools'])
        assert client.get('/api/messages', headers={'Authorization': 'Bearer malformed extra'}).status_code == 401
        assert client.post('/mcp', headers={'Authorization': 'Bearer malformed extra'}, json={}).status_code == 401
    assert embedded.state.agent_sessions is None and not (tmp_path / 'connections.sqlite3').exists()


@pytest.mark.parametrize('field', ['hermes_token', 'codex_token', 'claude_token', 'admin_token'])
@pytest.mark.parametrize('prefix', ['ehs_', 'ehi_', 'ehc_', 'EHS_', 'eHi_', 'EhC_'])
def test_enabled_reserved_prefix_collisions_fail_before_state_creation(tmp_path, field, prefix):
    values = dict(hermes_token='ordinary-hermes', codex_token='ordinary-codex',
                  claude_token='ordinary-claude', admin_token='ordinary-admin')
    values[field] = prefix + 'synthetic-collision'
    root = tmp_path / 'unopened'
    with pytest.raises(ValueError, match='legacy_session_credential_prefix_collision'):
        create_app(db_path=root / 'legacy.sqlite3', audit_path=root / 'audit.jsonl',
            file_root=root / 'files', auth=AuthSettings(**values),
            agent_sessions_config=AgentSessionsConfig(root / 'connections.sqlite3', OWNER, ORIGIN))
    assert not root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['session', 'installation', 'expiry'])
@pytest.mark.parametrize('composition', ['bus', 'combined'])
async def test_real_mcp_post_sse_ends_on_revocation_or_expiry(tmp_path, monkeypatch, ending, composition):
    import agent_message_bus.app as bus_module
    from mcp.server.fastmcp import Context
    from pydantic import StrictInt
    producer_entered, producer_release = asyncio.Event(), asyncio.Event()
    original_build = bus_module.build_mcp
    def streaming_build(service, **kwargs):
        mcp = original_build(service, **kwargs)
        mcp.settings.json_response = False
        # A test-only response producer occupies an already allowed session tool
        # before optional tool installation; actual core mutation tests stay separate.
        @mcp.tool()
        async def session_messages(ctx: Context, after: StrictInt = 0, limit: StrictInt = 50) -> dict:
            producer_entered.set()
            await producer_release.wait()
            return {'messages': [], 'next_cursor': 0}
        return mcp
    monkeypatch.setattr(bus_module, 'build_mcp', streaming_build)
    embedded = bus(tmp_path)
    repo = embedded.state.agent_sessions
    app = embedded if composition == 'bus' else create_hub(Settings(
        tmp_path / 'hub.sqlite3', AUTH, AuthConfig(ORIGIN, beau_github_id=42),
        agent_allowed_hosts=('127.0.0.1',)), bus_app=embedded)
    ia, ib = credential(repo, 'A'), credential(repo, 'B')
    if ending == 'expiry':
        monkeypatch.setattr('agent_message_bus.agent_sessions.SESSION_SECONDS', 2)
    a = repo.exchange(ia)
    monkeypatch.setattr('agent_message_bus.agent_sessions.SESSION_SECONDS', 900)
    sibling, b = repo.exchange(ia), repo.exchange(ib)
    async with listening(app) as origin:
        async with httpx.AsyncClient(base_url=origin, timeout=httpx.Timeout(5, read=None)) as client:
            sid = await initialize(client, a['token'])
            legacy_sid = await initialize(client, AUTH.codex_token)
            legacy_stream = await client.send(client.build_request('GET', '/mcp', headers=headers(AUTH.codex_token, legacy_sid)), stream=True)
            assert legacy_stream.status_code == 200
            legacy_consumer = asyncio.create_task(legacy_stream.aread())
            try:
                async with client.stream('POST', '/mcp', headers=headers(a['token'], sid), json={
                    'jsonrpc': '2.0', 'id': 10, 'method': 'tools/call',
                    'params': {'name': 'session_messages', 'arguments': {'after': 0, 'limit': 1}}}) as response:
                    assert response.status_code == 200 and response.headers['content-type'].startswith('text/event-stream')
                    await asyncio.wait_for(producer_entered.wait(), 2)
                    consumed = asyncio.create_task(response.aread())
                    if ending == 'session':
                        repo.revoke_session(OWNER, a['session']['id'], 1)
                    elif ending == 'installation':
                        repo.revoke_installation(OWNER, a['installation']['id'], 2)
                    try:
                        await asyncio.wait_for(consumed, 4)
                    finally:
                        producer_release.set()
                assert (await rpc(client, a['token'], sid, 'ping')).status_code == 401
                assert (await client.get(PREFIX + '/identity', headers=headers(b['token']))).status_code == 200
                assert (await client.get(PREFIX + '/identity', headers=headers(sibling['token']))).status_code == (401 if ending == 'installation' else 200)
                assert (await client.get('/api/messages', headers=headers(AUTH.codex_token))).status_code == 200
                assert not legacy_consumer.done()
                assert (await rpc(client, AUTH.codex_token, legacy_sid, 'ping')).status_code == 200
            finally:
                producer_release.set()
                legacy_consumer.cancel()
                await asyncio.gather(legacy_consumer, return_exceptions=True)
                await legacy_stream.aclose()
                assert (await client.delete('/mcp', headers=headers(AUTH.codex_token, legacy_sid))).status_code == 200
    assert repo._listeners == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('composition', ['bus', 'combined', 'direct'])
async def test_revocation_during_initialize_never_publishes_owned_sdk_session(tmp_path, composition, monkeypatch):
    monkeypatch.setattr('agent_message_bus.agent_sessions_transport.MAX_NEW_TRANSPORTS', 1)
    embedded = bus(tmp_path)
    repo = embedded.state.agent_sessions
    app = embedded if composition == 'bus' else create_hub(Settings(
        tmp_path / 'hub.sqlite3', AUTH, AuthConfig(ORIGIN, beau_github_id=42),
        ingress_mode='agents' if composition == 'direct' else 'combined',
        agent_allowed_hosts=('127.0.0.1',)), bus_app=embedded)
    ia, ib = credential(repo, 'A'), credential(repo, 'B')
    a, b = repo.exchange(ia), repo.exchange(ib)
    started, release = asyncio.Event(), asyncio.Event()
    blocked = False
    async def barrier(scope, receive, send):
        nonlocal blocked
        async def observed(message):
            nonlocal blocked
            if (not blocked and scope.get('method') == 'POST' and scope.get('path') == '/mcp'
                    and message['type'] == 'http.response.start' and message['status'] == 200):
                blocked = True
                started.set()
                await release.wait()
            await send(message)
        await app(scope, receive, observed)
    async with listening(barrier) as origin:
        async with httpx.AsyncClient(base_url=origin, timeout=5) as client:
            pending = asyncio.create_task(client.post('/mcp', headers=headers(a['token']), json={
                'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                    'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'initialize-race', 'version': '1'}}}))
            try:
                await asyncio.wait_for(started.wait(), 3)
                # The in-flight open owns the sole EHS slot, but cannot block
                # creation or use of a legacy transport in its own manager.
                assert (await rpc(client, b['token'], None, 'initialize', {})).status_code == 429
                legacy_sid = await initialize(client, AUTH.codex_token)
                assert (await rpc(client, AUTH.codex_token, legacy_sid, 'ping')).status_code == 200
                repo.revoke_session(OWNER, a['session']['id'], 1)
                response = await asyncio.wait_for(pending, 2)
                assert response.status_code == 401 and 'Mcp-Session-Id' not in response.headers
                other_sid = await initialize(client, b['token'])
                assert (await rpc(client, b['token'], other_sid, 'ping')).status_code == 200
                assert (await rpc(client, AUTH.codex_token, legacy_sid, 'ping')).status_code == 200
                assert (await client.delete('/mcp', headers=headers(AUTH.codex_token, legacy_sid))).status_code == 200
            finally:
                release.set()
                if not pending.done():
                    await asyncio.gather(pending, return_exceptions=True)
    assert repo._listeners == {}


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('token', ['synthetic-legacy-' + 'x' * 600,
    'synthetic legacy,with spaces', ' ehs_synthetic-leading-space'])
def test_legacy_raw_token_partition_and_length_remain_compatible(tmp_path, enabled, token):
    auth = AuthSettings('ordinary-hermes', token, claude_token='ordinary-claude')
    embedded = bus(tmp_path, enabled=enabled, auth=auth)
    core = create_hub(Settings(tmp_path / 'hub.sqlite3', auth, AuthConfig(ORIGIN, beau_github_id=42),
        ingress_mode='agents', agent_allowed_hosts=('127.0.0.1',)), bus_app=embedded)
    with TestClient(core, base_url=ORIGIN) as client:
        assert client.get('/api/messages', headers=headers(token)).status_code == 200
        assert client.get('/api/hub/v1/overview', headers=headers(token)).status_code == 200
        response = client.post('/mcp', headers=headers(token), json={'jsonrpc': '2.0', 'id': 1,
            'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                'clientInfo': {'name': 'legacy-raw-token', 'version': '1'}}})
        assert response.status_code == 200
        sid = response.headers['Mcp-Session-Id']
        called = client.post('/mcp', headers=headers(token, sid), json={'jsonrpc': '2.0', 'id': 2,
            'method': 'tools/call', 'params': {'name': 'receive_messages', 'arguments': {}}})
        assert called.status_code == 200 and not called.json()['result'].get('isError')
        assert client.get('/api/messages', headers={'Authorization': 'Bearer not-configured'}).status_code == 401
        assert client.post('/mcp', headers=[*headers(token).items(), ('Authorization', 'Bearer other')], json={}).status_code == 401


# Direct ASGI barriers isolate response completion from downstream task completion.
@asynccontextmanager
async def _response_guard_harness(tmp_path):
    from agent_message_bus.agent_sessions_transport import AgentSessionResponseGuardMiddleware

    app = bus(tmp_path)
    repo = app.state.agent_sessions
    issued = repo.exchange(credential(repo))
    release = asyncio.Event()
    cancellation_seen = asyncio.Queue()
    entered = asyncio.Queue()
    tasks = []

    async def downstream(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        if scope['method'] == 'POST' or scope.get('test_get_final'):
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        await entered.put(scope['method'])
        if scope.get('test_hold_cancellation'):
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await cancellation_seen.put(True)
        else:
            await release.wait()

    guard = AgentSessionResponseGuardMiddleware(downstream, auth=AUTH, repository=repo)

    def scope(method):
        return {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                'scheme': 'http', 'method': method, 'path': '/mcp', 'raw_path': b'/mcp',
                'query_string': b'', 'root_path': '', 'headers': [
                    (k.lower().encode('ascii'), v.encode('ascii'))
                    for k, v in headers(issued['token']).items()],
                'client': ('127.0.0.1', 12345), 'server': ('127.0.0.1', 80), 'state': {}}

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message):
        pass

    def start(method, sender=send, *, get_final=False, hold_cancellation=False):
        request_scope = scope(method)
        request_scope['test_get_final'] = get_final
        request_scope['test_hold_cancellation'] = hold_cancellation
        task = asyncio.create_task(guard(request_scope, receive, sender))
        tasks.append(task)
        return task

    async def denial(method):
        response = []
        async def capture(message):
            response.append(message)
        await asyncio.wait_for(guard(scope(method), receive, capture), 2)
        assert response[0]['status'] == 429
        assert json.loads(response[1]['body'])['detail']['code'] == 'session_transport_limit'

    try:
        yield guard, repo, issued, entered, start, denial, cancellation_seen
    finally:
        release.set()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        assert guard._total == 0 and not guard._active and not getattr(guard, '_completed_posts', {})
        assert not repo._listeners


@pytest.mark.asyncio
async def test_response_guard_completed_post_allowance_is_bounded(tmp_path):
    async with _response_guard_harness(tmp_path) as (guard, repo, issued, entered, start, denial, cancellation_seen):
        first = start('GET')
        assert await asyncio.wait_for(entered.get(), 2) == 'GET'
        second = start('POST')
        assert await asyncio.wait_for(entered.get(), 2) == 'POST'
        assert not second.done() and guard._total == 2
        third_status = asyncio.Future()
        async def third_send(message):
            if message['type'] == 'http.response.start':
                third_status.set_result(message['status'])
        third = start('POST', third_send)
        assert await asyncio.wait_for(third_status, 2) == 200
        assert await asyncio.wait_for(entered.get(), 2) == 'POST'
        assert not first.done() and not third.done() and guard._total == 3
        assert list(guard._completed_posts.values()) == [1]
        assert list(guard._active.values()) == [2]
        await denial('POST')


@pytest.mark.asyncio
async def test_response_guard_completed_post_still_counts_global(tmp_path, monkeypatch):
    monkeypatch.setattr('agent_message_bus.agent_sessions_transport.MAX_NEW_STREAMS', 2)
    async with _response_guard_harness(tmp_path) as (guard, repo, issued, entered, start, denial, cancellation_seen):
        start('GET')
        assert await asyncio.wait_for(entered.get(), 2) == 'GET'
        start('POST')
        assert await asyncio.wait_for(entered.get(), 2) == 'POST'
        assert guard._total == 2 and list(guard._completed_posts.values()) == [1]
        await denial('POST')


@pytest.mark.asyncio
async def test_response_guard_failed_final_send_never_promotes(tmp_path):
    async with _response_guard_harness(tmp_path) as (guard, repo, issued, entered, start, denial, cancellation_seen):
        reached = asyncio.Event()
        proceed = asyncio.Event()
        async def failed_send(message):
            if message['type'] == 'http.response.body':
                reached.set()
                await proceed.wait()
                raise RuntimeError('synthetic_send_failure')
        start('GET')
        assert await asyncio.wait_for(entered.get(), 2) == 'GET'
        task = start('POST', failed_send)
        await asyncio.wait_for(reached.wait(), 2)
        assert not guard._completed_posts and guard._total == 2
        await denial('POST')
        proceed.set()
        with pytest.raises(RuntimeError, match='synthetic_send_failure'):
            await asyncio.wait_for(task, 2)
        assert guard._total == 1 and not guard._completed_posts


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['revoke', 'expiry', 'cancel'])
async def test_response_guard_completed_post_retains_cancellation_ownership(tmp_path, monkeypatch, ending):
    if ending == 'expiry':
        monkeypatch.setattr('agent_message_bus.agent_sessions.SESSION_SECONDS', 1)
    async with _response_guard_harness(tmp_path) as (guard, repo, issued, entered, start, denial, cancellation_seen):
        task = start('POST')
        assert await asyncio.wait_for(entered.get(), 2) == 'POST'
        assert guard._total == 1 and list(guard._completed_posts.values()) == [1]
        if ending == 'revoke':
            repo.revoke_session(OWNER, issued['session']['id'], 1)
        elif ending == 'cancel':
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        assert guard._total == 0 and not guard._active and not getattr(guard, '_completed_posts', {})
        assert not repo._listeners


@pytest.mark.asyncio
async def test_response_guard_completed_get_does_not_use_post_allowance(tmp_path):
    async with _response_guard_harness(tmp_path) as (guard, repo, issued, entered, start, denial, cancellation_seen):
        start('GET', get_final=True)
        assert await asyncio.wait_for(entered.get(), 2) == 'GET'
        start('GET')
        assert await asyncio.wait_for(entered.get(), 2) == 'GET'
        assert guard._total == 2 and not guard._completed_posts
        await denial('POST')


@pytest.mark.asyncio
async def test_response_guard_repeated_caller_cancel_retains_live_task(tmp_path):
    async with _response_guard_harness(tmp_path) as (guard, repo, issued, entered, start, denial, cancellation_seen):
        task = start('POST', hold_cancellation=True)
        assert await asyncio.wait_for(entered.get(), 2) == 'POST'
        task.cancel()
        await asyncio.wait_for(cancellation_seen.get(), 2)
        assert not task.done() and guard._total == 1
        assert list(guard._completed_posts.values()) == [1] and repo._listeners
        task.cancel()
        await asyncio.wait_for(cancellation_seen.get(), 2)
        assert not task.done() and guard._total == 1
        assert list(guard._completed_posts.values()) == [1] and repo._listeners
