"""Actual Hub session/MCP composition with temporary operator settings only."""
import json
import os
import sqlite3

from fastapi.testclient import TestClient
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.private_gateway import browser_route_allowed


BASE = '/api/hub/v1/infrastructure'
OWNER = 'human:42'


def settings(tmp_path, *, configured=True):
    config = tmp_path / 'infrastructure.json'
    if configured:
        config.write_text(json.dumps({'schema_version': 1, 'enabled': True, 'principals': [
            {'kind': 'human', 'identity': OWNER,
             'capabilities': ['read', 'device_write', 'maintenance_write']},
            {'kind': 'agent', 'identity': 'hermes',
             'capabilities': ['read', 'maintenance_write', 'probe_write']}]}))
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
        assert client.get('/api/hub/v1/overview', headers=denied).json()['capabilities']['infrastructure'] is False
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
            'hub_infrastructure_probe_record'}
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
    ('PUT', BASE + '/devices/synthetic-pc', True),
    ('POST', BASE + '/devices/synthetic-pc/maintenance', True),
    ('POST', BASE + '/maintenance/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/resolve', True),
    ('POST', BASE + '/devices', False), ('DELETE', BASE + '/devices/synthetic-pc', False),
    ('GET', BASE + '/probes', False), ('GET', '/infrastructure/../settings.json', False),
    ('GET', '/infrastructure/synthetic-pc/extra', False)])
def test_gateway_surface_is_closed(method, path, expected):
    assert browser_route_allowed(method, path) is expected
