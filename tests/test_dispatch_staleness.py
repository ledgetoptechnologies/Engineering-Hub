import json
import uuid
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from engineering_hub.dispatch import _NAMESPACE as dispatch_namespace
from engineering_hub.repository import HubError
from engineering_hub.roles import RoleRepository
from engineering_hub.task_progress import TaskProgress
from tests.test_dispatch_coordination import setup_dispatch
from tests.test_session_hub_bridge import env, call, OWNER, PROJECT
from tests.test_hub_outbox import FakeBus
from engineering_hub.outbox import HubOutboxPublisher


def allow_progress(env, roles):
    installation = roles.owner_get(OWNER, 'backend')['installations'][0]
    caps = ('dispatch.read', 'dispatch.write', 'dispatch.report', 'role.read', 'role.lease',
            'role.claim', 'tracking.read', 'decision.request', 'decision.read', 'progress.write')
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT], list(caps))
    return installation, set(caps)


def heartbeat(env, worker, task, revision, key, generation=1):
    return call(env, worker, 'hub_task_progress_update', lambda actor: TaskProgress(env.repo).update(
        actor, task['id'], revision, key, [{'id': 'work', 'text': 'Work', 'done': False}],
        'Working', '', role='backend', generation=generation))


def checkpoint(env, orch, service, item, revision, key):
    return call(env, orch, 'hub_dispatch_checkpoint', lambda actor: service.checkpoint(actor, item['id'], revision, 1, key))


def test_attributed_progress_extends_report_timeout_without_wake(env):
    orch, worker, task, _, roles, service, _, item = setup_dispatch(env)
    allow_progress(env, roles)
    env.clock.value += 8
    heartbeat(env, worker, task, 0, 'heartbeat')
    env.clock.value += 3
    waiting = checkpoint(env, orch, service, item, 1, 'check-1')
    assert waiting['state'] == 'dispatched' and waiting['revision'] == 1
    env.clock.value += 8
    stale = checkpoint(env, orch, service, item, 1, 'check-2')
    assert stale['state'] == 'stale' and stale['coordination_stale'] is True
    assert stale['stale_basis'] == 'report_and_attributed_progress_timeout'
    assert stale['wake_delivery'] == 'not_attempted' and not stale['execution_authorization']
    assert env.repo._read(task['id'], 'tracking_task') == task


def test_same_lease_released_and_reclaimed_role_cannot_reuse_old_heartbeat(env):
    orch, worker, task, claim, roles, service, _, item = setup_dispatch(env)
    allow_progress(env, roles)
    env.clock.value += 8
    heartbeat(env, worker, task, 0, 'heartbeat')
    call(env, worker, 'hub_role_claim_release', lambda actor: roles.release_claim(actor, 'backend', 1, task['id'], claim['revision'], 'Transfer'))
    call(env, worker, 'hub_role_claim', lambda actor: roles.claim(actor, 'backend', 1, task['id'], 'reclaim'))
    env.clock.value += 3
    stale = checkpoint(env, orch, service, item, 1, 'check')
    assert stale['state'] == 'stale' and stale['stale_basis'] == 'report_and_attributed_progress_timeout'


def test_same_timestamp_progress_from_prior_dispatch_is_not_reused(env):
    orch, worker, task, _, roles, service, request, item = setup_dispatch(env)
    allow_progress(env, roles)
    heartbeat(env, worker, task, 0, 'old-progress')
    call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 1, 1, 'done', 'Reported done', 'done'))
    next_item = call(env, orch, 'hub_dispatch_create', lambda actor: service.create_dispatch(actor, 1, request['id'], task['id'], 'backend', 10, 'new-dispatch'))
    env.clock.value += 11
    stale = checkpoint(env, orch, service, next_item, 1, 'check')
    assert stale['state'] == 'stale' and stale['stale_basis'] == 'report_and_attributed_progress_timeout'


def test_first_attributed_progress_binds_a_dispatch_created_before_lease(env):
    orch, worker, task, _, roles, service, request, first = setup_dispatch(env)
    allow_progress(env, roles)
    call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, first['id'], 1, 1, 'done', 'Done', 'done'))
    released = call(env, worker, 'hub_role_release', lambda actor: roles.release(actor, 'backend', 2, 1, 0, 'Handoff'))
    item = call(env, orch, 'hub_dispatch_create', lambda actor: service.create_dispatch(actor, 1, request['id'], task['id'], 'backend', 10, 'next'))
    assert item['target_binding'] is None
    acquired = call(env, worker, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'backend', released['revision'], 60))
    assert acquired['generation'] == 2
    heartbeat(env, worker, task, 0, 'new-progress', generation=2)
    checked = checkpoint(env, orch, service, item, 1, 'check')
    assert checked['state'] == 'dispatched' and checked['target_binding']['generation'] == 2
    assert checked['wake_delivery'] == 'not_attempted'


