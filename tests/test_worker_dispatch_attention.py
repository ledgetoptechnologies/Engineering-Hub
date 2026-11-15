import json

import pytest

from engineering_hub.attention_hints import AttentionHintBroker
from engineering_hub.repository import HubError
from tests.test_attention_delivery import routed
from tests.test_session_hub_bridge import OWNER, PROJECT, call, pair
from tests.test_stale_evaluator import evaluator_env
from tests.test_stale_service_authority import owned


def setup_worker(env):
    orch, worker, task, roles, dispatch, item, delivery, orch_route = routed(env)
    principal = env.sessions.authenticate_session(worker)
    route = delivery.owner_configure(OWNER, PROJECT, 0, principal.installation_id,
                                     principal.session_id, 1, 50, role='backend')
    return orch, worker, task, roles, dispatch, item, delivery, route, orch_route


def pending(env, token, service, **kwargs):
    with env.bridge.admit(token, 'hub_worker_dispatch_pending') as op:
        return service.worker_pending(op.principal.actor, PROJECT, 'backend', 1, **kwargs)


def ack(env, token, service, item):
    with env.bridge.admit(token, 'hub_worker_dispatch_ack') as op:
        return service.worker_acknowledge(op.principal.actor, PROJECT, 'backend', 1, item['id'], item['revision'])


def test_worker_pending_ack_route_isolation_and_no_task_mutation(evaluator_env):
    env = evaluator_env
    orch, worker, task, roles, dispatch, item, delivery, route, orch_route = setup_worker(env)
    assert delivery.owner_get(OWNER, PROJECT)['id'] == orch_route['id']
    assert delivery.owner_get(OWNER, PROJECT, 'backend')['id'] == route['id']
    assert [r['dispatch_id'] for r in pending(env, worker, delivery)['items']] == [item['id']]
    with env.repo._connection() as db:
        outbox = db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0]
        task_bytes = db.execute('SELECT data FROM hub_entities WHERE id=?', (task['id'],)).fetchone()[0]
    receipt = ack(env, worker, delivery, item)
    assert receipt == ack(env, worker, delivery, item)
    assert receipt['state'] == 'agent_acknowledged' and not receipt['wake_verified']
    assert pending(env, worker, delivery)['items'] == []
    with pytest.raises(HubError):
        pending(env, orch, delivery)
    with env.repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0] == outbox
        assert db.execute('SELECT data FROM hub_entities WHERE id=?', (task['id'],)).fetchone()[0] == task_bytes
    assert env.repo._read(item['id'], 'role_dispatch') == item
    renewed = delivery.owner_configure(OWNER, PROJECT, route['revision'], route['installation_id'], route['session_id'], 1, 50, role='backend')
    assert ack(env, worker, delivery, item) == receipt
    delivery.owner_revoke(OWNER, PROJECT, renewed['revision'], 'backend')
    with pytest.raises(HubError):
        ack(env, worker, delivery, item)


def test_worker_pending_requires_current_binding_and_task_grant(evaluator_env):
    env = evaluator_env
    _, worker, _, _, _, item, delivery, route, _ = setup_worker(env)
    with env.repo._transaction() as db:
        item['target_binding']['session_id'] = '00000000-0000-4000-8000-000000000001'
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(item), item['id']))
    assert pending(env, worker, delivery)['items'] == []
    with pytest.raises(HubError):
        ack(env, worker, delivery, item)
    env.bridge.grant_put(OWNER, route['installation_id'], route['grant_revision'], [PROJECT],
                         ['dispatch.read', 'role.read', 'role.lease'])
    delivery.owner_configure(OWNER, PROJECT, route['revision'], route['installation_id'], route['session_id'], 1, 50, role='backend')
    assert pending(env, worker, delivery)['items'] == []


def test_worker_generation_change_invalidates_old_route_and_receipt(evaluator_env):
    env = evaluator_env
    _, worker, _, roles, _, item, delivery, route, _ = setup_worker(env)
    receipt = ack(env, worker, delivery, item)
    released = call(env, worker, 'hub_role_release', lambda actor: roles.release(actor, 'backend', 2, 1, 0, 'handoff'))
    call(env, worker, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'backend', released['revision'], 60))
    with pytest.raises(HubError):
        ack(env, worker, delivery, item)
    delivery.owner_configure(OWNER, PROJECT, route['revision'], route['installation_id'], route['session_id'], 2, 50, role='backend')
    with env.bridge.admit(worker, 'hub_worker_dispatch_ack') as op:
        with pytest.raises(HubError):
            delivery.worker_acknowledge(op.principal.actor, PROJECT, 'backend', 2, item['id'], item['revision'])
    assert receipt['generation'] == 1


@pytest.mark.asyncio
async def test_worker_broker_audience_isolated():
    import asyncio
    broker = AttentionHintBroker()
    a, qa = broker.register(('worker', PROJECT, 'backend'), 'a')
    b, qb = broker.register(('worker', PROJECT, 'frontend'), 'b')
    c, qc = broker.register(PROJECT, 'c')
    broker.signal(('worker', PROJECT, 'backend'))
    await asyncio.sleep(0)
    assert qa.qsize() == 1 and qb.empty() and qc.empty()
    for key in (a, b, c): broker.unregister(key)
    broker.close()


