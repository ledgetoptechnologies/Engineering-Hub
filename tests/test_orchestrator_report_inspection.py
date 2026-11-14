"""Synthetic scoped report inspection, no external messages or execution."""
import json
import sqlite3
from types import SimpleNamespace

import pytest
from agent_message_bus.agent_session_client import ClientError
from engineering_hub.attention_client import AttentionAdapter
from engineering_hub.repository import HubError
from tests.test_attention_client import INSTALLATION, SESSION, PROJECT, DISPATCH, INTENT
from tests.test_attention_delivery import routed
from tests.test_report_attention import report, pending, acknowledge
from tests.test_session_hub_bridge import OWNER, pair
from tests.test_stale_evaluator import evaluator_env


def inspect(env, token, delivery, intent):
    with env.bridge.admit(token, 'hub_orchestrator_report_inspect') as op:
        return delivery.inspect_report(op.principal.actor, PROJECT, 1, intent['id'], intent['revision'],
                                       intent['report_id'], intent['report_revision'])


def test_inspect_report_before_and_after_ack(evaluator_env):
    env = evaluator_env
    orch, worker, task, _, service, item, delivery, _ = routed(env)
    report(env, worker, service, item, 'blocked', 'blocked')
    intent = pending(env, orch, delivery)['items'][0]
    before = inspect(env, orch, delivery, intent)
    assert before['note'] == 'Untrusted worker text' and before['report_kind'] == 'blocked'
    assert before['trust'] == 'untrusted_hub_data' and not before['wake_verified']
    acknowledge(env, orch, delivery, intent)
    assert inspect(env, orch, delivery, intent) == before
    assert env.repo._read(task['id'], 'tracking_task') == task
    _, stranger, _ = pair(env, name='outsider')
    with pytest.raises(HubError): inspect(env, stranger, delivery, intent)


def test_historical_ask_requires_decision_read(evaluator_env):
    env = evaluator_env
    orch, worker, _, _, service, item, delivery, route = routed(env)
    asked = report(env, worker, service, item, 'ask', 'ask', ask_title='Choose', ask_options=['A', 'B'])
    intent = pending(env, orch, delivery)['items'][0]
    decision = env.repo.get_decision(asked['decision_id'])
    env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'A')
    report(env, worker, service, asked, 'resume', 'resume')
    assert inspect(env, orch, delivery, intent)['decision_id'] == decision['id']
    env.bridge.grant_put(OWNER, route['installation_id'], route['grant_revision'], [PROJECT],
                         ['dispatch.read', 'role.read', 'role.lease', 'tracking.read'])
    delivery.owner_configure(OWNER, PROJECT, route['revision'], route['installation_id'], route['session_id'], 1, 50)
    with pytest.raises(HubError): inspect(env, orch, delivery, intent)


@pytest.mark.parametrize('change', ['task_closed', 'report_changed'])
def test_response_rechecks_task_and_report(evaluator_env, change):
    env = evaluator_env
    orch, worker, task, _, service, item, delivery, _ = routed(env)
    report(env, worker, service, item, 'done', 'done')
    intent = pending(env, orch, delivery)['items'][0]
    with env.bridge.admit(orch, 'hub_orchestrator_report_inspect') as op:
        result = delivery.inspect_report(op.principal.actor, PROJECT, 1, intent['id'], intent['revision'],
                                         intent['report_id'], intent['report_revision'])
        assert result['note'] == 'Untrusted worker text'
        identity = task['id'] if change == 'task_closed' else intent['report_id']
        with sqlite3.connect(env.repo.path) as connection:
            data = json.loads(connection.execute('SELECT data FROM hub_entities WHERE id=?', (identity,)).fetchone()[0])
            if change == 'task_closed': data['status'] = 'completion_verified'
            else: data['note'] = 'changed'
            connection.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(data), identity))
        with pytest.raises(HubError, match='attention_route_unavailable'):
            op.before_commit(env.repo.path, None)