def test_other_role_progress_never_extends_target_dispatch(env):
    orch, worker, task, claim, roles, service, _, item = setup_dispatch(env)
    installation, _ = allow_progress(env, roles)
    roles.configure(OWNER, 'other', 0, [PROJECT], [installation], ['src'], 'Other', [])
    call(env, worker, 'hub_role_claim_release', lambda actor: roles.release_claim(actor, 'backend', 1, task['id'], claim['revision'], 'Transfer'))
    call(env, worker, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'other', 1, 60))
    call(env, worker, 'hub_role_claim', lambda actor: roles.claim(actor, 'other', 1, task['id'], 'other-claim'))
    env.clock.value += 8
    call(env, worker, 'hub_task_progress_update', lambda actor: TaskProgress(env.repo).update(
        actor, task['id'], 0, 'other-progress', [{'id': 'work', 'text': 'Work', 'done': False}],
        'Working elsewhere', '', role='other', generation=1))
    env.clock.value += 3
    stale = checkpoint(env, orch, service, item, 1, 'check')
    assert stale['state'] == 'stale' and stale['stale_basis'] == 'report_and_attributed_progress_timeout'


def test_target_lease_expiry_is_advisory_and_does_not_release_work(env):
    orch, worker, task, claim, roles, service, _, item = setup_dispatch(env)
    call(env, orch, 'hub_role_renew', lambda actor: roles.renew(actor, 'orchestrator', 2, 1, 3600))
    env.clock.value += 61
    stale = checkpoint(env, orch, service, item, 1, 'check')
    assert stale['state'] == 'stale' and stale['stale_basis'] == 'target_lease_or_authorization_lost'
    assert stale['wake_delivery'] == 'not_attempted' and not stale['execution_authorization']
    assert env.repo._read(claim['id'], 'role_claim')['status'] == 'claimed'
    assert env.repo._read(task['id'], 'tracking_task') == task


def test_role_generation_mismatch_is_stale_not_live(env):
    orch, _, _, _, roles, service, _, item = setup_dispatch(env)
    role = roles.owner_get(OWNER, 'backend')
    # Synthetic persisted corruption; public role API does not permit this.
    with env.repo._transaction() as db:
        role['generation'] += 1
        db.execute("UPDATE hub_entities SET data=? WHERE id=? AND kind='coordination_role'", (json.dumps(role), role['id']))
    stale = checkpoint(env, orch, service, item, 1, 'check')
    assert stale['state'] == 'stale' and stale['stale_basis'] == 'target_lease_or_authorization_lost'


@pytest.mark.parametrize('removed', ['role.read', 'dispatch.report', 'tracking.read'])
def test_target_loses_required_grant_marks_stale(env, removed):
    orch, _, _, _, roles, service, _, item = setup_dispatch(env)
    installation, caps = allow_progress(env, roles)
    env.bridge.grant_put(OWNER, installation, 2, [PROJECT], sorted(caps - {removed}))
    stale = checkpoint(env, orch, service, item, 1, 'check')
    assert stale['state'] == 'stale' and stale['stale_basis'] == 'target_lease_or_authorization_lost'


def test_asked_state_survives_target_decision_read_loss(env):
    orch, worker, _, _, roles, service, _, item = setup_dispatch(env)
    asked = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(actor, item['id'], 1, 1, 'ask', 'Question', 'ask', 'Choose', ['Yes', 'No']))
    installation, caps = allow_progress(env, roles)
    env.bridge.grant_put(OWNER, installation, 2, [PROJECT], sorted(caps - {'decision.read'}))
    stale = checkpoint(env, orch, service, item, asked['revision'], 'check')
    assert stale['state'] == 'asked' and stale['coordination_stale'] is True
    assert stale['decision_id'] == asked['decision_id'] and stale['stale_basis'] == 'target_lease_or_authorization_lost'


def test_legacy_unattributed_progress_does_not_extend_timeout(env):
    orch, worker, task, _, roles, service, _, item = setup_dispatch(env)
    allow_progress(env, roles)
    env.clock.value += 8
    heartbeat(env, worker, task, 0, 'heartbeat')
    baseline_id = str(uuid.uuid5(dispatch_namespace, 'progress-baseline:' + item['id']))
    # Synthetic legacy record lacked any pre-dispatch activity baseline.
    with env.repo._transaction() as db:
        db.execute("DELETE FROM hub_entities WHERE id=? AND kind='dispatch_progress_baseline'", (baseline_id,))
    env.clock.value += 3
    stale = checkpoint(env, orch, service, item, 1, 'check')
    assert stale['state'] == 'stale' and stale['stale_basis'] == 'report_and_attributed_progress_timeout'


def test_target_lease_expiring_during_checkpoint_rolls_back(env, monkeypatch):
    orch, _, _, _, roles, service, _, item = setup_dispatch(env)
    call(env, orch, 'hub_role_renew', lambda actor: roles.renew(actor, 'orchestrator', 2, 1, 3600))
    original = RoleRepository._guard
    def expire_at_commit(operation, expiry):
        if expiry == env.clock.value + 60:
            operation.commit_checks.append(lambda db: setattr(env.clock, 'value', expiry))
        original(operation, expiry)
    monkeypatch.setattr(RoleRepository, '_guard', staticmethod(expire_at_commit))
    with pytest.raises(HubError, match='role_lease_expired'):
        checkpoint(env, orch, service, item, 1, 'check')
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_requests WHERE key='check'").fetchone()[0] == 0
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'dispatched'


