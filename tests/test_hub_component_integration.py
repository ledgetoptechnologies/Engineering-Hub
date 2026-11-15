"""Real Hub composition: authenticated component edits and human-owned projects."""
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
COMPONENT_TOOLS = {'hub_component_list', 'hub_component_get', 'hub_component_create',
                   'hub_component_update', 'hub_component_retire'}


@pytest.fixture
def composed(tmp_path):
    settings = Settings(tmp_path / 'hub.sqlite3',
        AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
        AuthConfig('https://hub.test', beau_github_id=42))
    app = create_app(settings)
    with TestClient(app, base_url=settings.auth_config.origin) as client:
        yield app, client, settings


def human_session(composed):
    app, client, settings = composed
    session = app.state.sessions.create_session(42)
    client.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
    return {'Origin': settings.auth_config.origin, 'X-Hub-CSRF': session['csrf_token']}


def payload(**changes):
    return {'slug': 'pointcloud', 'name': 'Pointcloud', 'kind': 'web_app',
            'client_request_id': 'component-integration', **changes}


def bearer(agent):
    return {'Authorization': 'Bearer test-' + agent}


def rpc(client, headers, method, params):
    return client.post('/mcp/', headers=[*(list(headers.items()) if isinstance(headers, dict) else headers),
        ('Accept', 'application/json, text/event-stream'), ('MCP-Protocol-Version', '2025-06-18')],
        json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})


def tool_result(client, headers, name, arguments, *, error=False):
    response = rpc(client, headers, 'tools/call', {'name': name, 'arguments': arguments})
    assert response.status_code == 200, response.text
    result = response.json()['result']
    assert bool(result.get('isError')) is error, result
    if error:
        return result
    return result.get('structuredContent') or json.loads(result['content'][0]['text'])


def test_composed_human_mutations_preserve_session_csrf_and_mcp_discovery(composed):
    _, client, settings = composed
    path = BASE + '/projects/3d-viewer/components'
    assert client.get(path).status_code == 401
    assert client.post(path, json=payload()).status_code == 401
    human = human_session(composed)
    assert client.post(path, json=payload()).status_code == 403
    for invalid in ({'Origin': settings.auth_config.origin},
                    {'X-Hub-CSRF': human['X-Hub-CSRF']},
                    {**human, 'Origin': 'https://other.test'},
                    {**human, 'X-Hub-CSRF': 'invalid'}):
        assert client.post(path, headers=invalid, json=payload()).status_code == 403
    response = client.post(path, headers=human, json=payload())
    assert response.status_code == 201, response.text
    item = response.json()
    detail = BASE + '/components/' + item['id']
    for method, target, body in [('PATCH', detail, {'expected_revision': 1, 'name': 'No CSRF'}),
                                 ('POST', detail + '/retire', {'expected_revision': 1})]:
        assert client.request(method, target, json=body).status_code == 403
    assert client.patch(detail, headers=human,
                        json={'expected_revision': 1, 'name': 'Reviewed pointcloud'}).status_code == 200
    assert client.post(detail + '/retire', headers=human, json={'expected_revision': 1}).status_code == 409
    assert client.post(detail + '/retire', headers=human, json={'expected_revision': 2}).status_code == 200
    assert client.patch(detail, headers=human, json={'expected_revision': 3, 'status': 'active'}).json()['revision'] == 4
    response = rpc(client, bearer('codex'), 'tools/list', {})
    assert response.status_code == 200, response.text
    tools = {tool['name']: tool for tool in response.json()['result']['tools']}
    assert {name for name in tools if name.startswith('hub_component_')} == COMPONENT_TOOLS
    assert {name for name in tools if name.startswith('hub_project_')} == {
        'hub_project_list', 'hub_project_get', 'hub_project_tasks'}
    for name in COMPONENT_TOOLS:
        schema = tools[name]['inputSchema']
        assert not {'actor', 'identity', 'ctx'} & set(schema['properties'])
        if name in {'hub_component_create', 'hub_component_update', 'hub_component_retire'}:
            assert set(schema['properties']) == {'payload'}
            nested = schema['properties']['payload']
            if '$ref' in nested:
                nested = schema['$defs'][nested['$ref'].split('/')[-1]]
            assert nested['additionalProperties'] is False
            assert not {'actor', 'identity', 'created_by'} & set(nested['properties'])
    assert tool_result(client, bearer('codex'), 'hub_component_get', {'component_id': item['id']})['name'] == 'Reviewed pointcloud'


