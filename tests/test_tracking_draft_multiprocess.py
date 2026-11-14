"""Separate MCP clients race through the single authorized Hub owner."""
import asyncio
import multiprocessing
import os
from pathlib import Path
import secrets
import socket
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import TransportSecuritySettings
import pytest
import uvicorn
import sse_starlette.sse as sse

from agent_message_bus.agent_sessions_config import AgentSessionsConfig
from agent_message_bus.app import create_app as create_bus
from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.attention_client import _call
from engineering_hub.repository import HubError
from tests.test_mcp_push import ReadyServer
from tests.test_session_hub_bridge import OWNER, PROJECT, call, env, pair
from tests.test_stale_service_authority import owned


def _reap(process):
    if process.is_alive():
        process.terminate()
    process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
    assert not process.is_alive(), 'task-owned child did not exit'


@asynccontextmanager
async def _serve_owned(app, monkeypatch):
    owned_watchers = set()
    marker = ContextVar('multiprocess_test_server_watcher', default=False)
    original = sse._ensure_watcher_started_on_this_loop
    def track():
        if not marker.get():
            return original()
        before = asyncio.all_tasks()
        original()
        owned_watchers.update(task for task in asyncio.all_tasks() - before
                              if task.get_coro().__qualname__ == '_shutdown_watcher')
    monkeypatch.setattr(sse, '_ensure_watcher_started_on_this_loop', track)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server = task = None
    try:
        listener.bind(('127.0.0.1', 0)); listener.listen(); listener.setblocking(False)
        server = ReadyServer(uvicorn.Config(app, log_level='critical', lifespan='on'))
        token = marker.set(True)
        try:
            task = asyncio.create_task(server.serve(sockets=[listener]))
        finally:
            marker.reset(token)
        await asyncio.wait_for(server.ready.wait(), 5)
        yield f'http://127.0.0.1:{listener.getsockname()[1]}/mcp'
    finally:
        try:
            if server is not None:
                server.should_exit = True
            if task is not None:
                await asyncio.wait_for(task, 5)
        finally:
            listener.close()
            watchers = [watcher for watcher in owned_watchers if not watcher.done()]
            for watcher in watchers:
                watcher.cancel()
            if watchers:
                await asyncio.gather(*watchers, return_exceptions=True)


def _client(url, token, intake, barrier, output):
    async def run():
        async with httpx.AsyncClient(headers={'Host': 'staging.test', 'Authorization': 'Bearer ' + token},
                                     timeout=15, trust_env=False) as http:
            async with streamable_http_client(url, http_client=http, terminate_on_close=True) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    barrier.wait(timeout=15)
                    return await _call(session, 'hub_tracking_draft_propose',
                        {'project': PROJECT, 'title': 'Process race', 'client_request_id': 'process-race-key',
                         'intake_id': intake})
    try:
        item = asyncio.run(run())
        output.send(('ok', os.getpid(), item['id'], item['created_by']))
    except BaseException as error:
        output.send(('error', type(error).__name__))
    finally:
        output.close()


def _second_owner(paths, output):
    import errno
    import traceback
    from agent_message_bus.agent_sessions import AgentSessionRepository
    from agent_message_bus.knowledge import KnowledgeRepository
    from engineering_hub.components import ProjectComponentRepository
    from engineering_hub.projects import ProjectRepository
    from engineering_hub.repository import HubRepository
    from engineering_hub.session_hub_bridge import SessionHubBridge
    repo = HubRepository(paths['hub'])
    sessions = AgentSessionRepository(paths['sessions'], OWNER, lambda: 1700000000.0)
    projects = ProjectRepository(repo)
    components = ProjectComponentRepository(repo)
    knowledge = KnowledgeRepository(paths['knowledge'])
    try:
        bridge = SessionHubBridge(sessions, repo, projects, components, knowledge)
    except OSError as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        lock_site = frame.filename.endswith('session_hub_bridge.py') and (
            'fcntl.flock(' in frame.line or 'msvcrt.locking(' in frame.line)
        expected = lock_site and error.errno in (errno.EACCES, errno.EAGAIN)
        output.send(('refused' if expected else 'unexpected_error', os.getpid()))
    else:
        bridge.close()
        output.send(('unexpected_owner', os.getpid()))
    finally:
        output.close()