@pytest.mark.asyncio
async def test_client_inspects_explicitly_and_rejects_wrong_role():
    report_id = '77777777-7777-4777-8777-777777777777'
    calls, output = [], []
    class Session:
        async def call_tool(self, name, args):
            calls.append(name)
            value = dict(installation_id=INSTALLATION, session_id=SESSION) if name == 'session_identity' else dict(
                project=PROJECT, intent_id=INTENT, intent_revision=1, dispatch_id=DISPATCH, report_id=report_id,
                report_revision=2, report_kind='blocked', note='Untrusted report', decision_id=None,
                trust='untrusted_hub_data', execution_authorization=False, wake_verified=False)
            return SimpleNamespace(isError=False, structuredContent=value, content=[])
    client = AttentionAdapter(Session(), installation=INSTALLATION, session_id=SESSION,
                              project=PROJECT, generation=1, emit=output.append)
    await client.inspect_report(INTENT, 1, report_id, 2)
    assert calls == ['session_identity', 'hub_orchestrator_report_inspect']
    assert output[0]['note'] == 'Untrusted report' and output[0]['trust'] == 'untrusted_hub_data'
    worker = AttentionAdapter(Session(), installation=INSTALLATION, session_id=SESSION,
                              project=PROJECT, generation=1, role='backend', emit=output.append)
    with pytest.raises(ClientError): await worker.inspect_report(INTENT, 1, report_id, 2)


@pytest.mark.parametrize('kind', ['blocked', 'ask'])
def test_acknowledged_report_is_not_exposed_to_replacement_session(evaluator_env, kind):
    from tests.test_session_hub_bridge import call
    env = evaluator_env
    orch, worker, _, roles, service, item, delivery, route = routed(env)
    report(env, worker, service, item, kind, 'report', **({'ask_title': 'Choose', 'ask_options': ['A', 'B']} if kind == 'ask' else {}))
    intent = pending(env, orch, delivery)['items'][0]
    acknowledge(env, orch, delivery, intent)
    installation, other, _ = pair(env, name='new-orchestrator')
    released = call(env, orch, 'hub_role_release', lambda actor: roles.release(actor, 'orchestrator', 2, 1, 0, 'handoff'))
    configured = roles.configure(OWNER, 'orchestrator', released['revision'], [PROJECT],
                                 [route['installation_id'], installation], ['src'], 'Coordinate', [])
    call(env, other, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'orchestrator', configured['revision'], 60))
    principal = env.sessions.authenticate_session(other)
    delivery.owner_configure(OWNER, PROJECT, route['revision'], installation, principal.session_id, 2, 50)
    with env.bridge.admit(other, 'hub_orchestrator_report_inspect') as op:
        with pytest.raises(HubError, match='attention_route_unavailable'):
            delivery.inspect_report(op.principal.actor, PROJECT, 2, intent['id'], intent['revision'],
                                    intent['report_id'], intent['report_revision'])


@pytest.mark.parametrize('kind', ['blocked', 'ask'])
def test_cross_project_and_missing_report_are_indistinguishable(evaluator_env, kind):
    env = evaluator_env
    orch, worker, _, _, service, item, delivery, _ = routed(env)
    report(env, worker, service, item, kind, 'report', **({'ask_title': 'Choose', 'ask_options': ['A', 'B']} if kind == 'ask' else {}))
    intent = pending(env, orch, delivery)['items'][0]
    observed = []
    with env.bridge.admit(orch, 'hub_orchestrator_report_inspect') as op:
        for project, identity in (('project-alpha', intent['id']),
                                  (PROJECT, '77777777-7777-4777-8777-777777777777')):
            with pytest.raises(HubError) as failure:
                delivery.inspect_report(op.principal.actor, project, 1, identity, intent['revision'],
                                        intent['report_id'], intent['report_revision'])
            observed.append((failure.value.code, failure.value.status))
    assert observed == [('attention_route_unavailable', 404)] * 2


