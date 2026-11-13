import pytest
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from engineering_hub.repository import HubError
from engineering_hub.roles import RoleRepository
from tests.test_session_hub_bridge import env, pair, call, unified, sdk, data, OWNER, PROJECT
from tests.test_hub_outbox import FakeBus
from engineering_hub.events import HubOutboxCursorReader
from engineering_hub.outbox import HubOutboxPublisher


def setup_role(env):
    first_id, first, _ = pair(env, name='first', caps=('role.read', 'role.lease', 'role.claim', 'tracking.read'))
    second_id, second, _ = pair(env, name='second', caps=('role.read', 'role.lease', 'role.claim', 'tracking.read'))
    roles = RoleRepository(env.repo)
    role = roles.configure(OWNER, 'backend-review', 0, [PROJECT], [first_id, second_id],
                           ['src'], 'Review work.', ['task.read'])
    return roles, role, first, second


def op(env, token, name, callback):
    return call(env, token, name, callback)


def test_session_lease_handoff_and_role_claim_survive_session_roll(env):
    roles, role, first, second = setup_role(env)
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Work', 'task')
    lease = op(env, first, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], role['revision'], 60))
    assert lease['execution_authorization'] is False and lease['generation'] == 1
    with pytest.raises(HubError, match='role_leased'):
        op(env, second, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], lease['revision'], 60))
    claim = op(env, first, 'hub_role_claim', lambda actor: roles.claim(actor, role['role_id'], 1, task['id'], 'claim'))
    assert claim['coordination_only'] and not claim['execution_authorization']
    assert env.repo._read(task['id'], 'tracking_task') == task
    with pytest.raises(HubError, match='handoff_revision_conflict'):
        op(env, first, 'hub_role_release', lambda actor: roles.release(actor, role['role_id'], lease['revision'], 1, 9, 'Open work'))
    released = op(env, first, 'hub_role_release', lambda actor: roles.release(actor, role['role_id'], lease['revision'], 1, 0, 'Open work'))
    assert released['handoff']['text'] == 'Open work' and released['lease'] is None
    takeover = op(env, second, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], released['revision'], 60))
    assert takeover['generation'] == 2 and takeover['takeover'] == 'handoff_recorded'
    listed = op(env, second, 'hub_role_claims', lambda actor: roles.claims(actor, role['role_id'], 50, None))
    assert listed['items'][0]['task_id'] == task['id']
    with pytest.raises(HubError, match='role_lease_lost'):
        op(env, first, 'hub_role_claim', lambda actor: roles.claim(actor, role['role_id'], 1, task['id'], 'stale'))


def test_expiry_takeover_is_labelled_and_only_allowed_installation(env):
    roles, role, first, second = setup_role(env)
    _, stranger, _ = pair(env, name='stranger', caps=('role.lease', 'role.read'))
    with pytest.raises(HubError, match='role_holder_forbidden'):
        op(env, stranger, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], 1, 60))
    lease = op(env, first, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], 1, 10))
    env.clock.value += 11
    taken = op(env, second, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], lease['revision'], 60))
    assert taken['takeover'] == 'expired_without_handoff' and taken['generation'] == 2


def test_role_lease_expires_before_commit_and_rolls_back(env, monkeypatch):
    roles, role, first, _ = setup_role(env)
    original = roles._guard
    def expire_at_commit(operation, expiry):
        operation.commit_checks.append(lambda db: setattr(env.clock, 'value', expiry))
        original(operation, expiry)
    monkeypatch.setattr(roles, '_guard', expire_at_commit)
    with pytest.raises(HubError, match='role_lease_expired'):
        op(env, first, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], 1, 10))
    assert env.repo._read(role['id'], 'coordination_role')['revision'] == 1


