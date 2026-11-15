import json
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest

from engineering_hub.repository import HubError
from tests.test_session_hub_bridge import env, pair, call, unified, sdk, data, OWNER, PROJECT
from tests.test_dispatch_coordination import setup_dispatch


def test_origin_is_atomic_private_and_idempotent(env):
    orch, _, _, _, _, _, request, _ = setup_dispatch(env)
    installation, writer, _ = pair(env, name='origin-writer', caps=('tracking.propose', 'tracking.read', 'dispatch.read'))
    def propose(key, origin):
        return call(env, writer, 'hub_tracking_task_propose', lambda actor: env.repo.propose_tracking_task(
            actor, PROJECT, 'Origin task', key, origin_request_id=origin))
    item = propose('origin-key', request['id'])
    assert 'origin_request_id' not in item
    assert propose('origin-key', request['id']) == item
    with pytest.raises(HubError, match='idempotency_conflict'):
        propose('origin-key', None)
    with env.repo._connection() as db:
        rows = db.execute("SELECT data FROM hub_entities WHERE kind='tracking_origin'").fetchall()
        assert len(rows) == 1 and json.loads(rows[0]['data'])['request_id'] == request['id']
        assert request['id'] not in json.dumps(env.repo.tracking_tasks(PROJECT, 100, None))
    result = call(env, writer, 'hub_tracking_task_origin', lambda actor: env.repo.tracking_task_origin(actor, item['id']))
    assert result == {'task_id': item['id'], 'origin_request_id': request['id']}
    other = call(env, writer, 'hub_tracking_task_propose', lambda actor: env.repo.propose_tracking_task(
        actor, PROJECT, 'Second undispatched', 'second', origin_request_id=request['id']))
    def reverse(after):
        return call(env, writer, 'hub_orchestration_request_tasks', lambda actor: env.repo.tracking_request_tasks(actor, PROJECT, request['id'], 1, after))
    page = reverse(None)
    assert page['next_after'] and len(page['task_ids']) == 1
    page2 = reverse(page['next_after'])
    assert page2['next_after'] is None and set(page['task_ids'] + page2['task_ids']) == {item['id'], other['id']}
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT], ['tracking.propose', 'tracking.read'])
    with pytest.raises(HubError):
        call(env, writer, 'hub_tracking_task_origin', lambda actor: env.repo.tracking_task_origin(actor, item['id']))
    with pytest.raises(HubError):
        propose('new-key', request['id'])
    with pytest.raises(HubError):
        reverse(None)
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='tracking_origin'").fetchone()[0] == 2


def test_origin_denies_foreign_missing_and_legacy_is_null(env):
    _, _, _, _, _, _, request, _ = setup_dispatch(env)
    _, writer, _ = pair(env, name='origin-check', caps=('tracking.propose', 'tracking.read', 'dispatch.read'), projects=(PROJECT, 'project-alpha'))
    with pytest.raises(HubError):
        call(env, writer, 'hub_tracking_task_propose', lambda actor: env.repo.propose_tracking_task(
            actor, 'project-alpha', 'Foreign', 'foreign', origin_request_id=request['id']))
    with pytest.raises(HubError, match='dispatch_unavailable'):
        call(env, writer, 'hub_orchestration_request_tasks', lambda actor: env.repo.tracking_request_tasks(
            actor, 'project-alpha', request['id'], 10, None))
    with pytest.raises(HubError, match='invalid_limit'):
        call(env, writer, 'hub_orchestration_request_tasks', lambda actor: env.repo.tracking_request_tasks(
            actor, PROJECT, request['id'], 101, None))
    legacy = call(env, writer, 'hub_tracking_task_propose', lambda actor: env.repo.propose_tracking_task(actor, PROJECT, 'Legacy', 'legacy'))
    assert call(env, writer, 'hub_tracking_task_origin', lambda actor: env.repo.tracking_task_origin(actor, legacy['id']))['origin_request_id'] is None
    with pytest.raises(HubError, match='idempotency_conflict'):
        call(env, writer, 'hub_tracking_task_propose', lambda actor: env.repo.propose_tracking_task(actor, PROJECT, 'Legacy', 'legacy', origin_request_id=request['id']))
    _, outsider, _ = pair(env, name='origin-outsider', caps=('tracking.read', 'dispatch.read'), projects=('project-alpha',))
    for identity in (legacy['id'], '00000000-0000-0000-0000-000000000001'):
        with pytest.raises(HubError, match='hub_tracking_task_unavailable'):
            call(env, outsider, 'hub_tracking_task_origin', lambda actor: env.repo.tracking_task_origin(actor, identity))


