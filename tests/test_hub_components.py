from concurrent.futures import ThreadPoolExecutor
import sqlite3
import uuid

import pytest

from engineering_hub.components import ProjectComponentRepository
from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubError, HubRepository


HUMAN = 'human:1'


@pytest.fixture
def env(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    projects = ProjectRepository(repo)
    projects.create_project(HUMAN, 'processing', 'Processing', 'processing')
    return ProjectComponentRepository(repo), projects, repo


def create(env, project='ledge-top-ops', slug='viewer', key='create', **fields):
    return env[0].create_component(HUMAN, project, slug, fields.pop('name', '3D Viewer'), key, **fields)


def test_starts_empty_and_records_products_without_changing_projects_or_tasks(env):
    components, projects, repo = env
    before = projects.list_projects()
    assert components.list_components('ledge-top-ops') == {'items': [], 'next_after': None}
    item = create(env, kind='web_app', legacy_project_id='3d-viewer')
    assert str(uuid.UUID(item['id'])) == item['id']
    assert item['project'] == 'ledge-top-ops' and item['slug'] == 'viewer'
    assert item['created_by'] == HUMAN and item['revision'] == 1
    assert projects.list_projects() == before
    assert repo._list('task') == []
    assert ProjectComponentRepository(repo).get_component(item['id']) == item


def test_requires_registered_project_registry_and_active_project(env, tmp_path):
    raw = ProjectComponentRepository(HubRepository(tmp_path / 'unregistered.sqlite3'))
    with pytest.raises(HubError, match='project_registry_required'):
        raw.create_component(HUMAN, 'engineering-hub', 'x', 'x', 'x')
    with pytest.raises(HubError, match='invalid_project'):
        create(env, project='unknown')
    with pytest.raises(TypeError):
        ProjectComponentRepository(tmp_path / 'other.sqlite3')


def test_project_scoped_slug_and_actor_request_key_idempotency(env):
    first = create(env)
    assert create(env) == first
    with pytest.raises(HubError, match='idempotency_conflict'):
        create(env, name='Changed')
    with pytest.raises(HubError, match='component_exists'):
        create(env, key='different-key')
    second = create(env, project='processing', key='second-project')
    assert first['id'] != second['id'] and first['slug'] == second['slug']
    assert env[0].list_components('processing')['items'] == [second]
    assert env[0].list_components('ledge-top-ops')['items'] == [first]


@pytest.mark.parametrize('agent', ['codex', 'claude', 'hermes'])
def test_agent_mutations_denied(env, agent):
    store = env[0]
    item = create(env)
    with pytest.raises(HubError, match='forbidden'):
        store.create_component(agent, 'ledge-top-ops', 'agent-owned', 'agent', 'agent')
    with pytest.raises(HubError, match='forbidden'):
        store.update_component(agent, item['id'], item['revision'], name='Changed')
    with pytest.raises(HubError, match='forbidden'):
        store.retire_component(agent, item['id'], item['revision'])


def test_cas_retirement_reserved_slug_restoration_and_audit(env):
    store, _, repo = env
    item = create(env)
    changed = store.update_component(HUMAN, item['id'], 1, description='New description', package_url='https://packages.example.test/viewer')
    assert changed['id'] == item['id'] and changed['slug'] == item['slug'] and changed['revision'] == 2
    with pytest.raises(HubError, match='revision_conflict'):
        store.retire_component(HUMAN, item['id'], 1)
    retired = store.retire_component(HUMAN, item['id'], 2)
    assert store.list_components(item['project'])['items'] == []
    assert store.list_components(item['project'], include_retired=True)['items'] == [retired]
    assert store.retire_component(HUMAN, item['id'], 3) == retired
    with pytest.raises(HubError, match='component_exists'):
        create(env, key='reserved')
    restored = store.update_component(HUMAN, item['id'], 3, status='active')
    assert restored['revision'] == 4
    with repo._connection() as db:
        rows = db.execute('SELECT action,revision FROM hub_audit WHERE entity_id=?', (item['id'],)).fetchall()
        assert [(r['action'], r['revision']) for r in rows] == [
            ('component.created', 1), ('component.updated', 2), ('component.retired', 3), ('component.updated', 4)]


def test_archived_project_is_readable_but_no_mutations_or_late_retries(env):
    store, projects, _ = env
    item = create(env, project='processing', legacy_project_id='3d-viewer')
    projects.archive_project(HUMAN, 'processing', 1)
    assert store.get_component(item['id']) == item
    assert store.list_components('processing')['items'] == [item]
    for invoke in (lambda: create(env, project='processing', legacy_project_id='3d-viewer'),
                   lambda: store.update_component(HUMAN, item['id'], 1, name='changed'),
                   lambda: store.retire_component(HUMAN, item['id'], 1)):
        with pytest.raises(HubError, match='project_archived'):
            invoke()


def test_legacy_link_retains_archived_registered_history(env):
    _, projects, _ = env
    projects.archive_project(HUMAN, '3d-viewer', 1)
    assert create(env, legacy_project_id='3d-viewer')['legacy_project_id'] == '3d-viewer'
    with pytest.raises(HubError, match='invalid_project'):
        create(env, slug='bad-link', key='bad-link', legacy_project_id='missing')


@pytest.mark.parametrize('url', ['http://github.com/a/b', 'javascript:alert(1)',
    'https://user:password@example.test/x', 'https://example.test/has space',
    'https://example.test/%0aescape', 'https://example.test\\evil', '//example.test/x'])
def test_urls_are_inert_sanitized_https_metadata(env, url):
    with pytest.raises(HubError, match='invalid_component_url'):
        create(env, repository_url=url)
    with pytest.raises(HubError, match='invalid_component_url'):
        create(env, package_url=url)


def test_filtered_bounded_pagination_and_invalid_ids(env):
    store = env[0]
    items = [create(env, slug='product-' + str(i), key=str(i), kind='package' if i < 3 else 'service') for i in range(4)]
    first = store.list_components('ledge-top-ops', kind='package', limit=2)
    second = store.list_components('ledge-top-ops', kind='package', after=first['next_after'], limit=2)
    assert {r['id'] for r in first['items'] + second['items']} == {r['id'] for r in items[:3]}
    assert second['next_after'] is None
    for kwargs in ({'limit': 101}, {'limit': True}, {'include_retired': 1}, {'kind': 'container'}, {'status': 'running'}, {'after': 'viewer'}):
        with pytest.raises(HubError):
            store.list_components('ledge-top-ops', **kwargs)
    with pytest.raises(HubError, match='invalid_component_id'):
        store.get_component('viewer')


def test_outbox_failure_rolls_back_component_audit_and_idempotency(env, monkeypatch):
    store, _, repo = env
    def broken(*args):
        raise sqlite3.OperationalError('test rollback')
    with monkeypatch.context() as patcher:
        patcher.setattr(repo, '_emit', broken)
        with pytest.raises(sqlite3.OperationalError):
            create(env)
    assert store.list_components('ledge-top-ops')['items'] == []
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_component_requests').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM hub_audit WHERE action='component.created'").fetchone()[0] == 0
    assert create(env)['revision'] == 1


def test_simultaneous_slug_creation_has_one_winner(env):
    def attempt(key):
        try:
            return create(env, key=key)
        except HubError as error:
            assert error.code == 'component_exists'
            return None
    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(attempt, ['one', 'two']))
    assert sum(item is not None for item in results) == 1
