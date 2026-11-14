import json
import sqlite3
import uuid

import pytest

from engineering_hub.dispatch import DispatchRepository
from engineering_hub.repository import HubError
from tests.test_attention_delivery import routed
from tests.test_session_hub_bridge import OWNER, PROJECT, call
from tests.test_stale_evaluator import evaluator_env, advance
from tests.test_stale_service_authority import owned


def report(env, worker, service, item, state, key, **kwargs):
    return call(env, worker, 'hub_dispatch_report', lambda actor:
        service.report(actor, item['id'], item['revision'], 1, state, 'Untrusted worker text', key, **kwargs))


def pending(env, orch, service, *, limit=50, actionable_only=False):
    with env.bridge.admit(orch, 'hub_addressed_attention_list') as op:
        return service.pending(op.principal.actor, PROJECT, 1, limit=limit, actionable_only=actionable_only)


def acknowledge(env, orch, service, intent):
    with env.bridge.admit(orch, 'hub_addressed_attention_ack') as op:
        return service.acknowledge(op.principal.actor, PROJECT, 1, intent['id'], intent['revision'])


@pytest.mark.parametrize('state', ['blocked', 'done', 'failed'])
def test_report_atomically_creates_private_attention_and_explicit_ack(evaluator_env, state):
    env = evaluator_env
    orch, worker, task, _, service, item, delivery, _ = routed(env)
    class Broker:
        def __init__(self): self.calls = []
        def signal(self, project): self.calls.append(project)
    broker = Broker(); env.repo._attention_hint_broker = broker
    try:
        with env.repo._connection() as db:
            outbox = db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0]
            task_bytes = db.execute('SELECT data FROM hub_entities WHERE id=?', (task['id'],)).fetchone()[0]
        result = report(env, worker, service, item, state, 'report')
        assert broker.calls == [PROJECT]
        # Retry returns the same dispatch and must not create/signal twice.
        assert report(env, worker, service, item, state, 'report') == result
        assert broker.calls == [PROJECT]
        page = pending(env, orch, delivery, actionable_only=True)
        assert len(page['items']) == 1
        intent = page['items'][0]
        assert intent['intent_type'] == 'worker_report' and intent['report_kind'] == state
        assert intent['report_revision'] == result['revision'] and intent['actionable'] is True
        assert 'Untrusted worker text' not in json.dumps(intent)
        assert intent['delivery'] == 'not_attempted' and intent['wake_verified'] is False
        with env.repo._connection() as db:
            assert db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0] == outbox
            assert db.execute('SELECT data FROM hub_entities WHERE id=?', (task['id'],)).fetchone()[0] == task_bytes
        first = acknowledge(env, orch, delivery, intent)
        assert first['state'] == 'agent_acknowledged' and first['wake_verified'] is False
        assert acknowledge(env, orch, delivery, intent) == first
        assert pending(env, orch, delivery)['items'] == []
        assert env.repo._read(intent['id'], 'orchestration_attention')['state'] == 'agent_acknowledged'
        assert len(call(env, orch, 'hub_dispatch_reports', lambda actor: service.reports(actor, item['id'], 20, 0))['items']) == 1
    finally:
        del env.repo._attention_hint_broker


def test_working_is_silent_but_ask_and_resume_are_independent(evaluator_env):
    env = evaluator_env
    orch, worker, _, _, service, item, delivery, _ = routed(env)
    working = report(env, worker, service, item, 'working', 'working')
    assert pending(env, orch, delivery)['items'] == []
    asked = report(env, worker, service, working, 'ask', 'ask', ask_title='Choose', ask_options=['A', 'B'])
    first = pending(env, orch, delivery)['items'][0]
    assert first['report_kind'] == 'ask'
    decision = env.repo.get_decision(asked['decision_id'])
    env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'A')
    resumed = report(env, worker, service, asked, 'resume', 'resume')
    views = pending(env, orch, delivery)['items']
    assert {value['report_kind'] for value in views} == {'ask', 'resume'}
    assert {value['report_revision'] for value in views} == {asked['revision'], resumed['revision']}
    acknowledge(env, orch, delivery, first)
    assert [value['report_kind'] for value in pending(env, orch, delivery)['items']] == ['resume']


