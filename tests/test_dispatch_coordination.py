import pytest
import threading
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

from engineering_hub.dispatch import DispatchRepository
from engineering_hub.repository import HubError
from engineering_hub.roles import RoleRepository
from tests.test_session_hub_bridge import env, pair, call, unified, sdk, data, OWNER, PROJECT
from tests.test_hub_outbox import FakeBus
from engineering_hub.outbox import HubOutboxPublisher


def setup_dispatch(env):
    caps = ('dispatch.read', 'dispatch.write', 'dispatch.report', 'role.read', 'role.lease',
            'role.claim', 'tracking.read', 'decision.request', 'decision.read')
    orch_id, orch, _ = pair(env, name='orchestrator', caps=caps)
    worker_id, worker, _ = pair(env, name='worker', caps=caps)
    roles = RoleRepository(env.repo)
    roles.configure(OWNER, 'orchestrator', 0, [PROJECT], [orch_id], ['src'], 'Coordinate', [])
    roles.configure(OWNER, 'backend', 0, [PROJECT], [worker_id], ['src'], 'Work', [])
    call(env, orch, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'orchestrator', 1, 60))
    call(env, worker, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'backend', 1, 60))
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Work', 'task')
    claim = call(env, worker, 'hub_role_claim', lambda actor: roles.claim(actor, 'backend', 1, task['id'], 'claim'))
    dispatch = DispatchRepository(env.repo)
    request = call(env, orch, 'hub_orchestration_request_create', lambda actor: dispatch.create_request(actor, 1, PROJECT, 'Request', 'request'))
    item = call(env, orch, 'hub_dispatch_create', lambda actor: dispatch.create_dispatch(actor, 1, request['id'], task['id'], 'backend', 10, 'dispatch'))
    return orch, worker, task, claim, roles, dispatch, request, item


def test_dispatch_reports_are_advisory_and_retry_safe(env):
    orch, worker, task, _, roles, service, request, item = setup_dispatch(env)
    assert not item['execution_authorization'] and item['wake_delivery'] == 'not_attempted'
    assert call(env, orch, 'hub_orchestration_request_list', lambda actor: service.list_requests(actor, PROJECT, 10, None))['items'] == [request]
    retry = call(env, orch, 'hub_dispatch_create', lambda actor: service.create_dispatch(actor, 1, request['id'], task['id'], 'backend', 10, 'dispatch'))
    assert retry == item
    with pytest.raises(HubError, match='dispatch_already_open'):
        call(env, orch, 'hub_dispatch_create', lambda actor: service.create_dispatch(actor, 1, request['id'], task['id'], 'backend', 10, 'duplicate'))
    assert call(env, worker, 'hub_dispatch_list', lambda actor: service.list_dispatches(actor, request['id'], 10, None))['items'] == [item]
    done = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 1, 1, 'done', 'Reported done', 'done'))
    assert done['state'] == 'done' and done['completion_verified'] is False
    assert env.repo._read(task['id'], 'tracking_task') == task
    assert call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 1, 1, 'done', 'Reported done', 'done')) == done


def test_ask_is_atomic_idempotent_and_resumes_only_after_answer(env):
    _, worker, task, _, _, service, _, item = setup_dispatch(env)
    with env.repo._connection() as db:
        before = db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0]
    asked = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 1, 1, 'ask', 'Need a choice', 'ask', 'Choose', ['Yes', 'No']))
    assert asked['state'] == 'asked' and asked['ask_count'] == 1
    assert call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 1, 1, 'ask', 'Need a choice', 'ask', 'Choose', ['Yes', 'No'])) == asked
    with pytest.raises(HubError, match='dispatch_decision_pending'):
        call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 2, 1, 'resume', 'Continue', 'resume'))
    decision = env.repo.get_decision(asked['decision_id'])
    assert decision['status'] == 'open' and not decision['execution_authorization']
    with env.repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0] == before
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='decision'").fetchone()[0] == 1
    env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'Yes')
    resumed = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 2, 1, 'resume', 'Continue', 'resume'))
    assert resumed['state'] == 'working' and resumed['ask_count'] == 1
    assert call(env, worker, 'hub_dispatch_decisions', lambda actor: service.decisions(actor, item['id'], 10, 0))['items'][0]['id'] == decision['id']
    reports = call(env, worker, 'hub_dispatch_reports', lambda actor: service.reports(actor, item['id'], 10, 0))['items']
    assert [report['state'] for report in reports] == ['asked', 'working']
    assert [report['note'] for report in reports] == ['Need a choice', 'Continue']
    asked_again = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 3, 1, 'ask', 'Another question', 'ask-again', 'Choose again', ['A', 'B']))
    first = call(env, worker, 'hub_dispatch_decisions', lambda actor: service.decisions(actor, item['id'], 1, 0))
    assert first['items'][0]['id'] == decision['id'] and first['next_after_revision'] == 2
    second = call(env, worker, 'hub_dispatch_decisions', lambda actor: service.decisions(actor, item['id'], 1, first['next_after_revision']))
    assert second['items'][0]['id'] == asked_again['decision_id'] and second['next_after_revision'] is None
    assert env.repo._read(task['id'], 'tracking_task') == task