def test_activity_and_stale_never_publish_unscoped_pointers(env):
    orch, worker, task, _, roles, service, _, item = setup_dispatch(env)
    allow_progress(env, roles)
    env.clock.value += 1
    heartbeat(env, worker, task, 0, 'heartbeat')
    env.clock.value += 11
    checkpoint(env, orch, service, item, 1, 'check')
    with env.repo._connection() as db:
        actions = [row[0] for row in db.execute('SELECT action FROM hub_outbox ORDER BY sequence')]
    assert actions == []
    bus = FakeBus()
    result = asyncio.run(HubOutboxPublisher(env.repo, bus).drain_batch())
    assert result['failed'] == 0 and result['accepted'] == 0
    assert bus.requests == []


@pytest.mark.parametrize('first', ['heartbeat', 'checkpoint'])
def test_heartbeat_and_checkpoint_serialize_without_losing_activity(env, first):
    orch, worker, task, _, roles, service, _, item = setup_dispatch(env)
    allow_progress(env, roles)
    env.clock.value += 11
    entered, release, attempting, second_done = (threading.Event() for _ in range(4))
    def beat(hold):
        with env.bridge.admit(worker, 'hub_task_progress_update') as operation:
            if hold:
                entered.set()
                assert release.wait(5)
            result = TaskProgress(env.repo).update(operation.principal.actor, task['id'], 0, 'heartbeat',
                [{'id': 'work', 'text': 'Work', 'done': False}], 'Working', '', role='backend', generation=1)
        if not hold:
            second_done.set()
        return result
    def check(hold):
        with env.bridge.admit(orch, 'hub_dispatch_checkpoint') as operation:
            if hold:
                entered.set()
                assert release.wait(5)
            result = service.checkpoint(operation.principal.actor, item['id'], 1, 1, 'check')
        if not hold:
            second_done.set()
        return result
    def second_call():
        attempting.set()
        return check(False) if first == 'heartbeat' else beat(False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(beat if first == 'heartbeat' else check, True)
        assert entered.wait(5)
        second_future = pool.submit(second_call)
        assert attempting.wait(5) and not second_done.wait(.1)
        release.set()
        first_result, second_result = first_future.result(5), second_future.result(5)
    result = second_result if first == 'heartbeat' else first_result
    assert result['state'] == ('dispatched' if first == 'heartbeat' else 'stale')
    assert result['wake_delivery'] == 'not_attempted' and not result['execution_authorization']
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='role_progress_activity'").fetchone()[0] == 1
    assert env.repo._read(task['id'], 'tracking_task') == task


@pytest.mark.parametrize('first', ['revoke', 'checkpoint'])
def test_target_grant_revoke_and_checkpoint_serialize(env, first):
    orch, _, _, _, roles, service, _, item = setup_dispatch(env)
    installation = roles.owner_get(OWNER, 'backend')['installations'][0]
    if first == 'revoke':
        env.bridge.grant_revoke(OWNER, installation, 1)
        assert checkpoint(env, orch, service, item, 1, 'check')['stale_basis'] == 'target_lease_or_authorization_lost'
        return
    entered, release, attempting, revoked = (threading.Event() for _ in range(4))
    def check():
        with env.bridge.admit(orch, 'hub_dispatch_checkpoint') as operation:
            entered.set()
            assert release.wait(5)
            return service.checkpoint(operation.principal.actor, item['id'], 1, 1, 'check')
    def revoke():
        attempting.set()
        result = env.bridge.grant_revoke(OWNER, installation, 1)
        revoked.set()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        checking = pool.submit(check)
        assert entered.wait(5)
        revoking = pool.submit(revoke)
        assert attempting.wait(5) and not revoked.wait(.1)
        release.set()
        assert checking.result(5)['state'] == 'dispatched'
        assert revoking.result(5)['state'] == 'revoked'
    assert checkpoint(env, orch, service, item, 1, 'check-again')['stale_basis'] == 'target_lease_or_authorization_lost'


def test_clock_regression_fails_closed_without_mutation(env):
    orch, _, _, _, _, service, _, item = setup_dispatch(env)
    env.clock.value -= 1
    with pytest.raises(HubError, match='dispatch_clock_untrusted'):
        checkpoint(env, orch, service, item, 1, 'check')
    assert env.repo._read(item['id'], 'role_dispatch') == item


@pytest.mark.parametrize('expiry', ['bad', float('nan')])
def test_malformed_target_expiry_is_never_treated_live(env, expiry):
    orch, _, _, _, roles, service, _, item = setup_dispatch(env)
    target = roles.owner_get(OWNER, 'backend')
    # Synthetic persisted corruption, not a public mutation path.
    target['lease']['expires_at'] = expiry
    with env.repo._transaction() as db:
        db.execute("UPDATE hub_entities SET data=? WHERE id=? AND kind='coordination_role'", (json.dumps(target), target['id']))
    stale = checkpoint(env, orch, service, item, 1, 'check')
    assert stale['state'] == 'stale' and stale['stale_basis'] == 'target_lease_or_authorization_lost'
