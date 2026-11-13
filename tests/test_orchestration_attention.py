import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from engineering_hub.repository import HubError
from engineering_hub.roles import RoleRepository
from engineering_hub.dispatch import DispatchRepository
from engineering_hub.outbox import HubOutboxPublisher
from tests.test_hub_outbox import FakeBus
from tests.test_dispatch_coordination import setup_dispatch
from tests.test_session_hub_bridge import env, pair, call, unified, sdk, data, OWNER, PROJECT


def stale(env):
    orch, worker, task, claim, roles, service, request, item = setup_dispatch(env)
    env.clock.value += 11
    result = call(env, orch, 'hub_dispatch_checkpoint', lambda actor: service.checkpoint(actor, item['id'], 1, 1, 'checkpoint'))
    return orch, worker, task, roles, service, request, item, result


def test_stale_creates_one_private_pending_intent_and_resolution_retains_history(env):
    orch, worker, task, roles, service, _, item, stale_item = stale(env)
    assert stale_item['state'] == 'stale' and stale_item['wake_delivery'] == 'not_attempted'
    page = call(env, orch, 'hub_orchestrator_attention_list', lambda actor: service.attention(actor, 1, PROJECT, 10, None))
    assert len(page['items']) == 1
    intent = page['items'][0]
    assert intent['id'] == stale_item['attention_intent_id']
    assert intent['recipient_role'] == 'orchestrator' and intent['dispatch_revision'] == stale_item['revision']
    assert intent['state'] == 'pending_routing' and intent['delivery'] == 'not_attempted' and intent['wake_verified'] is False
    assert service.owner_attention(OWNER, PROJECT, 10, None)['items'] == [intent]
    assert call(env, orch, 'hub_dispatch_checkpoint', lambda actor: service.checkpoint(actor, item['id'], 1, 1, 'checkpoint')) == stale_item
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='orchestration_attention'").fetchone()[0] == 1
    resumed = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], stale_item['revision'], 1, 'working', 'Continuing', 'report'))
    assert resumed['state'] == 'working' and resumed['attention_intent_id'] is None
    assert call(env, orch, 'hub_orchestrator_attention_list', lambda actor: service.attention(actor, 1, PROJECT, 10, None))['items'] == []
    prior = env.repo._read(intent['id'], 'orchestration_attention')
    assert prior['state'] == 'resolved_without_delivery' and prior['wake_verified'] is False
    assert env.repo._read(task['id'], 'tracking_task') == task


def test_attention_is_only_visible_to_current_orchestrator_holder(env):
    orch, worker, _, _, service, _, _, _ = stale(env)
    with pytest.raises(HubError, match='dispatch_unavailable'):
        call(env, worker, 'hub_orchestrator_attention_list', lambda actor: service.attention(actor, 1, PROJECT, 10, None))
    _, outsider, _ = pair(env, name='outsider', caps=('dispatch.read', 'role.read', 'role.lease'), projects=('project-alpha',))
    with pytest.raises(HubError, match='dispatch_unavailable'):
        call(env, outsider, 'hub_orchestrator_attention_list', lambda actor: service.attention(actor, 1, PROJECT, 10, None))
    assert len(call(env, orch, 'hub_orchestrator_attention_list', lambda actor: service.attention(actor, 1, PROJECT, 10, None))['items']) == 1


@pytest.mark.parametrize('closed', ['withdrawn', 'verified', 'completion_verified'])
def test_closed_task_is_not_actionable_and_can_be_reconciled_idempotently(env, closed):
    orch, _, task, _, service, _, _, stale_item = stale(env)
    intent = service.owner_attention(OWNER, PROJECT, 10, None)['items'][0]
    assert intent['actionable'] is True
    # Synthetic storage drift tests all terminal states independently of the
    # existing public task/review state machine.
    with env.repo._transaction() as db:
        task['status'] = closed
        db.execute("UPDATE hub_entities SET data=? WHERE id=?", (json.dumps(task), task['id']))
    role_view = call(env, orch, 'hub_orchestrator_attention_list', lambda actor: service.attention(actor, 1, PROJECT, 10, None))['items'][0]
    owner_view = service.owner_attention(OWNER, PROJECT, 10, None)['items'][0]
    assert role_view['task_status'] == owner_view['task_status'] == closed
    assert role_view['actionable'] is owner_view['actionable'] is False
    with pytest.raises(HubError, match='revision_conflict'):
        call(env, orch, 'hub_orchestrator_attention_reconcile_closed', lambda actor: service.reconcile_closed_attention(actor, 1, intent['id'], 1, stale_item['revision'] + 1, 'wrong'))
    resolved = call(env, orch, 'hub_orchestrator_attention_reconcile_closed', lambda actor: service.reconcile_closed_attention(actor, 1, intent['id'], 1, stale_item['revision'], 'resolve'))
    assert resolved['state'] == 'resolved_task_closed_without_delivery' and resolved['wake_verified'] is False
    assert call(env, orch, 'hub_orchestrator_attention_reconcile_closed', lambda actor: service.reconcile_closed_attention(actor, 1, intent['id'], 1, stale_item['revision'], 'resolve')) == resolved
    assert service.owner_attention(OWNER, PROJECT, 10, None)['items'] == []
    assert env.repo._read(task['id'], 'tracking_task') == task
    assert env.repo._read(stale_item['id'], 'role_dispatch')['attention_intent_id'] is None