def test_old_claim_retry_cannot_return_reassigned_role_claim(env):
    roles, role, first, second = setup_role(env)
    second_role = roles.configure(OWNER, 'other', 0, [PROJECT], [role['installations'][1]], ['src'], 'Other', ['task.read'])
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Work', 'task')
    lease = op(env, first, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], 1, 60))
    claim = op(env, first, 'hub_role_claim', lambda actor: roles.claim(actor, role['role_id'], 1, task['id'], 'claim'))
    op(env, first, 'hub_role_claim_release', lambda actor: roles.release_claim(actor, role['role_id'], 1, task['id'], claim['revision'], 'Transfer'))
    op(env, second, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'other', second_role['revision'], 60))
    other = op(env, second, 'hub_role_claim', lambda actor: roles.claim(actor, 'other', 1, task['id'], 'other-claim'))
    assert other['role_id'] == 'other'
    with pytest.raises(HubError, match='role_claim_changed'):
        op(env, first, 'hub_role_claim', lambda actor: roles.claim(actor, role['role_id'], 1, task['id'], 'claim'))


def test_revoked_session_cannot_use_old_role_lease(env):
    roles, role, first, _ = setup_role(env)
    op(env, first, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], 1, 60))
    env.bridge.grant_revoke(OWNER, role['installations'][0], 1)
    with pytest.raises(HubError, match='hub_grant_required'):
        op(env, first, 'hub_role_renew', lambda actor: roles.renew(actor, role['role_id'], 2, 1, 60))
    assert env.repo._read(role['id'], 'coordination_role')['revision'] == 2


def test_owner_can_clear_only_expired_lease_without_fabricating_handoff(env):
    roles, role, first, _ = setup_role(env)
    lease = op(env, first, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], 1, 10))
    with pytest.raises(HubError, match='role_lease_not_expired'):
        roles.owner_clear_expired(OWNER, role['role_id'], lease['revision'], 'Old session', env.sessions._now)
    env.clock.value += 11
    cleared = roles.owner_clear_expired(OWNER, role['role_id'], lease['revision'], 'Old session', env.sessions._now)
    assert cleared['lease'] is None and cleared['handoff']['revision'] == 0
    assert cleared['takeover'] == 'owner_cleared_expired_without_handoff'


def test_owner_can_recover_expired_role_after_project_archived(env):
    holder_id, token, _ = pair(env, caps=('role.lease',), projects=('3d-viewer',))
    roles = RoleRepository(env.repo)
    role = roles.configure(OWNER, 'viewer', 0, ['3d-viewer'], [holder_id], ['src'], 'Work', [])
    lease = op(env, token, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], 1, 10))
    env.clock.value += 11
    env.projects.archive_project(OWNER, '3d-viewer', 1)
    cleared = roles.owner_clear_expired(OWNER, role['role_id'], lease['revision'], 'Project archived', env.sessions._now)
    assert cleared['lease'] is None and cleared['takeover'] == 'owner_cleared_expired_without_handoff'


def test_project_scope_filter_precedes_pagination_and_does_not_leak_roles(env):
    first_id, token, _ = pair(env, caps=('role.read',), projects=(PROJECT,))
    roles = RoleRepository(env.repo)
    for i in range(4):
        roles.configure(OWNER, f'visible-{i}', 0, [PROJECT], [first_id], ['src'], 'Work', ['task.read'])
    roles.configure(OWNER, 'hidden-mixed', 0, [PROJECT, '3d-viewer'], [first_id], ['src'], 'Work', ['task.read'])
    first = op(env, token, 'hub_role_list', lambda actor: roles.list(actor, PROJECT, 2, None))
    second = op(env, token, 'hub_role_list', lambda actor: roles.list(actor, PROJECT, 2, first['next_after']))
    assert len(first['items']) == len(second['items']) == 2 and second['next_after'] is None
    assert all(item['role_id'] != 'hidden-mixed' for item in first['items'] + second['items'])


def test_role_project_cannot_be_removed_while_claims_are_unclosed(env):
    roles, role, first, _ = setup_role(env)
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Work', 'task')
    lease = op(env, first, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], 1, 60))
    op(env, first, 'hub_role_claim', lambda actor: roles.claim(actor, role['role_id'], 1, task['id'], 'claim'))
    released = op(env, first, 'hub_role_release', lambda actor: roles.release(actor, role['role_id'], lease['revision'], 1, 0, 'Unclosed claim'))
    with pytest.raises(HubError, match='role_claim_scope_conflict'):
        roles.configure(OWNER, role['role_id'], released['revision'], ['3d-viewer'], role['installations'], ['src'], 'Work', ['task.read'])


