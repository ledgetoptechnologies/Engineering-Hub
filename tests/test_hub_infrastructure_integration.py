"""Actual Hub session/MCP composition with temporary operator settings only."""
import json
import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.private_gateway import browser_route_allowed


BASE = '/api/hub/v1/infrastructure'
OWNER = 'human:42'


def settings(tmp_path, *, configured=True, prometheus=False):
    config = tmp_path / 'infrastructure.json'
    if configured:
        value = {'schema_version': 1, 'enabled': True, 'principals': [
            {'kind': 'human', 'identity': OWNER,
             'capabilities': ['read', 'device_write', 'maintenance_write']},
            {'kind': 'agent', 'identity': 'hermes',
             'capabilities': ['read', 'maintenance_write', 'probe_write']}]}
        if prometheus:
            value['prometheus'] = {'address': '192.0.2.80', 'port': 30104, 'mappings': [
                {'job': 'Fixture', 'instance': '192.0.2.1:9100',
                 'device_id': 'synthetic-pc', 'probe_id': 'prometheus-node'}]}
        config.write_text(json.dumps(value))
        config.chmod(0o600)
    ui = tmp_path / 'ui'
    ui.mkdir()
    (ui / 'index.html').write_text('PACKAGED_SHELL')
    return Settings(tmp_path / 'hub.sqlite3',
        AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
        AuthConfig('https://hub.test', beau_github_id=42), ui_directory=ui,
        infrastructure_config_path=config)


def login(client, app, config):
    session = app.state.sessions.create_session(42)
    client.cookies.set(config.auth_config.cookie_name, session['cookie_token'])
    return {'Origin': config.auth_config.origin, 'X-Hub-CSRF': session['csrf_token']}


def draft():
    return {'name': 'Synthetic PC', 'hostname': 'pc.example.test', 'addresses': ['192.0.2.1'],
            'network': '192.0.2.0/24', 'vlan': None, 'role': 'Fixture',
            'retired': False, 'expected_revision': 0}