def test_origin_requires_session_even_for_legacy_agent(env):
    with pytest.raises(HubError, match='dispatch_unavailable'):
        env.repo.propose_tracking_task('codex', PROJECT, 'No session', 'key', origin_request_id='00000000-0000-0000-0000-000000000001')
    with pytest.raises(HubError, match='dispatch_unavailable'):
        env.repo.tracking_task_origin('codex', '00000000-0000-0000-0000-000000000001')


def test_real_mcp_origin_capability_and_legacy_privacy(unified):
    from types import SimpleNamespace
    from engineering_hub.roles import RoleRepository
    client, app, bus, _ = unified
    session_env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, _ = pair(session_env, name='creator', caps=(
        'role.lease', 'dispatch.write', 'dispatch.read', 'tracking.propose', 'tracking.read'))
    RoleRepository(app.state.repository).configure(OWNER, 'orchestrator', 0, [PROJECT], [installation], ['src'], 'Coordinate', [])
    invoke, _ = sdk(client, token)
    data(invoke('hub_role_acquire', {'role': 'orchestrator', 'expected_revision': 1, 'lease_seconds': 60}))
    request = data(invoke('hub_orchestration_request_create', {'generation': 1, 'project': PROJECT, 'title': 'Request', 'client_request_id': 'request'}))
    item = data(invoke('hub_tracking_task_propose', {'project': PROJECT, 'title': 'Task', 'client_request_id': 'task', 'origin_request_id': request['id']}))
    assert request['id'] not in json.dumps(item)
    assert data(invoke('hub_tracking_task_origin', {'task_id': item['id']}))['origin_request_id'] == request['id']
    assert data(invoke('hub_orchestration_request_tasks', {'project': PROJECT, 'request_id': request['id']}))['task_ids'] == [item['id']]
    summary = data(invoke('hub_orchestration_request_origin_task_summary', {'project': PROJECT, 'request_id': request['id']}))
    assert summary['scope'] == 'origin_linked_tracking_tasks_only' and summary['tasks'][0]['id'] == item['id']
    assert not summary['tasks'][0]['completion_verified'] and not summary['dispatch_only_tasks_included']
    path = f"/api/hub/v1/projects/{PROJECT}/orchestration-requests/{request['id']}/tracking-tasks"
    owner_response = client.get(path)
    assert owner_response.status_code == 200 and owner_response.json()['task_ids'] == [item['id']]
    assert client.get(path.replace(PROJECT, 'project-alpha')).status_code == 404
    assert client.get(path, headers={'Authorization': 'Bearer synthetic-codex'}).status_code == 403
    listed = data(invoke('hub_tracking_task_list', {'project': PROJECT}))
    assert request['id'] not in json.dumps(listed)
    app.state.session_hub_bridge.grant_put(OWNER, installation, 1, [PROJECT], ['tracking.read'])
    assert invoke('hub_tracking_task_origin', {'task_id': item['id']})['isError']
    assert invoke('hub_orchestration_request_tasks', {'project': PROJECT, 'request_id': request['id']})['isError']
    assert invoke('hub_orchestration_request_origin_task_summary', {'project': PROJECT, 'request_id': request['id']})['isError']
    assert request['id'] not in json.dumps(data(invoke('hub_tracking_task_list', {'project': PROJECT})))
    client.cookies.clear()
    assert client.get(path).status_code == 401


def test_origin_creation_rolls_back_task_and_link_on_commit_failure(env):
    from agent_message_bus import session_hub_context
    _, _, _, _, _, _, request, _ = setup_dispatch(env)
    _, writer, _ = pair(env, name='origin-failure', caps=('tracking.propose', 'dispatch.read'))
    def attempt(actor):
        def fail(_db):
            raise HubError('injected_failure', 409)
        session_hub_context.current.get().commit_checks.append(fail)
        return env.repo.propose_tracking_task(actor, PROJECT, 'Will roll back', 'rollback', origin_request_id=request['id'])
    with pytest.raises(HubError, match='injected_failure'):
        call(env, writer, 'hub_tracking_task_propose', attempt)
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='tracking_origin'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM hub_requests WHERE key='rollback'").fetchone()[0] == 0
        assert not any(item['title'] == 'Will roll back' for item in env.repo.tracking_tasks(PROJECT, 100, None))


