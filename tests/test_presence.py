from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import pytest

from agent_message_bus.app import create_app
from agent_message_bus.presence import MCPStreamPresenceMiddleware
from agent_message_bus.push import PushPublisher
from tests.test_native_push_events import TOKENS, loopback


@pytest.mark.asyncio
async def test_registered_subscription_is_not_reported_as_live_presence():
    publisher = PushPublisher(enabled=True)

    class Session:
        async def send_resource_updated(self, uri):
            pass

    await publisher.subscribe("private-session-id", "codex", Session())
    status = await publisher.presence_status()

    assert status["scope"] == "this_server_process"
    assert status["native_push_available"] is True
    assert [agent["agent"] for agent in status["agents"]] == ["claude", "codex", "hermes"]
    assert len(status["agents"]) == 3
    for agent in status["agents"]:
        assert set(agent) == {
            "agent", "active_streams", "inbox_subscriptions", "subscribed",
            "connected_since", "subscribed_since", "last_stream_write_at",
        }
        assert agent["active_streams"] == 0
        assert agent["inbox_subscriptions"] == 0
        assert agent["subscribed"] is False
        assert agent["connected_since"] is None
        assert agent["subscribed_since"] is None
        assert agent["last_stream_write_at"] is None
    assert datetime.fromisoformat(status["observed_at"].replace("Z", "+00:00")).utcoffset().total_seconds() == 0
    assert "private-session-id" not in repr(status)


def test_disabled_publisher_reports_native_push_unavailable():
    publisher = PushPublisher(enabled=False)

    async def read_status():
        return await publisher.presence_status()

    status = asyncio.run(read_status())
    assert status["native_push_available"] is False


def _agent(status: dict, name: str) -> dict:
    return next(agent for agent in status["agents"] if agent["agent"] == name)


