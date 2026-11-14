"""Synthetic worker answer inspection; no network messages or execution."""
import json
from types import SimpleNamespace

import pytest
from agent_message_bus.agent_session_client import ClientError
from engineering_hub.attention_client import AttentionAdapter
from engineering_hub.repository import HubError
from tests.test_attention_client import INSTALLATION, SESSION, PROJECT, DISPATCH
from tests.test_session_hub_bridge import OWNER, call, pair
from tests.test_stale_evaluator import evaluator_env
from tests.test_worker_answer_attention import answer_setup, ack


def inspect(env, worker, delivery, asked, decision):
    with env.bridge.admit(worker, 'hub_worker_decision_inspect') as operation:
        return delivery.worker_inspect_decision(operation.principal.actor, PROJECT, 'backend', 1,
            asked['id'], asked['revision'], decision['id'], decision['revision'])


def test_inspect_before_and_after_ack_without_mutation(evaluator_env):
    env = evaluator_env
    _, worker, task, _, service, asked, delivery, _, decision = answer_setup(env)
    answer = env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'Yes')
    before = inspect(env, worker, delivery, asked, answer)
    assert before['answer'] == 'Yes' and before['trust'] == 'untrusted_hub_data'
    assert before['wake_verified'] is False and before['execution_authorization'] is False
    ack(env, worker, delivery, asked, answer)
    assert inspect(env, worker, delivery, asked, answer) == before
    assert env.repo._read(task['id'], 'tracking_task') == task
    assert env.repo._read(asked['id'], 'role_dispatch') == asked
    resumed = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(
        actor, asked['id'], asked['revision'], 1, 'resume', 'Continuing', 'resume'))
    assert resumed['state'] == 'working'
    with pytest.raises(HubError, match='attention_route_unavailable'):
        inspect(env, worker, delivery, asked, answer)


def test_inspect_denies_wrong_reference_and_session_without_existence_leak(evaluator_env):
    env = evaluator_env
    _, worker, _, _, _, asked, delivery, _, decision = answer_setup(env)
    answer = env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'Yes')
    _, stranger, _ = pair(env, name='stranger')
    with pytest.raises(HubError):
        inspect(env, stranger, delivery, asked, answer)
    missing = dict(answer, id='77777777-7777-4777-8777-777777777777')
    wrong = dict(answer, revision=answer['revision'] + 1)
    messages = []
    for item in (missing, wrong):
        with pytest.raises(HubError) as failure:
            inspect(env, worker, delivery, asked, item)
        messages.append((failure.value.code, failure.value.status))
    assert messages[0] == messages[1] == ('attention_route_unavailable', 404)


def test_inspect_revalidates_decision_grant_and_closed_task(evaluator_env):
    env = evaluator_env
    _, worker, task, _, _, asked, delivery, route, decision = answer_setup(env)
    answer = env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'Yes')
    env.bridge.grant_put(OWNER, route['installation_id'], route['grant_revision'], [PROJECT],
                         ['dispatch.read', 'role.read', 'role.lease', 'tracking.read'])
    delivery.owner_configure(OWNER, PROJECT, route['revision'], route['installation_id'], route['session_id'], 1, 50, role='backend')
    with pytest.raises(HubError):
        inspect(env, worker, delivery, asked, answer)
    with env.repo._transaction() as db:
        task['status'] = 'completion_verified'
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(task), task['id']))
    with pytest.raises(HubError):
        inspect(env, worker, delivery, asked, answer)


@pytest.mark.asyncio
async def test_client_explicit_inspect_validates_and_labels_answer():
    decision = '77777777-7777-4777-8777-777777777777'
    calls, output = [], []
    class Session:
        async def call_tool(self, name, args):
            calls.append(name)
            result = dict(installation_id=INSTALLATION, session_id=SESSION) if name == 'session_identity' else dict(
                project=PROJECT, role='backend', generation=1, dispatch_id=DISPATCH, dispatch_revision=2,
                decision_id=decision, decision_revision=2, status='answered', answer='Untrusted answer',
                trust='untrusted_hub_data', execution_authorization=False, wake_verified=False)
            return SimpleNamespace(isError=False, structuredContent=result, content=[])
    client = AttentionAdapter(Session(), installation=INSTALLATION, session_id=SESSION,
                              project=PROJECT, role='backend', generation=1, emit=output.append)
    await client.inspect_answer(DISPATCH, 2, decision, 2)
    assert calls == ['session_identity', 'hub_worker_decision_inspect']
    assert output[0]['answer'] == 'Untrusted answer' and output[0]['trust'] == 'untrusted_hub_data'
    assert output[0]['execution_authorization'] is False and output[0]['wake_verified'] is False


@pytest.mark.parametrize('change', ['task_closed', 'decision_changed'])
def test_inspect_rechecks_snapshot_before_response(evaluator_env, change):
    import sqlite3
    env = evaluator_env
    _, worker, task, _, _, asked, delivery, _, decision = answer_setup(env)
    answered = env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'Yes')
    with env.bridge.admit(worker, 'hub_worker_decision_inspect') as operation:
        result = delivery.worker_inspect_decision(operation.principal.actor, PROJECT, 'backend', 1,
            asked['id'], asked['revision'], answered['id'], answered['revision'])
        assert result['answer'] == 'Yes'
        identity = task['id'] if change == 'task_closed' else answered['id']
        with sqlite3.connect(env.repo.path) as connection:
            record = json.loads(connection.execute('SELECT data FROM hub_entities WHERE id=?', (identity,)).fetchone()[0])
            if change == 'task_closed': record['status'] = 'completion_verified'
            else: record['revision'] += 1
            connection.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(record), identity))
        with pytest.raises(HubError, match='attention_route_unavailable'):
            operation.before_commit(env.repo.path, None)
