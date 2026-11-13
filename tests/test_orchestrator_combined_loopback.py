"""Synthetic loopback coordination proof, not agent wake or staging acceptance."""
import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import TransportSecuritySettings

from agent_message_bus.agent_sessions_config import AgentSessionsConfig
from agent_message_bus.app import create_app as create_bus
from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from tests.test_mcp_push import loopback
from tests.test_session_hub_bridge import OWNER, PROJECT, call, data, pair, sdk, unified
from tests.test_stale_service_authority import owned


@pytest.mark.asyncio
async def test_distinct_role_sessions_coordinate_over_real_loopback(owned, tmp_path):
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
    orch_id, orch_token, _ = pair(env, name='orchestrator', family='codex')
    worker_id, worker_token, _ = pair(env, name='worker', family='claude')
    _, reviewer_token, _ = pair(env, name='reviewer', family='hermes')
    _, decision_reader_token, _ = pair(env, name='decision-reader', caps=('decision.read',))
    assert orch_id != worker_id
    owner = app.state.sessions.create_session(42)

    async with loopback(app) as mcp_url:
        base = mcp_url.removesuffix('/mcp')
        async with AsyncExitStack() as stack:
            owner_http = await stack.enter_async_context(httpx.AsyncClient(timeout=5, headers={
                'Host': 'staging.test', 'Origin': origin, 'X-Hub-CSRF': owner['csrf_token'],
                'Cookie': 'hub_session=' + owner['cookie_token']}))
            for role, installation in (('orchestrator', orch_id), ('backend', worker_id)):
                response = await owner_http.put(base + '/api/hub/v1/roles/' + role, json=dict(
                    expected_revision=0, projects=[PROJECT], installations=[installation], paths=['src'],
                    standing_rules='Synthetic coordination', tool_scope=[]))
                assert response.status_code == 200, response.text

            async def client(token):
                http = await stack.enter_async_context(httpx.AsyncClient(timeout=httpx.Timeout(5, read=None),
                    headers={'Host': 'staging.test', 'Authorization': 'Bearer ' + token}))
                read, write, _ = await stack.enter_async_context(streamable_http_client(mcp_url, http_client=http, terminate_on_close=True))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                return session

            orch, worker, reviewer, decision_reader = [await client(token) for token in
                (orch_token, worker_token, reviewer_token, decision_reader_token)]

            async def invoke(session, name, args):
                result = await session.call_tool(name, args)
                assert not result.isError, (name, result.content)
                return result.structuredContent or json.loads(result.content[0].text)

            for session, role in ((orch, 'orchestrator'), (worker, 'backend')):
                lease = await invoke(session, 'hub_role_acquire', dict(role=role, expected_revision=1, lease_seconds=60))
                assert lease['generation'] == 1 and lease['execution_authorization'] is False

            for role, token in (('orchestrator', orch_token), ('backend', worker_token)):
                principal = env.sessions.authenticate_session(token)
                suffix = '/attention-route' if role == 'orchestrator' else '/worker-attention-routes/backend'
                response = await owner_http.put(base + '/api/hub/v1/projects/' + PROJECT + suffix, json=dict(
                    expected_revision=0, installation_id=principal.installation_id,
                    session_id=principal.session_id, generation=1, lifetime_seconds=50))
                assert response.status_code == 200, response.text

            task = await invoke(worker, 'hub_tracking_task_propose', dict(project=PROJECT, title='Synthetic task', client_request_id='task'))
            claim = await invoke(worker, 'hub_role_claim', dict(role='backend', generation=1, task_id=task['id'], client_request_id='claim'))
            assert claim['execution_authorization'] is False
            request = await invoke(orch, 'hub_orchestration_request_create', dict(generation=1, project=PROJECT,
                title='Synthetic request', client_request_id='request'))

            stream_http = await stack.enter_async_context(httpx.AsyncClient(timeout=httpx.Timeout(5, read=None),
                headers={'Host': 'staging.test', 'Authorization': 'Bearer ' + worker_token}))
            response = await stack.enter_async_context(stream_http.stream('GET', base + '/api/agent-sessions/v1/hub-attention-stream',
                params={'project': PROJECT, 'generation': 1, 'role': 'backend'}))
            assert response.status_code == 200
            lines = response.aiter_lines()
            for expected in ('event: attention-ready', 'data: {}', ''):
                assert await asyncio.wait_for(anext(lines), 3) == expected

            dispatch = await invoke(orch, 'hub_dispatch_create', dict(generation=1, request_id=request['id'],
                task_id=task['id'], role='backend', window_seconds=30, client_request_id='dispatch'))
            assert dispatch['execution_authorization'] is False
            assert await asyncio.wait_for(anext(lines), 3) == 'event: attention-check'
            assert await asyncio.wait_for(anext(lines), 3) == 'data: {}'
            assert await asyncio.wait_for(anext(lines), 3) == ''
            inbox = await invoke(worker, 'hub_role_dispatch_inbox', dict(role='backend', generation=1, project=PROJECT))
            assert [x['dispatch']['id'] for x in inbox['items']] == [dispatch['id']]
            pending = await invoke(worker, 'hub_worker_dispatch_pending', dict(role='backend', generation=1, project=PROJECT))
            assert [x['dispatch_id'] for x in pending['items']] == [dispatch['id']]
            receipt = await invoke(worker, 'hub_worker_dispatch_ack', dict(role='backend', generation=1, project=PROJECT,
                dispatch_id=dispatch['id'], expected_revision=dispatch['revision']))
            assert receipt['state'] == 'agent_acknowledged' and receipt['wake_verified'] is False
            progress = await invoke(worker, 'hub_task_progress_update', dict(task_id=task['id'], expected_revision=0,
                client_request_id='progress', checklist=[dict(id='a', text='Synthetic step', done=True)],
                status_line='Reported progress', blocked_on='', role='backend', generation=1))
            assert progress['percent'] == 100 and repo._read(task['id'], 'tracking_task')['revision'] == task['revision']
            asked = await invoke(worker, 'hub_dispatch_report', dict(dispatch_id=dispatch['id'], expected_revision=dispatch['revision'],
                generation=1, state='ask', note='Synthetic question', client_request_id='ask', ask_title='Continue?', ask_options=['Yes', 'No']))
            attention = await invoke(orch, 'hub_addressed_attention_list', dict(project=PROJECT, generation=1))
            assert len(attention['items']) == 1 and attention['items'][0]['dispatch_id'] == dispatch['id']
            item = attention['items'][0]
            ack = await invoke(orch, 'hub_addressed_attention_ack', dict(project=PROJECT, generation=1,
                intent_id=item['id'], expected_revision=item['revision']))
            assert ack['wake_verified'] is False and ack['task_completion_verified'] is False
            decision = repo.get_decision(asked['decision_id'])
            raw = repo._read(decision['id'], 'decision')
            assert raw['origin_dispatch_id'] == dispatch['id']
            assert 'origin_dispatch_id' not in decision and 'origin_dispatch_revision' not in decision
            generic = await invoke(decision_reader, 'hub_decision_get', dict(decision_id=decision['id']))
            assert 'origin_dispatch_id' not in generic and 'origin_dispatch_revision' not in generic
            denied = await decision_reader.call_tool('hub_dispatch_get', dict(dispatch_id=dispatch['id']))
            assert denied.isError
            permitted = await invoke(worker, 'hub_dispatch_decisions', dict(dispatch_id=dispatch['id']))
            assert permitted['items'][0]['origin_dispatch_id'] == dispatch['id']
            from engineering_hub.dto import DecisionDTO
            from pydantic import ValidationError
            with pytest.raises(ValidationError):
                DecisionDTO.model_validate({**raw, 'unexpected_private_field': 'not exposed'})
            overview = await owner_http.get(base + '/api/hub/v1/overview')
            assert overview.status_code == 200, overview.text
            shown = next(d for d in overview.json()['decisions'] if d['id'] == decision['id'])
            assert 'origin_dispatch_id' not in shown and 'origin_dispatch_revision' not in shown
            async with httpx.AsyncClient(timeout=5, headers={'Host': 'staging.test'}) as anonymous:
                assert (await anonymous.get(base + '/api/hub/v1/overview')).status_code == 401
            response = await owner_http.post(base + '/api/hub/v1/decisions/' + decision['id'] + '/answer',
                json=dict(expected_revision=decision['revision'], answer='Yes'))
            assert repo.get_decision(decision['id'])['status'] == 'answered'
            assert response.status_code == 200, response.text
            assert 'origin_dispatch_id' not in response.json() and 'origin_dispatch_revision' not in response.json()
            for expected in ('event: attention-check', 'data: {}', ''):
                assert await asyncio.wait_for(anext(lines), 3) == expected
            answered_page = await invoke(worker, 'hub_worker_dispatch_pending', dict(role='backend', generation=1, project=PROJECT))
            answered_ref = answered_page['items'][0]
            assert answered_ref['event_type'] == 'decision_answered' and answered_ref['decision_id'] == decision['id']
            assert answered_ref['decision_revision'] == response.json()['revision']
            received = await invoke(worker, 'hub_worker_dispatch_ack', dict(role='backend', generation=1, project=PROJECT,
                dispatch_id=dispatch['id'], expected_revision=asked['revision'], decision_id=decision['id'],
                decision_revision=answered_ref['decision_revision']))
            assert received['wake_verified'] is False and received['event_type'] == 'decision_answered'
            assert (await invoke(worker, 'hub_worker_dispatch_pending', dict(role='backend', generation=1, project=PROJECT)))['items'] == []
            with repo._connection() as db:
                assert db.execute('SELECT count(*) FROM hub_outbox WHERE entity_id=?', (decision['id'],)).fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM hub_audit WHERE entity_id=? AND action='decision.answered'", (decision['id'],)).fetchone()[0] == 1
            retry = await owner_http.post(base + '/api/hub/v1/decisions/' + decision['id'] + '/answer',
                json=dict(expected_revision=decision['revision'], answer='Yes'))
            assert retry.status_code == 409
            resumed = await invoke(worker, 'hub_dispatch_report', dict(dispatch_id=dispatch['id'], expected_revision=asked['revision'],
                generation=1, state='resume', note='Continuing', client_request_id='resume'))
            done = await invoke(worker, 'hub_dispatch_report', dict(dispatch_id=dispatch['id'], expected_revision=resumed['revision'],
                generation=1, state='done', note='Reported done', client_request_id='done'))
            assert done['completion_verified'] is False and repo._read(task['id'], 'tracking_task')['status'] == 'open'
            links = [dict(label='Synthetic evidence', url='https://example.com/evidence')]
            complete = await invoke(worker, 'hub_tracking_task_update', dict(task_id=task['id'], expected_revision=task['revision'],
                client_request_id='complete', progress='Complete', evidence_links=links, completion_claim=True))
            assert complete['status'] == 'completion_claimed'
            reviewed = await invoke(reviewer, 'hub_tracking_task_review', dict(task_id=task['id'], expected_revision=complete['revision'],
                client_request_id='review', verdict='verify', note='Reviewed synthetic evidence', evidence_links=links))
            assert reviewed['status'] == 'completion_verified'