@pytest.mark.skipif(os.name != 'posix', reason='Real private SQLite/file owner composition')
def test_environment_default_path_uses_owner_and_respects_operator_disable(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_DB', str(tmp_path / 'hub.sqlite3'))
    monkeypatch.setenv('HUB_BASE_URL', 'https://hub.test')
    monkeypatch.setenv('HUB_BEAU_GITHUB_ID', '42')
    monkeypatch.setenv('HERMES_TOKEN', 'test-hermes')
    monkeypatch.setenv('CODEX_TOKEN', 'test-codex')
    monkeypatch.delenv('AGENT_BUS_DB', raising=False)
    config = Settings.from_env()
    assert config.infrastructure_config_path == tmp_path / 'infrastructure.json'
    app = create_app(config)
    with TestClient(app, base_url=config.auth_config.origin) as client:
        login(client, app, config)
        assert app.state.infrastructure.state == 'ready'
        assert client.get('/api/hub/v1/overview').json()['capabilities']['infrastructure'] is True
        assert app.state.infrastructure.prometheus_origin.url == 'http://192.168.50.80:30104'
    config.infrastructure_config_path.write_text(json.dumps({'schema_version': 1, 'enabled': False, 'principals': []}))
    config.infrastructure_config_path.chmod(0o600)
    app = create_app(config)
    with TestClient(app, base_url=config.auth_config.origin):
        assert app.state.infrastructure.state == 'disabled'
    custom = replace(config, infrastructure_config_path=tmp_path / 'custom-missing.json')
    app = create_app(custom)
    with TestClient(app, base_url=custom.auth_config.origin):
        assert app.state.infrastructure.state == 'disabled'


@pytest.mark.skipif(os.name != 'posix', reason='Real private SQLite/file owner composition')
def test_owner_routes_caps_csrf_invalidation_and_lifetime(tmp_path):
    config = settings(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url=config.auth_config.origin) as client:
        headers = login(client, app, config)
        assert app.state.infrastructure.state == 'ready'
        assert client.get('/api/hub/v1/overview').json()['capabilities']['infrastructure'] is True
        assert client.put(BASE + '/devices/synthetic-pc', json=draft()).status_code == 403
        response = client.put(BASE + '/devices/synthetic-pc', headers=headers, json=draft())
        assert response.status_code == 200, response.text
        assert client.get(BASE + '/devices').json()['items'][0]['id'] == 'synthetic-pc'
        assert client.get('/infrastructure/synthetic-pc').text == 'PACKAGED_SHELL'
        with app.state.repository._connection() as db:
            rows = db.execute("SELECT action,entity_id FROM hub_outbox WHERE action='views.invalidated'").fetchall()
        assert len(rows) == 1 and rows[0]['entity_id'] == '00000000-0000-0000-0000-000000000000'
        maintenance = {'reason': 'Planned fixture maintenance', 'expected_end': None,
                       'client_request_id': 'same-original-request'}
        first = client.post(BASE + '/devices/synthetic-pc/maintenance', headers=headers, json=maintenance)
        second = client.post(BASE + '/devices/synthetic-pc/maintenance', headers=headers, json=maintenance)
        assert first.status_code == second.status_code == 200 and first.json() == second.json()
        with app.state.repository._connection() as db:
            assert db.execute("SELECT count(*) FROM hub_outbox WHERE action='views.invalidated'").fetchone()[0] == 2
        denied = {'Authorization': 'Bearer test-codex'}
        # Internal metrics admission exposes navigation independently of registry
        # read/write grants, which remain denied below.
        assert client.get('/api/hub/v1/overview', headers=denied).json()['capabilities']['infrastructure'] is True
        for suffix in ('/devices', '/devices/synthetic-pc', '/devices/missing'):
            assert client.get(BASE + suffix, headers=denied).status_code == 403
        assert client.put(BASE + '/devices/anything', headers=denied, content='not-json').status_code == 403
        allowed = {'Authorization': 'Bearer test-hermes'}
        assert client.get(BASE + '/devices', headers=allowed).status_code == 200
        assert client.put(BASE + '/devices/anything', headers=allowed, content='not-json').status_code == 403
        malformed = [('Authorization', 'Bearer test-hermes'), ('Authorization', 'Bearer test-codex')]
        assert client.get(BASE + '/devices', headers=malformed).status_code == 401
    # Closing one complete app releases only its own inventory owner lock.
    restarted = create_app(config)
    with TestClient(restarted, base_url=config.auth_config.origin) as client:
        login(client, restarted, config)
        assert client.get(BASE + '/devices').json()['items'][0]['id'] == 'synthetic-pc'


def test_missing_optional_settings_do_not_open_inventory(tmp_path):
    config = settings(tmp_path, configured=False)
    app = create_app(config)
    with TestClient(app, base_url=config.auth_config.origin) as client:
        login(client, app, config)
        assert client.get('/api/hub/v1/overview').json()['capabilities']['infrastructure'] is False
        assert client.get(BASE + '/devices').status_code == 503
        assert not (tmp_path / 'infrastructure').exists()


@pytest.mark.skipif(os.name != 'posix', reason='Real private SQLite/file owner composition')
def test_inventory_startup_failure_keeps_owner_navigation_without_exposing_data(tmp_path):
    config = settings(tmp_path)
    directory = tmp_path / 'infrastructure'
    directory.mkdir(mode=0o700)
    empty = directory / 'inventory.sqlite3'
    empty.touch(mode=0o600)
    app = create_app(config)
    with TestClient(app, base_url=config.auth_config.origin) as client:
        login(client, app, config)
        assert app.state.infrastructure.state == 'invalid'
        assert client.get('/api/hub/v1/overview').json()['capabilities']['infrastructure'] is True
        assert client.get(BASE + '/devices').status_code == 503
        agent = {'Authorization': 'Bearer test-codex'}
        assert client.get('/api/hub/v1/overview', headers=agent).json()['capabilities']['infrastructure'] is False
        assert empty.stat().st_size == 0


def test_owner_metrics_navigation_does_not_require_inventory_read_grant(tmp_path):
    config = settings(tmp_path)
    operator = json.loads(config.infrastructure_config_path.read_text())
    operator['principals'] = []
    config.infrastructure_config_path.write_text(json.dumps(operator))
    app = create_app(config)
    with TestClient(app, base_url=config.auth_config.origin) as client:
        login(client, app, config)
        assert client.get(BASE + '/devices').status_code == 403
        assert client.get('/api/hub/v1/overview').json()['capabilities']['infrastructure']
        response = client.get(BASE + '/metrics')
        assert response.status_code == 200 and response.json()['source']['status'] == 'not_configured'


@pytest.mark.skipif(os.name != 'posix', reason='Actual private inventory and session composition')
def test_composed_metrics_use_owner_session_and_authenticated_internal_agents(tmp_path, monkeypatch):
    from engineering_hub.infrastructure_discovery_metrics import ALL_QUERIES
    from engineering_hub.infrastructure_metrics import MetricsReader
    from engineering_hub.infrastructure_prometheus import TARGET_PATH
    from urllib.parse import parse_qs, urlsplit

    config = settings(tmp_path, prometheus=True)
    operator = json.loads(config.infrastructure_config_path.read_text())
    operator['principals'][0]['capabilities'].append('metrics_read')
    operator['prometheus']['mappings'][0]['exporter'] = 'node'
    config.infrastructure_config_path.write_text(json.dumps(operator))
    calls = []
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    by_query = {query: (kind, key) for kind, key, query in ALL_QUERIES}

    async def fetch(reader, client, path, budget):
        calls.append(path)
        if path == TARGET_PATH:
            return json.dumps({'status': 'success', 'data': {'activeTargets': [{
                'labels': {'job': 'Fixture', 'instance': '192.0.2.1:9100'},
                'health': 'up', 'lastScrape': stamp, 'scrapeInterval': '1m'}]}}).encode()
        query = parse_qs(urlsplit(path).query)['query'][0]
        kind, key = by_query[query]
        value = {'cpu': '0.25', 'mem_total': '1024', 'mem_available': '256', 'uptime': '42'}.get(key) if kind == 'node' else None
        series = [] if value is None else [{'metric': {'job': 'Fixture', 'instance': '192.0.2.1:9100'},
                                          'value': [0, value]}]
        return json.dumps({'status': 'success', 'data': {'resultType': 'vector', 'result': series}}).encode()

    # Only synthetic transport is replaced: composition, session auth and capability checks are real.
    monkeypatch.setattr(MetricsReader, '_get', fetch)
    app = create_app(config)
    with TestClient(app, base_url=config.auth_config.origin) as client:
        assert app.state.infrastructure.metrics is not None and calls == []
        assert client.get(BASE + '/metrics').status_code == 401
        assert client.get(BASE + '/metrics', headers={'Authorization': 'Bearer wrong'}).status_code == 401
        assert calls == []
        assert client.get(BASE + '/metrics', headers={'Authorization': 'Bearer test-hermes'}).status_code == 200
        assert len(calls) == 1 + len(by_query)
        calls.clear()
        login(client, app, config)
        response = client.get(BASE + '/metrics')
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['schema_version'] == 3
        assert result['source'] == {'status': 'ok', 'truncated': False, 'omitted_count': 0, 'warnings': []}
        target = result['devices'][0]
        assert target['cpu_busy_ratio'] == 0.25 and target['mem_total_bytes'] == 1024
        assert target['mem_available_bytes'] == 256 and target['up'] is True
        assert len(calls) == 1 + len(by_query)
        before = len(calls)
        assert client.get(BASE + '/metrics?query=up').status_code == 400
        assert len(calls) == before


@pytest.mark.skipif(os.name != 'posix', reason='Real inventory owner composition')
def test_real_mcp_uses_authenticated_agent_capabilities(tmp_path):
    config = settings(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url=config.auth_config.origin) as client:
        headers = login(client, app, config)
        assert client.put(BASE + '/devices/synthetic-pc', headers=headers, json=draft()).status_code == 200

        def rpc(method, params, token):
            response = client.post('/mcp/', headers={
                'Authorization': 'Bearer ' + token,
                'Accept': 'application/json, text/event-stream',
                'MCP-Protocol-Version': '2025-06-18'},
                json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
            assert response.status_code == 200, response.text
            result = response.json()['result']
            if method == 'tools/call' and not result.get('isError') and 'structuredContent' not in result:
                result['structuredContent'] = json.loads(result['content'][0]['text'])
            return result

        tools = rpc('tools/list', {}, 'test-hermes')['tools']
        inventory = {tool['name']: tool for tool in tools if tool['name'].startswith('hub_infrastructure_')}
        assert set(inventory) == {'hub_infrastructure_list', 'hub_infrastructure_get',
            'hub_infrastructure_maintenance_open', 'hub_infrastructure_maintenance_resolve',
            'hub_infrastructure_probe_record', 'hub_infrastructure_metrics'}
        assert all(not {'actor', 'identity', 'principal'} & set(tool['inputSchema'].get('properties', {}))
                   for tool in inventory.values())
        allowed = rpc('tools/call', {'name': 'hub_infrastructure_list', 'arguments': {}}, 'test-hermes')
        assert not allowed.get('isError'), allowed
        assert allowed['structuredContent']['items'][0]['id'] == 'synthetic-pc'
        denied = rpc('tools/call', {'name': 'hub_infrastructure_get',
                     'arguments': {'device_id': 'synthetic-pc'}}, 'test-codex')
        assert denied['isError'] is True
        assert 'Synthetic PC' not in str(denied)
        result = rpc('tools/call', {'name': 'hub_infrastructure_maintenance_open', 'arguments': {
            'device_id': 'synthetic-pc', 'payload': {'reason': 'Fixture agent maintenance',
            'expected_end': None, 'client_request_id': 'mcp-maintenance'}}}, 'test-hermes')
        assert result['structuredContent']['opened_by'] == {'kind': 'agent', 'identity': 'hermes'}


@pytest.mark.skipif(os.name != 'posix', reason='Real inventory owner composition')
def test_configured_mcp_refresh_checks_grants_and_current_mapping_before_fetch(tmp_path, monkeypatch):
    from engineering_hub.infrastructure_prometheus import PrometheusCollector
    calls = []
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    async def fetch(collector):
        calls.append(collector.origin.address)
        return json.dumps({'status': 'success', 'data': {'activeTargets': [{
            'labels': {'job': 'Fixture', 'instance': '192.0.2.1:9100'},
            'health': 'up', 'lastScrape': stamp, 'scrapeInterval': '1m'}]}}).encode()
    monkeypatch.setattr(PrometheusCollector, '_fetch', fetch)
    config = settings(tmp_path, prometheus=True)
    app = create_app(config)
    with TestClient(app, base_url=config.auth_config.origin) as client:
        assert app.state.infrastructure.collector is not None
        assert calls == []  # Configuring/starting Hub must not fetch or start a collector loop.
        headers = login(client, app, config)
        assert client.put(BASE + '/devices/synthetic-pc', headers=headers, json=draft()).status_code == 200
        def refresh(token):
            response = client.post('/mcp/', headers={'Authorization': 'Bearer ' + token,
                'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'},
                json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                      'params': {'name': 'hub_infrastructure_refresh', 'arguments': {}}})
            assert response.status_code == 200
            result = response.json()['result']
            return result if result.get('isError') else json.loads(result['content'][0]['text'])
        assert refresh('test-codex')['isError'] is True and calls == []
        assert refresh('test-hermes')['recorded'] == 1
        assert refresh('test-hermes')['unchanged'] == 1
        assert calls == ['192.0.2.80', '192.0.2.80']
        device = client.get(BASE + '/devices/synthetic-pc').json()['device']
        assert device['observations'][0]['state'] == 'ok'
        changed = {**draft(), 'addresses': ['192.0.2.2'], 'expected_revision': 1}
        assert client.put(BASE + '/devices/synthetic-pc', headers=headers, json=changed).status_code == 200
        assert refresh('test-hermes')['isError'] is True
        assert len(calls) == 2


@pytest.mark.skipif(os.name != 'posix', reason='Real inventory owner lock')
def test_transport_construction_failure_releases_inventory_owner(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP
    from engineering_hub import infrastructure_module
    config = settings(tmp_path)
    compose = infrastructure_module.compose_infrastructure
    captured = []
    def capture(*args, **kwargs):
        module = compose(*args, **kwargs)
        captured.append((module, module.service.registry.db))
        return module
    with monkeypatch.context() as patch:
        patch.setattr(infrastructure_module, 'compose_infrastructure', capture)
        def broken(*args, **kwargs):
            raise RuntimeError('injected transport construction failure')
        patch.setattr(FastMCP, 'streamable_http_app', broken)
        with pytest.raises(RuntimeError, match='injected transport'):
            create_app(config)
    assert captured[0][0].service is None
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0][1].execute('SELECT 1')
    with TestClient(create_app(config), base_url=config.auth_config.origin) as client:
        assert client.get('/health').status_code == 200


@pytest.mark.parametrize('method,path,expected', [
    ('GET', '/infrastructure', True), ('HEAD', '/infrastructure/synthetic-pc', True),
    ('GET', BASE + '/devices', True), ('GET', BASE + '/devices/synthetic-pc', True),
    ('GET', BASE + '/metrics', True), ('HEAD', BASE + '/metrics', True),
    ('POST', BASE + '/metrics', False), ('PUT', BASE + '/metrics', False),
    ('DELETE', BASE + '/metrics', False), ('GET', BASE + '/metrics/query', False),
    ('PUT', BASE + '/devices/synthetic-pc', True),
    ('POST', BASE + '/devices/synthetic-pc/maintenance', True),
    ('POST', BASE + '/maintenance/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/resolve', True),
    ('POST', BASE + '/devices', False), ('DELETE', BASE + '/devices/synthetic-pc', False),
    ('GET', BASE + '/probes', False), ('GET', '/infrastructure/../settings.json', False),
    ('GET', '/infrastructure/synthetic-pc/extra', False)])
def test_gateway_surface_is_closed(method, path, expected):
    assert browser_route_allowed(method, path) is expected


@pytest.mark.skipif(os.name != 'posix', reason='Real Inventory and agent-session ownership')
@pytest.mark.asyncio
@pytest.mark.parametrize('operator_grant', ['absent', 'omitted', 'read_only', 'granted'])
async def test_cloudflare_inventory_owner_override_through_signed_gateways(tmp_path, monkeypatch, operator_grant):
    import base64
    import hashlib
    import time
    from urllib.parse import parse_qs, urlsplit

    import httpx
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from mcp.server.transport_security import TransportSecuritySettings

    from agent_message_bus.agent_sessions_config import AgentSessionsConfig
    from agent_message_bus.app import create_app as create_bus
    from engineering_hub import cloudflare_access as access
    from engineering_hub.infrastructure_discovery_metrics import ALL_QUERIES
    from engineering_hub.infrastructure_metrics import MetricsReader
    from engineering_hub.infrastructure_prometheus import TARGET_PATH
    from engineering_hub.private_gateway import PrivateBrowserGateway, PublicBrowserGateway

    origin = 'https://hub.test'
    issuer = 'https://fixture.cloudflareaccess.com'
    audience = 'fixture-infrastructure'
    owner_email = 'owner@example.test'
    project_email = 'project@example.test'
    agent_owner_email = 'agent-owner@example.test'
    _cloudflare_environment(monkeypatch, tmp_path)
    monkeypatch.setenv('HUB_CF_ACCESS_ALLOWED_EMAILS', ','.join((owner_email, project_email, agent_owner_email)))

    def cloudflare_actor(subject):
        binding = '\0'.join((issuer, audience, subject))
        return 'human:cf:' + hashlib.sha256(binding.encode()).hexdigest()

    browser_owner = cloudflare_actor('inventory-owner')
    session_owner = cloudflare_actor('legacy-agent-owner')
    monkeypatch.setenv('HUB_INFRASTRUCTURE_OWNER', browser_owner)
    agent_auth = AuthSettings('test-hermes', 'test-codex', claude_token='test-claude',
                              allowed_ips=frozenset({'127.0.0.1'}))
    settings = replace(Settings.from_env(), agent_auth=agent_auth)
    if operator_grant != 'absent':
        assert settings.infrastructure_config_path is not None
        grants = {
            'omitted': [(session_owner, ['read'])],
            'read_only': [(browser_owner, ['read'])],
            'granted': [(browser_owner, ['read', 'metrics_read', 'device_write'])],
        }[operator_grant]
        settings.infrastructure_config_path.write_text(json.dumps({
            'schema_version': 1, 'enabled': True,
            'principals': [{'kind': 'human', 'identity': identity, 'capabilities': capabilities}
                           for identity, capabilities in grants],
            'prometheus': {'address': '192.0.2.20', 'port': 9090},
        }))
        settings.infrastructure_config_path.chmod(0o600)
    session_config = AgentSessionsConfig(tmp_path / 'agent-sessions.sqlite3', session_owner,
                                         origin, environment='staging')
    transport_security = TransportSecuritySettings(allowed_hosts=['hub.test'],
                                                    allowed_origins=[origin])
    bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=agent_auth, startup_cleanup=False,
        push_events_operational=False, transport_security_settings=transport_security,
        agent_sessions_config=session_config)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from jwt.algorithms import RSAAlgorithm
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update(kid='fixture-key', alg='RS256', use='sig')
    def signing_keys(request):
        assert str(request.url) == issuer + '/cdn-cgi/access/certs'
        assert 'authorization' not in request.headers
        return httpx.Response(200, json={'keys': [jwk]})
    cloudflare_config = settings.auth_config.cloudflare_config
    assert cloudflare_config is not None
    verifier = access.CloudflareAccessVerifier(cloudflare_config,
        transport=httpx.MockTransport(signing_keys))
    monkeypatch.setattr(access, 'CloudflareAccessVerifier', lambda _: verifier)
    monkeypatch.setattr('engineering_hub.ingress.CloudflareAccessVerifier', lambda _: verifier)
    app = create_app(settings, bus_app=bus)
    assert bus.state.agent_sessions.owner == session_owner
    assert app.state.infrastructure.state == 'ready'
    assert app.state.installation_scopes.sessions.owner == session_owner

    def assertion(subject, email):
        now = int(time.time())
        return jwt.encode({'iss': issuer, 'aud': [audience], 'sub': subject,
            'email': email, 'type': 'app', 'iat': now - 1, 'exp': now + 300},
            private_key, algorithm='RS256', headers={'kid': 'fixture-key'})

    def installation_token(name, kind):
        credential = 'ehi_' + base64.urlsafe_b64encode(hashlib.sha256(name.encode()).digest()).decode().rstrip('=')
        row = bus.state.agent_sessions.request_enrollment('codex', name, 'synthetic-host',
            hashlib.sha256(credential.encode()).hexdigest())
        bus.state.agent_sessions.approve(session_owner, row['id'], row['revision'], until_revoked=True)
        app.state.installation_scopes.set(session_owner, row['id'], 0, kind)
        return bus.state.agent_sessions.exchange(credential)['token']

    internal_token = installation_token('internal-metrics-fixture', 'internal')
    project_only_token = installation_token('project-metrics-fixture', 'project_only')
    calls = []
    stamp = datetime.now(timezone.utc).isoformat()
    by_query = {query: (kind, key) for kind, key, query in ALL_QUERIES}
    async def synthetic_prometheus(reader, client, path, budget):
        calls.append(path)
        if path == TARGET_PATH:
            return json.dumps({'status': 'success', 'data': {'activeTargets': [{
                'labels': {'job': 'node', 'instance': '192.0.2.1:9100', 'host': '192.0.2.1'},
                'health': 'up', 'lastScrape': stamp, 'scrapeInterval': '1m'}]}}).encode()
        query = parse_qs(urlsplit(path).query)['query'][0]
        kind, key = by_query[query]
        value = {'cpu': '0.25', 'mem_total': '1024', 'mem_available': '256', 'uptime': '42'}.get(key) if kind == 'node' else None
        series = [] if value is None else [{'metric': {'job': 'node', 'instance': '192.0.2.1:9100'},
                                            'value': [0, value]}]
        return json.dumps({'status': 'success', 'data': {'resultType': 'vector', 'result': series}}).encode()
    monkeypatch.setattr(MetricsReader, '_get', synthetic_prometheus)

    async with app.router.lifespan_context(app):
        private = PrivateBrowserGateway(app, agent_auth=agent_auth,
                                        cloudflare_config=cloudflare_config)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=private), base_url=origin) as private_client:
            gateway = PublicBrowserGateway(private_client)

            async def sign_in(subject: str, email: str) -> tuple[httpx.AsyncClient, dict]:
                client = httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url=origin,
                    headers={'Cf-Access-Jwt-Assertion': assertion(subject, email)}, follow_redirects=False)
                boot = await client.post('/api/hub/v1/auth/bootstrap', headers={'Origin': origin})
                assert boot.status_code == 200, boot.text
                me = await client.get('/api/hub/v1/auth/me')
                assert me.status_code == 200 and me.json()['authenticated'] is True
                return client, me.json()

            owner, owner_me = await sign_in('inventory-owner', owner_email)
            assert owner_me['actor'] == browser_owner
            owner_headers = {'Origin': origin, 'X-Hub-CSRF': owner_me['csrf_token']}
            overview = await owner.get('/api/hub/v1/overview')
            assert overview.status_code == 200
            if operator_grant in {'omitted', 'read_only'}:
                assert overview.json()['capabilities']['infrastructure'] is (operator_grant == 'read_only')
                for path in ('metrics', 'metrics/history'):
                    denied = await owner.get('/api/hub/v1/infrastructure/' + path)
                    assert denied.status_code == 403, denied.text
                assert calls == []
                await owner.aclose()
                return
            assert overview.json()['capabilities']['infrastructure'] is True
            saved = await owner.put('/api/hub/v1/infrastructure/devices/synthetic-pc', headers=owner_headers,
                json={'name': 'Synthetic PC', 'hostname': 'pc.example.test', 'metrics_host': '192.0.2.1',
                      'addresses': ['192.0.2.1'], 'network': '192.0.2.0/24', 'vlan': None,
                      'role': 'Fixture', 'retired': False, 'expected_revision': 0})
            assert saved.status_code == 200, saved.text
            metrics = await owner.get('/api/hub/v1/infrastructure/metrics')
            assert metrics.status_code == 200, metrics.text
            assert metrics.json()['schema_version'] == 3
            assert metrics.json()['source']['status'] == 'ok'
            assert metrics.json()['devices'][0]['device_id'] == 'synthetic-pc'
            assert metrics.json()['devices'][0]['cpu_busy_ratio'] == 0.25
            assert len(calls) == 1 + len(by_query)

            # A future project-only Access user and the prior agent-session
            # owner are authenticated humans, but neither is the Inventory owner.
            project_only, project_me = await sign_in('future-project-user', project_email)
            old_owner, old_owner_me = await sign_in('legacy-agent-owner', agent_owner_email)
            assert project_me['actor'] != browser_owner and old_owner_me['actor'] == session_owner
            for client in (project_only, old_owner):
                projects = await client.get('/api/hub/v1/projects')
                other_overview = await client.get('/api/hub/v1/overview')
                other_metrics = await client.get('/api/hub/v1/infrastructure/metrics')
                assert projects.status_code == 200
                assert other_overview.json()['capabilities']['infrastructure'] is False
                assert other_metrics.status_code == 403
                await client.aclose()

            # The override is browser-only: legacy family bearers and the old
            # session owner's explicitly classified installations retain their authority.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as agents:
                assert (await agents.get('/api/hub/v1/infrastructure/metrics',
                    headers={'Authorization': 'Bearer test-hermes'})).status_code == 200
                assert (await agents.get('/api/hub/v1/infrastructure/metrics',
                    headers={'Authorization': 'Bearer ' + internal_token})).status_code == 200
                assert (await agents.get('/api/hub/v1/infrastructure/metrics',
                    headers={'Authorization': 'Bearer ' + project_only_token})).status_code == 403
            await owner.aclose()

    with sqlite3.connect(session_config.database) as db:
        assert db.execute('SELECT owner FROM session_owner WHERE singleton=1').fetchone()[0] == session_owner
    # Reopening the same persisted session ledger remains pinned to its original owner.
    restarted_bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=agent_auth, startup_cleanup=False,
        push_events_operational=False, transport_security_settings=transport_security,
        agent_sessions_config=session_config)
    assert restarted_bus.state.agent_sessions.owner == session_owner

    # Invalid operator config leaves recovery navigation with only the explicit
    # browser Inventory owner, without opening data or changing agent ownership.
    recovery_root = tmp_path / 'recovery'
    recovery_root.mkdir(mode=0o700)
    recovery_config = recovery_root / 'infrastructure.json'
    recovery_config.write_bytes(b'not-json')
    recovery_config.chmod(0o600)
    recovery_settings = replace(settings, database=recovery_root / 'hub.sqlite3',
        infrastructure_config_path=recovery_config, infrastructure_default_path=recovery_config,
        staging_config_path=recovery_root / 'staging-service.json')
    recovery_bus = create_bus(db_path=recovery_root / 'bus.sqlite3',
        audit_path=recovery_root / 'audit.jsonl', file_root=recovery_root / 'files',
        auth=agent_auth, startup_cleanup=False, push_events_operational=False,
        transport_security_settings=transport_security, agent_sessions_config=session_config)
    recovery_app = create_app(recovery_settings, bus_app=recovery_bus)
    assert recovery_app.state.infrastructure.state == 'invalid'
    async with recovery_app.router.lifespan_context(recovery_app):
        private = PrivateBrowserGateway(recovery_app, agent_auth=agent_auth,
                                        cloudflare_config=cloudflare_config)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=private), base_url=origin) as private_client:
            gateway = PublicBrowserGateway(private_client)
            async def recovery_sign_in(subject: str, email: str) -> httpx.AsyncClient:
                client = httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url=origin,
                    headers={'Cf-Access-Jwt-Assertion': assertion(subject, email)}, follow_redirects=False)
                boot = await client.post('/api/hub/v1/auth/bootstrap', headers={'Origin': origin})
                assert boot.status_code == 200, boot.text
                return client

            recovery_owner = await recovery_sign_in('inventory-owner', owner_email)
            recovery_metrics = await recovery_owner.get('/api/hub/v1/infrastructure/metrics')
            recovery_overview = await recovery_owner.get('/api/hub/v1/overview')
            assert recovery_overview.json()['capabilities']['infrastructure'] is True
            assert recovery_metrics.status_code == 503
            old_session_owner = await recovery_sign_in('legacy-agent-owner', agent_owner_email)
            assert (await old_session_owner.get('/api/hub/v1/overview')).json()['capabilities']['infrastructure'] is False
            await recovery_owner.aclose()
            await old_session_owner.aclose()
    assert not (recovery_root / 'infrastructure').exists()


