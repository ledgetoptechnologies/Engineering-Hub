"""Synthetic worker answer routing; no external sends or inferred execution."""
import json

import pytest

from engineering_hub.repository import HubError
from tests.test_session_hub_bridge import OWNER, PROJECT, call
from tests.test_stale_evaluator import evaluator_env
from tests.test_worker_dispatch_attention import setup_worker, pending


def answer_setup(env):
    orch, worker, task, roles, service, dispatch, delivery, route, _ = setup_worker(env)
    asked = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(
        actor, dispatch['id'], 1, 1, 'ask', 'Private question', 'ask', 'Choose', ['Yes', 'No']))
    decision = env.repo.get_decision(asked['decision_id'])
    return orch, worker, task, roles, service, asked, delivery, route, decision


def ack(env, token, delivery, dispatch, decision):
    with env.bridge.admit(token, 'hub_worker_dispatch_ack') as op:
        return delivery.worker_acknowledge(op.principal.actor, PROJECT, 'backend', 1, dispatch['id'],
                                            dispatch['revision'], decision['id'], decision['revision'])


def test_answer_generates_private_durable_event_and_explicit_idempotent_receipt(evaluator_env):
    env = evaluator_env
    _, worker, task, _, service, asked, delivery, route, decision = answer_setup(env)
    class Broker:
        def __init__(self): self.calls = []
        def signal(self, audience): self.calls.append(audience)
    broker = Broker(); env.repo._attention_hint_broker = broker
    try:
        answered = env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'Yes')
        assert broker.calls == [('worker', PROJECT, 'backend')]
        entry = pending(env, worker, delivery)['items'][0]
        assert entry['event_type'] == 'decision_answered' and entry['decision_id'] == decision['id']
        assert entry['decision_revision'] == answered['revision'] and 'Yes' not in json.dumps(entry)
        first = ack(env, worker, delivery, asked, answered)
        assert first == ack(env, worker, delivery, asked, answered)
        assert first['wake_verified'] is False and first['execution_authorization'] is False
        assert pending(env, worker, delivery)['items'] == []
        assert env.repo._read(task['id'], 'tracking_task') == task
        with env.repo._connection() as db:
            assert db.execute('SELECT count(*) FROM hub_outbox WHERE entity_id=?', (decision['id'],)).fetchone()[0] == 0
    finally:
        del env.repo._attention_hint_broker


def test_answer_event_requires_decision_read_and_current_binding(evaluator_env):
    env = evaluator_env
    _, worker, _, roles, service, asked, delivery, route, decision = answer_setup(env)
    answered = env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'Yes')
    env.bridge.grant_put(OWNER, route['installation_id'], route['grant_revision'], [PROJECT],
                         ['dispatch.read', 'role.read', 'role.lease', 'tracking.read'])
    delivery.owner_configure(OWNER, PROJECT, route['revision'], route['installation_id'], route['session_id'], 1, 50, role='backend')
    assert pending(env, worker, delivery)['items'] == []
    with pytest.raises(HubError):
        ack(env, worker, delivery, asked, answered)


def test_answer_failure_never_signals_or_creates_receipt(evaluator_env):
    env = evaluator_env
    _, worker, _, _, _, asked, delivery, _, decision = answer_setup(env)
    class Broker:
        def __init__(self): self.calls = []
        def signal(self, audience): self.calls.append(audience)
    broker = Broker(); env.repo._attention_hint_broker = broker
    try:
        with pytest.raises(HubError):
            env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'Not an option')
        assert broker.calls == [] and pending(env, worker, delivery)['items'] == []
        with env.repo._connection() as db:
            assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='worker_dispatch_receipt'").fetchone()[0] == 0
    finally:
        del env.repo._attention_hint_broker


def test_answer_catchup_after_route_renewal_and_prior_dispatch_receipt(evaluator_env):
    env = evaluator_env
    _, worker, _, _, service, asked, delivery, route, decision = answer_setup(env)
    # A previously acknowledged dispatch must not suppress a later answer.
    with env.repo._transaction() as db:
        db.execute("INSERT INTO hub_entities(id,kind,data) VALUES(?,'worker_dispatch_receipt',?)", (
            '77777777-7777-4777-8777-777777777777', json.dumps(dict(dispatch_id=asked['id'],
                session_id=route['session_id'], generation=1))))
    answered = env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'Yes')
    delivery.owner_configure(OWNER, PROJECT, route['revision'], route['installation_id'], route['session_id'], 1, 50, role='backend')
    assert pending(env, worker, delivery)['items'][0]['decision_id'] == answered['id']
    assert ack(env, worker, delivery, asked, answered) == ack(env, worker, delivery, asked, answered)