def test_owner_decision_context_and_withdraw_redact_dispatch_lineage(unified):
    from tests.test_dispatch_coordination import setup_dispatch

    client, app, bus, csrf = unified
    env = SimpleNamespace(repo=app.state.repository, bridge=app.state.session_hub_bridge,
                          sessions=bus.state.agent_sessions)
    _, worker, _, _, _, service, _, item = setup_dispatch(env)
    asked = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(
        actor, item['id'], 1, 1, 'ask', 'Question', 'ask-context', 'Choose', ['Yes', 'No']))
    identity = asked['decision_id']
    raw = env.repo._read(identity, 'decision')
    assert raw['origin_dispatch_id'] == item['id']
    path = '/api/hub/v1/decisions/' + identity
    for listing in (client.get('/api/hub/v1/decisions').json()['items'],
                    client.get('/api/hub/v1/overview').json()['decisions']):
        value = next(value for value in listing if value['id'] == identity)
        assert 'origin_dispatch_id' not in value and 'origin_dispatch_revision' not in value
    updated = client.post(path + '/context', headers=csrf, json={'expected_revision': raw['revision'], 'details': 'Clarified'})
    assert updated.status_code == 200 and 'origin_dispatch_id' not in updated.json()
    withdrawn = client.post(path + '/withdraw', headers=csrf, json={'expected_revision': updated.json()['revision'], 'reason': 'Not needed'})
    assert withdrawn.status_code == 200 and 'origin_dispatch_id' not in withdrawn.json()
    assert env.repo._read(identity, 'decision')['origin_dispatch_id'] == item['id']
    with env.repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_outbox WHERE entity_id=?', (identity,)).fetchone()[0] == 0
        actions = [row[0] for row in db.execute('SELECT action FROM hub_audit WHERE entity_id=? ORDER BY sequence', (identity,))]
        assert actions == ['decision.created', 'decision.context_updated', 'decision.withdrawn']


