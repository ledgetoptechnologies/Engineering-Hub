import asyncio
import json

import pytest

from agent_message_bus.agent_sessions import AgentSessionError
from engineering_hub.attention_delivery import AttentionDelivery
from engineering_hub.attention_hints import AttentionHintBroker
from engineering_hub.dispatch import DispatchRepository
from engineering_hub.repository import HubError
from tests.test_dispatch_coordination import setup_dispatch
from tests.test_session_hub_bridge import OWNER, PROJECT, call, pair
from tests.test_stale_evaluator import evaluator_env, advance
from tests.test_stale_service_authority import owned


def service_for(env):
    return AttentionDelivery(env.repo, env.bridge, clock=env.clock,
        boottime=lambda: env.tick.value, boot_id=lambda: '11111111-1111-4111-8111-111111111111')


def routed(env):
    orch, worker, task, _, roles, dispatch, _, item = setup_dispatch(env)
    principal = env.sessions.authenticate_session(orch)
    service = service_for(env)
    route = service.owner_configure(OWNER, PROJECT, 0, principal.installation_id,
                                    principal.session_id, 1, 50)
    return orch, worker, task, roles, dispatch, item, service, route


def test_owner_admitted_session_reads_and_acks_idempotently(evaluator_env):
    env = evaluator_env
    orch, _, _, _, dispatch, item, service, route = routed(env)
    advance(env, 11)
    stale = call(env, orch, 'hub_dispatch_checkpoint', lambda actor: dispatch.checkpoint(actor, item['id'], 1, 1, 'check'))
    intent = env.repo._read(stale['attention_intent_id'], 'orchestration_attention')
    with env.bridge.admit(orch, 'hub_addressed_attention_list') as op:
        result = service.pending(op.principal.actor, PROJECT, 1)
    assert [value['id'] for value in result['items']] == [intent['id']]
    with env.repo._connection() as db:
        before = db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0]
    with env.bridge.admit(orch, 'hub_addressed_attention_ack') as op:
        receipt = service.acknowledge(op.principal.actor, PROJECT, 1, intent['id'], intent['revision'])
    with env.bridge.admit(orch, 'hub_addressed_attention_ack') as op:
        again = service.acknowledge(op.principal.actor, PROJECT, 1, intent['id'], intent['revision'])
    assert receipt == again and receipt['state'] == 'agent_acknowledged'
    assert not receipt['wake_verified'] and not receipt['task_completion_verified']
    with env.repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0] == before
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='attention_receipt'").fetchone()[0] == 1


def test_other_session_and_revoked_route_cannot_read(evaluator_env):
    env = evaluator_env
    orch, _, _, _, _, _, service, route = routed(env)
    _, other, _ = pair(env, name='other')
    with env.bridge.admit(other, 'hub_addressed_attention_list') as op:
        with pytest.raises(HubError, match='attention_route_unavailable'):
            service.pending(op.principal.actor, PROJECT, 1)
    service.owner_revoke(OWNER, PROJECT, route['revision'])
    with env.bridge.admit(orch, 'hub_addressed_attention_list') as op:
        with pytest.raises(HubError, match='attention_route_unavailable'):
            service.pending(op.principal.actor, PROJECT, 1)


def test_route_expires_and_scope_is_not_a_capability(evaluator_env):
    env = evaluator_env
    orch, _, _, _, _, _, service, route = routed(env)
    advance(env, 50)
    with env.bridge.admit(orch, 'hub_addressed_attention_list') as op:
        with pytest.raises(HubError, match='attention_route_unavailable'):
            service.pending(op.principal.actor, PROJECT, 1)


@pytest.mark.asyncio
async def test_broker_is_bounded_coalesced_and_project_scoped():
    broker = AttentionHintBroker(maximum=2)
    one, q1 = broker.register('project-a', 'session-a')
    two, q2 = broker.register('project-b', 'session-b')
    broker.signal('project-a'); broker.signal('project-a')
    await asyncio.sleep(0)
    assert q1.qsize() == 1 and q2.empty()
    broker.unregister(one); broker.unregister(two); broker.close()
    with pytest.raises(HubError, match='attention_stream_unavailable'):
        broker.register('project-a', 'session-a')


