import pytest
import threading
from concurrent.futures import ThreadPoolExecutor

from engineering_hub.repository import HubError
from engineering_hub.roles import RoleRepository
from engineering_hub.task_progress import TaskProgress
from tests.test_session_hub_bridge import env, pair, call, unified, sdk, data, OWNER, PROJECT


def setup_progress(env):
    installation, token, _ = pair(env, caps=('role.lease', 'role.claim', 'tracking.read', 'progress.read', 'progress.write'))
    role = RoleRepository(env.repo)
    role.configure(OWNER, 'progress-writer', 0, [PROJECT], [installation], ['src'], 'Coordinate', [])
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Progress', 'initial')
    call(env, token, 'hub_role_acquire', lambda actor: role.acquire(actor, 'progress-writer', 1, 60))
    call(env, token, 'hub_role_claim', lambda actor: role.claim(actor, 'progress-writer', 1, task['id'], 'claim'))
    return token, task, role, TaskProgress(env.repo)


def test_progress_independent_revision_and_observed_eta(env):
    token, task, _, progress = setup_progress(env)
    boxes = [dict(id='a', text='First', done=False), dict(id='b', text='Second', done=False)]
    first = call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 0, 'first', boxes, 'Working', '', role='progress-writer', generation=1))
    assert first['revision'] == 1 and first['percent'] == 0 and first['eta_seconds'] is None
    assert call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 0, 'first', boxes, 'Working', '', role='progress-writer', generation=1)) == first
    assert env.repo._read(task['id'], 'tracking_task') == task
    env.clock.value += 10
    boxes[0]['done'] = True
    second = call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 1, 'second', boxes, 'Working', '', agent_eta_guess=50, role='progress-writer', generation=1))
    assert second['percent'] == 50 and second['eta_seconds'] == 10 and second['agent_eta_guess'] == 50
    assert env.repo._read(task['id'], 'tracking_task') == task
    with pytest.raises(HubError, match='revision_conflict'):
        call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 1, 'stale', boxes, '', '', role='progress-writer', generation=1))
    env.clock.value += 2
    boxes[0]['done'] = False
    third = call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 2, 'third', boxes, '', '', role='progress-writer', generation=1))
    assert third['eta_seconds'] is None
    with pytest.raises(HubError, match='progress_changed'):
        call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 0, 'first',
            [dict(id='a', text='First', done=False), dict(id='b', text='Second', done=False)], 'Working', '', role='progress-writer', generation=1))
    with env.repo._connection() as db:
        receipts = db.execute("SELECT data FROM hub_entities WHERE kind='task_progress_receipt'").fetchall()
        assert len(receipts) == 3 and all(len(row['data']) < 150 for row in receipts)


@pytest.mark.parametrize('removed', ['role.lease', 'role.claim', 'tracking.read'])
def test_progress_requires_current_role_and_task_grants(env, removed):
    token, task, role, progress = setup_progress(env)
    item = role.owner_get(OWNER, 'progress-writer')
    installation = item['installations'][0]
    caps = {'role.lease', 'role.claim', 'tracking.read', 'progress.read', 'progress.write'} - {removed}
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT], sorted(caps))
    with pytest.raises(HubError, match='progress_unavailable'):
        call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 0, 'forbidden', [], '', '', role='progress-writer', generation=1))
    assert env.repo._read(task['id'], 'tracking_task') == task


def test_progress_requires_live_role_claim_and_task_read(env):
    token, task, roles, progress = setup_progress(env)
    _, other, _ = pair(env, name='other', caps=('progress.read', 'progress.write'))
    with pytest.raises(HubError, match='progress_unavailable'):
        call(env, other, 'hub_task_progress_get', lambda actor: progress.get(actor, task['id']))
    env.clock.value += 61
    with pytest.raises(HubError, match='progress_unavailable'):
        call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 0, 'expired', [], '', '', role='progress-writer', generation=1))
    assert env.repo._read(task['id'], 'tracking_task') == task