def test_close_and_reconcile_serialize_without_early_resolution(env):
    orch, _, task, _, service, _, _, stale_item = stale(env)
    intent = service.owner_attention(OWNER, PROJECT, 10, None)['items'][0]
    entered, release, attempted, resolved = (threading.Event() for _ in range(4))
    def close_task():
        with env.repo._transaction() as db:
            # Synthetic transition tests Hub-writer serialization only.
            task['status'] = 'completion_verified'
            db.execute("UPDATE hub_entities SET data=? WHERE id=?", (json.dumps(task), task['id']))
            entered.set()
            assert release.wait(5)
    def reconcile():
        attempted.set()
        result = call(env, orch, 'hub_orchestrator_attention_reconcile_closed',
            lambda actor: service.reconcile_closed_attention(actor, 1, intent['id'], 1, stale_item['revision'], 'resolve'))
        resolved.set()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        closing = pool.submit(close_task)
        assert entered.wait(5)
        reconciling = pool.submit(reconcile)
        assert attempted.wait(5) and not resolved.wait(.1)
        release.set()
        closing.result(5)
        assert reconciling.result(5)['state'] == 'resolved_task_closed_without_delivery'


def test_failed_intent_write_rolls_back_stale_and_receipt(env, monkeypatch):
    orch, _, _, _, _, service, _, item = setup_dispatch(env)
    env.clock.value += 11
    original = env.repo._save
    def fail(db, actor, action, kind, value, *args, **kwargs):
        if kind == 'orchestration_attention':
            raise HubError('synthetic_failure', 409)
        return original(db, actor, action, kind, value, *args, **kwargs)
    monkeypatch.setattr(env.repo, '_save', fail)
    with pytest.raises(HubError, match='synthetic_failure'):
        call(env, orch, 'hub_dispatch_checkpoint', lambda actor: service.checkpoint(actor, item['id'], 1, 1, 'check'))
    assert env.repo._read(item['id'], 'role_dispatch') == item
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='orchestration_attention'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM hub_requests WHERE key='check'").fetchone()[0] == 0


@pytest.mark.parametrize('condition', ['revoked', 'expired'])
def test_closed_intent_reconcile_requires_live_orchestrator(env, condition):
    orch, _, task, roles, service, _, _, stale_item = stale(env)
    intent = service.owner_attention(OWNER, PROJECT, 10, None)['items'][0]
    with env.repo._transaction() as db:
        task['status'] = 'completion_verified'
        db.execute("UPDATE hub_entities SET data=? WHERE id=?", (json.dumps(task), task['id']))
    if condition == 'revoked':
        installation = roles.owner_get(OWNER, 'orchestrator')['installations'][0]
        env.bridge.grant_revoke(OWNER, installation, 1)
        expected = 'hub_grant_required'
    else:
        env.clock.value += 50
        expected = 'dispatch_unavailable'
    with pytest.raises(HubError, match=expected):
        call(env, orch, 'hub_orchestrator_attention_reconcile_closed',
            lambda actor: service.reconcile_closed_attention(actor, 1, intent['id'], 1, stale_item['revision'], 'resolve'))
    assert env.repo._read(intent['id'], 'orchestration_attention')['state'] == 'pending_routing'