def test_checkpoint_is_advisory_no_execution_or_wake(env):
    orch, worker, task, _, _, service, _, item = setup_dispatch(env)
    env.clock.value += 11
    stale = call(env, orch, 'hub_dispatch_checkpoint', lambda actor: service.checkpoint(actor, item['id'], 1, 1, 'checkpoint'))
    assert stale['state'] == 'stale' and stale['stale_basis'] == 'report_and_attributed_progress_timeout'
    assert stale['wake_delivery'] == 'not_attempted' and not stale['execution_authorization']
    resumed = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], stale['revision'], 1, 'working', 'Still working', 'report'))
    assert resumed['state'] == 'working' and env.repo._read(task['id'], 'tracking_task') == task


def test_report_requires_current_target_lease_and_claim(env):
    _, worker, task, claim, roles, service, _, item = setup_dispatch(env)
    call(env, worker, 'hub_role_claim_release', lambda actor: roles.release_claim(actor, 'backend', 1, task['id'], claim['revision'], 'Finished'))
    with pytest.raises(HubError, match='dispatch_unavailable'):
        call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 1, 1, 'done', 'No claim', 'done'))
    assert call(env, worker, 'hub_dispatch_get', lambda actor: service.get_dispatch(actor, item['id']))['state'] == 'dispatched'


@pytest.mark.parametrize('closed', ['withdrawn', 'completion_verified'])
def test_closed_task_denies_report_ask_and_stale_checkpoint(env, closed):
    orch, worker, task, _, _, service, _, item = setup_dispatch(env)
    # Synthetic legacy drift tests the dispatch guard independently of the
    # public task state machine and does not represent a supported transition.
    with env.repo._transaction() as db:
        task['status'] = closed
        db.execute("UPDATE hub_entities SET data=? WHERE id=? AND kind='tracking_task'", (json.dumps(task), task['id']))
    with pytest.raises(HubError, match='dispatch_task_closed'):
        call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 1, 1, 'ask', 'Question', 'ask', 'Choose', ['Yes', 'No']))
    with pytest.raises(HubError, match='dispatch_task_closed'):
        call(env, orch, 'hub_dispatch_checkpoint', lambda actor: service.checkpoint(actor, item['id'], 1, 1, 'check'))
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind IN ('decision','dispatch_report')").fetchone()[0] == 0
    assert call(env, worker, 'hub_dispatch_get', lambda actor: service.get_dispatch(actor, item['id'])) == item


@pytest.mark.parametrize('removed', ['decision.request', 'decision.read'])
def test_ask_without_decision_grant_rolls_back_both(env, removed):
    _, worker, task, _, roles, service, _, item = setup_dispatch(env)
    installation = roles.owner_get(OWNER, 'backend')['installations'][0]
    caps = sorted({'dispatch.read', 'dispatch.write', 'dispatch.report', 'role.read', 'role.lease', 'role.claim', 'tracking.read', 'decision.request', 'decision.read'} - {removed})
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT], caps)
    with pytest.raises(HubError, match='dispatch_unavailable'):
        call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 1, 1, 'ask', 'Question', 'ask', 'Choose', ['Yes', 'No']))
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='decision'").fetchone()[0] == 0
    assert call(env, worker, 'hub_dispatch_get', lambda actor: service.get_dispatch(actor, item['id'])) == item
    assert env.repo._read(task['id'], 'tracking_task') == task