def test_origin_write_serializes_with_grant_revoke(env):
    _, _, _, _, _, _, request, _ = setup_dispatch(env)
    installation, writer, _ = pair(env, name='origin-race', caps=('tracking.propose', 'dispatch.read'))
    entered, release, attempting, revoked = (threading.Event() for _ in range(4))
    def write():
        with env.bridge.admit(writer, 'hub_tracking_task_propose') as operation:
            entered.set()
            assert release.wait(5)
            return env.repo.propose_tracking_task(operation.principal.actor, PROJECT, 'Race', 'race', origin_request_id=request['id'])
    def revoke():
        attempting.set()
        result = env.bridge.grant_revoke(OWNER, installation, 1)
        revoked.set()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write)
        try:
            assert entered.wait(5)
            second = pool.submit(revoke)
            assert attempting.wait(5)
            assert not revoked.wait(.1)
        finally:
            release.set()
        assert first.result(timeout=5)['title'] == 'Race'
        second.result(timeout=5)
    with pytest.raises(HubError):
        call(env, writer, 'hub_tracking_task_propose', lambda actor: env.repo.propose_tracking_task(
            actor, PROJECT, 'Race', 'race', origin_request_id=request['id']))


def test_origin_task_summary_scope_review_and_pagination(env):
    orch, _, _, _, _, service, request, _ = setup_dispatch(env)
    installation, writer, _ = pair(env, name='summary-writer', caps=(
        'tracking.propose', 'tracking.read', 'dispatch.read'))
    second_request = call(env, orch, 'hub_orchestration_request_create', lambda actor: service.create_request(
        actor, 1, PROJECT, 'Second', 'second-request'))
    tasks = [call(env, writer, 'hub_tracking_task_propose', lambda actor, n=n: env.repo.propose_tracking_task(
        actor, PROJECT, f'Origin {n}', f'origin-{n}', origin_request_id=request['id'])) for n in range(2)]
    # The origin is A even if dispatch associates the same task with B.
    call(env, orch, 'hub_dispatch_create', lambda actor: service.create_dispatch(
        actor, 1, second_request['id'], tasks[0]['id'], 'backend', 10, 'other-request-dispatch'))
    proof = [{'label': 'proof', 'url': 'https://example.test/proof'}]
    claimed = env.repo.update_tracking_task('codex', tasks[0]['id'], tasks[0]['revision'], 'summary-claim',
        progress='Done', evidence_links=proof, completion_claim=True)
    def page(req, after=None):
        return call(env, writer, 'hub_orchestration_request_origin_task_summary', lambda actor:
            env.repo.tracking_request_summary(actor, PROJECT, req, 1, after))
    first = page(request['id'])
    second = page(request['id'], first['next_after'])
    items = first['tasks'] + second['tasks']
    assert {v['id'] for v in items} == {t['id'] for t in tasks}
    assert first['scope'] == 'origin_linked_tracking_tasks_only'
    assert not first['dispatch_only_tasks_included'] and not first['execution_authorization']
    assert next(v for v in items if v['id'] == claimed['id'])['status'] == 'completion_claimed'
    assert all(not v['completion_verified'] for v in items)
    assert page(second_request['id'])['tasks'] == []
    env.repo.review_tracking_task(OWNER, claimed['id'], claimed['revision'], 'summary-review',
        verdict='verify', note='Checked', evidence_links=proof)
    items = page(request['id'])['tasks']
    if items[0]['id'] != claimed['id']:
        items = page(request['id'], items[0]['id'])['tasks']
    assert items[0]['status'] == 'completion_verified' and items[0]['completion_verified']
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT], ['tracking.read'])
    with pytest.raises(HubError):
        page(request['id'])
    _, dispatch_only, _ = pair(env, name='summary-no-tracking', caps=('dispatch.read',))
    with pytest.raises(HubError):
        call(env, dispatch_only, 'hub_orchestration_request_origin_task_summary', lambda actor:
             env.repo.tracking_request_summary(actor, PROJECT, request['id'], 1, None))
    _, reader, _ = pair(env, name='summary-reader', caps=('tracking.read', 'dispatch.read'))
    with pytest.raises(HubError, match='invalid_limit'):
        call(env, reader, 'hub_orchestration_request_origin_task_summary', lambda actor:
             env.repo.tracking_request_summary(actor, PROJECT, request['id'], 51, None))
    with pytest.raises(HubError):
        call(env, reader, 'hub_orchestration_request_origin_task_summary', lambda actor:
             env.repo.tracking_request_summary(actor, PROJECT, request['id'], 1, 'bad-cursor'))
    with pytest.raises(HubError, match='dispatch_unavailable'):
        call(env, reader, 'hub_orchestration_request_origin_task_summary', lambda actor:
             env.repo.tracking_request_summary(actor, PROJECT, '00000000-0000-0000-0000-000000000001', 1, None))
    _, foreign, _ = pair(env, name='summary-foreign', caps=('tracking.read', 'dispatch.read'), projects=('project-alpha',))
    with pytest.raises(HubError):
        call(env, foreign, 'hub_orchestration_request_origin_task_summary', lambda actor:
             env.repo.tracking_request_summary(actor, PROJECT, request['id'], 1, None))
