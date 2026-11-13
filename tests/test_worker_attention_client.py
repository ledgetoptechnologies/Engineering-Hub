import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from agent_message_bus.agent_session_client import ClientError
from engineering_hub.attention_client import AttentionAdapter
from tests.test_attention_client import INSTALLATION, SESSION, PROJECT, DISPATCH, RECEIPT
from tests.test_stale_service_authority import owned

TASK = '66666666-6666-4666-8666-666666666666'


class WorkerSession:
    def __init__(self):
        self.calls = []
        self.identity = dict(installation_id=INSTALLATION, session_id=SESSION)
        self.page = dict(items=[dict(dispatch_id=DISPATCH, dispatch_revision=1, task_id=TASK,
                                     project=PROJECT, role='backend', execution_authorization=False,
                                     wake_verified=False, untrusted_text='SENSITIVE-PEER-TEXT')], next_after=None)
    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == 'session_identity':
            result = self.identity
        elif name == 'hub_worker_dispatch_pending':
            result = self.page
        elif name == 'hub_worker_dispatch_ack':
            result = dict(id=RECEIPT, project=PROJECT, role='backend', dispatch_id=args['dispatch_id'],
                          dispatch_revision=args['expected_revision'], installation_id=INSTALLATION,
                          session_id=SESSION, generation=1, state='agent_acknowledged',
                          wake_verified=False, execution_authorization=False)
        else:
            raise AssertionError('unexpected call')
        return SimpleNamespace(isError=False, structuredContent=result, content=[])


def adapter(session, output):
    return AttentionAdapter(session, installation=INSTALLATION, session_id=SESSION,
                            project=PROJECT, generation=1, emit=output.append, role='backend')


@pytest.mark.asyncio
async def test_worker_emits_only_untrusted_pointers_and_explicit_ack():
    session, output = WorkerSession(), []
    client = adapter(session, output)
    await client.catch_up()
    assert output[0] == dict(type='worker_dispatch_reference', trust='untrusted_hub_data', project=PROJECT,
        role='backend', dispatch_id=DISPATCH, dispatch_revision=1, task_id=TASK, role_generation=1,
        execution_authorization=False, wake_verified=False)
    assert 'SENSITIVE-PEER-TEXT' not in json.dumps(output)
    assert not any(name == 'hub_worker_dispatch_ack' for name, _ in session.calls)
    await client.acknowledge(DISPATCH, 1)
    assert session.calls[-1][0] == 'hub_worker_dispatch_ack'
    assert output[-1]['receipt_id'] == RECEIPT and output[-1]['wake_verified'] is False


@pytest.mark.asyncio
async def test_worker_identity_scope_and_receipt_mismatch_fail_closed():
    session, output = WorkerSession(), []
    client = adapter(session, output)
    session.identity['session_id'] = TASK
    with pytest.raises(ClientError, match='attention_session_mismatch'):
        await client.catch_up()
    assert output == []
    session.identity['session_id'] = SESSION
    session.page['items'][0]['role'] = 'frontend'
    with pytest.raises(ClientError, match='invalid_attention_response'):
        await client.catch_up()
    assert output == []
    original = session.call_tool
    async def bad_receipt(name, args):
        result = await original(name, args)
        if name == 'hub_worker_dispatch_ack':
            result.structuredContent['session_id'] = TASK
        return result
    session.call_tool = bad_receipt
    with pytest.raises(ClientError, match='invalid_attention_response'):
        await client.acknowledge(DISPATCH, 1)


@pytest.mark.asyncio
async def test_worker_pages_are_bounded_and_require_monotonic_cursor():
    session, output = WorkerSession(), []
    original = session.call_tool
    async def page(name, args):
        if name != 'hub_worker_dispatch_pending':
            return await original(name, args)
        after = uuid.UUID(args['after']).int if args.get('after') else 0
        records = [dict(session.page['items'][0], dispatch_id=str(uuid.UUID(int=n))) for n in range(after+1, after+51)]
        return SimpleNamespace(isError=False, structuredContent=dict(items=records, next_after=records[-1]['dispatch_id']), content=[])
    session.call_tool = page
    await adapter(session, output).catch_up()
    assert len(output) == 501 and output[-1] == dict(type='attention_catchup', complete=False, next_after=str(uuid.UUID(int=500)))
    assert all(value['type'] == 'worker_dispatch_reference' for value in output[:-1])


