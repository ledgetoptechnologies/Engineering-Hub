"""Real MCP/SSE task-group coverage for the authenticated ASGI send timeout."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from sse_starlette.sse import AppStatus, EventSourceResponse

from agent_message_bus.app import create_app
from tests.test_native_push_events import TOKENS


async def _initialized_session(client):
    headers = {
        'Authorization': f'Bearer {TOKENS.codex_token}',
        'Accept': 'application/json, text/event-stream',
    }
    response = await client.post('/mcp', headers=headers, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'bounded-sse-timeout-test', 'version': '1'},
        },
    })
    assert response.status_code == 200
    headers['Mcp-Session-Id'] = response.headers['mcp-session-id']
    headers['MCP-Protocol-Version'] = '2025-11-25'
    response = await client.post('/mcp', headers=headers, json={
        'jsonrpc': '2.0', 'method': 'notifications/initialized',
    })
    assert response.status_code == 202
    response = await client.post('/mcp', headers=headers, json={
        'jsonrpc': '2.0', 'id': 2, 'method': 'resources/subscribe',
        'params': {'uri': 'agentbus://inbox/codex'},
    })
    assert response.status_code == 200
    return headers


def _get_scope(headers):
    return {
        'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
        'http_version': '1.1', 'scheme': 'http', 'method': 'GET',
        'path': '/mcp', 'raw_path': b'/mcp', 'root_path': '',
        'query_string': b'', 'server': ('127.0.0.1', 8765),
        'client': ('127.0.0.1', 12345),
        'headers': [(b'host', b'127.0.0.1:8765')] + [
            (key.lower().encode('ascii'), value.encode('ascii')) for key, value in headers.items()
        ],
    }


def _receive_until_disconnect(disconnected):
    requested = False

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await disconnected.wait()
        return {'type': 'http.disconnect'}

    return receive


def _leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for nested in error.exceptions for leaf in _leaves(nested)]
    return [error]


@pytest.fixture
async def cleanup_sse_shutdown_watchers():
    # sse-starlette's process-shutdown observer outlives individual responses.
    # No Uvicorn lifecycle runs for this in-process ASGI test, so retire only
    # observer tasks created by this test, not other tests' tasks.
    before = set(asyncio.all_tasks())
    try:
        yield
    finally:
        observers = [task for task in asyncio.all_tasks() if task not in before
                     and not task.done() and task.get_coro().__qualname__ == '_shutdown_watcher']
        for task in observers:
            task.cancel()
        if observers:
            await asyncio.gather(*observers, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_mcp_sse_blocked_send_finishes_and_preserves_other_session(
        tmp_path, monkeypatch, cleanup_sse_shutdown_watchers):
    monkeypatch.setenv('AGENT_BUS_ALLOWED_HOSTS', '127.0.0.1:*')
    monkeypatch.setattr('agent_message_bus.presence.PRESENCE_SEND_TIMEOUT_SECONDS', 0.2)
    monkeypatch.setattr(EventSourceResponse, 'DEFAULT_PING_INTERVAL', 0.01)
    monkeypatch.setattr(AppStatus, 'should_exit', False)
    app = create_app(db_path=tmp_path / 'presence.sqlite3', audit_path=tmp_path / 'audit.jsonl',
                     file_root=tmp_path / 'files', auth=TOKENS, push_events_operational=True)
    publisher = app.state.push_publisher
    replacement_started = asyncio.Event()
    replacement_wrote = asyncio.Event()
    blocked_started = asyncio.Event()
    blocked_body = asyncio.Event()
    blocked_send_cancelled = asyncio.Event()
    replacement_disconnect = asyncio.Event()
    blocked_disconnect = asyncio.Event()
    never_release = asyncio.Event()
    statuses = {'blocked': [], 'replacement': []}
    tasks = []

    async def replacement_send(message):
        if message['type'] == 'http.response.start':
            statuses['replacement'].append(message['status'])
            replacement_started.set()
        if message['type'] == 'http.response.body' and message.get('body'):
            replacement_wrote.set()

    async def blocked_send(message):
        if message['type'] == 'http.response.start':
            statuses['blocked'].append(message['status'])
            blocked_started.set()
        if message['type'] == 'http.response.body' and message.get('body'):
            blocked_body.set()
            try:
                await never_release.wait()
            except asyncio.CancelledError:
                blocked_send_cancelled.set()
                raise

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1:8765') as client:
            first = await _initialized_session(client)
            second = await _initialized_session(client)
            assert first['Mcp-Session-Id'] != second['Mcp-Session-Id']
            replacement = asyncio.create_task(app(_get_scope(second),
                _receive_until_disconnect(replacement_disconnect), replacement_send))
            tasks.append(replacement)
            try:
                await asyncio.wait_for(replacement_started.wait(), 2)
                await asyncio.wait_for(replacement_wrote.wait(), 2)
                blocked = asyncio.create_task(app(_get_scope(first),
                    _receive_until_disconnect(blocked_disconnect), blocked_send))
                tasks.append(blocked)
                await asyncio.wait_for(blocked_started.wait(), 2)
                await asyncio.wait_for(blocked_body.wait(), 2)
                active = next(row for row in (await publisher.presence_status())['agents']
                              if row['agent'] == 'codex')
                assert active['active_streams'] == active['inbox_subscriptions'] == 2
                # The real SDK may swallow or wrap the transport TimeoutError.
                # Either way, the ASGI request itself must finish, not hang.
                try:
                    await asyncio.wait_for(asyncio.shield(blocked), 2)
                except Exception as error:
                    assert blocked.done() and not blocked.cancelled()
                    assert all(isinstance(leaf, TimeoutError) for leaf in _leaves(error)), repr(error)
                assert blocked.done() and not blocked.cancelled()
                assert blocked_send_cancelled.is_set() and not blocked_disconnect.is_set()
                assert statuses == {'blocked': [200], 'replacement': [200]}
                after = next(row for row in (await publisher.presence_status())['agents']
                             if row['agent'] == 'codex')
                assert after['active_streams'] == after['inbox_subscriptions'] == 1
                assert after['stale_streams'] == 0 and after['subscribed'] is True
                assert not replacement.done()
                replacement_wrote.clear()
                await asyncio.wait_for(replacement_wrote.wait(), 2)
                assert not replacement.done()
            finally:
                blocked_disconnect.set()
                replacement_disconnect.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            final = next(row for row in (await publisher.presence_status())['agents']
                         if row['agent'] == 'codex')
            assert final['active_streams'] == final['inbox_subscriptions'] == 0