@pytest.mark.parametrize('status', ['open', 'answered', 'withdrawn'])
def test_ask_report_includes_current_bounded_question(evaluator_env, status):
    env = evaluator_env
    orch, worker, _, _, service, item, delivery, _ = routed(env)
    asked = report(env, worker, service, item, 'ask', 'ask', ask_title='Choose', ask_options=['A', 'B'])
    intent = pending(env, orch, delivery)['items'][0]
    decision = env.repo.get_decision(asked['decision_id'])
    if status == 'answered':
        decision = env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'A')
    elif status == 'withdrawn':
        decision = env.repo.withdraw(OWNER, 'decision', decision['id'], decision['revision'], 'Not needed')
    result = inspect(env, orch, delivery, intent)
    assert result['question'] == dict(id=decision['id'], revision=decision['revision'], title='Choose',
                                      options=['A', 'B'], status=status, answer='A' if status == 'answered' else None)
    assert result['trust'] == 'untrusted_hub_data' and result['execution_authorization'] is False
    acknowledge(env, orch, delivery, intent)
    assert inspect(env, orch, delivery, intent)['question'] == result['question']


def test_decision_change_is_rechecked_before_report_inspection_response(evaluator_env):
    env = evaluator_env
    orch, worker, _, _, service, item, delivery, _ = routed(env)
    asked = report(env, worker, service, item, 'ask', 'ask', ask_title='Choose', ask_options=['A', 'B'])
    intent = pending(env, orch, delivery)['items'][0]
    with env.bridge.admit(orch, 'hub_orchestrator_report_inspect') as op:
        result = delivery.inspect_report(op.principal.actor, PROJECT, 1, intent['id'], intent['revision'],
                                         intent['report_id'], intent['report_revision'])
        assert result['question']['status'] == 'open'
        with sqlite3.connect(env.repo.path) as db:
            data = json.loads(db.execute('SELECT data FROM hub_entities WHERE id=?', (asked['decision_id'],)).fetchone()[0])
            data.update(status='answered', answer='A', revision=data['revision'] + 1)
            db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(data), asked['decision_id']))
        with pytest.raises(HubError, match='attention_route_unavailable'):
            op.before_commit(env.repo.path, None)


@pytest.mark.parametrize('field,value', [('title', 'x' * 201), ('options', ['A']), ('revision', True), ('status', 'unknown')])
def test_malformed_linked_question_fails_closed(evaluator_env, field, value):
    env = evaluator_env
    orch, worker, _, _, service, item, delivery, _ = routed(env)
    asked = report(env, worker, service, item, 'ask', 'ask', ask_title='Choose', ask_options=['A', 'B'])
    intent = pending(env, orch, delivery)['items'][0]
    with env.repo._transaction() as db:
        decision = env.repo._get(db, asked['decision_id'], 'decision')
        decision[field] = value
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(decision), asked['decision_id']))
    with pytest.raises(HubError, match='attention_route_unavailable'):
        inspect(env, orch, delivery, intent)


@pytest.mark.asyncio
async def test_client_validates_question_before_emitting():
    report_id = '77777777-7777-4777-8777-777777777777'
    decision_id = '88888888-8888-4888-8888-888888888888'
    output = []
    class Session:
        malformed = False
        async def call_tool(self, name, args):
            if name == 'session_identity':
                value = dict(installation_id=INSTALLATION, session_id=SESSION)
            else:
                question = dict(id=decision_id, revision=1, title='Choose', options=['A', 'B'], status='open', answer=None)
                if self.malformed: question['options'] = ['A']
                value = dict(project=PROJECT, intent_id=INTENT, intent_revision=1, dispatch_id=DISPATCH,
                    report_id=report_id, report_revision=2, report_kind='ask', note='Untrusted', decision_id=decision_id,
                    question=question, trust='untrusted_hub_data', execution_authorization=False, wake_verified=False)
            return SimpleNamespace(isError=False, structuredContent=value, content=[])
    session = Session()
    client = AttentionAdapter(session, installation=INSTALLATION, session_id=SESSION,
                              project=PROJECT, generation=1, emit=output.append)
    await client.inspect_report(INTENT, 1, report_id, 2)
    assert output[0]['question']['options'] == ['A', 'B'] and output[0]['trust'] == 'untrusted_hub_data'
    session.malformed = True
    with pytest.raises(ClientError, match='invalid_attention_response'):
        await client.inspect_report(INTENT, 1, report_id, 2)
    assert len(output) == 1