@pytest.mark.asyncio
async def test_successful_open_mcp_get_sse_is_counted_until_disconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")
    app = create_app(db_path=tmp_path / "presence.sqlite3", audit_path=tmp_path / "audit.jsonl",
                     file_root=tmp_path / "files", auth=TOKENS, push_events_operational=True)
    async with loopback(app) as base_url:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=None)) as client:
            headers = {
                "Authorization": f"Bearer {TOKENS.codex_token}",
                "Accept": "application/json, text/event-stream",
            }
            initialized = await client.post(base_url + "/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "spoofed-client-name", "version": "1"},
                },
            })
            assert initialized.status_code == 200
            headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
            headers["MCP-Protocol-Version"] = "2025-11-25"
            unauthorized = await client.get(base_url + "/mcp", headers={
                "Authorization": "Bearer invalid-token", "Accept": "text/event-stream",
                "Mcp-Session-Id": headers["Mcp-Session-Id"],
            })
            assert unauthorized.status_code == 401
            wrong_owner = await client.get(base_url + "/mcp", headers={
                "Authorization": f"Bearer {TOKENS.hermes_token}",
                "Accept": "text/event-stream", "Mcp-Session-Id": headers["Mcp-Session-Id"],
            })
            assert wrong_owner.status_code == 404
            assert _agent(await app.state.push_publisher.presence_status(), "codex")["active_streams"] == 0
            acknowledged = await client.post(base_url + "/mcp", headers=headers, json={
                "jsonrpc": "2.0", "method": "notifications/initialized",
            })
            assert acknowledged.status_code == 202
            subscribed = await client.post(base_url + "/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "resources/subscribe",
                "params": {"uri": "agentbus://inbox/codex"},
            })
            assert subscribed.status_code == 200
            before_stream = await app.state.push_publisher.presence_status()
            assert _agent(before_stream, "codex")["subscribed"] is False

            async with client.stream("GET", base_url + "/mcp", headers=headers) as stream:
                assert stream.status_code == 200
                opened = await app.state.push_publisher.presence_status()
                codex = _agent(opened, "codex")
                assert codex["active_streams"] == 1
                assert codex["connected_since"] is not None
                assert codex["last_stream_write_at"] is None
                assert codex["inbox_subscriptions"] == 1
                assert codex["subscribed"] is True
                assert codex["subscribed_since"] is not None
                assert headers["Mcp-Session-Id"] not in repr(opened)
                assert TOKENS.codex_token not in repr(opened)
                assert "spoofed-client-name" not in repr(opened)
                assert _agent(opened, "hermes")["active_streams"] == 0

                async with client.stream("GET", base_url + "/mcp", headers=headers) as duplicate:
                    assert duplicate.status_code != 200
                still_one = await app.state.push_publisher.presence_status()
                assert _agent(still_one, "codex")["active_streams"] == 1

                assert await app.state.push_publisher.publish_committed("codex") == 1
                lines = stream.aiter_lines()
                event_line = await asyncio.wait_for(lines.__anext__(), 2)
                assert event_line.startswith("event:") or event_line.startswith("data:")
                wrote = await app.state.push_publisher.presence_status()
                assert _agent(wrote, "codex")["last_stream_write_at"] is not None

                unsubscribed = await client.post(base_url + "/mcp", headers=headers, json={
                    "jsonrpc": "2.0", "id": 3, "method": "resources/unsubscribe",
                    "params": {"uri": "agentbus://inbox/codex"},
                })
                assert unsubscribed.status_code == 200
                after_unsubscribe = await app.state.push_publisher.presence_status()
                codex = _agent(after_unsubscribe, "codex")
                assert codex["active_streams"] == 1
                assert codex["inbox_subscriptions"] == 0
                assert codex["subscribed"] is False

                resubscribed = await client.post(base_url + "/mcp", headers=headers, json={
                    "jsonrpc": "2.0", "id": 4, "method": "resources/subscribe",
                    "params": {"uri": "agentbus://inbox/codex"},
                })
                assert resubscribed.status_code == 200
                assert _agent(await app.state.push_publisher.presence_status(), "codex")["inbox_subscriptions"] == 1
                deleted = await client.delete(base_url + "/mcp", headers=headers)
                assert deleted.status_code == 200
                after_delete = await app.state.push_publisher.presence_status()
                assert _agent(after_delete, "codex")["inbox_subscriptions"] == 0
                assert app.state.push_publisher.subscription_count == 0

            await asyncio.sleep(0.1)  # Let Uvicorn deliver the client disconnect to the SSE task.
            closed = await app.state.push_publisher.presence_status()
            assert _agent(closed, "codex")["active_streams"] == 0


@pytest.mark.asyncio
async def test_simultaneous_sessions_count_distinct_streams_and_active_subscriptions():
    publisher = PushPublisher(enabled=True)

    class Session:
        async def send_resource_updated(self, uri):
            pass

    await publisher.subscribe("session-one", "codex", Session())
    await publisher.subscribe("session-two", "codex", Session())
    await publisher.subscribe("session-idle", "codex", Session())
    first = await publisher.open_presence_stream("session-one", "codex")
    replacement = await publisher.open_presence_stream("session-one", "codex")
    second = await publisher.open_presence_stream("session-two", "codex")
    await publisher.record_presence_stream_write(replacement)

    status = await publisher.presence_status()
    codex = _agent(status, "codex")
    assert codex["active_streams"] == 3
    assert codex["inbox_subscriptions"] == 2
    assert codex["subscribed"] is True
    assert codex["last_stream_write_at"] is not None

    await publisher.close_presence_stream(first)
    assert _agent(await publisher.presence_status(), "codex")["active_streams"] == 2
    await publisher.unsubscribe("session-one", "codex")
    assert _agent(await publisher.presence_status(), "codex")["inbox_subscriptions"] == 1
    await publisher.drop_session("session-two")
    codex = _agent(await publisher.presence_status(), "codex")
    assert codex["active_streams"] == 2
    assert codex["inbox_subscriptions"] == 0
    assert codex["subscribed"] is False
    await publisher.close_presence_stream(replacement)
    await publisher.close_presence_stream(second)
    assert _agent(await publisher.presence_status(), "codex")["active_streams"] == 0


