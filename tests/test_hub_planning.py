"""Advisory planning shares knowledge durability, identities and CAS only."""
import json

import pytest
from fastapi.testclient import TestClient

from agent_message_bus.knowledge import (KnowledgeRepository, KnowledgeConflict,
                                         KnowledgeNotFound, KnowledgeError)
from engineering_hub import planning
from tests.test_hub_unified_mcp import composed, AUTH, BASE
from tests.test_hub_cloudflare_auth import hub as cf_hub, sign_in
from tests.test_hub_api import hub, agent_headers


API = '/api/hub/v1/planning'
ITEM = dict(scope='topic', project='treedar', topic='TreeDAR:pilot-v1', kind='plan',
    item_id='baseline-study', title='Compare a small representative dataset',
    content='Proposal only; no permission to start processing.', status='proposed', expected_version=0)


@pytest.fixture
def repo(tmp_path):
    return KnowledgeRepository(tmp_path / 'knowledge.sqlite3')


def test_store_roundtrip_is_durable_shared_and_advisory(repo):
    item = planning.put_item(repo, author='codex', **ITEM)
    assert item['path'] == 'planning/topics/TreeDAR:pilot-v1/plan/baseline-study.json'
    assert item['created_by'] == item['updated_by'] == 'codex'
    assert item['advisory'] is True and item['execution_authorization'] is False
    entry = repo.get(ITEM['project'], item['path'])
    envelope = json.loads(entry.content)
    assert envelope['schema'] == planning.SCHEMA
    assert envelope['topic'] == ITEM['topic'] and envelope['content'] == ITEM['content']
    assert 'author' not in envelope and 'expected_version' not in envelope
    reopened = KnowledgeRepository(repo.db_path)
    address = {key: ITEM[key] for key in ('scope', 'project', 'topic', 'kind', 'item_id')}
    assert planning.get_item(reopened, **address) == item
    changed = planning.put_item(reopened, author='claude', **{
        **ITEM, 'expected_version': 1, 'status': 'planned'})
    assert changed['created_by'] == 'codex' and changed['updated_by'] == 'claude'
    assert changed['version'] == 2 and changed['status'] == 'planned'
    with pytest.raises(KnowledgeConflict):
        planning.put_item(repo, author='hermes', **ITEM)


def test_topic_and_hub_backlogs_are_separate_not_authorization(repo):
    planning.put_item(repo, author='codex', **ITEM)
    hub = {**ITEM, 'scope': 'hub_improvements', 'project': 'engineering-hub',
           'topic': None, 'kind': 'improvement', 'title': 'Make missing status clearer'}
    saved = planning.put_item(repo, author='hermes', **hub)
    assert saved['path'] == 'planning/hub-improvements/improvement/baseline-study.json'
    other = {**ITEM, 'topic': 'other-topic'}
    planning.put_item(repo, author='claude', **other)
    result = planning.list_items(repo, scope='topic', project='treedar', topic=ITEM['topic'])
    assert len(result['items']) == 1 and result['items'][0]['topic'] == ITEM['topic']
    assert 'content' not in result['items'][0]
    assert result['advisory'] is True and result['execution_authorization'] is False
    improvements = planning.list_items(repo, scope='hub_improvements', project='engineering-hub')
    assert len(improvements['items']) == 1
    assert improvements['items'][0]['kind'] == 'improvement'


@pytest.mark.parametrize('patch', [
    {'scope': 'other'}, {'project': '../escape'}, {'topic': '../escape'}, {'topic': ''},
    {'topic': None}, {'kind': 'execute'}, {'item_id': 'UPPER'}, {'item_id': '../escape'},
    {'status': 'approved'}, {'status': 'execute'}, {'title': ' '}, {'content': '\x00'},
    {'title': 'bad\x01title'}, {'content': 'bad\x1bcontent'}, {'content': 'bad\x7fcontent'},
    {'content': 'é' * 25000}, {'expected_version': True},
    {'scope': 'hub_improvements'},
    {'scope': 'hub_improvements', 'project': 'engineering-hub'},
    {'scope': 'hub_improvements', 'project': 'treedar', 'topic': None},
])
def test_invalid_items_do_not_write(repo, patch):
    with pytest.raises(KnowledgeError):
        planning.put_item(repo, author='codex', **{**ITEM, **patch})
    assert repo.list(project='treedar') == []


def test_list_is_bounded_filtered_and_explicitly_truncated(repo):
    for index in range(4):
        planning.put_item(repo, author='codex', **{**ITEM, 'item_id': 'item-' + str(index)})
    planning.put_item(repo, author='hermes', **{**ITEM, 'kind': 'idea'})
    result = planning.list_items(repo, scope='topic', project='treedar', topic=ITEM['topic'],
                                 kind='plan', limit=2)
    assert len(result['items']) == 2 and result['truncated'] is True
    assert all(item['kind'] == 'plan' and 'content' not in item for item in result['items'])
    for limit in (0, 100, True, '50'):
        with pytest.raises(planning.PlanningError):
            planning.list_items(repo, scope='topic', project='treedar', topic=ITEM['topic'], limit=limit)