def test_same_installation_different_session_is_denied(evaluator_env):
    from engineering_hub.roles import RoleRepository
    env = evaluator_env
    installation, first, secret = pair(env, name='same-installation')
    second = env.sessions.exchange(secret)['token']
    roles = RoleRepository(env.repo)
    roles.configure(OWNER, 'orchestrator', 0, [PROJECT], [installation], ['src'], 'Coordinate', [])
    call(env, first, 'hub_role_acquire', lambda actor: roles.acquire(actor, 'orchestrator', 1, 60))
    principal = env.sessions.authenticate_session(first)
    service = service_for(env)
    service.owner_configure(OWNER, PROJECT, 0, installation, principal.session_id, 1, 50)
    with env.bridge.admit(second, 'hub_addressed_attention_list') as op:
        with pytest.raises(HubError, match='attention_route_unavailable'):
            service.pending(op.principal.actor, PROJECT, 1)


def test_route_revoke_serializes_with_authorized_read(evaluator_env, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    env = evaluator_env
    orch, _, _, _, dispatch, item, service, route = routed(env)
    advance(env, 11)
    call(env, orch, 'hub_dispatch_checkpoint', lambda actor: dispatch.checkpoint(actor, item['id'], 1, 1, 'check'))
    entered, release, attempted = (threading.Event() for _ in range(3))
    original = DispatchRepository._attention_in
    def held(self, *args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(DispatchRepository, '_attention_in', held)
    def read():
        with env.bridge.admit(orch, 'hub_addressed_attention_list') as op:
            return service.pending(op.principal.actor, PROJECT, 1)
    def revoke():
        attempted.set()
        return service.owner_revoke(OWNER, PROJECT, route['revision'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(read)
        assert entered.wait(10)
        revoking = pool.submit(revoke)
        assert attempted.wait(10) and not revoking.done()
        release.set()
        assert len(reading.result(timeout=10)['items']) == 1
        assert revoking.result(timeout=10)['enabled'] is False
    with env.bridge.admit(orch, 'hub_addressed_attention_list') as op:
        with pytest.raises(HubError, match='attention_route_unavailable'):
            service.pending(op.principal.actor, PROJECT, 1)


@pytest.mark.asyncio
async def test_real_sse_exact_session_hint_postcommit_and_revocation(owned, tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from types import SimpleNamespace
    import httpx
    from mcp.server.transport_security import TransportSecuritySettings
    from agent_message_bus.app import create_app as create_bus
    from agent_message_bus.agent_sessions_config import AgentSessionsConfig
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
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
    orch, _, _, _, _, dispatch, _, item = setup_dispatch(env)
    _, other, _ = pair(env, name='other-stream')
    principal = env.sessions.authenticate_session(orch)
    app.state.attention_delivery.owner_configure(OWNER, PROJECT, 0, principal.installation_id, principal.session_id, 1, 50)
    with repo._transaction() as db:
        item['last_report_at'] -= 20  # Synthetic overdue report, lease remains live.
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(item), item['id']))
    async with loopback(app) as mcp_url:
        # An abandoned response before generator entry must own no subscription.
        from starlette.requests import Request
        endpoint = next(route.endpoint for route in app.routes
                        if getattr(route, 'path', None) == '/api/agent-sessions/v1/hub-attention-stream')
        request = Request({'type': 'http', 'method': 'GET', 'path': '/api/agent-sessions/v1/hub-attention-stream',
                           'headers': [(b'host', b'staging.test'), (b'authorization', ('Bearer ' + orch).encode())],
                           'client': ('127.0.0.1', 12345), 'query_string': b''})
        abandoned = await endpoint(request, PROJECT, 1)
        assert repo._attention_hint_broker._listeners == {}
        await abandoned.body_iterator.aclose()
        assert repo._attention_hint_broker._listeners == {}
        # Cancel during blocked session subscription; the eventual subscription
        # and broker reservation must both be drained before the task exits.
        import threading
        entered, release = threading.Event(), threading.Event()
        original_subscribe = env.sessions.subscribe_session
        def blocked_subscribe(*args, **kwargs):
            entered.set()
            assert release.wait(10)
            return original_subscribe(*args, **kwargs)
        monkeypatch.setattr(env.sessions, 'subscribe_session', blocked_subscribe)
        cancelling = await endpoint(request, PROJECT, 1)
        started = asyncio.create_task(anext(cancelling.body_iterator))
        assert await asyncio.to_thread(entered.wait, 5)
        started.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await started
        assert repo._attention_hint_broker._listeners == {}
        assert env.sessions._listeners == {}
        monkeypatch.setattr(env.sessions, 'subscribe_session', original_subscribe)
        url = mcp_url.removesuffix('/mcp') + '/api/agent-sessions/v1/hub-attention-stream'
        params = {'project': PROJECT, 'generation': 1}
        async with httpx.AsyncClient(timeout=httpx.Timeout(5, read=None), headers={'Host': 'staging.test'}) as client:
            denied = await client.get(url, params=params, headers={'Authorization': 'Bearer ' + other})
            assert denied.status_code == 404
            async with client.stream('GET', url, params=params, headers={'Authorization': 'Bearer ' + orch}) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                assert await asyncio.wait_for(anext(lines), 3) == 'event: attention-ready'
                assert await asyncio.wait_for(anext(lines), 3) == 'data: {}'
                assert await asyncio.wait_for(anext(lines), 3) == ''
                stale = await asyncio.to_thread(call, env, orch, 'hub_dispatch_checkpoint',
                    lambda actor: dispatch.checkpoint(actor, item['id'], 1, 1, 'stream-check'))
                assert stale['state'] == 'stale'
                assert await asyncio.wait_for(anext(lines), 3) == 'event: attention-check'
                assert await asyncio.wait_for(anext(lines), 3) == 'data: {}'
                assert await asyncio.wait_for(anext(lines), 3) == ''
                from mcp import ClientSession
                from mcp.client.streamable_http import streamable_http_client
                async with httpx.AsyncClient(timeout=httpx.Timeout(5, read=None), headers={
                        'Host': 'staging.test', 'Authorization': 'Bearer ' + orch}) as mcp_http:
                    async with streamable_http_client(mcp_url, http_client=mcp_http, terminate_on_close=True) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            listed = await session.call_tool('hub_addressed_attention_list', {'project': PROJECT, 'generation': 1})
                            assert not listed.isError
                            result = json.loads(listed.content[0].text)
                            assert [entry['id'] for entry in result['items']] == [stale['attention_intent_id']]
                            args = {'project': PROJECT, 'generation': 1, 'intent_id': stale['attention_intent_id'], 'expected_revision': 1}
                            acknowledged = await session.call_tool('hub_addressed_attention_ack', args)
                            retry = await session.call_tool('hub_addressed_attention_ack', args)
                            assert not acknowledged.isError and not retry.isError
                            assert json.loads(acknowledged.content[0].text)['id'] == json.loads(retry.content[0].text)['id']
                await asyncio.to_thread(env.sessions.revoke_session, OWNER, principal.session_id, 1)
                with pytest.raises(StopAsyncIteration):
                    await asyncio.wait_for(anext(lines), 3)


def test_failed_checkpoint_never_signals(evaluator_env):
    import sqlite3
    env = evaluator_env
    orch, _, _, _, dispatch, item, service, _ = routed(env)
    class RecordingBroker:
        calls = []
        def signal(self, project):
            self.calls.append(project)
    broker = RecordingBroker()
    env.repo._attention_hint_broker = broker
    try:
        with env.repo._transaction() as db:
            db.execute("CREATE TRIGGER synthetic_fail_attention BEFORE INSERT ON hub_entities WHEN NEW.kind='orchestration_attention' BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        advance(env, 11)
        with pytest.raises(sqlite3.IntegrityError):
            call(env, orch, 'hub_dispatch_checkpoint', lambda actor: dispatch.checkpoint(actor, item['id'], 1, 1, 'fail-check'))
        assert broker.calls == []
        assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'dispatched'
    finally:
        del env.repo._attention_hint_broker


def test_grant_revocation_denies_route_and_receipt(evaluator_env):
    env = evaluator_env
    orch, _, _, _, dispatch, item, service, route = routed(env)
    advance(env, 11)
    stale = call(env, orch, 'hub_dispatch_checkpoint', lambda actor: dispatch.checkpoint(actor, item['id'], 1, 1, 'check'))
    env.bridge.grant_revoke(OWNER, route['installation_id'], route['grant_revision'])
    with pytest.raises(HubError):
        with env.bridge.admit(orch, 'hub_addressed_attention_ack') as op:
            service.acknowledge(op.principal.actor, PROJECT, 1, stale['attention_intent_id'], 1)
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='attention_receipt'").fetchone()[0] == 0


def test_owner_http_route_requires_owner_and_csrf(owned, tmp_path):
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
    repo, _, _, _ = owned
    origin = 'https://staging.test'
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude')
    bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=auth, startup_cleanup=False, push_events_operational=False,
        agent_sessions_config=AgentSessionsConfig(tmp_path / 'connections.sqlite3', OWNER, origin, hub_bridge=True),
        transport_security_settings=TransportSecuritySettings(allowed_hosts=['staging.test'], allowed_origins=[origin]))
    app = create_app(Settings(Path(repo.path), auth, AuthConfig(origin, beau_github_id=42)), repository=repo, bus_app=bus)
    env = SimpleNamespace(repo=repo, bridge=app.state.session_hub_bridge, sessions=app.state.session_hub_bridge.sessions)
    orch, _, _, _, _, _, _, _ = setup_dispatch(env)
    principal = env.sessions.authenticate_session(orch)
    path = '/api/hub/v1/projects/engineering-hub/attention-route'
    payload = dict(expected_revision=0, installation_id=principal.installation_id,
                   session_id=principal.session_id, generation=1, lifetime_seconds=50)
    assert browser_route_allowed('GET', path) and browser_route_allowed('PUT', path)
    assert browser_route_allowed('POST', path + '/revoke') and not browser_route_allowed('DELETE', path)
    with TestClient(app, base_url=origin) as client:
        assert client.get(path).status_code == 401
        owner_session = app.state.sessions.create_session(42)
        client.cookies.set('hub_session', owner_session['cookie_token'])
        csrf = {'Origin': origin, 'X-Hub-CSRF': owner_session['csrf_token']}
        assert client.put(path, json=payload).status_code == 403
        assert client.put(path, headers={**csrf, 'Authorization': 'Bearer synthetic-codex'}, json=payload).status_code == 403
        configured = client.put(path, headers=csrf, json=payload)
        assert configured.status_code == 200 and configured.json()['enabled'] is True
        assert client.get(path).json()['session_id'] == principal.session_id
        receipts = client.get('/api/hub/v1/projects/engineering-hub/attention-receipts')
        assert receipts.status_code == 200 and receipts.json()['items'] == []
        assert client.post(path + '/revoke', headers=csrf, json={'expected_revision': 1}).json()['enabled'] is False
        other = app.state.sessions.create_session(43)
        client.cookies.set('hub_session', other['cookie_token'])
        assert client.get(path).status_code == 403


def test_ack_retry_after_task_closure_is_unavailable_but_owner_can_audit(evaluator_env):
    import json
    env = evaluator_env
    orch, _, task, _, dispatch, item, service, _ = routed(env)
    advance(env, 11)
    stale = call(env, orch, 'hub_dispatch_checkpoint', lambda actor: dispatch.checkpoint(actor, item['id'], 1, 1, 'check'))
    intent_id = stale['attention_intent_id']
    with env.bridge.admit(orch, 'hub_addressed_attention_ack') as op:
        receipt = service.acknowledge(op.principal.actor, PROJECT, 1, intent_id, 1)
    with env.repo._transaction() as db:
        task['status'] = 'completion_verified'  # Synthetic closure after acknowledgment.
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(task), task['id']))
    with env.bridge.admit(orch, 'hub_addressed_attention_ack') as op:
        with pytest.raises(HubError, match='attention_route_unavailable'):
            service.acknowledge(op.principal.actor, PROJECT, 1, intent_id, 1)
    assert service.owner_receipts(OWNER, PROJECT)['items'][0]['id'] == receipt['id']


def test_route_reapproval_does_not_duplicate_receipt(evaluator_env):
    env = evaluator_env
    orch, _, _, _, dispatch, item, service, route = routed(env)
    advance(env, 11)
    stale = call(env, orch, 'hub_dispatch_checkpoint', lambda actor: dispatch.checkpoint(actor, item['id'], 1, 1, 'check'))
    with env.bridge.admit(orch, 'hub_addressed_attention_ack') as op:
        first = service.acknowledge(op.principal.actor, PROJECT, 1, stale['attention_intent_id'], 1)
    service.owner_configure(OWNER, PROJECT, route['revision'], route['installation_id'], route['session_id'], 1, 30)
    with env.bridge.admit(orch, 'hub_addressed_attention_ack') as op:
        retry = service.acknowledge(op.principal.actor, PROJECT, 1, stale['attention_intent_id'], 1)
    assert retry['id'] == first['id'] and retry['route_revision'] == first['route_revision']
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='attention_receipt'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_sse_suppresses_hints_without_underlying_task_grant(owned, tmp_path):
    from pathlib import Path
    from types import SimpleNamespace
    import httpx
    from mcp.server.transport_security import TransportSecuritySettings
    from agent_message_bus.app import create_app as create_bus
    from agent_message_bus.agent_sessions_config import AgentSessionsConfig
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    from engineering_hub.session_hub_bridge import CAPABILITIES
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
    orch, _, _, _, _, dispatch, _, item = setup_dispatch(env)
    with repo._transaction() as db:
        item['last_report_at'] -= 20
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(item), item['id']))
    stale = call(env, orch, 'hub_dispatch_checkpoint', lambda actor: dispatch.checkpoint(actor, item['id'], 1, 1, 'check'))
    assert stale['state'] == 'stale'
    principal = env.sessions.authenticate_session(orch)
    caps = [value for value in CAPABILITIES if value not in ('tracking.read', 'task.read')]
    bridge.grant_put(OWNER, principal.installation_id, 1, [PROJECT], caps)
    app.state.attention_delivery.owner_configure(OWNER, PROJECT, 0, principal.installation_id, principal.session_id, 1, 50)
    async with loopback(app) as mcp_url:
        url = mcp_url.removesuffix('/mcp') + '/api/agent-sessions/v1/hub-attention-stream'
        async with httpx.AsyncClient(timeout=httpx.Timeout(5, read=None), headers={'Host': 'staging.test'}) as client:
            async with client.stream('GET', url, params={'project': PROJECT, 'generation': 1},
                                     headers={'Authorization': 'Bearer ' + orch}) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                assert await asyncio.wait_for(anext(lines), 3) == 'event: attention-ready'
                assert await asyncio.wait_for(anext(lines), 3) == 'data: {}'
                assert await asyncio.wait_for(anext(lines), 3) == ''
                repo._attention_hint_broker.signal(PROJECT)
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(anext(lines), 0.3)


