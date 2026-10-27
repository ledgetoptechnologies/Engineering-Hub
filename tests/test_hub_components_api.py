from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.responses import JSONResponse
from starlette.requests import Request

from engineering_hub.components import ProjectComponentRepository
from engineering_hub.components_api import (ComponentCreate, ComponentRetirement, ComponentUpdate,
    attach_component_routes, register_component_tools)
from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubError, HubRepository


BASE = '/api/hub/v1'
HUMAN = {'X-Test-Human': 'human:1', 'X-Test-CSRF': 'ok'}
AGENT = {'Authorization': 'Bearer codex'}


@pytest.fixture
def env(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    projects = ProjectRepository(repo)
    store = ProjectComponentRepository(repo)
    app = FastAPI()

    @app.exception_handler(HubError)
    async def error(request, exc):
        return JSONResponse({'code': exc.code}, status_code=exc.status)

    def actor(request, mutation=False):
        if request.headers.get('authorization') == 'Bearer codex':
            return 'codex'
        principal = request.headers.get('x-test-human')
        if principal is None or request.headers.get('authorization'):
            raise HubError('unauthorized', 401)
        repo._actor(principal, human=True)
        if mutation and request.headers.get('x-test-csrf') != 'ok':
            raise HubError('csrf_required', 403)
        return principal

    def human(request, mutation=False):
        principal = actor(request)
        repo._actor(principal, human=True)
        if mutation and request.headers.get('x-test-csrf') != 'ok':
            raise HubError('csrf_required', 403)
        return principal

    attach_component_routes(app, store, actor, human)
    return TestClient(app), store, projects


def payload(**changes):
    return {'slug': 'viewer', 'name': '3D Viewer', 'kind': 'web_app',
            'client_request_id': 'viewer-create', 'legacy_project_id': '3d-viewer', **changes}


def create(client, **changes):
    response = client.post(BASE + '/projects/ledge-top-ops/components', headers=HUMAN, json=payload(**changes))
    assert response.status_code == 201, response.text
    return response.json()


def test_human_create_agent_read_and_project_scope(env):
    client, _, _ = env
    item = create(client)
    assert client.get(BASE + '/components/' + item['id'], headers=AGENT).json() == item
    assert client.get(BASE + '/projects/ledge-top-ops/components', headers=AGENT).json() == {'items': [item], 'next_after': None}
    assert client.get(BASE + '/projects/project-alpha/components', headers=AGENT).json()['items'] == []
    assert client.get(BASE + '/projects/ledge-top-ops/components').status_code == 401


def test_csrf_and_immutable_fields(env):
    client, _, _ = env
    path = BASE + '/projects/ledge-top-ops/components'
    assert client.post(path, headers={'X-Test-Human': 'human:1'}, json=payload()).status_code == 403
    item = create(client)
    path = BASE + '/components/' + item['id']
    for headers in (HUMAN, AGENT):
        for immutable in ({'project': 'project-alpha'}, {'slug': 'other'}, {'id': item['id']}, {'created_by': 'codex'}, {'actor': 'hermes'}):
            assert client.patch(path, headers=headers, json={'expected_revision': 1, **immutable}).status_code == 422
    assert client.delete(path, headers=HUMAN).status_code == 405


def test_strict_cas_idempotency_and_retirement(env):
    client, _, _ = env
    item = create(client)
    assert create(client) == item
    assert client.post(BASE + '/projects/ledge-top-ops/components', headers=HUMAN, json=payload(name='changed')).status_code == 409
    path = BASE + '/components/' + item['id']
    response = client.patch(path, headers=HUMAN, json={'expected_revision': 1, 'status': 'active'})
    assert response.status_code == 200 and response.json()['revision'] == 2
    assert client.post(path + '/retire', headers=HUMAN, json={'expected_revision': 1}).status_code == 409
    assert client.post(path + '/retire', headers=HUMAN, json={'expected_revision': 2}).status_code == 200
    assert client.get(BASE + '/projects/ledge-top-ops/components', headers=AGENT).json()['items'] == []
    assert len(client.get(BASE + '/projects/ledge-top-ops/components?include_retired=true', headers=AGENT).json()['items']) == 1


@pytest.mark.parametrize('changes', [{'kind': 'docker'}, {'status': 'running'}, {'name': 'x' * 101},
    {'description': 'x' * 4097}, {'slug': 'Bad_slug'}, {'worker_url': 'https://example.test'},
    {'created_by': 'codex'}])
def test_closed_metadata_schema(env, changes):
    client = env[0]
    assert client.post(BASE + '/projects/ledge-top-ops/components', headers=HUMAN, json=payload(**changes)).status_code == 422


def test_safe_urls_and_archived_history(env):
    client, _, projects = env
    assert client.post(BASE + '/projects/ledge-top-ops/components', headers=HUMAN,
                       json=payload(repository_url='https://name:secret@example.test/repo')).status_code == 400
    item = create(client, repository_url='https://github.com/example/synthetic')
    projects.archive_project('human:1', 'ledge-top-ops', 1)
    assert client.get(BASE + '/components/' + item['id'], headers=AGENT).status_code == 200
    assert client.patch(BASE + '/components/' + item['id'], headers=HUMAN,
                        json={'expected_revision': 1, 'name': 'changed'}).status_code == 409


def test_typed_component_tool_surface(env):
    client, store, _ = env
    item = create(client)
    class Tools:
        def __init__(self):
            self.tools = {}
        def tool(self):
            def register(fn):
                self.tools[fn.__name__] = fn
                return fn
            return register
    tools = Tools()
    register_component_tools(tools, repository=store, identity=lambda ctx: 'codex', invoke=lambda fn: fn())
    assert set(tools.tools) == {'hub_component_list', 'hub_component_get', 'hub_component_create', 'hub_component_update', 'hub_component_retire'}
    assert tools.tools['hub_component_get'](item['id'], None) == item
    assert tools.tools['hub_component_list']('ledge-top-ops', None)['items'] == [item]

    ctx = SimpleNamespace(request_context=SimpleNamespace(request=Request({
        'type': 'http', 'headers': [(b'authorization', b'Bearer codex')]})))
    created = tools.tools['hub_component_create'](ComponentCreate(project='ledge-top-ops', **payload(slug='agent', client_request_id='agent')), ctx)
    assert created['created_by'] == 'codex'
    updated = tools.tools['hub_component_update'](ComponentUpdate(component_id=created['id'], expected_revision=1, name='Agent edit'), ctx)
    assert updated['revision'] == 2
    retired = tools.tools['hub_component_retire'](ComponentRetirement(component_id=created['id'], expected_revision=2), ctx)
    assert retired['status'] == 'retired'
    restored = tools.tools['hub_component_update'](ComponentUpdate(component_id=created['id'], expected_revision=3, status='active'), ctx)
    assert restored['revision'] == 4
