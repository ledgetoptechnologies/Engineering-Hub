"""Real Hub composition: catalog auth, closed browser routes and MCP discovery."""
import json

from fastapi.testclient import TestClient
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.browser_pages import client_page_allowed
from engineering_hub.private_gateway import browser_route_allowed


BASE = '/api/hub/v1'
UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


def test_composed_catalog_uses_real_sessions_csrf_and_read_only_mcp(tmp_path):
    settings = Settings(tmp_path / 'hub.sqlite3',
        AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
        AuthConfig('https://hub.test', beau_github_id=42))
    app = create_app(settings)
    path = BASE + '/projects/3d-viewer/components'
    with TestClient(app, base_url=settings.auth_config.origin) as client:
        assert client.get(path).status_code == 401
        payload = {'slug': 'pointcloud', 'name': 'Pointcloud', 'kind': 'web_app',
                   'client_request_id': 'component-integration'}
        assert client.post(path, headers={'Authorization': 'Bearer test-codex'}, json=payload).status_code == 403
        session = app.state.sessions.create_session(42)
        client.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
        assert client.post(path, json=payload).status_code == 403
        human = {'Origin': settings.auth_config.origin, 'X-Hub-CSRF': session['csrf_token']}
        response = client.post(path, headers=human, json=payload)
        assert response.status_code == 201, response.text
        item = response.json()
        assert client.get(path).json()['items'] == [item]
        assert client.get(BASE + '/components/' + item['id']).json() == item
        assert client.patch(BASE + '/components/' + item['id'], headers=human,
                            json={'expected_revision': 1, 'name': 'Reviewed pointcloud'}).status_code == 200
        assert client.post(BASE + '/components/' + item['id'] + '/retire', headers=human,
                           json={'expected_revision': 1}).status_code == 409
        client.cookies.clear()
        headers = {'Authorization': 'Bearer test-codex', 'Accept': 'application/json, text/event-stream',
                   'MCP-Protocol-Version': '2025-06-18'}
        response = client.post('/mcp/', headers=headers, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}})
        assert response.status_code == 200, response.text
        tools = {tool['name']: tool for tool in response.json()['result']['tools']}
        assert {name for name in tools if name.startswith('hub_component_')} == {'hub_component_list', 'hub_component_get'}
        assert not {'actor', 'identity', 'ctx'} & set(tools['hub_component_get']['inputSchema']['properties'])
        response = client.post('/mcp/', headers=headers, json={'jsonrpc': '2.0', 'id': 2,
            'method': 'tools/call', 'params': {'name': 'hub_component_get', 'arguments': {'component_id': item['id']}}})
        result = response.json()['result']
        assert not result.get('isError'), result
        data = result.get('structuredContent') or json.loads(result['content'][0]['text'])
        assert data['name'] == 'Reviewed pointcloud'


@pytest.mark.parametrize('method,path', [
    ('GET', '/projects/treedar/components'), ('HEAD', '/projects/treedar/components'),
    ('GET', BASE + '/projects/treedar/components'), ('POST', BASE + '/projects/treedar/components'),
    ('GET', BASE + '/components/' + UUID), ('HEAD', BASE + '/components/' + UUID),
    ('PATCH', BASE + '/components/' + UUID), ('POST', BASE + '/components/' + UUID + '/retire')])
def test_catalog_closed_browser_routes(method, path):
    assert browser_route_allowed(method, path)
    if path.startswith('/projects/'):
        assert client_page_allowed(method, path)


@pytest.mark.parametrize('method,path', [
    ('DELETE', BASE + '/components/' + UUID), ('POST', BASE + '/components/' + UUID),
    ('PUT', BASE + '/components/' + UUID), ('PATCH', BASE + '/projects/treedar/components'),
    ('GET', BASE + '/components/not-a-uuid'), ('POST', BASE + '/components/' + UUID + '/run'),
    ('GET', '/projects/treedar/components/secret'), ('GET', BASE + '/projects/INVALID/components')])
def test_catalog_does_not_broaden_browser_gateway(method, path):
    assert not browser_route_allowed(method, path)