def test_actionable_filter_precedes_limit_and_skips_closed(evaluator_env):
    import uuid
    env = evaluator_env
    orch, _, task, _, dispatch, item, service, _ = routed(env)
    advance(env, 11)
    stale = call(env, orch, 'hub_dispatch_checkpoint', lambda actor: dispatch.checkpoint(actor, item['id'], 1, 1, 'check'))
    actual_intent = env.repo._read(stale['attention_intent_id'], 'orchestration_attention')
    closed_id = '00000000-0000-4000-8000-000000000000'
    closed_task = dict(task, id=str(uuid.uuid4()), status='completion_verified')
    closed_dispatch = dict(stale, id=str(uuid.uuid4()), task_id=closed_task['id'], attention_intent_id=closed_id)
    closed_intent = dict(actual_intent, id=closed_id, dispatch_id=closed_dispatch['id'])
    with env.repo._transaction() as db:
        for kind, value in (('tracking_task', closed_task), ('role_dispatch', closed_dispatch), ('orchestration_attention', closed_intent)):
            db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)', (value['id'], kind, json.dumps(value)))
    with env.bridge.admit(orch, 'hub_addressed_attention_list') as op:
        result = service.pending(op.principal.actor, PROJECT, 1, limit=1, actionable_only=True)
    assert [value['id'] for value in result['items']] == [actual_intent['id']]
    assert result['items'][0]['actionable'] is True


