"""Notes reuse the original MCP store, auth and compare-and-swap rules."""
import pytest
from fastapi.testclient import TestClient

from agent_message_bus.knowledge import KnowledgeRepository
from engineering_hub.app import create_app
from engineering_hub.private_gateway import browser_route_allowed
from tests.test_hub_api import hub, agent_headers, human_headers
from tests.test_hub_unified_mcp import composed, initialize, content, AUTH, BASE
from tests.test_hub_cloudflare_auth import hub as cf_hub, sign_in

API = '/api/hub/v1/notes'
DOCUMENT = {'project': 'engineering-hub', 'path': 'notes/planning.md', 'title': 'Planning',
            'content': 'Track blockers and next steps, not approval authority.', 'expected_version': 0}


def test_unavailable_store_is_not_an_empty_or_parallel_store(hub):
    client, app, settings = hub
    assert client.get(API, params={'project': 'engineering-hub'}).status_code == 401
    assert client.get(API, params={'project': 'engineering-hub'}, headers=agent_headers()).status_code == 503
    headers = human_headers(client, app, settings)
    assert client.post(API, headers=headers, json=DOCUMENT).status_code == 503
    with app.state.repository._connection() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='knowledge'").fetchone() is None


def test_original_mcp_and_notes_share_cas_and_durable_content(composed):
    core, bus, settings = composed
    with TestClient(core, base_url=BASE) as client:
        session = initialize(client)
        saved = content(client, session, 'knowledge_put', DOCUMENT)
        read = client.get(API + '/document', params={'project': DOCUMENT['project'], 'path': DOCUMENT['path']},
                          headers={'Authorization': 'Bearer ' + AUTH.claude_token})
        assert read.status_code == 200 and read.json() == saved
        updated = client.post(API, headers={'Authorization': 'Bearer ' + AUTH.hermes_token},
                              json={**DOCUMENT, 'expected_version': 1, 'content': 'Updated tracking'})
        assert updated.status_code == 200
        assert updated.json()['updated_by'] == 'hermes'
        assert updated.json()['version'] == 2
        assert content(client, session, 'knowledge_get', {
            'project': DOCUMENT['project'], 'path': DOCUMENT['path']})['content'] == 'Updated tracking'
        conflict = client.post(API, headers={'Authorization': 'Bearer ' + AUTH.codex_token}, json=DOCUMENT)
        assert conflict.status_code == 409
        assert conflict.json()['detail'] == {'code': 'notes_version_conflict', 'current_version': 2}
        assert KnowledgeRepository(bus.state.service.repository.path).get(
            DOCUMENT['project'], DOCUMENT['path']).version == 2


def test_lists_search_prefix_and_limits_do_not_dump_content(composed):
    core, bus, _ = composed
    repo = KnowledgeRepository(bus.state.service.repository.path)
    for index in range(3):
        repo.put(project='engineering-hub', path=f'notes/{index}.md', title=f'Plan {index}',
                 content='searchable tracking text', author='codex', expected_version=0)
    repo.put(project='treedar', path='notes/private.md', title='Other project', content='searchable',
             author='claude', expected_version=0)
    with TestClient(core, base_url=BASE) as client:
        headers = {'Authorization': 'Bearer ' + AUTH.codex_token}
        result = client.get(API, headers=headers, params={'project': 'engineering-hub', 'limit': 2}).json()
        assert len(result['items']) == 2 and result['truncated'] is True
        assert all('content' not in item and item['project'] == 'engineering-hub' for item in result['items'])
        result = client.get(API, headers=headers, params={'project': 'engineering-hub', 'query': 'searchable'}).json()
        assert len(result['items']) == 3 and result['truncated'] is False
        assert len(client.get(API, headers=headers, params={
            'project': 'engineering-hub', 'path_prefix': 'notes'}).json()['items']) == 3
        assert client.get(API, headers=headers, params={
            'project': 'engineering-hub', 'query': 'x', 'path_prefix': 'notes'}).status_code == 422


@pytest.mark.parametrize('patch', [{'author': 'claude'}, {'expected_version': True},
                                  {'path': '../escape'}, {'content': '\x00'},
                                  {'content': 'é' * 40000}, {'project': '../invalid'}])
def test_invalid_or_spoofed_writes_rejected(composed, patch):
    core, _, _ = composed
    with TestClient(core, base_url=BASE) as client:
        response = client.post(API, headers={'Authorization': 'Bearer ' + AUTH.codex_token},
                               json={**DOCUMENT, **patch})
        assert response.status_code == 422


def test_missing_invalid_auth_and_unknown_document(composed):
    core, _, _ = composed
    with TestClient(core, base_url=BASE) as client:
        for headers in ({}, {'Authorization': 'Bearer wrong'}):
            assert client.get(API, params={'project': 'engineering-hub'}, headers=headers).status_code == 401
            assert client.post(API, json=DOCUMENT, headers=headers).status_code == 401
        assert client.get(API + '/document', params={'project': 'engineering-hub', 'path': 'missing.md'},
                          headers={'Authorization': 'Bearer ' + AUTH.codex_token}).status_code == 404


def test_private_gateway_exposes_only_notes_operations():
    for method, path in [('GET', API), ('HEAD', API), ('GET', API + '/document'), ('POST', API)]:
        assert browser_route_allowed(method, path)
    for method, path in [('PUT', API), ('DELETE', API), ('POST', API + '/document'), ('GET', API + '/other')]:
        assert not browser_route_allowed(method, path)


def test_cloudflare_human_can_save_with_full_identity_but_not_without_csrf(cf_hub, tmp_path):
    from agent_message_bus.app import create_app as create_bus
    from mcp.server.transport_security import TransportSecuritySettings
    _, fixture_app, config, assertion, _ = cf_hub
    settings = fixture_app.state.settings
    bus = create_bus(db_path=tmp_path / 'notes-bus.sqlite3', audit_path=tmp_path / 'notes-audit.jsonl',
                     file_root=tmp_path / 'files', auth=settings.agent_auth,
                     push_events_operational=False, transport_security_settings=TransportSecuritySettings(
                         allowed_hosts=['hub.example'], allowed_origins=[config.origin]))
    core = create_app(settings, bus_app=bus)
    with TestClient(core, base_url=config.origin, follow_redirects=False) as client:
        headers, _ = sign_in(client, assertion())
        for override in ({'X-Hub-CSRF': ''}, {'X-Hub-CSRF': 'wrong'}, {'Origin': 'https://wrong.example'}):
            assert client.post(API, headers={**headers, **override}, json=DOCUMENT).status_code == 403
        saved = client.post(API, headers=headers, json=DOCUMENT)
        assert saved.status_code == 200, saved.text
        assert saved.json()['updated_by'].startswith('human:cf:')
        assert len(saved.json()['updated_by']) == 73
        read = client.get(API + '/document', headers=headers,
                          params={'project': DOCUMENT['project'], 'path': DOCUMENT['path']})
        assert read.json() == saved.json()
        assert KnowledgeRepository(bus.state.repository.path).get(
            DOCUMENT['project'], DOCUMENT['path']).updated_by == saved.json()['updated_by']