def test_role_grants_do_not_imply_task_read_access(env):
    holder_id, holder, _ = pair(env, caps=('role.read', 'role.lease', 'role.claim'))
    roles = RoleRepository(env.repo)
    role = roles.configure(OWNER, 'readless', 0, [PROJECT], [holder_id], ['src'], 'Work', [])
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Private', 'task')
    op(env, holder, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], 1, 60))
    with pytest.raises(HubError, match='role_task_unavailable'):
        op(env, holder, 'hub_role_claim', lambda actor: roles.claim(actor, role['role_id'], 1, task['id'], 'claim'))
    assert op(env, holder, 'hub_role_claims', lambda actor: roles.claims(actor, role['role_id'], 50, None))['items'] == []


def test_owner_http_and_session_mcp_role_transport(unified):
    client, app, bus, csrf = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, _ = pair(env, caps=('role.read', 'role.lease', 'role.claim', 'tracking.read'))
    payload = dict(expected_revision=0, projects=[PROJECT], installations=[installation], paths=['src'],
                   standing_rules='Review changes.', tool_scope=['task.read'])
    path = '/api/hub/v1/roles/backend-review'
    assert client.put(path, json=payload).status_code == 403
    assert client.put(path, headers={**csrf, 'Authorization': 'Bearer synthetic-codex'}, json=payload).status_code == 403
    created = client.put(path, headers=csrf, json=payload)
    assert created.status_code == 200 and created.json()['coordination_only'] is True
    assert client.get('/api/hub/v1/roles').json()['items'][0]['role_id'] == 'backend-review'
    assert client.get(path).json()['revision'] == 1
    invoke, _ = sdk(client, token)
    listed = data(invoke('hub_role_list', {'project': PROJECT}))
    assert [role['role_id'] for role in listed['items']] == ['backend-review']
    lease = data(invoke('hub_role_acquire', {'role': 'backend-review', 'expected_revision': 1, 'lease_seconds': 60}))
    assert lease['generation'] == 1 and lease['execution_authorization'] is False
    task = app.state.repository.propose_tracking_task('codex', PROJECT, 'Task', 'transport-task')
    claim = data(invoke('hub_role_claim', {'role': 'backend-review', 'generation': 1, 'task_id': task['id'],
                                           'client_request_id': 'claim'}))
    assert claim['task_id'] == task['id'] and claim['execution_authorization'] is False
    assert data(invoke('hub_role_claims', {'role': 'backend-review'}))['items'][0]['task_id'] == task['id']
    released = data(invoke('hub_role_release', {'role': 'backend-review', 'expected_revision': lease['revision'],
        'generation': 1, 'expected_handoff_revision': 0, 'handoff': 'Task remains open.'}))
    assert released['lease'] is None and released['handoff']['text'] == 'Task remains open.'
    env.bridge.grant_revoke(OWNER, installation, 1)
    assert invoke('hub_role_get', {'role': 'backend-review'})['isError']


def test_role_acquire_serializes_with_grant_revocation(env):
    roles, role, first, _ = setup_role(env)
    entered, release, attempting, revoked = (threading.Event() for _ in range(4))
    def acquire():
        with env.bridge.admit(first, 'hub_role_acquire') as operation:
            entered.set()
            assert release.wait(5)
            return roles.acquire(operation.principal.actor, role['role_id'], 1, 60)
    def revoke():
        attempting.set()
        result = env.bridge.grant_revoke(OWNER, role['installations'][0], 1)
        revoked.set()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        lease_future = pool.submit(acquire)
        assert entered.wait(5)
        revoke_future = pool.submit(revoke)
        assert attempting.wait(5)
        assert not revoked.wait(0.1)
        release.set()
        lease = lease_future.result(timeout=5)
        assert lease['generation'] == 1
        assert revoke_future.result(timeout=5)['state'] == 'revoked'
    with pytest.raises(HubError, match='hub_grant_required'):
        op(env, first, 'hub_role_get', lambda actor: roles.get(actor, role['role_id']))