def test_reconcile_requires_dispatch_read_even_with_write(env):
    orch, _, task, roles, service, _, _, stale_item = stale(env)
    intent = service.owner_attention(OWNER, PROJECT, 10, None)['items'][0]
    with env.repo._transaction() as db:
        task['status'] = 'completion_verified'
        db.execute("UPDATE hub_entities SET data=? WHERE id=?", (json.dumps(task), task['id']))
    installation = roles.owner_get(OWNER, 'orchestrator')['installations'][0]
    caps = ['dispatch.write', 'dispatch.report', 'role.read', 'role.lease', 'role.claim', 'tracking.read', 'decision.request', 'decision.read']
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT], caps)
    with pytest.raises(HubError, match='dispatch_unavailable'):
        call(env, orch, 'hub_orchestrator_attention_reconcile_closed',
            lambda actor: service.reconcile_closed_attention(actor, 1, intent['id'], 1, stale_item['revision'], 'resolve'))
    assert env.repo._read(intent['id'], 'orchestration_attention')['state'] == 'pending_routing'


def test_archived_project_visible_to_owner_only_as_nonactionable(env):
    orch, _, _, _, service, _, _, _ = stale(env)
    # Synthetic archived metadata exercises read behavior; the real system
    # project itself cannot be archived through public APIs.
    with env.repo._transaction() as db:
        row = db.execute('SELECT data FROM hub_projects WHERE id=?', (PROJECT,)).fetchone()
        project = json.loads(row['data'])
        project['archived'] = True
        db.execute('UPDATE hub_projects SET data=? WHERE id=?', (json.dumps(project), PROJECT))
    owner = service.owner_attention(OWNER, PROJECT, 10, None)['items'][0]
    assert owner['project_archived'] is True and owner['actionable'] is False
    with pytest.raises(HubError, match='dispatch_unavailable'):
        call(env, orch, 'hub_orchestrator_attention_list', lambda actor: service.attention(actor, 1, PROJECT, 10, None))


def test_owner_attention_project_and_intent_share_read_snapshot(env, monkeypatch):
    _, _, _, _, service, _, _, _ = stale(env)
    with env.repo._connection() as db:
        db.execute('PRAGMA journal_mode=WAL')
    original_connection = env.repo._connection
    class Cursor:
        def __init__(self, row):
            self.row = row
        def fetchone(self):
            return self.row
    class Proxy:
        def __init__(self, db):
            self.db, self.project_reads = db, 0
        def execute(self, sql, parameters=()):
            result = self.db.execute(sql, parameters)
            if sql == 'SELECT data FROM hub_projects WHERE id=?':
                self.project_reads += 1
                if self.project_reads == 2:
                    row = result.fetchone()
                    # Write between the archived-flag read and joined intent
                    # read; WAL permits this independent transaction to commit.
                    with env.repo._transaction() as other:
                        project = json.loads(row['data'])
                        project['archived'] = True
                        other.execute('UPDATE hub_projects SET data=? WHERE id=?', (json.dumps(project), PROJECT))
                    return Cursor(row)
            return result
        def rollback(self):
            return self.db.rollback()
    @contextmanager
    def connection():
        with original_connection() as db:
            yield Proxy(db)
    monkeypatch.setattr(env.repo, '_connection', connection)
    snapshot = service.owner_attention(OWNER, PROJECT, 10, None)['items'][0]
    assert snapshot['project_archived'] is False and snapshot['actionable'] is True
    monkeypatch.setattr(env.repo, '_connection', original_connection)
    current = service.owner_attention(OWNER, PROJECT, 10, None)['items'][0]
    assert current['project_archived'] is True and current['actionable'] is False