def test_second_bridge_owner_in_another_process_is_refused(env):
    paths = {'sessions': str(env.sessions.path), 'hub': str(env.repo.path),
             'knowledge': str(env.knowledge.db_path)}
    ctx = multiprocessing.get_context('spawn')
    receiver, sender = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_second_owner, args=(paths, sender))
    process.start(); sender.close()
    try:
        assert receiver.poll(20)
        outcome, child_pid = receiver.recv()
        process.join(timeout=5)
        assert process.exitcode == 0 and outcome == 'refused' and child_pid != os.getpid()
    finally:
        _reap(process)
        receiver.close()


@pytest.mark.asyncio
async def test_v2_intake_race_from_separate_client_processes(owned, tmp_path, monkeypatch):
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
    identities, siblings = {}, {}
    for family in ('codex', 'claude'):
        ident, token, _ = pair(env, name='process-' + family, family=family,
                               caps=('tracking.propose', 'tracking.read', 'tracking.update'))
        identities[family] = (ident, token)
        _, siblings[family], _ = pair(env, name='process-review-' + family, family=family, caps=('tracking.review',))
    identity = secrets.token_urlsafe(32)
    ctx = multiprocessing.get_context('spawn')
    barrier = ctx.Barrier(2)
    processes, receivers = [], []
    async with _serve_owned(app, monkeypatch) as url:
        try:
            for family in ('codex', 'claude'):
                receiver, sender = ctx.Pipe(duplex=False)
                process = ctx.Process(target=_client, args=(url, identities[family][1], identity, barrier, sender))
                process.start(); sender.close()
                processes.append(process); receivers.append(receiver)
            results = []
            for receiver in receivers:
                assert await asyncio.to_thread(receiver.poll, 30), 'worker timed out'
                results.append(receiver.recv())
            for process in processes:
                await asyncio.to_thread(process.join, 10)
                assert process.exitcode == 0
            assert all(result[0] == 'ok' for result in results), results
            assert len({result[1] for result in results}) == 2 and all(result[1] != os.getpid() for result in results)
            assert results[0][2:] == results[1][2:]
            task_id, creator = results[0][2:]
            family = next(family for family, (ident, _) in identities.items() if creator == 'installation:' + ident)
            other = 'claude' if family == 'codex' else 'codex'
            with repo._connection() as db:
                assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='tracking_task'").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='tracking_intake_receipt'").fetchone()[0] == 1
            with pytest.raises(HubError, match='idempotency_conflict|tracking_draft_conflict'):
                call(env, identities[other][1], 'hub_tracking_draft_propose', lambda actor:
                     repo.propose_tracking_draft(actor, PROJECT, 'Changed', 'process-race-key', identity))
            links = [{'label': 'Evidence', 'url': 'https://example.com/evidence'}]
            claim = call(env, identities[family][1], 'hub_tracking_task_update', lambda actor:
                         repo.update_tracking_task(actor, task_id, 1, 'claim', progress='Done',
                                                   evidence_links=links, completion_claim=True))
            def review(token):
                return call(env, token, 'hub_tracking_task_review', lambda actor:
                            repo.review_tracking_task(actor, task_id, claim['revision'], 'review',
                                                      verdict='verify', note='Reviewed', evidence_links=links))
            with pytest.raises(HubError, match='independent_review_required'):
                review(siblings[family])
            assert review(siblings[other])['status'] == 'completion_verified'
        finally:
            for process in processes:
                await asyncio.to_thread(_reap, process)
            for receiver in receivers:
                receiver.close()