def test_simultaneous_role_acquire_has_one_winner(env):
    roles, role, first, second = setup_role(env)
    barrier = threading.Barrier(2)
    def acquire(token):
        barrier.wait(timeout=5)
        try:
            return op(env, token, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], 1, 60))
        except HubError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acquire, (first, second)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(result in ('revision_conflict', 'role_leased') for result in results if isinstance(result, str)) == 1


def test_expiry_during_claim_commit_rolls_back(env, monkeypatch):
    roles, role, first, _ = setup_role(env)
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Work', 'task')
    op(env, first, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], 1, 10))
    original = roles._guard
    def expire_at_commit(operation, expiry):
        operation.commit_checks.append(lambda db: setattr(env.clock, 'value', expiry))
        original(operation, expiry)
    monkeypatch.setattr(roles, '_guard', expire_at_commit)
    with pytest.raises(HubError, match='role_lease_expired'):
        op(env, first, 'hub_role_claim', lambda actor: roles.claim(actor, role['role_id'], 1, task['id'], 'claim'))
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind IN ('role_claim','role_claim_receipt')").fetchone()[0] == 0
    assert env.repo._read(task['id'], 'tracking_task') == task


def test_claim_listing_and_release_require_underlying_read_capability(env):
    first_id, first, _ = pair(env, name='first', caps=('role.read', 'role.lease', 'role.claim', 'tracking.read'))
    second_id, second, _ = pair(env, name='second', caps=('role.read', 'role.lease', 'role.claim'))
    roles = RoleRepository(env.repo)
    role = roles.configure(OWNER, 'bounded', 0, [PROJECT], [first_id, second_id], ['src'], 'Work', [])
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Private', 'task')
    lease = op(env, first, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], 1, 60))
    claim = op(env, first, 'hub_role_claim', lambda actor: roles.claim(actor, role['role_id'], 1, task['id'], 'claim'))
    released = op(env, first, 'hub_role_release', lambda actor: roles.release(actor, role['role_id'], lease['revision'], 1, 0, 'Open'))
    op(env, second, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], released['revision'], 60))
    assert op(env, second, 'hub_role_claims', lambda actor: roles.claims(actor, role['role_id'], 50, None))['items'] == []
    with pytest.raises(HubError, match='role_task_unavailable'):
        op(env, second, 'hub_role_claim_release', lambda actor: roles.release_claim(actor, role['role_id'], 2, task['id'], claim['revision'], 'Transfer'))


def test_role_changes_never_publish_unscoped_bus_or_sse_events(env):
    roles, role, first, _ = setup_role(env)
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Visible existing event', 'task')
    with env.repo._connection() as db:
        _, before = HubOutboxCursorReader()(db, None)
    lease = op(env, first, 'hub_role_acquire', lambda actor: roles.acquire(actor, role['role_id'], 1, 60))
    claim = op(env, first, 'hub_role_claim', lambda actor: roles.claim(actor, role['role_id'], 1, task['id'], 'claim'))
    op(env, first, 'hub_role_claim_release', lambda actor: roles.release_claim(actor, role['role_id'], 1, task['id'], claim['revision'], 'Done coordinating'))
    op(env, first, 'hub_role_release', lambda actor: roles.release(actor, role['role_id'], lease['revision'], 1, 0, 'Handed off'))
    with env.repo._connection() as db:
        events, after = HubOutboxCursorReader()(db, before)
        actions = [row[0] for row in db.execute('SELECT action FROM hub_outbox ORDER BY sequence')]
        audits = [row[0] for row in db.execute("SELECT action FROM hub_audit WHERE action LIKE 'role.%'")]
    assert after == before and events == ()
    assert actions == ['tracking_task.proposed'] and len(audits) >= 5
    bus = FakeBus()
    result = asyncio.run(HubOutboxPublisher(env.repo, bus).drain_batch())
    assert result['failed'] == 0 and result['accepted'] == 2
    assert {request['body']['event'] for request in bus.requests} == {'tracking_task.proposed'}
    assert all(request['body']['entity_id'] == task['id'] for request in bus.requests)