def test_stale_and_report_intents_do_not_overwrite_history(evaluator_env):
    env = evaluator_env
    orch, worker, _, _, service, item, delivery, _ = routed(env)
    advance(env, 11)
    stale = call(env, orch, 'hub_dispatch_checkpoint', lambda actor: service.checkpoint(actor, item['id'], 1, 1, 'stale'))
    stale_id = stale['attention_intent_id']
    blocked = report(env, worker, service, stale, 'blocked', 'blocked')
    old = env.repo._read(stale_id, 'orchestration_attention')
    assert old['state'] == 'resolved_without_delivery' and blocked['attention_intent_id'] is None
    items = pending(env, orch, delivery)['items']
    assert len(items) == 1 and items[0]['report_kind'] == 'blocked' and items[0]['id'] != stale_id


def test_report_attention_failure_rolls_back_report_decision_and_signal(evaluator_env):
    env = evaluator_env
    _, worker, _, _, service, item, _, _ = routed(env)
    class Broker:
        def __init__(self): self.calls = []
        def signal(self, project): self.calls.append(project)
    broker = Broker(); env.repo._attention_hint_broker = broker
    try:
        with env.repo._transaction() as db:
            db.execute("CREATE TRIGGER synthetic_fail_report_attention BEFORE INSERT ON hub_entities WHEN NEW.kind='orchestration_attention' BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        with pytest.raises(sqlite3.IntegrityError):
            report(env, worker, service, item, 'ask', 'ask', ask_title='Choose', ask_options=['A', 'B'])
        assert env.repo._read(item['id'], 'role_dispatch') == item and broker.calls == []
        with env.repo._connection() as db:
            assert db.execute("SELECT count(*) FROM hub_entities WHERE kind IN ('decision','dispatch_report','orchestration_attention')").fetchone()[0] == 0
    finally:
        del env.repo._attention_hint_broker


def test_closed_report_intent_remains_owner_visible_and_reconciles(evaluator_env):
    env = evaluator_env
    orch, worker, task, _, service, item, delivery, _ = routed(env)
    updated = report(env, worker, service, item, 'done', 'done')
    intent = pending(env, orch, delivery)['items'][0]
    with env.repo._transaction() as db:
        task['status'] = 'completion_verified'
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(task), task['id']))
    assert pending(env, orch, delivery)['items'][0]['actionable'] is False
    with pytest.raises(HubError, match='attention_route_unavailable'):
        acknowledge(env, orch, delivery, intent)
    resolved = call(env, orch, 'hub_orchestrator_attention_reconcile_closed', lambda actor:
        service.reconcile_closed_attention(actor, 1, intent['id'], intent['revision'], updated['revision'], 'close-report'))
    assert resolved['state'] == 'resolved_task_closed_without_delivery'
    assert env.repo._read(item['id'], 'role_dispatch') == updated


def test_report_ack_cannot_be_reused_by_new_role_generation(evaluator_env):
    env = evaluator_env
    orch, worker, _, _, service, item, delivery, route = routed(env)
    blocked = report(env, worker, service, item, 'blocked', 'blocked')
    intent = pending(env, orch, delivery)['items'][0]
    receipt = acknowledge(env, orch, delivery, intent)
    roles = __import__('engineering_hub.roles', fromlist=['RoleRepository']).RoleRepository(env.repo)
    released = call(env, orch, 'hub_role_release', lambda actor: roles.release(actor, 'orchestrator', 2, 1, 0, 'handoff'))
    call(env, orch, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'orchestrator', released['revision'], 60))
    with env.bridge.admit(orch, 'hub_addressed_attention_ack') as op:
        with pytest.raises(HubError, match='attention_route_unavailable'):
            delivery.acknowledge(op.principal.actor, PROJECT, 1, intent['id'], intent['revision'])
    delivery.owner_configure(OWNER, PROJECT, route['revision'], route['installation_id'], route['session_id'], 2, 50)
    with env.bridge.admit(orch, 'hub_addressed_attention_ack') as op:
        with pytest.raises(HubError, match='attention_route_unavailable'):
            delivery.acknowledge(op.principal.actor, PROJECT, 2, intent['id'], intent['revision'])
    assert delivery.owner_receipts(OWNER, PROJECT)['items'][0]['id'] == receipt['id']
    assert env.repo._read(item['id'], 'role_dispatch') == blocked