def test_answer_ack_rejects_wrong_decision_revision_and_closed_task(evaluator_env):
    env = evaluator_env
    _, worker, task, _, _, asked, delivery, _, decision = answer_setup(env)
    answered = env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'Yes')
    with env.bridge.admit(worker, 'hub_worker_dispatch_ack') as op:
        with pytest.raises(HubError):
            delivery.worker_acknowledge(op.principal.actor, PROJECT, 'backend', 1, asked['id'], asked['revision'],
                answered['id'], answered['revision'] + 1)
    with env.repo._transaction() as db:
        task['status'] = 'completion_verified'
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(task), task['id']))
    assert pending(env, worker, delivery)['items'] == []
    with pytest.raises(HubError):
        ack(env, worker, delivery, asked, answered)


def test_answer_ack_rejects_new_role_generation(evaluator_env):
    env = evaluator_env
    _, worker, _, roles, _, asked, delivery, route, decision = answer_setup(env)
    answered = env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'Yes')
    receipt = ack(env, worker, delivery, asked, answered)
    released = call(env, worker, 'hub_role_release', lambda actor: roles.release(actor, 'backend', 2, 1, 0, 'handoff'))
    call(env, worker, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'backend', released['revision'], 60))
    delivery.owner_configure(OWNER, PROJECT, route['revision'], route['installation_id'], route['session_id'], 2, 50, role='backend')
    with env.bridge.admit(worker, 'hub_worker_dispatch_ack') as op:
        with pytest.raises(HubError):
            delivery.worker_acknowledge(op.principal.actor, PROJECT, 'backend', 2, asked['id'], asked['revision'],
                                        answered['id'], answered['revision'])
    assert receipt['generation'] == 1


def test_answer_without_decision_read_is_filtered_before_limit(evaluator_env):
    env = evaluator_env
    orch, worker, _, roles, service, item, delivery, route, _ = setup_worker(env)
    old = item['id']; item['id'] = '00000000-0000-4000-8000-000000000001'
    with env.repo._transaction() as db:
        db.execute('UPDATE hub_entities SET id=?, data=? WHERE id=?', (item['id'], json.dumps(item), old))
    asked = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(
        actor, item['id'], 1, 1, 'ask', 'Private', 'ask', 'Choose', ['Yes', 'No']))
    decision = env.repo.get_decision(asked['decision_id'])
    env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'Yes')
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Second', 'second-task')
    call(env, worker, 'hub_role_claim', lambda actor: roles.claim(actor, 'backend', 1, task['id'], 'second-claim'))
    second = call(env, orch, 'hub_dispatch_create', lambda actor: service.create_dispatch(
        actor, 1, item['request_id'], task['id'], 'backend', 10, 'second'))
    env.bridge.grant_put(OWNER, route['installation_id'], route['grant_revision'], [PROJECT],
                         ['dispatch.read', 'role.read', 'role.lease', 'tracking.read'])
    delivery.owner_configure(OWNER, PROJECT, route['revision'], route['installation_id'], route['session_id'], 1, 50, role='backend')
    page = pending(env, worker, delivery, limit=1)
    assert [entry['dispatch_id'] for entry in page['items']] == [second['id']] and page['next_after'] is None


@pytest.mark.parametrize('corruption', ['invalid_json', 'bad_revision', 'wrong_id'])
def test_malformed_answer_does_not_mask_other_dispatch(evaluator_env, corruption):
    env = evaluator_env
    orch, worker, _, roles, service, asked, delivery, _, decision = answer_setup(env)
    env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'Yes')
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Second', 'second-task')
    call(env, worker, 'hub_role_claim', lambda actor: roles.claim(actor, 'backend', 1, task['id'], 'second-claim'))
    second = call(env, orch, 'hub_dispatch_create', lambda actor: service.create_dispatch(
        actor, 1, asked['request_id'], task['id'], 'backend', 10, 'second'))
    with env.repo._transaction() as db:
        old = asked['id']; asked['id'] = '00000000-0000-4000-8000-000000000001'
        db.execute('UPDATE hub_entities SET id=?,data=? WHERE id=?', (asked['id'], json.dumps(asked), old))
        raw = env.repo._get(db, decision['id'], 'decision')
        raw['origin_dispatch_id'] = asked['id']
        if corruption == 'bad_revision': raw['revision'] = 0
        if corruption == 'wrong_id': raw['id'] = '77777777-7777-4777-8777-777777777777'
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', ('{' if corruption == 'invalid_json' else json.dumps(raw), decision['id']))
    page = pending(env, worker, delivery, limit=1)
    assert [entry['dispatch_id'] for entry in page['items']] == [second['id']] and page['next_after'] is None
