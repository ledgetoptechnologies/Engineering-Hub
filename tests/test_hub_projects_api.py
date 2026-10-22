"""Project metadata acceptance through real Hub routes; no live services."""
import sys

import pytest

from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubError, PROJECTS
from engineering_hub.work_queue import WorkAction
from tests.test_hub_api import BASE, agent_headers, hub, human_headers
from tests.test_hub_interactive_api import call, rpc


def project_payload(**changes):
    return {'project_id': 'landscript', 'name': 'LandScript',
            'client_request_id': 'landscript-first', **changes}


def create(client, headers):
    response = client.post(BASE + '/projects', headers=headers, json=project_payload())
    assert response.status_code == 201, response.text
    return response.json()


def test_legacy_seed_is_fixed_once_and_overview_uses_registry(hub):
    client, app, settings = hub
    projects = client.get(BASE + '/projects', headers=agent_headers()).json()['items']
    assert {p['id'] for p in projects} == PROJECTS
    assert {p['id']: p['name'] for p in projects} == {
        'engineering-hub': 'Engineering Hub', '3d-viewer': '3D Viewer',
        'project-alpha': 'Project Alpha', 'ledge-top-ops': 'LedgeTop Ops', 'treedar': 'TreeDar'}
    headers = human_headers(client, app, settings)
    renamed = client.patch(BASE + '/projects/3d-viewer', headers=headers,
        json={'expected_revision': 1, 'name': 'Viewer in Operations', 'parent_project': 'ledge-top-ops'})
    assert renamed.status_code == 200, renamed.text
    ProjectRepository(app.state.repository)
    overview = client.get(BASE + '/overview').json()
    viewer = next(p for p in overview['projects'] if p['id'] == '3d-viewer')
    assert viewer['name'] == 'Viewer in Operations'
    assert viewer['parent_project'] == 'ledge-top-ops'
    assert len(overview['projects']) == 5


def test_human_project_mutations_require_csrf_origin_and_cas(hub):
    client, app, settings = hub
    assert client.post(BASE + '/projects', json=project_payload()).status_code == 401
    headers = human_headers(client, app, settings)
    bad_headers = [{}, {'Origin': settings.auth_config.origin},
                   headers | {'X-Hub-CSRF': 'incorrect'}, headers | {'Origin': 'https://evil.test'}]
    for rejected in bad_headers:
        assert client.post(BASE + '/projects', headers=rejected, json=project_payload()).status_code == 403
    item = create(client, headers)
    assert item['repository_url'] is None
    assert item['environment'] == 'notes_only' and item['executor_enabled'] is False
    assert item['health'] == 'unknown' and item['reason'] == 'metadata_only'
    assert create(client, headers) == item
    path = BASE + '/projects/landscript'
    for rejected in bad_headers:
        assert client.patch(path, headers=rejected, json={'expected_revision': 1, 'name': 'Changed'}).status_code == 403
        assert client.post(path + '/archive', headers=rejected, json={'expected_revision': 1}).status_code == 403
    updated = client.patch(path, headers=headers,
        json={'expected_revision': 1, 'description': 'Client notes, not execution permission', 'lead_agent': 'claude'})
    assert updated.status_code == 200, updated.text
    assert updated.json()['revision'] == 2
    assert client.patch(path, headers=headers, json={'expected_revision': 1, 'name': 'Stale'}).status_code == 409
    assert client.post(path + '/archive', headers=headers, json={'expected_revision': 1}).status_code == 409
    assert client.post(path + '/archive', headers=headers, json={'expected_revision': 2}).status_code == 200
    assert all(a['actor'] == 'human:42' for a in app.state.repository.list_audit())


@pytest.mark.parametrize('agent', ['codex', 'claude', 'hermes'])
def test_agents_read_mcp_but_cannot_mutate_projects_even_with_human_cookie(hub, agent):
    client, app, settings = hub
    human = human_headers(client, app, settings)
    original = create(client, human)
    mixed = human | agent_headers(agent)
    assert client.post(BASE + '/projects', headers=mixed,
        json=project_payload(project_id='unauthorized', client_request_id='other')).status_code == 403
    assert client.patch(BASE + '/projects/landscript', headers=mixed,
        json={'expected_revision': 1, 'name': 'Agent renamed'}).status_code == 403
    assert client.post(BASE + '/projects/landscript/archive', headers=mixed,
        json={'expected_revision': 1}).status_code == 403
    assert client.delete(BASE + '/projects/landscript', headers=mixed).status_code == 405
    assert call(client, 'hub_project_get', {'project_id': 'landscript'}, token='test-' + agent) == original
    assert original in call(client, 'hub_project_list', token='test-' + agent)['items']
    names = {t['name'] for t in rpc(client, 'tools/list', token='test-' + agent).json()['result']['tools']}
    assert {n for n in names if n.startswith('hub_project_')} == {'hub_project_list', 'hub_project_get'}
    assert rpc(client, 'tools/call', {'name': 'hub_project_get',
        'arguments': {'project_id': 'landscript'}}, token=None).status_code == 401