@pytest.mark.parametrize('agent', ['codex', 'claude', 'hermes'])
def test_each_authenticated_agent_manages_http_components_but_not_projects(composed, agent):
    app, client, _ = composed
    # A valid human cookie must not replace the bearer actor or bypass its limits.
    human_session(composed)
    headers = bearer(agent)
    path = BASE + '/projects/3d-viewer/components'
    before = app.state.project_registry.list_projects()
    response = client.post(path, headers=headers, json=payload())
    assert response.status_code == 201, response.text
    item = response.json()
    assert item['created_by'] == agent and item['revision'] == 1
    assert client.post(path, headers=headers, json=payload()).json() == item
    assert client.post(path, headers=headers, json=payload(name='conflicting retry')).status_code == 409
    detail = BASE + '/components/' + item['id']
    assert client.patch(detail, headers=headers, json={'expected_revision': 1, 'name': 'Agent edit'}).json()['revision'] == 2
    assert client.post(detail + '/retire', headers=headers, json={'expected_revision': 1}).status_code == 409
    retired = client.post(detail + '/retire', headers=headers, json={'expected_revision': 2})
    assert retired.status_code == 200 and retired.json()['status'] == 'retired'
    assert client.get(path, headers=headers).json()['items'] == []
    assert client.get(path + '?include_retired=true', headers=headers).json()['items'][0]['id'] == item['id']
    restored = client.patch(detail, headers=headers, json={'expected_revision': 3, 'status': 'active'})
    assert restored.status_code == 200 and restored.json()['revision'] == 4
    assert restored.json()['created_by'] == agent and restored.json()['slug'] == item['slug']
    for fields in ({'actor': 'human:42'}, {'identity': 'hermes'}, {'created_by': 'human:42'}):
        assert client.post(path, headers=headers, json=payload(**fields)).status_code == 422
    for fields in ({'project': 'treedar'}, {'slug': 'moved'}, {'id': UUID}, {'created_by': 'human:42'}, {'actor': 'hermes'}):
        assert client.patch(detail, headers=headers, json={'expected_revision': 4, **fields}).status_code == 422
    assert client.post(BASE + '/projects', headers=headers,
                       json={'project_id': 'agent-project', 'name': 'Agent', 'client_request_id': 'agent'}).status_code == 403
    assert client.patch(BASE + '/projects/3d-viewer', headers=headers,
                        json={'expected_revision': 1, 'name': 'Agent renamed project'}).status_code == 403
    assert client.post(BASE + '/projects/3d-viewer/archive', headers=headers, json={'expected_revision': 1}).status_code == 403
    assert client.delete(BASE + '/projects/3d-viewer', headers=headers).status_code == 405
    assert app.state.project_registry.list_projects() == before
    app.state.project_registry.archive_project('human:42', '3d-viewer', 1)
    assert client.get(detail, headers=headers).status_code == 200
    for method, target, body in [('POST', path, payload()),
                                 ('PATCH', detail, {'expected_revision': 4, 'status': 'paused'}),
                                 ('POST', detail + '/retire', {'expected_revision': 4})]:
        response = client.request(method, target, headers=headers, json=body)
        assert response.status_code == 409 and response.json()['detail']['code'] == 'project_archived'
    with app.state.repository._connection() as db:
        rows = db.execute('SELECT actor,action FROM hub_audit WHERE entity_id=? ORDER BY sequence', (item['id'],)).fetchall()
        assert [tuple(row) for row in rows] == [(agent, action) for action in
            ('component.created', 'component.updated', 'component.retired', 'component.updated')]


