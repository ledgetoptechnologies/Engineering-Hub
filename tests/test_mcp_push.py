from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from agent_message_bus.app import create_app
from agent_message_bus.auth import AuthSettings
from agent_message_bus.push import PushPublisher, SubscriptionLimit


TOKENS = AuthSettings("hermes-push-test", "codex-push-test", claude_token="claude-push-test")
BASE_URI = "agentbus://inbox/{}"


@pytest.fixture(autouse=True)
def allow_ephemeral_loopback_host(monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")


class ReadyServer(uvicorn.Server):
    def __init__(self, config):
        super().__init__(config)
        self.ready = asyncio.Event()

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        self.ready.set()


@asynccontextmanager
async def loopback(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = ReadyServer(uvicorn.Config(app, log_level="critical", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(server.ready.wait(), 5)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        except TimeoutError:
            server.force_exit = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        listener.close()
        # sse-starlette owns one shutdown notifier per event loop. Its notifier
        # normally lives until the server process exits; cancel the test loop's
        # notifier after Uvicorn has closed all connections to keep pytest clean.
        current = asyncio.current_task()
        watchers = [
            pending for pending in asyncio.all_tasks()
            if pending is not current and not pending.done()
            and pending.get_coro().__qualname__ == "_shutdown_watcher"
        ]
        for watcher in watchers:
            watcher.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)


@asynccontextmanager
async def mcp_client(url: str, token: str):
    notifications: asyncio.Queue[types.ServerNotification] = asyncio.Queue()

    async def on_message(message):
        if isinstance(message, types.ServerNotification):
            await notifications.put(message)

    timeout = httpx.Timeout(10, read=None)
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=timeout
    ) as http_client:
        async with streamable_http_client(
            url, http_client=http_client, terminate_on_close=True
        ) as (read, write, get_session_id):
            async with ClientSession(read, write, message_handler=on_message) as session:
                initialized = await session.initialize()
                yield session, notifications, get_session_id, initialized


async def no_notification(queue: asyncio.Queue, timeout: float = 0.12) -> bool:
    try:
        await asyncio.wait_for(queue.get(), timeout)
        return False
    except TimeoutError:
        return True


def resource_json(result) -> dict:
    assert result.contents and isinstance(result.contents[0], types.TextResourceContents)
    return json.loads(result.contents[0].text)


def live_app(tmp_path: Path, *, push_events_operational: bool = True):
    return create_app(
        db_path=tmp_path / "messages.sqlite3",
        audit_path=tmp_path / "audit.jsonl",
        auth=TOKENS,
        push_events_operational=push_events_operational,
    )


@pytest.mark.asyncio
async def test_real_streamable_http_push_is_recipient_scoped_and_idle_without_selects(tmp_path):
    app = live_app(tmp_path)
    repository = app.state.repository
    select_count = 0
    original_connect = repository.connect

    def traced_connect():
        nonlocal select_count
        conn = original_connect()

        def trace(sql):
            nonlocal select_count
            if sql.lstrip().upper().startswith(("SELECT", "WITH")):
                select_count += 1

        conn.set_trace_callback(trace)
        return conn

    repository.connect = traced_connect
    publisher = app.state.push_publisher

    async with loopback(app) as url:
        async with mcp_client(url, TOKENS.hermes_token) as (hermes, hermes_events, _, hermes_init):
            async with mcp_client(url, TOKENS.codex_token) as (codex, codex_events, _, codex_init):
                assert hermes_init.capabilities.resources.subscribe is True
                assert codex_init.capabilities.resources.subscribe is True
                assert [str(item.uri) for item in (await hermes.list_resources()).resources] == [BASE_URI.format("hermes")]
                assert [str(item.uri) for item in (await codex.list_resources()).resources] == [BASE_URI.format("codex")]
                await hermes.subscribe_resource(AnyUrl(BASE_URI.format("hermes")))
                await codex.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
                assert publisher.subscription_count == 2

                idle_selects = select_count
                await asyncio.sleep(0.08)
                assert select_count == idle_selects

                result = await hermes.call_tool("send_message", {
                    "recipient": "codex",
                    "message_type": "task",
                    "body": {"private": "recipient-only message body"},
                })
                assert not result.isError
                notification = await asyncio.wait_for(codex_events.get(), 2)
                assert str(notification.root.params.uri) == BASE_URI.format("codex")
                wire_notification = notification.root.model_dump(by_alias=True, exclude_none=True)
                assert "id" not in wire_notification
                assert set(wire_notification["params"]) == {"uri"}
                assert await no_notification(hermes_events)

                with pytest.raises(McpError):
                    await hermes.unsubscribe_resource(AnyUrl(BASE_URI.format("codex")))
                with pytest.raises(McpError):
                    await codex.subscribe_resource(AnyUrl(BASE_URI.format("codex") + "?after=0"))
                assert publisher.subscription_count == 2

                page = resource_json(await codex.read_resource(
                    AnyUrl(BASE_URI.format("codex") + "?after=0&limit=1")
                ))
                assert len(page["messages"]) == 1
                assert page["messages"][0]["body"] == {"private": "recipient-only message body"}
                assert page["messages"][0]["seq"] == page["next_cursor"]
                externally_committed = repository.insert(
                    sender="hermes", recipient="codex", message_type="external-event",
                    body={"private": "external postcommit payload"},
                )
                # The running native source must deliver this external commit.
                # A second manual publish races that source and may correctly
                # produce two invalidations through the bounded dirty bit.
                external_invalidation = await asyncio.wait_for(codex_events.get(), 2)
                assert str(external_invalidation.root.params.uri) == BASE_URI.format("codex")
                assert set(external_invalidation.root.params.model_dump(by_alias=True, exclude_none=True)) == {"uri"}
                next_page = resource_json(await codex.read_resource(AnyUrl(
                    BASE_URI.format("codex") + f"?after={page['next_cursor']}&limit=1"
                )))
                assert [item["id"] for item in next_page["messages"]] == [externally_committed.id]
                assert await no_notification(hermes_events)
                with pytest.raises(McpError):
                    await hermes.read_resource(AnyUrl(BASE_URI.format("codex") + "?after=0"))
                with pytest.raises(McpError):
                    await codex.read_resource(AnyUrl(BASE_URI.format("codex") + "?after=0&limit=101"))
                for invalid_query in (
                    "?after=-1", "?after=0&after=1", "?after=0&unknown=1", "?limit=0",
                ):
                    with pytest.raises(McpError):
                        await codex.read_resource(AnyUrl(BASE_URI.format("codex") + invalid_query))

                await hermes.unsubscribe_resource(AnyUrl(BASE_URI.format("hermes")))
                assert publisher.subscription_count == 1
        assert publisher.subscription_count == 0


@pytest.mark.asyncio
async def test_default_app_does_not_advertise_or_activate_unavailable_event_source(tmp_path):
    app = live_app(tmp_path, push_events_operational=False)
    async with loopback(app) as url:
        async with mcp_client(url, TOKENS.codex_token) as (client, _, _, initialized):
            assert initialized.capabilities.resources is not None
            assert initialized.capabilities.resources.subscribe is False
            with pytest.raises(McpError):
                await client.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
            assert app.state.push_publisher.subscription_count == 0
            assert await app.state.push_publisher.publish_committed("codex") == 0


@pytest.mark.asyncio
async def test_session_id_is_bound_to_authentication_for_get_post_and_delete(tmp_path):
    app = live_app(tmp_path)
    async with loopback(app) as url:
        async with mcp_client(url, TOKENS.codex_token) as (codex, _, session_id, _):
            await codex.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
            sid = session_id()
            assert sid
            async with httpx.AsyncClient(timeout=2) as attacker:
                headers = {"Authorization": f"Bearer {TOKENS.hermes_token}", "Mcp-Session-Id": sid}
                get = await attacker.get(url, headers={**headers, "Accept": "text/event-stream"})
                post = await attacker.post(url, headers=headers, json={
                    "jsonrpc": "2.0", "method": "ping", "id": 1,
                })
                delete = await attacker.delete(url, headers=headers)
                invalid_headers = {"Authorization": "Bearer invalid-token", "Mcp-Session-Id": sid}
                invalid = [
                    await attacker.get(url, headers={**invalid_headers, "Accept": "text/event-stream"}),
                    await attacker.post(url, headers=invalid_headers, json={"jsonrpc": "2.0", "method": "ping", "id": 2}),
                    await attacker.delete(url, headers=invalid_headers),
                ]
            assert (get.status_code, post.status_code, delete.status_code) == (404, 404, 404)
            assert [response.status_code for response in invalid] == [401, 401, 401]
            assert app.state.push_publisher.subscription_count == 1
            assert await codex.list_tools()
        async with httpx.AsyncClient(timeout=2) as attacker:
            stale = await attacker.delete(url, headers={
                "Authorization": f"Bearer {TOKENS.codex_token}", "Mcp-Session-Id": sid,
            })
        assert stale.status_code == 404
        assert app.state.push_publisher.subscription_count == 0


@pytest.mark.asyncio
async def test_failed_insert_emits_no_invalidation(tmp_path):
    app = live_app(tmp_path)
    with app.state.repository.connect() as conn:
        conn.execute("""CREATE TRIGGER reject_push_fixture BEFORE INSERT ON messages
            WHEN NEW.message_type='ROLLBACK_FIXTURE'
            BEGIN SELECT RAISE(ABORT, 'rollback fixture'); END""")

    async with loopback(app) as url:
        async with mcp_client(url, TOKENS.codex_token) as (codex, events, _, _):
            await codex.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
            async with mcp_client(url, TOKENS.hermes_token) as (hermes, _, _, _):
                result = await hermes.call_tool("send_message", {
                    "recipient": "codex", "message_type": "ROLLBACK_FIXTURE", "body": {"should": "rollback"},
                })
            assert result.isError
            with app.state.repository.connect() as conn:
                assert conn.execute("SELECT COUNT(*) FROM messages WHERE message_type='ROLLBACK_FIXTURE'").fetchone()[0] == 0
            assert await no_notification(events)


@pytest.mark.asyncio
async def test_reconnect_uses_explicit_cursor_catchup_not_queued_notifications(tmp_path):
    app = live_app(tmp_path)
    publisher = app.state.push_publisher
    async with loopback(app) as url:
        async with mcp_client(url, TOKENS.codex_token) as (first, first_events, _, _):
            await first.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
            initial = resource_json(await first.read_resource(AnyUrl(BASE_URI.format("codex") + "?after=0")))
            cursor = initial["next_cursor"]
        assert publisher.subscription_count == 0

        committed = app.state.repository.insert(
            sender="hermes", recipient="codex", message_type="offline", body={"catchup": True}
        )
        await publisher.publish_committed(committed.recipient)
        assert publisher.subscription_count == 0

        async with mcp_client(url, TOKENS.codex_token) as (reconnected, events, _, initialized):
            assert initialized.capabilities.resources.subscribe is True
            await reconnected.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
            caught_up = resource_json(await reconnected.read_resource(
                AnyUrl(BASE_URI.format("codex") + f"?after={cursor}")
            ))
            assert [item["id"] for item in caught_up["messages"]] == [committed.id]
            assert caught_up["messages"][0]["recipient"] == "codex"
            assert await no_notification(events)
        assert publisher.subscription_count == 0


@pytest.mark.asyncio
async def test_push_registry_coalesces_inflight_invalidations_and_bounds_subscriptions():
    class BlockedSession:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.uris = []

        async def send_resource_updated(self, uri):
            self.uris.append(str(uri))
            self.entered.set()
            await self.release.wait()

    publisher = PushPublisher(max_subscriptions=1, enabled=True)
    session = BlockedSession()
    await publisher.subscribe("session-one", "codex", session)
    with pytest.raises(SubscriptionLimit):
        await publisher.subscribe("session-two", "hermes", BlockedSession())

    first_publish = asyncio.create_task(publisher.publish_committed("codex"))
    await asyncio.wait_for(session.entered.wait(), 1)
    await publisher.publish_committed("codex")
    assert publisher.pending_count == 1
    session.release.set()
    await first_publish
    assert session.uris == [BASE_URI.format("codex"), BASE_URI.format("codex")]
    assert publisher.coalesced_notifications == 1
    assert publisher.pending_count == 0
    await publisher.drop_session("session-one")
    assert publisher.subscription_count == 0