def test_new_project_tasks_decisions_and_archive_history(hub):
    client, app, settings = hub
    human = human_headers(client, app, settings)
    create(client, human)
    task = client.post(BASE + '/tasks', headers=human, json={'project': 'landscript',
        'title': 'Ownership questions', 'client_request_id': 'ownership'})
    assert task.status_code == 201, task.text
    task = task.json()
    decision = call(client, 'hub_decision_request', {'project': 'landscript', 'title': 'Next meeting?',
        'options': ['Monday', 'Tuesday']})
    assert decision['project'] == 'landscript'
    with pytest.raises(HubError, match='invalid_work_project'):
        WorkAction('staging-pc', project='landscript')
    assert client.get(BASE + '/overview').json()['capabilities']['host_dispatch'] is False
    assert client.post(BASE + '/projects/landscript/archive', headers=human,
        json={'expected_revision': 1}).status_code == 200
    assert client.get(BASE + '/tasks/' + task['id']).json() == task
    assert decision in client.get(BASE + '/decisions').json()['items']
    assert client.get(BASE + '/projects/landscript').json()['archived'] is True
    assert 'landscript' not in {p['id'] for p in call(client, 'hub_project_list')['items']}
    assert 'landscript' in {p['id'] for p in call(client, 'hub_project_list', {'include_archived': True})['items']}
    assert client.post(BASE + '/tasks', headers=human, json={'project': 'landscript',
        'title': 'Too late', 'client_request_id': 'late'}).status_code == 409
    assert client.post(BASE + '/tasks/' + task['id'] + '/claim', headers=agent_headers(),
        json={'expected_revision': task['revision']}).status_code == 409
    result = call(client, 'hub_decision_request', {'project': 'landscript', 'title': 'Too late?', 'options': ['A', 'B']})
    assert result['isError'] is True and 'project_archived' in str(result)


@pytest.mark.parametrize('identity', ['Upper', 'bad_id', '-prefix', 'a' * 65])
def test_malformed_ids_never_create_records(hub, identity):
    client, app, settings = hub
    human = human_headers(client, app, settings)
    assert client.post(BASE + '/projects', headers=human,
        json=project_payload(project_id=identity)).status_code == 422
    assert client.get(BASE + '/projects/' + identity).status_code in {400, 422}
    assert len(app.state.project_registry.list_projects()) == 5


def test_unknown_project_and_spoofed_fields_are_rejected(hub):
    client, app, settings = hub
    assert client.get(BASE + '/projects/missing', headers=agent_headers()).status_code == 404
    human = human_headers(client, app, settings)
    for field in ['actor', 'executor_enabled', 'id', 'archived', 'environment']:
        assert client.post(BASE + '/projects', headers=human,
            json=project_payload(**{field: 'spoof'})).status_code == 422
    assert len(app.state.project_registry.list_projects()) == 5


@pytest.mark.skipif(sys.platform == 'win32', reason='Real unified bus requires Linux fcntl')
def test_dynamic_project_notes_remain_readable_but_writes_stop_after_archive(tmp_path, monkeypatch):
    # Use the actual unified composition, not a parallel notes store or mocked route.
    from fastapi.testclient import TestClient
    from agent_message_bus.app import create_app as create_bus
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    from mcp.server.transport_security import TransportSecuritySettings
    monkeypatch.setenv('AGENT_BUS_ALLOWED_HOSTS', 'hub.test')
    auth = AuthSettings('test-hermes', 'test-codex', claude_token='test-claude')
    settings = Settings(tmp_path / 'hub.sqlite3', auth, AuthConfig('https://hub.test', beau_github_id=42))
    bus = create_bus(db_path=tmp_path / 'messages.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=auth, push_events_operational=False,
        transport_security_settings=TransportSecuritySettings(
            allowed_hosts=list(dict.fromkeys(['hub.test', *settings.agent_allowed_hosts])),
            allowed_origins=[settings.auth_config.origin]))
    app = create_app(settings, bus_app=bus)
    with TestClient(app, base_url=settings.auth_config.origin) as client:
        human = human_headers(client, app, settings)
        create(client, human)
        note = {'project': 'landscript', 'path': 'notes/meeting.md', 'title': 'Meeting',
                'content': 'Ownership still requires agreement.', 'expected_version': 0}
        saved = client.post(BASE + '/notes', headers=agent_headers(), json=note)
        assert saved.status_code == 200, saved.text
        assert client.post(BASE + '/projects/landscript/archive', headers=human,
            json={'expected_revision': 1}).status_code == 200
        read = client.get(BASE + '/notes/document', params={'project': 'landscript', 'path': note['path']})
        assert read.status_code == 200 and read.json() == saved.json()
        assert client.post(BASE + '/notes', headers=agent_headers(),
            json=note | {'expected_version': 1, 'content': 'Not admitted after archive'}).status_code == 409