@pytest.mark.asyncio
async def test_broker_shutdown_waits_for_stream_cleanup():
    broker = AttentionHintBroker()
    identity, queue = broker.register('project-a', 'session-a')
    broker.close()
    draining = asyncio.create_task(asyncio.to_thread(broker.wait_closed))
    await asyncio.sleep(0)
    assert not draining.done()
    await asyncio.wait_for(queue.get(), 3)
    broker.unregister(identity)
    await asyncio.wait_for(draining, 3)


@pytest.mark.asyncio
async def test_shutdown_drains_blocked_initial_validation_and_abandoned_response(owned, tmp_path, monkeypatch):
    import threading
    from pathlib import Path
    from types import SimpleNamespace
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.requests import Request
    from agent_message_bus.app import create_app as create_bus
    from agent_message_bus.agent_sessions_config import AgentSessionsConfig
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
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
    orch, _, _, _, _, _, _, _ = setup_dispatch(env)
    principal = env.sessions.authenticate_session(orch)
    app.state.attention_delivery.owner_configure(OWNER, PROJECT, 0, principal.installation_id, principal.session_id, 1, 50)
    entered, release = threading.Event(), threading.Event()
    original = app.state.attention_delivery.pending
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(app.state.attention_delivery, 'pending', blocked)
    async with loopback(app):
        endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', None) == '/api/agent-sessions/v1/hub-attention-stream')
        request = Request({'type': 'http', 'method': 'GET', 'path': '/api/agent-sessions/v1/hub-attention-stream',
                           'headers': [(b'host', b'staging.test'), (b'authorization', ('Bearer ' + orch).encode())],
                           'client': ('127.0.0.1', 12345), 'query_string': b''})
        opening = asyncio.create_task(endpoint(request, PROJECT, 1))
        assert await asyncio.to_thread(entered.wait, 5)
        broker = repo._attention_hint_broker
        broker.close()
        draining = asyncio.create_task(asyncio.to_thread(broker.wait_closed))
        await asyncio.sleep(0)
        assert not draining.done()
        release.set()
        response = await asyncio.wait_for(opening, 5)
        await asyncio.wait_for(draining, 5)
        await response.body_iterator.aclose()
        assert broker._listeners == {}