def test_ask_commit_expiry_rolls_back_decision(env, monkeypatch):
    _, worker, _, _, _, service, _, item = setup_dispatch(env)
    original = RoleRepository._guard
    def expire(operation, expiry):
        operation.commit_checks.append(lambda db: setattr(env.clock, 'value', expiry))
        original(operation, expiry)
    monkeypatch.setattr(RoleRepository, '_guard', staticmethod(expire))
    with pytest.raises(HubError, match='role_lease_expired'):
        call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 1, 1, 'ask', 'Question', 'ask', 'Choose', ['Yes', 'No']))
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='decision'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM hub_requests WHERE key='ask'").fetchone()[0] == 0


def test_concurrent_duplicate_dispatch_has_one_winner(env):
    orch, worker, task, _, _, service, request, item = setup_dispatch(env)
    call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 1, 1, 'done', 'Done', 'done'))
    ready = threading.Barrier(2)
    def create(key):
        ready.wait(timeout=5)
        try:
            return call(env, orch, 'hub_dispatch_create', lambda actor: service.create_dispatch(actor, 1, request['id'], task['id'], 'backend', 10, key))
        except HubError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, key) for key in ('one', 'two')]
        results = [future.result(10) for future in futures]
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count('dispatch_already_open') == 1


def test_real_mcp_and_owner_http_recover_request_and_reports(unified):
    from types import SimpleNamespace
    client, app, bus, _ = unified
    session_env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    caps = ('dispatch.read', 'dispatch.write', 'dispatch.report', 'role.read', 'role.lease',
            'role.claim', 'tracking.read', 'decision.request', 'decision.read')
    installation, token, _ = pair(session_env, caps=caps)
    roles = RoleRepository(app.state.repository)
    roles.configure(OWNER, 'orchestrator', 0, [PROJECT], [installation], ['src'], 'Coordinate', [])
    roles.configure(OWNER, 'backend', 0, [PROJECT], [installation], ['src'], 'Work', [])
    task = app.state.repository.propose_tracking_task('codex', PROJECT, 'Task', 'task')
    invoke, _ = sdk(client, token)
    for role in ('orchestrator', 'backend'):
        assert data(invoke('hub_role_acquire', {'role': role, 'expected_revision': 1, 'lease_seconds': 60}))['generation'] == 1
    assert data(invoke('hub_role_claim', {'role': 'backend', 'generation': 1, 'task_id': task['id'], 'client_request_id': 'claim'}))['task_id'] == task['id']
    request = data(invoke('hub_orchestration_request_create', {'generation': 1, 'project': PROJECT, 'title': 'Request', 'client_request_id': 'request'}))
    item = data(invoke('hub_dispatch_create', {'generation': 1, 'request_id': request['id'], 'task_id': task['id'], 'role': 'backend', 'window_seconds': 10, 'client_request_id': 'dispatch'}))
    report = data(invoke('hub_dispatch_report', {'dispatch_id': item['id'], 'expected_revision': 1, 'generation': 1,
        'state': 'blocked', 'note': 'Waiting', 'client_request_id': 'report'}))
    assert report['state'] == 'blocked'
    assert data(invoke('hub_orchestration_request_list', {'project': PROJECT}))['items'][0]['id'] == request['id']
    assert data(invoke('hub_dispatch_reports', {'dispatch_id': item['id']}))['items'][0]['note'] == 'Waiting'
    base = '/api/hub/v1/orchestration'
    assert client.get('/api/hub/v1/projects/' + PROJECT + '/orchestration-requests').json()['items'][0]['id'] == request['id']
    assert client.get(base + '/requests/' + request['id'] + '/dispatches').json()['items'][0]['id'] == item['id']
    assert client.get(base + '/dispatches/' + item['id'] + '/reports').json()['items'][0]['note'] == 'Waiting'
    assert client.get(base + '/dispatches/' + item['id'] + '/reports', headers={'Authorization': 'Bearer synthetic-codex'}).status_code == 403
    client.cookies.clear()
    assert client.get(base + '/requests/' + request['id']).status_code == 401