def test_attention_filters_decision_grant_before_pagination(env):
    orch, worker, _, roles, service, request, _, first = stale(env)
    other = env.repo.propose_tracking_task('codex', PROJECT, 'Other', 'other')
    call(env, worker, 'hub_role_claim', lambda actor: roles.claim(actor, 'backend', 1, other['id'], 'other-claim'))
    second = call(env, orch, 'hub_dispatch_create', lambda actor: service.create_dispatch(actor, 1, request['id'], other['id'], 'backend', 10, 'other-dispatch'))
    asked = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, second['id'], 1, 1, 'ask', 'Question', 'ask', 'Choose', ['Yes', 'No']))
    caps = {'dispatch.read', 'dispatch.write', 'dispatch.report', 'role.read', 'role.lease', 'role.claim', 'tracking.read', 'decision.request'}
    worker_id = roles.owner_get(OWNER, 'backend')['installations'][0]
    env.bridge.grant_put(OWNER, worker_id, 1, [PROJECT], sorted(caps))
    pending = call(env, orch, 'hub_dispatch_checkpoint', lambda actor: service.checkpoint(actor, second['id'], asked['revision'], 1, 'other-check'))
    assert pending['state'] == 'asked' and pending['coordination_stale'] is True
    orch_id = roles.owner_get(OWNER, 'orchestrator')['installations'][0]
    env.bridge.grant_put(OWNER, orch_id, 1, [PROJECT], sorted(caps))
    page = call(env, orch, 'hub_orchestrator_attention_list', lambda actor: service.attention(actor, 1, PROJECT, 1, None))
    assert [item['id'] for item in page['items']] == [first['attention_intent_id']]
    assert page['next_after'] is None


def test_attention_intent_never_enters_generic_outbox(env):
    _, _, _, _, _, _, _, _ = stale(env)
    with env.repo._connection() as db:
        assert [row[0] for row in db.execute('SELECT action FROM hub_outbox ORDER BY sequence')] == ['tracking_task.proposed']
    bus = FakeBus()
    result = asyncio.run(HubOutboxPublisher(env.repo, bus).drain_batch())
    assert result['failed'] == 0 and result['accepted'] == 2
    assert {request['body']['event'] for request in bus.requests} == {'tracking_task.proposed'}


def test_real_mcp_and_owner_http_show_pending_not_wake(unified, monkeypatch):
    client, app, bus, _ = unified
    session_env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    caps = ('dispatch.read', 'dispatch.write', 'dispatch.report', 'role.read', 'role.lease',
            'role.claim', 'tracking.read', 'decision.request', 'decision.read')
    installation, token, _ = pair(session_env, caps=caps)
    roles = RoleRepository(app.state.repository)
    for role in ('orchestrator', 'backend'):
        roles.configure(OWNER, role, 0, [PROJECT], [installation], ['src'], 'Work', [])
    task = app.state.repository.propose_tracking_task('codex', PROJECT, 'Task', 'task')
    invoke, _ = sdk(client, token)
    for role in ('orchestrator', 'backend'):
        assert data(invoke('hub_role_acquire', {'role': role, 'expected_revision': 1, 'lease_seconds': 60}))['generation'] == 1
    request = data(invoke('hub_orchestration_request_create', {'generation': 1, 'project': PROJECT, 'title': 'Request', 'client_request_id': 'request'}))
    item = data(invoke('hub_dispatch_create', {'generation': 1, 'request_id': request['id'], 'task_id': task['id'], 'role': 'backend', 'window_seconds': 10, 'client_request_id': 'dispatch'}))
    original = session_env.sessions._now
    monkeypatch.setattr(session_env.sessions, '_now', lambda: original() + 11)
    stale_item = data(invoke('hub_dispatch_checkpoint', {'dispatch_id': item['id'], 'expected_revision': 1, 'generation': 1, 'client_request_id': 'check'}))
    page = data(invoke('hub_orchestrator_attention_list', {'generation': 1, 'project': PROJECT}))
    assert page['items'][0]['id'] == stale_item['attention_intent_id'] and page['items'][0]['delivery'] == 'not_attempted'
    path = '/api/hub/v1/projects/' + PROJECT + '/orchestration-attention'
    assert client.get(path).json()['items'][0]['id'] == stale_item['attention_intent_id']
    assert client.get(path, headers={'Authorization': 'Bearer synthetic-codex'}).status_code == 403
    with app.state.repository._transaction() as db:
        task['status'] = 'completion_verified'
        db.execute("UPDATE hub_entities SET data=? WHERE id=?", (json.dumps(task), task['id']))
    closed_page = data(invoke('hub_orchestrator_attention_list', {'generation': 1, 'project': PROJECT}))
    assert closed_page['items'][0]['actionable'] is False
    resolved = data(invoke('hub_orchestrator_attention_reconcile_closed', {'generation': 1,
        'intent_id': stale_item['attention_intent_id'], 'expected_revision': 1,
        'expected_dispatch_revision': stale_item['revision'], 'client_request_id': 'resolve'}))
    assert resolved['state'] == 'resolved_task_closed_without_delivery'
    assert client.get(path).json()['items'] == []
    client.cookies.clear()
    assert client.get(path).status_code == 401
