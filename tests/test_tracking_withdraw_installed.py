"""Opt-in installed-client smoke against a task-owned real EHS loopback server.

Set EH_TRACKING_INSTALLED_PYTHON to a private venv containing a separately
verified source-matched tracking-client wheel. This test does not verify that
external precondition. No production profiles, grants, or endpoints are used.
"""
import asyncio
import json
import os
import socket
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
import sse_starlette.sse as sse

from agent_message_bus.agent_session_client import _write
from agent_message_bus.agent_sessions_config import AgentSessionsConfig
from agent_message_bus.app import create_app as create_bus
from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from tests.test_mcp_push import ReadyServer
from tests.test_session_hub_bridge import OWNER, PROJECT, pair
from tests.test_stale_service_authority import owned


@pytest.fixture
def owned_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.setblocking(False)
        yield listener
    finally:
        listener.close()


@pytest.mark.asyncio
async def test_installed_outbox_withdrawal_against_real_ehs(owned, tmp_path, monkeypatch, owned_listener):
    executable = os.environ.get('EH_TRACKING_INSTALLED_PYTHON')
    if not executable:
        pytest.skip('requires a source-matched installed tracking client')
    assert Path(executable).is_file()
    repo, _, _, _ = owned
    listener = owned_listener
    origin = f'http://127.0.0.1:{listener.getsockname()[1]}'
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude')
    bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=auth, startup_cleanup=False, push_events_operational=False,
        agent_sessions_config=AgentSessionsConfig(tmp_path / 'connections.sqlite3', OWNER, origin, hub_bridge=True),
        transport_security_settings=TransportSecuritySettings(allowed_hosts=[origin.removeprefix('http://')], allowed_origins=[origin]))
    app = create_app(Settings(Path(repo.path), auth, AuthConfig(origin, beau_github_id=42)), repository=repo, bus_app=bus)
    bridge = app.state.session_hub_bridge
    env = SimpleNamespace(repo=repo, bridge=bridge, sessions=bridge.sessions)
    writer_id, writer, _ = pair(env, name='installed-withdraw-writer', family='codex',
        caps=('tracking.propose', 'tracking.read', 'tracking.update', 'tracking.review'))
    reviewer_id, reviewer, _ = pair(env, name='installed-withdraw-reviewer', family='claude',
        caps=('tracking.read', 'tracking.review'))
    writer_session = env.sessions.authenticate_session(writer)
    reviewer_session = env.sessions.authenticate_session(reviewer)
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    outbox = tmp_path / 'outbox'; outbox.mkdir(mode=0o700)

    async def cli(action, payload, *, staged=False):
        command = [executable, '-I', '-m', 'engineering_hub.tracking_client',
                   '--profile-dir', str(profile), '--session', 'first']
        if staged:
            command += ['--outbox-dir', str(outbox)]
        command.append(action)
        process = await asyncio.create_subprocess_exec(*command, cwd=tmp_path,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, error = await asyncio.wait_for(process.communicate(json.dumps(payload).encode()), 30)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.communicate()
            raise
        assert writer.encode() not in output + error and reviewer.encode() not in output + error
        return process.returncode, json.loads(output) if output else None

    def bind(base, installation, token, principal):
        _write(profile / 'installation.private', {'origin': base, 'installation_id': installation})
        _write(profile / 'first.session.private', {'installation_id': installation, 'token': token,
            'session_id': principal.session_id, 'expires_at': principal.expires_at})

    # Track only a watcher created by this test's server. An existing shared
    # watcher belongs to the surrounding loop and is never cancelled here.
    owned_watchers = set()
    watcher_owner = ContextVar('withdraw_test_server_watcher', default=False)
    original_start_watcher = sse._ensure_watcher_started_on_this_loop
    def track_new_watcher():
        if not watcher_owner.get():
            return original_start_watcher()
        before = asyncio.all_tasks()
        original_start_watcher()
        owned_watchers.update(task for task in asyncio.all_tasks() - before
                              if task.get_coro().__qualname__ == '_shutdown_watcher')
    monkeypatch.setattr(sse, '_ensure_watcher_started_on_this_loop', track_new_watcher)

    @asynccontextmanager
    async def serve():
        server = ReadyServer(uvicorn.Config(app, log_level='critical', lifespan='on'))
        marker = watcher_owner.set(True)
        try:
            task = asyncio.create_task(server.serve(sockets=[listener]))
        finally:
            watcher_owner.reset(marker)
        try:
            await asyncio.wait_for(server.ready.wait(), 5)
            yield origin
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(task, 5)
            finally:
                watchers = [pending for pending in owned_watchers if not pending.done()]
                for pending in watchers:
                    pending.cancel()
                if watchers:
                    await asyncio.gather(*watchers, return_exceptions=True)

    async with serve() as base:
        bind(base, writer_id, writer, writer_session)
        code, proposal = await cli('propose', {'project': PROJECT, 'title': 'Synthetic withdrawal',
                                               'client_request_id': 'withdraw-e2e-propose'})
        assert code == 0
        task = proposal['result']; task_id = task['id']
        evidence = [{'label': 'Synthetic evidence', 'url': 'https://example.com/evidence'}]
        code, result = await cli('update', {'task_id': task_id, 'expected_revision': task['revision'],
            'client_request_id': 'withdraw-e2e-claim', 'progress': 'Claimed done', 'evidence_links': evidence,
            'completion_claim': True})
        assert code == 0 and result['result']['status'] == 'completion_claimed'
        claim = result['result']
        review = {'task_id': task_id, 'expected_revision': claim['revision'], 'client_request_id': 'withdraw-e2e-review',
                  'verdict': 'verify', 'note': 'Synthetic review', 'evidence_links': evidence}
        assert (await cli('review', review))[0] == 1  # Same-family self-review is forbidden.
        bind(base, reviewer_id, reviewer, reviewer_session)
        code, result = await cli('review', review)
        assert code == 0 and result['result']['status'] == 'completion_verified'
        verified = result['result']
        bind(base, writer_id, writer, writer_session)
        payload = {'task_id': task_id, 'expected_revision': verified['revision'],
                   'expected_claim_revision': claim['revision'], 'client_request_id': 'withdraw-e2e-stable',
                   'reason': 'Synthetic work remains'}
        code, result = await cli('stage', {'action': 'withdraw-claim', 'arguments': payload}, staged=True)
        assert code == 0 and result['sent'] is False
        record_id = result['record_id']
        assert repo._read(task_id, 'tracking_task')['status'] == 'completion_verified'
        # Remove read while retaining write; the response must not disclose history.
        bridge.grant_put(OWNER, writer_id, 1, [PROJECT], ['tracking.propose', 'tracking.update', 'tracking.review'])
        code, result = await cli('send-staged', {'record_id': record_id}, staged=True)
        assert code == 0 and result['result'] == {'id': task_id, 'response_scope': 'mutation_receipt',
                                                   'current_state_included': False}
        # Treat the successful response as lost and explicitly retry the unchanged record.
        code, retry = await cli('send-staged', {'record_id': record_id}, staged=True)
        assert code == 0 and retry == result
        current = repo._read(task_id, 'tracking_task')
        assert current['status'] == 'open' and len(current['reviews']) == 1
        assert sum(u.get('claim_withdrawal') is True for u in current['updates']) == 1
        assert current['updates'][-1]['withdrawn_claim_revision'] == claim['revision']
        assert (await cli('list', {'project': PROJECT}))[0] == 1
        bridge.grant_put(OWNER, writer_id, 2, [PROJECT], ['tracking.propose', 'tracking.read', 'tracking.update', 'tracking.review'])
        code, listed = await cli('list', {'project': PROJECT, 'unfinished': True})
        assert code == 0 and [t['id'] for t in listed['result']['tasks']] == [task_id]
        code, fresh = await cli('update', {'task_id': task_id, 'expected_revision': current['revision'],
            'client_request_id': 'withdraw-e2e-reclaim', 'progress': 'Claimed again', 'evidence_links': evidence,
            'completion_claim': True})
        assert code == 0 and fresh['result']['status'] == 'completion_claimed'
        second_review = dict(review, expected_revision=fresh['result']['revision'],
                             client_request_id='withdraw-e2e-review-again')
        assert (await cli('review', second_review))[0] == 1
        bind(base, reviewer_id, reviewer, reviewer_session)
        code, final = await cli('review', second_review)
        assert code == 0 and final['result']['status'] == 'completion_verified'
        assert len(final['result']['reviews']) == 2