def test_executable_task_owner_is_exact_session(env):
    _, token, secret = pair(env, caps=('task.propose', 'task.claim', 'task.read', 'progress.read', 'progress.write'))
    task = call(env, token, 'hub_staging_task_propose', lambda actor: env.repo.propose_staging_task(actor, PROJECT, 'Executable', 'propose'))
    task = call(env, token, 'hub_task_claim', lambda actor: env.repo.claim_task(actor, task['id'], task['revision']))
    progress = TaskProgress(env.repo)
    other_session = env.sessions.exchange(secret)['token']
    with pytest.raises(HubError, match='progress_unavailable'):
        call(env, other_session, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 0, 'other', [], '', ''))
    saved = call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 0, 'owner', [], '', ''))
    assert saved['percent'] is None and not saved['execution_authorization']
    assert env.repo.get_task(task['id']) == task


def test_released_role_claim_cannot_write_progress(env):
    token, task, roles, progress = setup_progress(env)
    # The claim is known to be the first revision from setup_progress.
    call(env, token, 'hub_role_claim_release', lambda actor: roles.release_claim(actor, 'progress-writer', 1, task['id'], 1, 'Finished'))
    with pytest.raises(HubError, match='progress_unavailable'):
        call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 0, 'released', [], '', '', role='progress-writer', generation=1))


def test_expiry_at_commit_rolls_back_progress_and_receipt(env, monkeypatch):
    token, task, _, progress = setup_progress(env)
    original = RoleRepository._guard
    def expire_at_commit(operation, expiry):
        operation.commit_checks.append(lambda db: setattr(env.clock, 'value', expiry))
        original(operation, expiry)
    monkeypatch.setattr(RoleRepository, '_guard', staticmethod(expire_at_commit))
    with pytest.raises(HubError, match='role_lease_expired'):
        call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 0, 'expiring', [], '', '', role='progress-writer', generation=1))
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind IN ('task_progress', 'task_progress_receipt')").fetchone()[0] == 0


def test_write_serializes_with_cross_connection_grant_revocation(env):
    token, task, roles, progress = setup_progress(env)
    installation = roles.owner_get(OWNER, 'progress-writer')['installations'][0]
    entered, release, attempting, revoked = (threading.Event() for _ in range(4))
    def write():
        with env.bridge.admit(token, 'hub_task_progress_update') as operation:
            entered.set()
            assert release.wait(5)
            return progress.update(operation.principal.actor, task['id'], 0, 'first', [], '', '', role='progress-writer', generation=1)
    def revoke():
        attempting.set()
        result = env.bridge.grant_revoke(OWNER, installation, 1)
        revoked.set()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(write)
        assert entered.wait(5)
        revoking = pool.submit(revoke)
        assert attempting.wait(5)
        assert not revoked.wait(.1)
        release.set()
        assert writing.result(5)['revision'] == 1
        assert revoking.result(5)['state'] == 'revoked'
    with pytest.raises(HubError, match='hub_grant_required'):
        call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 1, 'second', [], '', '', role='progress-writer', generation=1))


def test_progress_does_not_publish_unscoped_outbox(env):
    token, task, _, progress = setup_progress(env)
    with env.repo._connection() as db:
        before = db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0]
    call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 0, 'first', [], '', '', role='progress-writer', generation=1))
    with env.repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0] == before
        assert db.execute("SELECT count(*) FROM hub_audit WHERE action='task.progress_updated'").fetchone()[0] == 1


def test_competing_progress_writers_have_one_cas_winner(env):
    token, task, _, progress = setup_progress(env)
    ready = threading.Barrier(2)
    def write(key):
        ready.wait(timeout=5)
        try:
            return call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], 0, key, [], '', '', role='progress-writer', generation=1))
        except HubError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, key) for key in ('first', 'second')]
        results = [future.result(10) for future in futures]
    assert sum(isinstance(result, dict) and result['revision'] == 1 for result in results) == 1
    assert results.count('revision_conflict') == 1
    assert env.repo._read(task['id'], 'tracking_task') == task