def test_ask_resume_decision_grant_filters_report_intents_before_limit(evaluator_env):
    env = evaluator_env
    orch, worker, _, roles, service, item, delivery, route = routed(env)
    asked = report(env, worker, service, item, 'ask', 'ask', ask_title='Choose', ask_options=['A', 'B'])
    decision = env.repo.get_decision(asked['decision_id'])
    env.repo.answer_decision(OWNER, decision['id'], decision['revision'], 'A')
    resumed = report(env, worker, service, asked, 'resume', 'resume')
    second_task = env.repo.propose_tracking_task('codex', PROJECT, 'Other', 'other-task')
    call(env, worker, 'hub_role_claim', lambda actor: roles.claim(actor, 'backend', 1, second_task['id'], 'other-claim'))
    second = call(env, orch, 'hub_dispatch_create', lambda actor: service.create_dispatch(
        actor, 1, item['request_id'], second_task['id'], 'backend', 10, 'other-dispatch'))
    report(env, worker, service, second, 'blocked', 'other-blocked')
    with env.repo._transaction() as db:
        rows = db.execute("SELECT id,data FROM hub_entities WHERE kind='orchestration_attention' AND json_extract(data,'$.dispatch_id')=? ORDER BY id", (item['id'],)).fetchall()
        assert len(rows) == 2
        for index, row in enumerate(rows):
            record = json.loads(row['data']); old = row['id']
            record['id'] = str(uuid.UUID(int=index + 1))
            db.execute('DELETE FROM hub_entities WHERE id=?', (old,))
            db.execute("INSERT INTO hub_entities(id,kind,data) VALUES(?,'orchestration_attention',?)", (record['id'], json.dumps(record)))
    caps = ['dispatch.read', 'role.read', 'role.lease', 'tracking.read']
    env.bridge.grant_put(OWNER, route['installation_id'], route['grant_revision'], [PROJECT], caps)
    delivery.owner_configure(OWNER, PROJECT, route['revision'], route['installation_id'], route['session_id'], 1, 50)
    page = pending(env, orch, delivery, limit=1, actionable_only=True)
    assert [record['report_kind'] for record in page['items']] == ['blocked']
    assert page['next_after'] is None


def test_role_expiry_at_report_commit_rolls_back_attention_and_hint(evaluator_env, monkeypatch):
    env = evaluator_env
    _, worker, _, _, service, item, _, _ = routed(env)
    class Broker:
        def __init__(self): self.calls = []
        def signal(self, project): self.calls.append(project)
    broker = Broker(); env.repo._attention_hint_broker = broker
    original = env.repo._save
    def expire(db, actor, action, kind, value, *args, **kwargs):
        result = original(db, actor, action, kind, value, *args, **kwargs)
        if action == 'orchestration.report_attention_pending':
            advance(env, 61)
        return result
    monkeypatch.setattr(env.repo, '_save', expire)
    try:
        with pytest.raises(HubError, match='role_lease_expired'):
            report(env, worker, service, item, 'blocked', 'blocked')
        assert env.repo._read(item['id'], 'role_dispatch') == item and broker.calls == []
        with env.repo._connection() as db:
            assert db.execute("SELECT count(*) FROM hub_entities WHERE kind IN ('dispatch_report','orchestration_attention')").fetchone()[0] == 0
    finally:
        del env.repo._attention_hint_broker