def _cloudflare_environment(monkeypatch, tmp_path):
    monkeypatch.setenv('HUB_DB', str(tmp_path / 'hub.sqlite3'))
    monkeypatch.setenv('HUB_BASE_URL', 'https://hub.test')
    monkeypatch.setenv('HUB_HUMAN_AUTH_MODE', 'cloudflare_access')
    monkeypatch.setenv('HUB_CF_ACCESS_ISSUER', 'https://fixture.cloudflareaccess.com')
    monkeypatch.setenv('HUB_CF_ACCESS_AUDIENCE', 'fixture-infrastructure')
    monkeypatch.setenv('HUB_CF_ACCESS_ALLOWED_EMAILS', 'owner@example.test,project@example.test')
    monkeypatch.setenv('HERMES_TOKEN', 'test-hermes')
    monkeypatch.setenv('CODEX_TOKEN', 'test-codex')
    monkeypatch.setenv('CLAUDE_TOKEN', 'test-claude')
    monkeypatch.delenv('AGENT_BUS_DB', raising=False)


def test_settings_reads_exact_cloudflare_infrastructure_owner(monkeypatch, tmp_path):
    _cloudflare_environment(monkeypatch, tmp_path)
    owner = 'human:cf:' + 'a' * 64
    monkeypatch.setenv('HUB_INFRASTRUCTURE_OWNER', owner)

    config = Settings.from_env()

    assert getattr(config, 'infrastructure_browser_owner', None) == owner
    with pytest.raises(ValueError, match='HUB_INFRASTRUCTURE_OWNER'):
        replace(config, infrastructure_browser_owner='human:42')