def test_empty_invalid_and_edited_checklists_do_not_fabricate_eta(env):
    token, task, _, progress = setup_progress(env)
    write = lambda revision, key, items: call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], revision, key, items, '', '', role='progress-writer', generation=1))
    assert write(0, 'empty', [])['percent'] is None
    with pytest.raises(HubError, match='invalid_progress'):
        write(1, 'invalid', [{'id': 'x', 'text': 'X', 'done': 1}])
    first = write(1, 'first', [{'id': 'x', 'text': 'X', 'done': False}])
    assert first['eta_seconds'] is None
    env.clock.value += 10
    edited = write(2, 'edited', [{'id': 'x', 'text': 'Changed', 'done': True}])
    assert edited['percent'] == 100 and edited['eta_seconds'] is None


def test_status_heartbeats_do_not_reset_observed_rate(env):
    token, task, _, progress = setup_progress(env)
    write = lambda revision, key, items: call(env, token, 'hub_task_progress_update', lambda actor: progress.update(actor, task['id'], revision, key, items, 'Working', '', role='progress-writer', generation=1))
    boxes = [{'id': 'a', 'text': 'A', 'done': False}, {'id': 'b', 'text': 'B', 'done': False}]
    write(0, 'first', boxes)
    env.clock.value += 29
    write(1, 'heartbeat', boxes)
    env.clock.value += 1
    boxes[0]['done'] = True
    result = write(2, 'complete-one', boxes)
    assert result['eta_seconds'] == 30


def test_real_session_mcp_progress_and_missing_grant(unified):
    client, app, bus, _ = unified
    from types import SimpleNamespace
    session_env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, _ = pair(session_env, caps=('role.lease', 'role.claim', 'tracking.read', 'progress.read', 'progress.write'))
    roles = RoleRepository(app.state.repository)
    roles.configure(OWNER, 'transport-writer', 0, [PROJECT], [installation], ['src'], 'Work', [])
    task = app.state.repository.propose_tracking_task('codex', PROJECT, 'Task', 'task')
    invoke, _ = sdk(client, token)
    assert data(invoke('hub_role_acquire', {'role': 'transport-writer', 'expected_revision': 1, 'lease_seconds': 60}))['generation'] == 1
    assert data(invoke('hub_role_claim', {'role': 'transport-writer', 'generation': 1, 'task_id': task['id'], 'client_request_id': 'claim'}))['task_id'] == task['id']
    saved = data(invoke('hub_task_progress_update', {'task_id': task['id'], 'expected_revision': 0, 'client_request_id': 'first',
        'checklist': [{'id': 'x', 'text': 'Check', 'done': True}], 'status_line': 'Done', 'blocked_on': '',
        'role': 'transport-writer', 'generation': 1}))
    assert saved['percent'] == 100 and saved['execution_authorization'] is False
    assert data(invoke('hub_task_progress_get', {'task_id': task['id']}))['revision'] == 1
    assert app.state.repository._read(task['id'], 'tracking_task') == task
    path = '/api/hub/v1/task-progress/' + task['id']
    assert client.get(path).status_code == 200 and client.get(path).json()['percent'] == 100
    assert client.get(path, headers={'Authorization': 'Bearer synthetic-codex'}).status_code == 403
    assert client.get('/api/hub/v1/task-progress/not-an-id').status_code == 404
    from engineering_hub.private_gateway import browser_route_allowed
    assert browser_route_allowed('GET', path)
    assert not browser_route_allowed('PUT', path)
    session_env.bridge.grant_revoke(OWNER, installation, 1)
    assert invoke('hub_task_progress_get', {'task_id': task['id']})['isError']
    archived = app.state.repository.propose_tracking_task('codex', '3d-viewer', 'Archived', 'archived-task')
    session_env.bridge.projects.archive_project(OWNER, '3d-viewer', 1)
    assert client.get('/api/hub/v1/task-progress/' + archived['id']).status_code == 404
    client.cookies.clear()
    assert client.get(path).status_code == 401
