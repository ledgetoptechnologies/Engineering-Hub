import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import TransportSecuritySettings

from agent_message_bus.agent_session_client import ClientError
from agent_message_bus.agent_sessions_config import AgentSessionsConfig
from agent_message_bus.app import create_app as create_bus
from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.tracking_client import _arguments, _run, invoke
from tests.test_mcp_push import loopback
from tests.test_session_hub_bridge import OWNER, PROJECT, pair
from tests.test_stale_service_authority import owned


def test_explicit_bounded_tool_arguments_only():
    assert _arguments('list', {'project': PROJECT})[0] == 'hub_tracking_task_list'
    assert _arguments('origin', {'task_id': 'task'})[0] == 'hub_tracking_task_origin'
    assert _arguments('request-summary', {'project': PROJECT, 'request_id': 'req'})[0] == 'hub_orchestration_request_origin_task_summary'
    for action, value in [('other', {}), ('list', {'project': PROJECT, 'token': 'secret'}),
                          ('update', {'task_id': 'x'}), ('list', [])]:
        with pytest.raises(ClientError):
            _arguments(action, value)


@pytest.mark.asyncio
async def test_loopback_session_tracking_scope_retry_and_independent_review(owned, tmp_path, monkeypatch, capsys):
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
    writer_id, writer, _ = pair(env, name='tracking-client-writer', family='codex',
        caps=('tracking.propose', 'tracking.read', 'tracking.update', 'tracking.review', 'dispatch.read'))
    reviewer_id, reviewer, _ = pair(env, name='tracking-client-reviewer', family='claude',
        caps=('tracking.read', 'tracking.review'))
    wp = env.sessions.authenticate_session(writer)
    rp = env.sessions.authenticate_session(reviewer)

    @asynccontextmanager
    async def connected(url, token):
        async with httpx.AsyncClient(headers={'Host': 'staging.test', 'Authorization': 'Bearer ' + token},
                                     timeout=10, trust_env=False) as http:
            async with streamable_http_client(url, http_client=http, terminate_on_close=True) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async with loopback(app) as url:
        from agent_message_bus.agent_session_client import _write
        from engineering_hub import tracking_client
        profile = tmp_path / 'profile'; profile.mkdir()
        _write(profile / 'installation.private', dict(origin=url.removesuffix('/mcp'), installation_id=writer_id))
        _write(profile / 'first.session.private', dict(token=writer, installation_id=writer_id,
            session_id=wp.session_id, expires_at=wp.expires_at))
        args = SimpleNamespace(profile_dir=profile, session='first', private_vpn=False, action='list')
        # Loopback binds an ephemeral port; the real endpoint expects its exact
        # staging Host, so inject that host only in this synthetic transport.
        original_client = httpx.AsyncClient
        class TestClient(original_client):
            def __init__(self, *args, **kwargs):
                kwargs['headers']['Host'] = 'staging.test'
                super().__init__(*args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(tracking_client.httpx, 'AsyncClient', TestClient)
            assert (await _run(args, {'project': PROJECT}))['result']['tasks'] == []
            from engineering_hub import tracking_outbox
            outbox = tmp_path / 'outbox'; outbox.mkdir(mode=0o700)
            body = dict(project=PROJECT, title='Outbox synthetic', client_request_id='outbox-stable')
            rid = tracking_outbox.stage(outbox, url.removesuffix('/mcp'), writer_id, None, 'propose', body)
            saved = tracking_outbox.read(outbox, rid)
            attempts = await asyncio.gather(*[_run(args, saved['arguments'], binding=saved, action=saved['action'])
                                              for _ in range(2)], return_exceptions=True)
            successful = [item['result'] for item in attempts if isinstance(item, dict)]
            assert successful
            retry = (await _run(args, saved['arguments'], binding=saved, action=saved['action']))['result']
            assert all(item['id'] == retry['id'] for item in successful)
            # Exercise the real CLI path against EHS after an ambiguous earlier
            # send: it saves a local receipt only after the same-key retry.
            import io
            class Input:
                buffer = io.BytesIO(json.dumps({'record_id': rid}).encode())
            monkeypatch.setattr(tracking_client.sys, 'stdin', Input())
            argv = ['--profile-dir', str(profile), '--session', 'first', '--outbox-dir', str(outbox), 'send-staged']
            assert await asyncio.to_thread(tracking_client.main, argv) == 0
            assert json.loads(capsys.readouterr().out)['result']['id'] == retry['id']
            assert tracking_outbox.receipt(outbox, saved)['task_id'] == retry['id']
            all_items = (await _run(args, {'project': PROJECT}))['result']['tasks']
            assert len([item for item in all_items if item['title'] == 'Outbox synthetic']) == 1
            # MCP transport may wrap the denial in an AnyIO ExceptionGroup.
            with pytest.raises(Exception):
                await _run(args, dict(body, title='changed'), binding=saved, action='propose')
            assert tracking_outbox.read(outbox, rid) == saved
            with pytest.raises(ClientError):
                await _run(SimpleNamespace(**dict(vars(args), private_vpn=True)), {'project': PROJECT})
            _write(profile / 'first.session.private', dict(token=writer, installation_id=reviewer_id,
                session_id=wp.session_id, expires_at=wp.expires_at))
            with pytest.raises(ClientError):
                await _run(args, {'project': PROJECT})
        from tests.test_dispatch_coordination import setup_dispatch
        _, _, _, _, _, _, source_request, _ = setup_dispatch(env)
        async with connected(url, writer) as w, connected(url, reviewer) as r:
            async def action(session, principal, name, payload):
                return (await invoke(session, principal.installation_id, principal.session_id, name, payload))['result']
            with pytest.raises(ClientError):
                await invoke(w, reviewer_id, rp.session_id, 'list', {'project': PROJECT})
            request = dict(project=PROJECT, title='Record synthetic work', client_request_id='stable-proposal',
                           details='Acceptance: retain evidence')
            task = await action(w, wp, 'propose', request)
            assert await action(w, wp, 'propose', request) == task
            with pytest.raises(ClientError):
                await action(w, wp, 'propose', dict(request, title='changed'))
            with pytest.raises(ClientError):
                await action(w, wp, 'list', {'project': 'project-alpha'})
            assert task['id'] in [item['id'] for item in (await action(w, wp, 'list', {'project': PROJECT, 'unfinished': True}))['tasks']]
            linked = await action(w, wp, 'propose', dict(project=PROJECT, title='Linked synthetic work',
                client_request_id='linked', origin_request_id=source_request['id']))
            assert (await action(w, wp, 'origin', {'task_id': linked['id']}))['origin_request_id'] == source_request['id']
            assert (await action(w, wp, 'origin', {'task_id': task['id']}))['origin_request_id'] is None
            summary = await action(w, wp, 'request-summary', {'project': PROJECT, 'request_id': source_request['id']})
            assert summary['scope'] == 'origin_linked_tracking_tasks_only'
            assert not summary['dispatch_only_tasks_included']
            assert [item['id'] for item in summary['tasks']] == [linked['id']]
            with pytest.raises(ClientError):
                await action(r, rp, 'request-summary', {'project': PROJECT, 'request_id': source_request['id']})
            with pytest.raises(ClientError):
                await action(w, wp, 'request-summary', {'project': 'project-alpha', 'request_id': source_request['id']})
            links = [{'label': 'Synthetic evidence', 'url': 'https://example.com/evidence'}]
            claim = await action(w, wp, 'update', dict(task_id=task['id'], expected_revision=task['revision'],
                client_request_id='claim', progress='Synthetic evidence ready', evidence_links=links, completion_claim=True))
            assert claim['status'] == 'completion_claimed'
            review = dict(task_id=task['id'], expected_revision=claim['revision'], client_request_id='review',
                          verdict='verify', note='Reviewed synthetic evidence', evidence_links=links)
            with pytest.raises(ClientError):
                await action(w, wp, 'review', review)
            # Preserve the exact review/key before network I/O. Only the reviewer
            # installation can send or retry this record after a lost response.
            _write(profile / 'installation.private', dict(origin=url.removesuffix('/mcp'), installation_id=reviewer_id))
            _write(profile / 'first.session.private', dict(token=reviewer, installation_id=reviewer_id,
                session_id=rp.session_id, expires_at=rp.expires_at))
            review_id = tracking_outbox.stage(outbox, url.removesuffix('/mcp'), reviewer_id, None, 'review', review)
            saved_review = tracking_outbox.read(outbox, review_id)
            with monkeypatch.context() as patch:
                patch.setattr(tracking_client.httpx, 'AsyncClient', TestClient)
                done = (await _run(args, saved_review['arguments'], binding=saved_review, action=saved_review['action']))['result']
                retry_review = (await _run(args, saved_review['arguments'], binding=saved_review, action=saved_review['action']))['result']
            assert done['status'] == 'completion_verified'
            assert retry_review == done
            assert len(done['reviews']) == 1
            with pytest.raises(ClientError):
                await action(w, wp, 'update', dict(task_id=task['id'], expected_revision=task['revision'],
                    client_request_id='old', progress='stale', evidence_links=[]))
            bridge.grant_revoke(OWNER, writer_id, 1)
            with pytest.raises(ClientError):
                await action(w, wp, 'list', {'project': PROJECT})


def test_cli_suppresses_raw_errors_and_limits_stdin(monkeypatch, capsys, tmp_path):
    import io
    from engineering_hub import tracking_client
    class Input:
        def __init__(self, raw): self.buffer = io.BytesIO(raw)
    async def fail(_args, _arguments):
        raise RuntimeError('secret-token-and-private-content')
    monkeypatch.setattr(tracking_client, '_run', fail)
    argv = ['--profile-dir', str(tmp_path), '--session', 'first', 'list']
    monkeypatch.setattr('sys.stdin', Input(b'{"project":"engineering-hub"}'))
    assert tracking_client.main(argv) == 1
    out = capsys.readouterr()
    assert out.out == '' and 'secret-token' not in out.err and 'same request id' in out.err
    monkeypatch.setattr('sys.stdin', Input(b' ' * 65537))
    assert tracking_client.main(argv) == 1
    assert capsys.readouterr().out == ''
    assert tracking_client.main(argv + ['secret-token-and-private-content']) == 1
    out = capsys.readouterr()
    assert out.out == '' and 'secret-token' not in out.err


def test_cli_success_displays_only_explicit_result(monkeypatch, capsys, tmp_path):
    import io
    from engineering_hub import tracking_client
    class Input:
        buffer = io.BytesIO(b'{"project":"engineering-hub"}')
    async def successful(args, arguments):
        assert args.action == 'list' and arguments == {'project': PROJECT}
        return {'type': 'tracking_result', 'trust': 'untrusted_hub_data',
                'execution_authorization': False, 'result': {'tasks': []}}
    monkeypatch.setattr(tracking_client, '_run', successful)
    monkeypatch.setattr('sys.stdin', Input())
    assert tracking_client.main(['--profile-dir', str(tmp_path), '--session', 'first', 'list']) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)['trust'] == 'untrusted_hub_data' and out.err == ''