def test_dispatch_creation_signals_once_only_after_commit(evaluator_env):
    env = evaluator_env
    orch, worker, _, roles, dispatch, item, delivery, _, _ = setup_worker(env)
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Second', 'second-task')
    call(env, worker, 'hub_role_claim', lambda actor: roles.claim(actor, 'backend', 1, task['id'], 'claim-second'))
    class Broker:
        def __init__(self): self.calls = []
        def signal(self, audience): self.calls.append(audience)
    broker = Broker(); env.repo._attention_hint_broker = broker
    try:
        result = call(env, orch, 'hub_dispatch_create', lambda actor: dispatch.create_dispatch(actor, 1, item['request_id'], task['id'], 'backend', 10, 'second'))
        assert broker.calls == [('worker', PROJECT, 'backend')]
        assert call(env, orch, 'hub_dispatch_create', lambda actor: dispatch.create_dispatch(actor, 1, item['request_id'], task['id'], 'backend', 10, 'second')) == result
        assert broker.calls == [('worker', PROJECT, 'backend')]
    finally:
        del env.repo._attention_hint_broker


def test_closed_task_is_not_delivered_or_acknowledged(evaluator_env):
    env = evaluator_env
    _, worker, task, _, _, item, delivery, _, _ = setup_worker(env)
    with env.repo._transaction() as db:
        task['status'] = 'completion_verified'
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(task), task['id']))
    assert pending(env, worker, delivery)['items'] == []
    with pytest.raises(HubError):
        ack(env, worker, delivery, item)


@pytest.mark.asyncio
async def test_real_stream_catches_up_and_receives_new_dispatch(owned, tmp_path):
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
    orch, worker, _, _, roles, dispatch, _, item = setup_dispatch(env)
    principal = env.sessions.authenticate_session(worker)
    app.state.attention_delivery.owner_configure(OWNER, PROJECT, 0, principal.installation_id, principal.session_id, 1, 50, role='backend')
    async with loopback(app) as mcp_url:
        url = mcp_url.removesuffix('/mcp') + '/api/agent-sessions/v1/hub-attention-stream'
        async with httpx.AsyncClient(timeout=httpx.Timeout(5, read=None), headers={'Host': 'staging.test'}) as http:
            async with http.stream('GET', url, params={'project': PROJECT, 'generation': 1, 'role': 'backend'},
                                   headers={'Authorization': 'Bearer ' + worker}) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                assert await asyncio.wait_for(anext(lines), 3) == 'event: attention-ready'
                assert await asyncio.wait_for(anext(lines), 3) == 'data: {}'
                assert await asyncio.wait_for(anext(lines), 3) == ''
                assert await asyncio.wait_for(anext(lines), 3) == 'event: attention-check'
                assert await asyncio.wait_for(anext(lines), 3) == 'data: {}'
                assert await asyncio.wait_for(anext(lines), 3) == ''
                async with httpx.AsyncClient(timeout=httpx.Timeout(5, read=None), headers={
                        'Host': 'staging.test', 'Authorization': 'Bearer ' + worker}) as mcp_http:
                    async with streamable_http_client(mcp_url, http_client=mcp_http, terminate_on_close=True) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool('hub_worker_dispatch_pending', {'project': PROJECT, 'role': 'backend', 'generation': 1})
                            assert not result.isError
                            page = result.structuredContent or json.loads(result.content[0].text)
                            assert page['items'][0]['dispatch_id'] == item['id']
                            ack_result = await session.call_tool('hub_worker_dispatch_ack', {'project': PROJECT, 'role': 'backend',
                                'generation': 1, 'dispatch_id': item['id'], 'expected_revision': item['revision']})
                            assert not ack_result.isError
                            task = env.repo.propose_tracking_task('codex', PROJECT, 'Second', 'second-task')
                            await asyncio.to_thread(call, env, worker, 'hub_role_claim', lambda actor: roles.claim(actor, 'backend', 1, task['id'], 'claim-second'))
                            new = await asyncio.to_thread(call, env, orch, 'hub_dispatch_create', lambda actor:
                                dispatch.create_dispatch(actor, 1, item['request_id'], task['id'], 'backend', 10, 'second'))
                            assert await asyncio.wait_for(anext(lines), 3) == 'event: attention-check'
                            assert await asyncio.wait_for(anext(lines), 3) == 'data: {}'
                            page_result = await session.call_tool('hub_worker_dispatch_pending', {'project': PROJECT, 'role': 'backend', 'generation': 1})
                            page = page_result.structuredContent or json.loads(page_result.content[0].text)
                            assert [value['dispatch_id'] for value in page['items']] == [new['id']]


