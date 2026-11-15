import json
import uuid
from types import SimpleNamespace

import pytest

from engineering_hub.dispatch import DispatchRepository
from engineering_hub.repository import HubError
from engineering_hub.roles import RoleRepository, _NAMESPACE as role_namespace
from tests.test_dispatch_coordination import setup_dispatch
from tests.test_session_hub_bridge import env, pair, call, unified, sdk, data, OWNER, PROJECT, CAPABILITIES


def inbox(env, token, role='backend', generation=1, project=PROJECT, limit=50, after=None):
    service = DispatchRepository(env.repo)
    return call(env, token, 'hub_role_dispatch_inbox', lambda actor:
        service.role_inbox(actor, role, generation, project, limit, after))


def test_exact_role_inbox_discovers_dispatch_without_request_id_and_does_not_write(env):
    _, worker, task, _, _, _, _, item = setup_dispatch(env)
    with env.repo._connection() as db:
        before = db.execute("SELECT count(*) FROM hub_outbox").fetchone()[0]
        task_bytes = db.execute('SELECT data FROM hub_entities WHERE id=?', (task['id'],)).fetchone()[0]
    result = inbox(env, worker)
    assert len(result['items']) == 1
    view = result['items'][0]
    assert view['dispatch']['id'] == item['id'] and view['target_binding_state'] == 'current_session'
    assert view['coordination_claim_state'] == 'current_session' and view['report_text_untrusted'] is True
    assert view['execution_authorization'] is False and result['wake_delivery'] == 'not_attempted'
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_outbox").fetchone()[0] == before
        assert db.execute('SELECT data FROM hub_entities WHERE id=?', (task['id'],)).fetchone()[0] == task_bytes


def test_same_installation_other_session_cannot_use_held_role(env):
    installation, first, secret = pair(env, name='worker')
    other = env.sessions.exchange(secret)['token']
    roles = RoleRepository(env.repo)
    roles.configure(OWNER, 'backend', 0, [PROJECT], [installation], ['src'], 'Work', [])
    call(env, first, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'backend', 1, 60))
    with pytest.raises(HubError, match='dispatch_unavailable'):
        inbox(env, other)


def test_prior_binding_and_claim_are_explicit_not_takeover(env):
    orch_id, orch, _ = pair(env, name='orchestrator')
    worker_id, worker, secret = pair(env, name='worker')
    next_worker = env.sessions.exchange(secret)['token']
    roles, service = RoleRepository(env.repo), DispatchRepository(env.repo)
    roles.configure(OWNER, 'orchestrator', 0, [PROJECT], [orch_id], ['src'], 'Coordinate', [])
    roles.configure(OWNER, 'backend', 0, [PROJECT], [worker_id], ['src'], 'Work', [])
    call(env, orch, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'orchestrator', 1, 60))
    call(env, worker, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'backend', 1, 60))
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Work', 'role-inbox-task')
    call(env, worker, 'hub_role_claim', lambda actor: roles.claim(actor, 'backend', 1, task['id'], 'role-inbox-claim'))
    req = call(env, orch, 'hub_orchestration_request_create', lambda actor: service.create_request(actor, 1, PROJECT, 'Request', 'role-inbox-request'))
    item = call(env, orch, 'hub_dispatch_create', lambda actor: service.create_dispatch(actor, 1, req['id'], task['id'], 'backend', 10, 'role-inbox-dispatch'))
    released = call(env, worker, 'hub_role_release', lambda actor: roles.release(actor, 'backend', 2, 1, 0, 'Handoff'))
    call(env, next_worker, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'backend', released['revision'], 60))
    view = inbox(env, next_worker, generation=2)['items'][0]
    assert view['dispatch']['id'] == item['id']
    assert view['target_binding_state'] == 'prior_or_other_session'
    assert view['coordination_claim_state'] == 'prior_or_other_session'
    assert view['execution_authorization'] is False


def test_foreign_claim_is_indistinguishable_from_no_claim(env):
    _, worker, task, _, _, _, _, _ = setup_dispatch(env)
    claim_id = str(uuid.uuid5(role_namespace, 'claim:' + task['id']))
    with env.repo._transaction() as db:
        claim = env.repo._get(db, claim_id, 'role_claim')
        claim['role_id'] = 'private-other-role'
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(claim), claim_id))
    foreign = inbox(env, worker)['items'][0]['coordination_claim_state']
    with env.repo._transaction() as db:
        db.execute('DELETE FROM hub_entities WHERE id=?', (claim_id,))
    absent = inbox(env, worker)['items'][0]['coordination_claim_state']
    assert foreign == absent == 'unavailable'