def test_decision_only_session_withdraw_redacts_origin_and_cannot_read_other_project(unified):
    from tests.test_dispatch_coordination import setup_dispatch

    client, app, bus, _ = unified
    env = SimpleNamespace(repo=app.state.repository, bridge=app.state.session_hub_bridge,
                          sessions=bus.state.agent_sessions)
    _, worker, _, _, roles, service, _, item = setup_dispatch(env)
    asked = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(
        actor, item['id'], 1, 1, 'ask', 'Question', 'ask-withdraw', 'Choose', ['Yes', 'No']))
    identity = asked['decision_id']
    installation = roles.owner_get(OWNER, 'backend')['installations'][0]
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT], ['decision.read', 'entity.withdraw'])
    invoke, _ = sdk(client, worker)
    decision = data(invoke('hub_decision_get', {'decision_id': identity}))
    assert 'origin_dispatch_id' not in decision and 'origin_dispatch_revision' not in decision
    assert invoke('hub_dispatch_get', {'dispatch_id': item['id']})['isError']
    foreign = env.repo.create_decision(OWNER, 'project-alpha', 'Other project', ['Yes', 'No'])
    denied = invoke('hub_decision_get', {'decision_id': foreign['id']})
    missing = invoke('hub_decision_get', {'decision_id': '00000000-0000-4000-8000-000000000000'})
    assert denied['isError'] and missing['isError'] and denied['content'][0]['text'] == missing['content'][0]['text']
    withdrawn = data(invoke('hub_entity_withdraw', {'kind': 'decision', 'entity_id': identity,
        'expected_revision': decision['revision'], 'reason': 'No longer needed'}))
    assert withdrawn['status'] == 'withdrawn' and 'origin_dispatch_id' not in withdrawn
    assert env.repo._read(identity, 'decision')['origin_dispatch_id'] == item['id']