@pytest.mark.parametrize('change', ['raw', 'duplicate', 'schema', 'title', 'scope', 'project', 'path', 'extra'])
def test_malformed_generic_planning_records_are_not_trusted(repo, change):
    saved = planning.put_item(repo, author='codex', **ITEM)
    entry = repo.get('treedar', saved['path'])
    data = json.loads(entry.content)
    if change == 'schema': data['schema'] = 'unknown'
    if change == 'title': data['title'] = 'different title'
    if change == 'scope': data['scope'] = 'hub_improvements'
    if change == 'project': data['project'] = 'engineering-hub'
    if change == 'path': data['item_id'] = 'different-id'
    if change == 'extra': data['execution_authorization'] = True
    raw = json.dumps(data)
    if change == 'raw': raw = 'ordinary markdown pretending to be a planning item'
    if change == 'duplicate': raw = '{"schema":"one","schema":"two"}'
    repo.put(project='treedar', path=saved['path'], title=entry.title, content=raw,
             expected_version=1, author='hermes')
    with pytest.raises(planning.PlanningIntegrityError):
        planning.list_items(repo, scope='topic', project='treedar', topic=ITEM['topic'])
    # Ordinary notes are not parsed as planning envelopes.
    repo.put(project='treedar', path='notes/example.md', title='Ordinary', content='Plain markdown',
             expected_version=0, author='claude')
    assert planning.list_items(repo, scope='topic', project='treedar', topic='unrelated')['items'] == []


def test_api_auth_cas_and_server_identity(composed):
    core, bus, _ = composed
    with TestClient(core, base_url=BASE) as client:
        assert client.get(API, params={'scope': 'topic', 'project': 'treedar', 'topic': ITEM['topic']}).status_code == 401
        assert client.post(API, json=ITEM).status_code == 401
        headers = {'Authorization': 'Bearer ' + AUTH.codex_token}
        saved = client.post(API, headers=headers, json=ITEM)
        assert saved.status_code == 200, saved.text
        item = saved.json()
        assert item['created_by'] == 'codex' and item['execution_authorization'] is False
        assert client.post(API, headers=headers, json={**ITEM, 'author': 'hermes'}).status_code == 422
        conflict = client.post(API, headers=headers, json=ITEM)
        assert conflict.status_code == 409 and conflict.json()['detail']['current_version'] == 1
        address = {key: ITEM[key] for key in ('scope', 'project', 'topic', 'kind', 'item_id')}
        assert client.get(API + '/document', headers=headers, params=address).json() == item
        assert client.get(API + '/document', headers=headers, params={**address, 'item_id': 'missing'}).status_code == 404
        listing = client.get(API, headers=headers, params={
            'scope': 'topic', 'project': 'treedar', 'topic': ITEM['topic']}).json()
        assert 'content' not in listing['items'][0]
        entry = KnowledgeRepository(bus.state.service.repository.path).get('treedar', item['path'])
        assert entry.updated_by == 'codex'


def test_api_denies_malformed_planning_without_raw_content_leaks(composed):
    core, bus, _ = composed
    repo = KnowledgeRepository(bus.state.service.repository.path)
    path = planning.item_path(repo, **{key: ITEM[key] for key in ('scope', 'project', 'topic', 'kind', 'item_id')})
    repo.put(project='treedar', path=path, title='Invalid', content='SECRET-DO-NOT-ECHO',
             author='codex', expected_version=0)
    with TestClient(core, base_url=BASE) as client:
        response = client.get(API, headers={'Authorization': 'Bearer ' + AUTH.codex_token},
            params={'scope': 'topic', 'project': 'treedar', 'topic': ITEM['topic']})
        assert response.status_code == 503
        assert response.json()['detail']['code'] == 'planning_integrity_failure'
        assert 'SECRET' not in response.text


def test_unavailable_store_never_creates_parallel_planning_store(hub):
    client, app, _ = hub
    assert client.get(API, params={'scope': 'topic', 'project': 'treedar',
        'topic': ITEM['topic']}, headers=agent_headers()).status_code == 503
    with app.state.repository._connection() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='knowledge'").fetchone() is None


def test_cloudflare_human_writes_require_csrf_and_use_real_identity(cf_hub, tmp_path):
    from agent_message_bus.app import create_app as create_bus
    from engineering_hub.app import create_app
    from mcp.server.transport_security import TransportSecuritySettings
    _, fixture_app, config, assertion, _ = cf_hub
    settings = fixture_app.state.settings
    bus = create_bus(db_path=tmp_path / 'planning-bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=settings.agent_auth, push_events_operational=False,
        transport_security_settings=TransportSecuritySettings(
            allowed_hosts=['hub.example'], allowed_origins=[config.origin]))
    core = create_app(settings, bus_app=bus)
    with TestClient(core, base_url=config.origin, follow_redirects=False) as client:
        headers, _ = sign_in(client, assertion())
        for override in ({'X-Hub-CSRF': ''}, {'X-Hub-CSRF': 'wrong'}, {'Origin': 'https://wrong.example'}):
            assert client.post(API, headers={**headers, **override}, json=ITEM).status_code == 403
        result = client.post(API, headers=headers, json=ITEM)
        assert result.status_code == 200
        assert result.json()['updated_by'].startswith('human:cf:')
        assert len(result.json()['updated_by']) == 73
        assert result.json()['execution_authorization'] is False