def test_project_scope_hides_request_and_list(env):
    caps = ('dispatch.write', 'role.lease')
    installation, creator, _ = pair(env, name='creator', caps=caps, projects=('project-alpha',))
    roles = RoleRepository(env.repo)
    roles.configure(OWNER, 'orchestrator', 0, ['project-alpha'], [installation], ['src'], 'Coordinate', [])
    call(env, creator, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'orchestrator', 1, 60))
    service = DispatchRepository(env.repo)
    request = call(env, creator, 'hub_orchestration_request_create', lambda actor: service.create_request(actor, 1, 'project-alpha', 'Private', 'request'))
    _, outsider, _ = pair(env, name='outsider', caps=('dispatch.read',), projects=(PROJECT,))
    with pytest.raises(HubError, match='dispatch_unavailable'):
        call(env, outsider, 'hub_orchestration_request_get', lambda actor: service.get_request(actor, request['id']))
    with pytest.raises(HubError, match='hub_project_forbidden'):
        call(env, outsider, 'hub_orchestration_request_list', lambda actor: service.list_requests(actor, 'project-alpha', 10, None))
    assert call(env, outsider, 'hub_orchestration_request_list', lambda actor: service.list_requests(actor, PROJECT, 10, None))['items'] == []


def test_dispatch_visibility_filters_decision_grant_before_limit(env):
    orch, worker, _, _, roles, service, request, first = setup_dispatch(env)
    call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, first['id'], 1, 1, 'ask', 'Question', 'ask', 'Choose', ['Yes', 'No']))
    another = env.repo.propose_tracking_task('codex', PROJECT, 'Other', 'other')
    second = call(env, orch, 'hub_dispatch_create', lambda actor: service.create_dispatch(actor, 1, request['id'], another['id'], 'backend', 10, 'other-dispatch'))
    installation = roles.owner_get(OWNER, 'backend')['installations'][0]
    caps = ['dispatch.read', 'dispatch.write', 'dispatch.report', 'role.read', 'role.lease', 'role.claim', 'tracking.read', 'decision.request']
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT], caps)
    with pytest.raises(HubError, match='dispatch_unavailable'):
        call(env, worker, 'hub_dispatch_get', lambda actor: service.get_dispatch(actor, first['id']))
    page = call(env, worker, 'hub_dispatch_list', lambda actor: service.list_dispatches(actor, request['id'], 1, None))
    assert [item['id'] for item in page['items']] == [second['id']] and page['next_after'] is None


def test_report_serializes_with_cross_connection_revoke(env):
    _, worker, _, _, roles, service, _, item = setup_dispatch(env)
    installation = roles.owner_get(OWNER, 'backend')['installations'][0]
    entered, release, attempting, revoked = (threading.Event() for _ in range(4))
    def report():
        with env.bridge.admit(worker, 'hub_dispatch_report') as operation:
            entered.set()
            assert release.wait(5)
            return service.report(operation.principal.actor, item['id'], 1, 1, 'working', 'Working', 'report')
    def revoke():
        attempting.set()
        result = env.bridge.grant_revoke(OWNER, installation, 1)
        revoked.set()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(report)
        assert entered.wait(5)
        revoking = pool.submit(revoke)
        assert attempting.wait(5)
        assert not revoked.wait(.1)
        release.set()
        assert writing.result(5)['state'] == 'working'
        assert revoking.result(5)['state'] == 'revoked'
    with pytest.raises(HubError, match='hub_grant_required'):
        call(env, worker, 'hub_dispatch_get', lambda actor: service.get_dispatch(actor, item['id']))


def test_mixed_outbox_delivers_normal_events_without_dispatch_pointers(env):
    _, worker, _, _, _, service, _, item = setup_dispatch(env)
    call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 1, 1, 'ask', 'Question', 'ask', 'Choose', ['Yes', 'No']))
    with env.repo._connection() as db:
        actions = [row[0] for row in db.execute('SELECT action FROM hub_outbox ORDER BY sequence')]
    assert actions == ['tracking_task.proposed']
    bus = FakeBus()
    result = asyncio.run(HubOutboxPublisher(env.repo, bus).drain_batch())
    assert result['failed'] == 0 and result['accepted'] == 2
    assert {request['body']['event'] for request in bus.requests} == {'tracking_task.proposed'}