def test_cli_rejects_cross_audience_reference(monkeypatch, tmp_path, capsys):
    from engineering_hub import attention_client
    async def run(args):
        assert args.role == 'backend' and args.intent_id == DISPATCH
        raise ClientError('synthetic-secret')
    monkeypatch.setattr(attention_client, '_run', run)
    assert attention_client.main(['--profile-dir', str(tmp_path), '--session', 'first', '--project', PROJECT,
        '--generation', '1', '--role', 'backend', 'ack', '--intent-id', DISPATCH, '--revision', '1']) == 1
    assert 'synthetic-secret' not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_worker_foreground_profile_sse_mcp_and_explicit_ack(owned, tmp_path, monkeypatch):
    import socket
    import time
    from pathlib import Path
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings
    from agent_message_bus.app import create_app as create_bus
    from agent_message_bus.agent_session_client import _write
    from agent_message_bus.agent_sessions_config import AgentSessionsConfig
    from agent_message_bus.auth import AuthSettings
    from engineering_hub import attention_client
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    from tests.test_dispatch_coordination import setup_dispatch
    from tests.test_session_hub_bridge import OWNER, call
    from tests.test_mcp_push import ReadyServer

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0)); listener.listen(); listener.setblocking(False)
    port = listener.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    repo, _, _, _ = owned
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude')
    bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=auth, startup_cleanup=False, push_events_operational=False,
        agent_sessions_config=AgentSessionsConfig(tmp_path / 'connections.sqlite3', OWNER, origin, hub_bridge=True),
        transport_security_settings=TransportSecuritySettings(allowed_hosts=[f'127.0.0.1:{port}'], allowed_origins=[origin]))
    app = create_app(Settings(Path(repo.path), auth, AuthConfig(origin, beau_github_id=42)), repository=repo, bus_app=bus)
    bridge = app.state.session_hub_bridge
    env = SimpleNamespace(repo=repo, bridge=bridge, sessions=bridge.sessions)
    orch, worker_token, _, _, _, dispatch, _, work = setup_dispatch(env)
    principal = env.sessions.authenticate_session(worker_token)
    app.state.attention_delivery.owner_configure(OWNER, PROJECT, 0, principal.installation_id, principal.session_id, 1, 50, role='backend')
    profile_dir = tmp_path / 'profile'
    _write(profile_dir / 'installation.private', dict(origin=origin, installation_id=principal.installation_id))
    _write(profile_dir / 'test.session.private', dict(token=worker_token, installation_id=principal.installation_id,
        session_id=principal.session_id, expires_at=principal.expires_at))
    output = []; ready = asyncio.Event(); delivered = asyncio.Event()
    def emit(value):
        output.append(value)
        if value['type'] == 'attention_catchup':
            ready.set()
        if value['type'] == 'worker_dispatch_reference':
            delivered.set()
    monkeypatch.setattr(attention_client, '_emit', emit)
    args = SimpleNamespace(profile_dir=profile_dir, session='test', private_vpn=False,
                           project=PROJECT, generation=1, role='backend', command='listen')
    server = ReadyServer(uvicorn.Config(app, log_level='critical', lifespan='on'))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    worker = None
    try:
        await asyncio.wait_for(server.ready.wait(), 5)
        worker = asyncio.create_task(attention_client._run(args))
        await asyncio.wait_for(ready.wait(), 5)
        await asyncio.wait_for(delivered.wait(), 5)
        reference = next(value for value in output if value['type'] == 'worker_dispatch_reference')
        assert reference['dispatch_id'] == work['id']
        with repo._connection() as db:
            assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='worker_dispatch_receipt'").fetchone()[0] == 0
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        args.command, args.dispatch_id, args.revision = 'ack', reference['dispatch_id'], reference['dispatch_revision']
        await attention_client._run(args)
        with repo._connection() as db:
            assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='worker_dispatch_receipt'").fetchone()[0] == 1
        assert worker_token not in json.dumps(output)
    finally:
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(server_task, 10)
        listener.close()
        current = asyncio.current_task()
        watchers = [pending for pending in asyncio.all_tasks() if pending is not current and not pending.done()
                    and pending.get_coro().__qualname__ == '_shutdown_watcher']
        for watcher in watchers:
            watcher.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)




@pytest.mark.asyncio
async def test_worker_rejects_orchestrator_ack_reference_before_mcp(monkeypatch, tmp_path):
    import time
    from engineering_hub import attention_client
    monkeypatch.setattr(attention_client, '_profile', lambda *_: ('https://hub.invalid', 'ehs_' + 'a' * 43,
        INSTALLATION, SESSION, time.time() + 30))
    args = SimpleNamespace(profile_dir=tmp_path, session='first', private_vpn=False, project=PROJECT,
        generation=1, role='backend', command='ack', intent_id=DISPATCH, dispatch_id=None, revision=1)
    with pytest.raises(ClientError, match='invalid_attention_reference'):
        await attention_client._run(args)