@pytest.mark.asyncio
async def test_source_failure_disables_subscription_presence_without_erasing_transport():
    publisher = PushPublisher(enabled=True)

    class Session:
        def __init__(self):
            self.failure_notified = asyncio.Event()

        async def send_resource_updated(self, uri):
            pass

        async def send_log_message(self, **kwargs):
            self.failure_notified.set()

    session = Session()
    await publisher.subscribe("session", "hermes", session)
    stream = await publisher.open_presence_stream("session", "hermes")
    publisher.fail_source("test source failed")
    await asyncio.wait_for(session.failure_notified.wait(), 1)

    status = await publisher.presence_status()
    hermes = _agent(status, "hermes")
    assert status["native_push_available"] is False
    assert publisher.subscription_count == 0
    assert hermes["active_streams"] == 1
    assert hermes["inbox_subscriptions"] == 0
    assert hermes["subscribed"] is False
    await publisher.close_presence_stream(stream)


@pytest.mark.asyncio
async def test_generation_token_close_cannot_remove_replacement_stream():
    publisher = PushPublisher(enabled=True)
    old = await publisher.open_presence_stream("session", "claude")
    new = await publisher.open_presence_stream("session", "claude")

    await publisher.close_presence_stream(old)
    claude = _agent(await publisher.presence_status(), "claude")
    assert claude["active_streams"] == 1
    assert claude["connected_since"] is not None
    await publisher.close_presence_stream(new)
    assert _agent(await publisher.presence_status(), "claude")["active_streams"] == 0


@pytest.mark.asyncio
async def test_body_send_failure_cleans_up_stream_without_recording_write():
    publisher = PushPublisher(enabled=True)

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"event: update\n\n"})

    async def failing_send(message):
        if message["type"] == "http.response.body":
            raise OSError("socket closed")

    middleware = MCPStreamPresenceMiddleware(downstream, publisher)
    scope = {"type": "http", "method": "GET", "path": "/mcp", "state": {
        "mcp_presence_agent": "codex", "mcp_presence_session_id": "private-session",
    }}
    with pytest.raises(OSError, match="socket closed"):
        await middleware(scope, lambda: None, failing_send)

    codex = _agent(await publisher.presence_status(), "codex")
    assert codex["active_streams"] == 0
    assert codex["last_stream_write_at"] is None


@pytest.mark.asyncio
async def test_cancellation_cleans_up_open_stream():
    publisher = PushPublisher(enabled=True)
    started = asyncio.Event()
    hold_open = asyncio.Event()

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        started.set()
        await hold_open.wait()

    async def send(message):
        pass

    middleware = MCPStreamPresenceMiddleware(downstream, publisher)
    scope = {"type": "http", "method": "GET", "path": "/mcp", "state": {
        "mcp_presence_agent": "hermes", "mcp_presence_session_id": "private-session",
    }}
    task = asyncio.create_task(middleware(scope, lambda: None, send))
    await asyncio.wait_for(started.wait(), 1)
    assert _agent(await publisher.presence_status(), "hermes")["active_streams"] == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _agent(await publisher.presence_status(), "hermes")["active_streams"] == 0


@pytest.mark.asyncio
async def test_client_supplied_identity_without_validated_auth_state_is_ignored():
    publisher = PushPublisher(enabled=True)

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"data: fixture\n\n"})

    async def send(message):
        pass

    middleware = MCPStreamPresenceMiddleware(downstream, publisher)
    scope = {"type": "http", "method": "GET", "path": "/mcp", "headers": [
        (b"x-agent-name", b"hermes"), (b"mcp-session-id", b"spoofed-session"),
    ]}
    await middleware(scope, lambda: None, send)

    for name in ("hermes", "codex", "claude"):
        assert _agent(await publisher.presence_status(), name)["active_streams"] == 0