def test_acknowledged_row_is_filtered_before_limit(evaluator_env):
    env = evaluator_env
    orch, worker, _, roles, dispatch, item, delivery, _, _ = setup_worker(env)
    old = item['id']
    item['id'] = '00000000-0000-4000-8000-000000000001'
    with env.repo._transaction() as db:
        db.execute('UPDATE hub_entities SET id=?, data=? WHERE id=?', (item['id'], json.dumps(item), old))
    ack(env, worker, delivery, item)
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Second', 'second-task')
    call(env, worker, 'hub_role_claim', lambda actor: roles.claim(actor, 'backend', 1, task['id'], 'claim-second'))
    second = call(env, orch, 'hub_dispatch_create', lambda actor: dispatch.create_dispatch(actor, 1, item['request_id'], task['id'], 'backend', 10, 'second'))
    page = pending(env, worker, delivery, limit=1)
    assert [value['dispatch_id'] for value in page['items']] == [second['id']]
    assert page['next_after'] is None
    receipts = delivery.owner_worker_receipts(OWNER, PROJECT, 'backend')
    assert len(receipts['items']) == 1 and receipts['items'][0]['dispatch_id'] == item['id']
    assert delivery.owner_worker_receipts(OWNER, PROJECT, 'frontend')['items'] == []


def test_worker_owner_http_authorization_and_gateway(owned, tmp_path):
    from pathlib import Path
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from mcp.server.transport_security import TransportSecuritySettings
    from agent_message_bus.app import create_app as create_bus
    from agent_message_bus.agent_sessions_config import AgentSessionsConfig
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    from engineering_hub.private_gateway import browser_route_allowed
    from tests.test_dispatch_coordination import setup_dispatch
    repo, _, _, _ = owned
    origin = 'https://staging.test'
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude')
    bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=auth, startup_cleanup=False, push_events_operational=False,
        agent_sessions_config=AgentSessionsConfig(tmp_path / 'connections.sqlite3', OWNER, origin, hub_bridge=True),
        transport_security_settings=TransportSecuritySettings(allowed_hosts=['staging.test'], allowed_origins=[origin]))
    app = create_app(Settings(Path(repo.path), auth, AuthConfig(origin, beau_github_id=42)), repository=repo, bus_app=bus)
    env = SimpleNamespace(repo=repo, bridge=app.state.session_hub_bridge, sessions=app.state.session_hub_bridge.sessions)
    _, worker, _, _, _, _, _, item = setup_dispatch(env)
    principal = env.sessions.authenticate_session(worker)
    path = '/api/hub/v1/projects/engineering-hub/worker-attention-routes/backend'
    receipts = '/api/hub/v1/projects/engineering-hub/worker-attention-receipts/backend'
    payload = dict(expected_revision=0, installation_id=principal.installation_id,
                   session_id=principal.session_id, generation=1, lifetime_seconds=50)
    assert browser_route_allowed('GET', path) and browser_route_allowed('PUT', path)
    assert browser_route_allowed('POST', path + '/revoke') and browser_route_allowed('GET', receipts)
    assert not browser_route_allowed('DELETE', path)
    with TestClient(app, base_url=origin) as client:
        assert client.get(path).status_code == client.get(receipts).status_code == 401
        owner_session = app.state.sessions.create_session(42)
        client.cookies.set('hub_session', owner_session['cookie_token'])
        csrf = {'Origin': origin, 'X-Hub-CSRF': owner_session['csrf_token']}
        assert client.put(path, json=payload).status_code == 403
        assert client.put(path, headers={**csrf, 'Authorization': 'Bearer synthetic-codex'}, json=payload).status_code == 403
        assert client.put(path, headers=csrf, json=payload).status_code == 200
        assert client.get(path).json()['role_id'] == 'backend'
        assert client.get(receipts).json()['items'] == []
        assert client.post(path + '/revoke', headers=csrf, json={'expected_revision': 1}).json()['enabled'] is False
        other = app.state.sessions.create_session(43)
        client.cookies.set('hub_session', other['cookie_token'])
        assert client.get(path).status_code == client.get(receipts).status_code == 403


def test_failed_dispatch_commit_never_signals(evaluator_env):
    import sqlite3
    env = evaluator_env
    orch, worker, _, roles, dispatch, item, _, _, _ = setup_worker(env)
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Second', 'second-task')
    call(env, worker, 'hub_role_claim', lambda actor: roles.claim(actor, 'backend', 1, task['id'], 'claim-second'))
    class Broker:
        def __init__(self): self.calls = []
        def signal(self, audience): self.calls.append(audience)
    broker = Broker(); env.repo._attention_hint_broker = broker
    try:
        with env.repo._transaction() as db:
            db.execute("CREATE TRIGGER fail_worker_dispatch BEFORE INSERT ON hub_entities WHEN NEW.kind='role_dispatch' BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        with pytest.raises(sqlite3.IntegrityError):
            call(env, orch, 'hub_dispatch_create', lambda actor: dispatch.create_dispatch(actor, 1, item['request_id'], task['id'], 'backend', 10, 'second'))
        assert broker.calls == []
    finally:
        del env.repo._attention_hint_broker