def test_task_and_decision_scope_filter_before_pagination(env):
    _, worker, _, _, roles, _, _, item = setup_dispatch(env)
    installation = roles.owner_get(OWNER, 'backend')['installations'][0]
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT],
        ['dispatch.read', 'role.read', 'role.lease', 'tracking.read'])
    hidden = dict(item, id='00000000-0000-4000-8000-000000000000', state='asked', decision_id=str(uuid.uuid4()))
    with env.repo._transaction() as db:
        db.execute("INSERT INTO hub_entities(id,kind,data) VALUES(?,'role_dispatch',?)", (hidden['id'], json.dumps(hidden)))
    page = inbox(env, worker, limit=1)
    assert [view['dispatch']['id'] for view in page['items']] == [item['id']]
    assert page['next_after'] is None
    env.bridge.grant_put(OWNER, installation, 2, [PROJECT], ['dispatch.read', 'role.read', 'role.lease'])
    assert inbox(env, worker, limit=1)['items'] == []


def test_empty_result_still_checks_lease_at_response_guard(env):
    _, worker, _, _, roles, _, _, _ = setup_dispatch(env)
    installation = roles.owner_get(OWNER, 'backend')['installations'][0]
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT], ['dispatch.read', 'role.read', 'role.lease'])
    with env.bridge.admit(worker, 'hub_role_dispatch_inbox') as op:
        assert DispatchRepository(env.repo).role_inbox(op.principal.actor, 'backend', 1, PROJECT, 10, None)['items'] == []
        env.clock.value += 61
        with pytest.raises(HubError, match='role_lease_expired'):
            op.before_commit(env.repo.path, None)


def test_real_mcp_role_inbox_and_wrong_role_denial(unified):
    client, app, bus, _ = unified
    test_env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge,
                               repo=app.state.repository)
    _, worker, _, _, _, _, _, item = setup_dispatch(test_env)
    invoke, _ = sdk(client, worker)
    result = data(invoke('hub_role_dispatch_inbox', {'role': 'backend', 'generation': 1, 'project': PROJECT}))
    assert result['items'][0]['dispatch']['id'] == item['id']
    denied = invoke('hub_role_dispatch_inbox', {'role': 'orchestrator', 'generation': 1, 'project': PROJECT})
    assert denied['isError'] and 'dispatch_unavailable' in denied['content'][0]['text']


def test_latest_report_visible_as_untrusted_and_closed_task_filtered(env):
    _, worker, task, _, _, service, _, item = setup_dispatch(env)
    report = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(
        actor, item['id'], 1, 1, 'blocked', 'Untrusted peer report', 'blocked'))
    view = inbox(env, worker)['items'][0]
    assert view['dispatch']['revision'] == report['revision'] and view['dispatch']['report'] == 'Untrusted peer report'
    assert view['report_text_untrusted'] is True
    with env.repo._transaction() as db:
        task['status'] = 'completion_verified'  # Synthetic closure race.
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(task), task['id']))
    assert inbox(env, worker)['items'] == []


def test_revocation_serializes_with_real_mcp_inbox(unified, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    client, app, bus, _ = unified
    test_env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge,
                               repo=app.state.repository)
    _, worker, _, _, roles, _, _, item = setup_dispatch(test_env)
    installation = roles.owner_get(OWNER, 'backend')['installations'][0]
    invoke, _ = sdk(client, worker)
    entered, release, revoked = threading.Event(), threading.Event(), threading.Event()
    original = DispatchRepository._dispatch
    def held(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        entered.set()
        assert release.wait(10)
        return result
    monkeypatch.setattr(DispatchRepository, '_dispatch', held)
    args = {'role': 'backend', 'generation': 1, 'project': PROJECT}
    def revoke():
        try:
            return test_env.bridge.grant_revoke(OWNER, installation, 1)
        finally:
            revoked.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(invoke, 'hub_role_dispatch_inbox', args)
        try:
            assert entered.wait(10)
            revoking = pool.submit(revoke)
            assert not revoked.wait(.05)
        finally:
            release.set()
        assert data(reading.result(timeout=10))['items'][0]['dispatch']['id'] == item['id']
        assert revoking.result(timeout=10)['state'] == 'revoked'
    assert invoke('hub_role_dispatch_inbox', args)['isError']


def test_wrong_role_and_project_scope_do_not_disclose_dispatch(env):
    _, worker, _, _, _, _, _, _ = setup_dispatch(env)
    with pytest.raises(HubError, match='dispatch_unavailable'):
        inbox(env, worker, role='orchestrator')
    with pytest.raises(HubError):
        inbox(env, worker, project='project-alpha')


def test_inbox_task_status_is_one_snapshot_across_concurrent_close(env, monkeypatch):
    _, worker, task, _, _, _, _, item = setup_dispatch(env)
    service = DispatchRepository(env.repo)
    original = service._dispatch
    changed = False
    def after_read(db, actor, identity):
        nonlocal changed
        result = original(db, actor, identity)
        if not changed:
            changed = True
            with env.repo._transaction() as other:
                current = env.repo._get(other, task['id'], 'tracking_task')
                current['status'] = 'completion_verified'
                other.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(current), task['id']))
        return result
    monkeypatch.setattr(service, '_dispatch', after_read)
    page = call(env, worker, 'hub_role_dispatch_inbox', lambda actor: service.role_inbox(actor, 'backend', 1, PROJECT, 10, None))
    assert page['items'][0]['dispatch']['id'] == item['id'] and page['items'][0]['task_status'] == 'open'
    assert inbox(env, worker)['items'] == []
