import json
import sqlite3
import uuid

import pytest

from engineering_hub.components import ProjectComponentRepository
from engineering_hub.interactive_reviews import InteractiveReviewRepository
from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubError, HubRepository


PROJECT = 'ledge-top-ops'


@pytest.fixture
def env(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    projects = ProjectRepository(repo)
    components = ProjectComponentRepository(repo)
    component = components.create_component('human:1', PROJECT, 'viewer', 'Viewer', 'viewer', status='active')
    grants = {(actor, PROJECT, action) for actor in ('codex', 'human:1', 'human:2')
              for action in ('review_read', 'review_create', 'review_answer')}
    store = InteractiveReviewRepository(repo, components, authorize=lambda *key: key in grants)
    return store, components, component, grants, repo, projects


def payload(env, **changes):
    return {'project': PROJECT, 'component_id': env[2]['id'], 'component_revision': 1,
        'topic': 'synthetic-labels', 'dataset_digest': 'sha256:' + 'a' * 64,
        'title': 'Private review title', 'schema': 'choice-text.v1', 'client_request_id': 'create',
        'items': [{'id': 'label', 'title': 'Label', 'prompt': 'Private classification prompt',
                   'choices': [{'id': 'yes', 'label': 'Yes'}, {'id': 'no', 'label': 'No'}]},
                  {'id': 'note', 'title': 'Notes', 'prompt': 'Describe this item'}], **changes}


def create(env, **changes):
    return env[0].create_review('codex', **payload(env, **changes))


def test_default_deny_and_no_catalog_implied_grants(env):
    closed = InteractiveReviewRepository(env[4], env[1])
    with pytest.raises(HubError, match='interactive_review_forbidden'):
        closed.create_review('codex', **payload(env))
    with pytest.raises(HubError, match='interactive_review_forbidden'):
        closed.list_reviews('human:1', PROJECT)


def test_dynamic_binding_create_read_and_small_list_summary(env):
    item = create(env)
    assert env[0].get_review('human:1', item['id']) == item
    assert item['component_id'] == env[2]['id'] and item['component_revision'] == 1
    assert item['dataset_digest'] == 'sha256:' + 'a' * 64
    summary = env[0].list_reviews('human:1', PROJECT)['items'][0]
    assert summary['item_count'] == 2 and summary['answered_count'] == 0
    assert 'items' not in summary and 'answers' not in summary
    assert str(uuid.UUID(item['id'])) == item['id']


def test_create_is_agent_only_and_idempotent_without_implicit_read(env):
    item = create(env)
    assert create(env) == item
    with pytest.raises(HubError, match='forbidden'):
        env[0].create_review('human:1', **payload(env))
    with pytest.raises(HubError, match='idempotency_conflict'):
        create(env, title='Changed')
    env[3].remove(('codex', PROJECT, 'review_read'))
    with pytest.raises(HubError, match='interactive_review_forbidden'):
        create(env)


def test_cross_project_scope_and_current_read_revocation(env):
    env[3].update(('codex', 'project-alpha', action) for action in ('review_read', 'review_create'))
    with pytest.raises(HubError, match='review_component_scope'):
        create(env, project='project-alpha')
    item = create(env)
    env[3].remove(('human:1', PROJECT, 'review_read'))
    with pytest.raises(HubError, match='interactive_review_forbidden'):
        env[0].get_review('human:1', item['id'])
    with pytest.raises(HubError, match='interactive_review_forbidden'):
        env[0].answer_review('human:1', item['id'], 1, 'answer', [{'item_id': 'label', 'value': 'yes'}])


@pytest.mark.parametrize('status', ['planned', 'paused', 'retired'])
def test_disabled_component_denies_interaction_but_preserves_ledger(env, status):
    item = create(env)
    env[1].update_component('human:1', env[2]['id'], 1, status=status)
    with pytest.raises(HubError, match='review_component_disabled'):
        env[0].get_review('human:1', item['id'])
    with pytest.raises(HubError, match='review_component_disabled'):
        env[0].answer_review('human:1', item['id'], 1, 'answer', [{'item_id': 'label', 'value': 'yes'}])
    with env[4]._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_interactive_reviews').fetchone()[0] == 1


def test_component_version_change_cannot_reinterpret_old_review(env):
    item = create(env)
    env[1].update_component('human:1', env[2]['id'], 1, description='New version context')
    with pytest.raises(HubError, match='review_component_changed'):
        env[0].get_state('human:1', item['id'])
    assert env[0].list_reviews('human:1', PROJECT)['items'] == []


def test_answers_are_human_attributed_cas_idempotent_and_completed(env):
    store = env[0]
    item = create(env)
    first = store.answer_review('human:1', item['id'], 1, 'first', [{'item_id': 'label', 'value': 'yes'}])
    assert first['revision'] == 2 and first['answers']['label']['by'] == 'human:1'
    assert store.answer_review('human:1', item['id'], 1, 'first', [{'item_id': 'label', 'value': 'yes'}]) == first
    with pytest.raises(HubError, match='revision_conflict'):
        store.answer_review('human:2', item['id'], 1, 'stale', [{'item_id': 'label', 'value': 'no'}])
    with pytest.raises(HubError, match='idempotency_conflict'):
        store.answer_review('human:1', item['id'], 1, 'first', [{'item_id': 'label', 'value': 'no'}])
    done = store.answer_review('human:2', item['id'], 2, 'complete', [{'item_id': 'note', 'value': 'Private human answer'}], complete=True)
    assert done['state'] == 'completed' and done['revision'] == 3
    assert done['answers']['note']['by'] == 'human:2'
    assert store.get_state('human:1', item['id'])['answered_count'] == 2
    with pytest.raises(HubError, match='interactive_review_completed'):
        store.answer_review('human:1', item['id'], 3, 'late', [{'item_id': 'note', 'value': 'Late'}])
    with env[4]._connection() as db:
        events = db.execute('SELECT actor,value FROM hub_interactive_review_answer_events ORDER BY revision').fetchall()
        assert [(r['actor'], r['value']) for r in events] == [('human:1', 'yes'), ('human:2', 'Private human answer')]


def test_invalid_choice_incomplete_and_agent_answers_do_not_write(env):
    store = env[0]
    item = create(env)
    with pytest.raises(HubError, match='forbidden'):
        store.answer_review('codex', item['id'], 1, 'agent', [{'item_id': 'label', 'value': 'yes'}])
    with pytest.raises(HubError, match='invalid_review_choice'):
        store.answer_review('human:1', item['id'], 1, 'wrong', [{'item_id': 'label', 'value': 'unknown'}])
    with pytest.raises(HubError, match='review_answers_incomplete'):
        store.answer_review('human:1', item['id'], 1, 'early', [{'item_id': 'label', 'value': 'yes'}], complete=True)
    assert store.get_review('human:1', item['id']) == item


def test_manifest_bounds_and_no_urls_or_code_fields(env):
    for items in ([], [{'id': 'x', 'title': 'x', 'prompt': 'x', 'url': 'https://example.test'}],
                  [{'id': 'x', 'title': 'x', 'prompt': 'x'}] * 2):
        with pytest.raises(HubError):
            create(env, items=items)
    with pytest.raises(HubError, match='review_manifest_too_large'):
        create(env, items=[{'id': 'item' + str(i), 'title': 'Item', 'prompt': 'x' * 2000} for i in range(1000)])
    with pytest.raises(HubError, match='invalid_review_schema'):
        create(env, schema='run-python.v1')


def test_asset_uuid_requires_explicit_registered_binding_for_every_read(env):
    asset = str(uuid.uuid4())
    items = [{'id': 'x', 'title': 'x', 'prompt': 'x', 'asset_id': asset}]
    with pytest.raises(HubError, match='review_asset_not_registered'):
        create(env, items=items)
    allowed = [True]
    env[0].asset_authorize = lambda actor, project, component_id, asset_id: allowed[0] and asset_id == asset
    item = create(env, items=items)
    allowed[0] = False
    with pytest.raises(HubError, match='review_asset_not_registered'):
        env[0].get_review('human:1', item['id'])


def test_global_events_and_entity_registry_do_not_expose_private_review(env, monkeypatch):
    emitted = []
    original = env[4]._emit
    def emit(db, action, value):
        emitted.append(dict(value))
        original(db, action, value)
    monkeypatch.setattr(env[4], '_emit', emit)
    item = create(env)
    env[0].answer_review('human:1', item['id'], 1, 'answer', [{'item_id': 'note', 'value': 'Private human answer'}])
    for value in emitted:
        assert set(value) == {'id', 'project', 'component_id', 'revision', 'state', 'updated_at'}
        assert 'Private' not in json.dumps(value) and 'sha256:' not in json.dumps(value)
    assert env[4]._list('interactive_review') == []
    with env[4]._connection() as db:
        assert db.execute('SELECT 1 FROM hub_entities WHERE id=?', (item['id'],)).fetchone() is None
        for table in ('hub_audit', 'hub_outbox'):
            assert 'Private' not in str([tuple(row) for row in db.execute('SELECT * FROM ' + table)])


def test_answer_event_and_state_roll_back_on_outbox_failure(env, monkeypatch):
    item = create(env)
    def broken(*args):
        raise sqlite3.OperationalError('synthetic failure')
    with monkeypatch.context() as patcher:
        patcher.setattr(env[4], '_emit', broken)
        with pytest.raises(sqlite3.OperationalError):
            env[0].answer_review('human:1', item['id'], 1, 'answer', [{'item_id': 'note', 'value': 'Private'}])
    assert env[0].get_review('human:1', item['id']) == item
    with env[4]._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_interactive_review_answer_events').fetchone()[0] == 0