@pytest.mark.asyncio
async def test_worker_report_reaches_authorized_subscribed_orchestrator_synthetically(owned, tmp_path):
    import asyncio
    from pathlib import Path
    from types import SimpleNamespace
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.transport_security import TransportSecuritySettings
    from agent_message_bus.app import create_app as create_bus
    from agent_message_bus.agent_sessions_config import AgentSessionsConfig
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    from tests.test_dispatch_coordination import setup_dispatch
    from tests.test_mcp_push import loopback
    repo, _, _, _ = owned
    origin = 'https://staging.test'
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude')
    bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=auth, startup_cleanup=False, push_events_operational=False,
        agent_sessions_config=AgentSessionsConfig(tmp_path / 'connections.sqlite3', OWNER, origin, hub_bridge=True),
        transport_security_settings=TransportSecuritySettings(allowed_hosts=['staging.test'], allowed_origins=[origin]))
    app = create_app(Settings(Path(repo.path), auth, AuthConfig(origin, beau_github_id=42)), repository=repo, bus_app=bus)
    bridge = app.state.session_hub_bridge
    env = SimpleNamespace(repo=repo, bridge=bridge, sessions=bridge.sessions)
    orch, worker, task, _, _, service, _, item = setup_dispatch(env)
    principal = env.sessions.authenticate_session(orch)
    app.state.attention_delivery.owner_configure(OWNER, PROJECT, 0, principal.installation_id, principal.session_id, 1, 50)
    async with loopback(app) as mcp_url:
        url = mcp_url.removesuffix('/mcp') + '/api/agent-sessions/v1/hub-attention-stream'
        async with httpx.AsyncClient(timeout=httpx.Timeout(5, read=None), headers={'Host': 'staging.test'}) as http:
            async with http.stream('GET', url, params={'project': PROJECT, 'generation': 1},
                                   headers={'Authorization': 'Bearer ' + orch}) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                assert await asyncio.wait_for(anext(lines), 3) == 'event: attention-ready'
                assert await asyncio.wait_for(anext(lines), 3) == 'data: {}'
                assert await asyncio.wait_for(anext(lines), 3) == ''
                updated = await asyncio.to_thread(report, env, worker, service, item, 'blocked', 'block')
                assert await asyncio.wait_for(anext(lines), 3) == 'event: attention-check'
                assert await asyncio.wait_for(anext(lines), 3) == 'data: {}'
                assert await asyncio.wait_for(anext(lines), 3) == ''
                async with httpx.AsyncClient(timeout=httpx.Timeout(5, read=None), headers={
                        'Host': 'staging.test', 'Authorization': 'Bearer ' + orch}) as mcp_http:
                    async with streamable_http_client(mcp_url, http_client=mcp_http, terminate_on_close=True) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool('hub_addressed_attention_list', {'project': PROJECT, 'generation': 1})
                            assert not result.isError
                            page = result.structuredContent or json.loads(result.content[0].text)
                            intent = page['items'][0]
                            assert intent['report_kind'] == 'blocked' and intent['report_revision'] == updated['revision']
                            ack = await session.call_tool('hub_addressed_attention_ack', {'project': PROJECT, 'generation': 1,
                                'intent_id': intent['id'], 'expected_revision': intent['revision']})
                            assert not ack.isError
                            receipt = ack.structuredContent or json.loads(ack.content[0].text)
                            assert receipt['state'] == 'agent_acknowledged' and receipt['wake_verified'] is False
    assert repo._read(task['id'], 'tracking_task') == task


def test_live_report_and_later_stale_attention_coexist(evaluator_env):
    env = evaluator_env
    orch, worker, _, roles, service, item, delivery, _ = routed(env)
    blocked = report(env, worker, service, item, 'blocked', 'block')
    report_intent = pending(env, orch, delivery)['items'][0]
    call(env, worker, 'hub_role_release', lambda actor: roles.release(actor, 'backend', 2, 1, 0, 'handoff'))
    stale = call(env, orch, 'hub_dispatch_checkpoint', lambda actor: service.checkpoint(actor, item['id'], blocked['revision'], 1, 'stale-after-block'))
    items = pending(env, orch, delivery, actionable_only=True)['items']
    assert {entry['id'] for entry in items} == {report_intent['id'], stale['attention_intent_id']}
    acknowledge(env, orch, delivery, report_intent)
    assert [entry['id'] for entry in pending(env, orch, delivery, actionable_only=True)['items']] == [stale['attention_intent_id']]
    assert env.repo._read(item['id'], 'role_dispatch')['attention_intent_id'] == stale['attention_intent_id']


def test_report_attention_requires_underlying_task_read(evaluator_env):
    env = evaluator_env
    orch, worker, _, _, service, item, delivery, route = routed(env)
    report(env, worker, service, item, 'blocked', 'blocked')
    original = pending(env, orch, delivery)['items'][0]
    env.bridge.grant_put(OWNER, route['installation_id'], route['grant_revision'], [PROJECT],
                         ['dispatch.read', 'role.read', 'role.lease'])
    delivery.owner_configure(OWNER, PROJECT, route['revision'], route['installation_id'], route['session_id'], 1, 50)
    assert pending(env, orch, delivery, actionable_only=True)['items'] == []
    with pytest.raises(HubError):
        acknowledge(env, orch, delivery, original)
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='attention_receipt'").fetchone()[0] == 0