@pytest.mark.parametrize('agent', ['codex', 'claude', 'hermes'])
def test_each_authenticated_agent_manages_typed_mcp_components(composed, agent):
    app, client, _ = composed
    headers = bearer(agent)
    create = {'project': '3d-viewer', **payload()}
    item = tool_result(client, headers, 'hub_component_create', {'payload': create})
    assert item['created_by'] == agent and item['revision'] == 1
    assert tool_result(client, headers, 'hub_component_create', {'payload': create}) == item
    tool_result(client, headers, 'hub_component_create', {'payload': {**create, 'name': 'Conflict'}}, error=True)
    edit = {'component_id': item['id'], 'expected_revision': 1, 'name': 'MCP edit'}
    assert tool_result(client, headers, 'hub_component_update', {'payload': edit})['revision'] == 2
    revision = {'component_id': item['id'], 'expected_revision': 2}
    tool_result(client, headers, 'hub_component_retire', {'payload': {**revision, 'expected_revision': 1}}, error=True)
    assert tool_result(client, headers, 'hub_component_retire', {'payload': revision})['status'] == 'retired'
    restored = tool_result(client, headers, 'hub_component_update', {'payload': {
        **revision, 'expected_revision': 3, 'status': 'active'}})
    assert restored['revision'] == 4 and restored['created_by'] == agent
    for name, body in [('hub_component_create', create), ('hub_component_update', edit), ('hub_component_retire', revision)]:
        for forbidden in ('actor', 'identity', 'created_by', 'unknown_field'):
            tool_result(client, headers, name, {'payload': {**body, forbidden: 'human:42'}}, error=True)
    for immutable in ('project', 'slug', 'id'):
        tool_result(client, headers, 'hub_component_update', {'payload': {**edit, immutable: 'treedar'}}, error=True)
    tool_result(client, headers, 'hub_component_retire', {'payload': {**revision, 'expected_revision': True}}, error=True)
    for name in ('hub_project_create', 'hub_project_archive', 'hub_project_delete'):
        tool_result(client, headers, name, {'project': '3d-viewer'}, error=True)
    app.state.project_registry.archive_project('human:42', '3d-viewer', 1)
    for name, body in [('hub_component_create', create),
                       ('hub_component_update', {**edit, 'expected_revision': 4}),
                       ('hub_component_retire', {**revision, 'expected_revision': 4})]:
        result = tool_result(client, headers, name, {'payload': body}, error=True)
        assert 'project_archived' in json.dumps(result)
    assert tool_result(client, headers, 'hub_component_get', {'component_id': item['id']}) == restored
    with app.state.repository._connection() as db:
        rows = db.execute('SELECT actor FROM hub_audit WHERE entity_id=?', (item['id'],)).fetchall()
        assert [row['actor'] for row in rows] == [agent] * 4


@pytest.mark.parametrize('authorization', ['Bearer unknown', 'Bearer', 'Bearer test-codex extra', 'Basic test-codex', '',
    [('Authorization', 'Bearer test-codex'), ('Authorization', 'Bearer test-hermes')]])
def test_invalid_or_duplicate_bearer_never_falls_back_to_human_cookie(composed, authorization):
    app, client, _ = composed
    human = human_session(composed)
    path = BASE + '/projects/3d-viewer/components'
    item = client.post(path, headers=human, json=payload()).json()
    detail = BASE + '/components/' + item['id']
    headers = authorization if isinstance(authorization, list) else [('Authorization', authorization)]
    headers = [*headers, *human.items()]
    for method, target, body in [('POST', path, payload(slug='other', client_request_id='other')),
                                 ('PATCH', detail, {'expected_revision': 1, 'name': 'Forged'}),
                                 ('POST', detail + '/retire', {'expected_revision': 1})]:
        assert client.request(method, target, headers=headers, json=body).status_code == 401
    response = rpc(client, headers, 'tools/call', {'name': 'hub_component_create',
        'arguments': {'payload': {'project': '3d-viewer', **payload(slug='other', client_request_id='other')}}})
    assert response.status_code == 401 or (response.status_code == 200 and response.json()['result'].get('isError'))
    assert client.get(detail, headers=human).json() == item
    assert client.get(path, headers=human).json()['items'] == [item]


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