def test_settings_without_override_preserves_existing_selection(monkeypatch, tmp_path):
    _cloudflare_environment(monkeypatch, tmp_path)
    monkeypatch.delenv('HUB_INFRASTRUCTURE_OWNER', raising=False)

    config = Settings.from_env()

    assert config.infrastructure_browser_owner is None


@pytest.mark.parametrize('owner', [
    '', 'owner@example.test', 'human:42', 'human:cf:abc',
    'human:cf:' + 'A' * 64, 'human:cf:' + 'a' * 64 + ' ',
])
def test_settings_rejects_malformed_cloudflare_infrastructure_owner(monkeypatch, tmp_path, owner):
    _cloudflare_environment(monkeypatch, tmp_path)
    monkeypatch.setenv('HUB_INFRASTRUCTURE_OWNER', owner)

    with pytest.raises(ValueError, match='HUB_INFRASTRUCTURE_OWNER'):
        Settings.from_env()


def test_settings_rejects_cloudflare_infrastructure_owner_in_github_mode(monkeypatch, tmp_path):
    _cloudflare_environment(monkeypatch, tmp_path)
    monkeypatch.setenv('HUB_HUMAN_AUTH_MODE', 'github')
    monkeypatch.delenv('HUB_CF_ACCESS_ISSUER', raising=False)
    monkeypatch.delenv('HUB_CF_ACCESS_AUDIENCE', raising=False)
    monkeypatch.delenv('HUB_CF_ACCESS_ALLOWED_EMAILS', raising=False)
    monkeypatch.setenv('HUB_INFRASTRUCTURE_OWNER', 'human:cf:' + 'a' * 64)

    with pytest.raises(ValueError, match='HUB_INFRASTRUCTURE_OWNER'):
        Settings.from_env()
