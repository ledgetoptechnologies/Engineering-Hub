from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import socket
import sqlite3
import struct
import uuid
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
from agent_message_bus.database import MessageRepository
from agent_message_bus.push import PushPublisher
from agent_message_bus.sqlite_events import (
    IN_Q_OVERFLOW,
    MessageCursorReader,
    SQLiteEventSource,
    WatcherFailed,
)


BASE_URI = "agentbus://inbox/{}"


@pytest.mark.asyncio
async def test_commit_after_client_read_during_send_remains_dirty_for_second_invalidation():
    class ReadDuringSendSession:
        def __init__(self):
            self.send_started = asyncio.Event()
            self.client_read = asyncio.Event()
            self.finish_first_send = asyncio.Event()
            self.uris: list[str] = []

        async def send_resource_updated(self, uri):
            self.uris.append(str(uri))
            if len(self.uris) == 1:
                self.send_started.set()
                await self.client_read.wait()
                await self.finish_first_send.wait()

    publisher = PushPublisher(enabled=True)
    session = ReadDuringSendSession()
    await publisher.subscribe("client-session", "codex", session)

    send_a = asyncio.create_task(publisher.publish_committed("codex"))
    await asyncio.wait_for(session.send_started.wait(), 1)
    session.client_read.set()  # The client reads commit A while send A is still in flight.
    await publisher.publish_committed("codex")  # Commit B must dirty the pending send.
    session.finish_first_send.set()
    await asyncio.wait_for(send_a, 1)

    assert session.uris == [BASE_URI.format("codex"), BASE_URI.format("codex")]
    assert publisher.pending_count == 0
    assert publisher.subscription_count == 1


@pytest.mark.asyncio
async def test_thread_bridge_keeps_commit_during_inflight_send_dirty():
    class BlockedSession:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.second_send = asyncio.Event()
            self.uris: list[str] = []

        async def send_resource_updated(self, uri):
            self.uris.append(str(uri))
            if len(self.uris) == 1:
                self.entered.set()
                await self.release.wait()
            else:
                self.second_send.set()

    publisher = PushPublisher(enabled=True)
    publisher.bind_loop(asyncio.get_running_loop())
    session = BlockedSession()
    await publisher.subscribe("thread-session", "codex", session)

    assert await asyncio.to_thread(publisher.publish_committed_from_thread, "codex")
    await asyncio.wait_for(session.entered.wait(), 1)
    assert await asyncio.to_thread(publisher.publish_committed_from_thread, "codex")
    session.release.set()
    await asyncio.wait_for(session.second_send.wait(), 1)
    await asyncio.sleep(0)  # Let the bounded drain clear its scheduler state.
    assert session.uris == [BASE_URI.format("codex"), BASE_URI.format("codex")]
    assert publisher.pending_count == 0


def test_cursor_reader_bootstraps_at_head_and_pages_recipient_only_with_gaps(tmp_path):
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    ids = [str(uuid.uuid4()) for _ in range(701)]
    recipients = ("hermes", "codex", "claude")
    with repository.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for index, message_id in enumerate(ids):
            conn.execute("""INSERT INTO messages
                (id,sender,recipient,message_type,body_json,created_at)
                VALUES (?, 'hermes', ?, 'event', ?, '2026-01-01T00:00:00Z')""",
                (message_id, recipients[index % len(recipients)], '{"never":"read"}'))
            conn.execute("INSERT INTO message_delivery_sequence (message_id) VALUES (?)", (message_id,))
        conn.execute("DELETE FROM messages WHERE id IN (SELECT message_id FROM message_delivery_sequence WHERE seq % 4 = 0)")
        conn.commit()

    reader = MessageCursorReader(page_size=23)
    trace: list[str] = []
    with repository.connect() as conn:
        conn.set_trace_callback(trace.append)
        startup_recipients, head = reader(conn, None)
        assert startup_recipients == ()
        assert head == 701
        assert reader.page_reads == 0

        changed_recipients, cursor = reader(conn, 0)

    assert changed_recipients == tuple(sorted(recipients))
    assert cursor == head
    assert reader.page_reads >= 20
    assert reader.max_page_rows <= 23
    assert all("body_json" not in statement.lower() for statement in trace)
    assert all("select message_id" not in statement.lower() for statement in trace)


TOKENS = AuthSettings("hermes-native-test", "codex-native-test", claude_token="claude-native-test")


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
        await asyncio.wait_for(server.ready.wait(), 10)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 10)
        except TimeoutError:
            server.force_exit = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        listener.close()
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
            f"{url}/mcp", http_client=http_client, terminate_on_close=True
        ) as (read, write, get_session_id):
            async with ClientSession(read, write, message_handler=on_message) as session:
                initialized = await session.initialize()
                yield session, notifications, get_session_id, initialized


def resource_json(result) -> dict:
    assert result.contents and isinstance(result.contents[0], types.TextResourceContents)
    return json.loads(result.contents[0].text)


async def no_notification(queue: asyncio.Queue, timeout: float = 0.15) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return True
        try:
            notification = await asyncio.wait_for(queue.get(), remaining)
        except TimeoutError:
            return True
        if getattr(notification.root, "method", None) == "notifications/resources/updated":
            return False


def assert_recipient_only_notification(notification, recipient: str, *, secret: str, message_id: str):
    wire = json.loads(notification.root.model_dump_json(by_alias=True, exclude_none=True))
    assert "id" not in wire
    assert wire["method"] == "notifications/resources/updated"
    assert wire["params"] == {"uri": BASE_URI.format(recipient)}
    serialized = json.dumps(wire)
    assert secret not in serialized
    assert message_id not in serialized


def _independent_writer(db_path: str, sender: str, recipient: str, message_id: str,
                       marker: str, barrier=None) -> None:
    if barrier is not None:
        barrier.wait(timeout=10)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""INSERT INTO messages
            (id,sender,recipient,message_type,body_json,created_at)
            VALUES (?, ?, ?, 'process-event', ?, '2026-01-01T00:00:00Z')""",
            (message_id, sender, recipient, json.dumps({"private": marker})))
        conn.execute("INSERT INTO message_delivery_sequence (message_id) VALUES (?)", (message_id,))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_native_eventsource_delivers_mcp_rest_and_independent_process_commits(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")
    db_path = tmp_path / "messages.sqlite3"
    seed = MessageRepository(db_path)
    historical = seed.insert(sender="hermes", recipient="codex", message_type="historical",
                             body={"private": "startup history is read only by client catchup"})
    app = create_app(db_path=db_path, audit_path=tmp_path / "audit.jsonl", auth=TOKENS,
                     file_root=tmp_path / "files")
    repository = app.state.repository
    source = None
    secrets: dict[str, str] = {}

    async with loopback(app) as base_url:
        source = app.state.push_event_source
        assert isinstance(source, SQLiteEventSource)
        assert source.healthy
        assert source.stats["startup_catchups"] == 1
        # A queued WAL CREATE is a genuine filesystem event and must also
        # reconcile commits that preceded installation of the WAL inode watch.
        assert int(source.stats["snapshot_reads"] or 0) >= 1
        assert source.stats["cursor"] == repository.delivery_head()
        assert app.state.push_cursor_reader.page_reads == 0
        assert app.state.push_publisher.enabled
        async with httpx.AsyncClient() as health_client:
            health = (await health_client.get(f"{base_url}/health")).json()
        assert health["push"]["state"] == "healthy"
        assert health["push"]["subscriptions_enabled"] is True

        async with mcp_client(base_url, TOKENS.hermes_token) as (hermes, hermes_events, _, hermes_init):
            async with mcp_client(base_url, TOKENS.codex_token) as (codex, codex_events, _, codex_init):
                async with mcp_client(base_url, TOKENS.claude_token) as (claude, claude_events, _, claude_init):
                    sessions = {"hermes": hermes, "codex": codex, "claude": claude}
                    queues = {"hermes": hermes_events, "codex": codex_events, "claude": claude_events}
                    initialized = {"hermes": hermes_init, "codex": codex_init, "claude": claude_init}
                    baseline = source.stats["cursor"]
                    for recipient, session in sessions.items():
                        assert initialized[recipient].capabilities.resources.subscribe is True
                        await session.subscribe_resource(AnyUrl(BASE_URI.format(recipient)))
                        initial = resource_json(await session.read_resource(AnyUrl(
                            f"{BASE_URI.format(recipient)}?after={baseline}&limit=100"
                        )))
                        assert initial["messages"] == []
                    historical_page = resource_json(await codex.read_resource(AnyUrl(
                        f"{BASE_URI.format('codex')}?after=0&limit=100"
                    )))
                    assert [message["id"] for message in historical_page["messages"]] == [historical.id]
                    assert await no_notification(codex_events)

                    idle_snapshots = source.stats["snapshot_reads"]
                    await asyncio.sleep(0.12)
                    assert source.stats["snapshot_reads"] == idle_snapshots

                    mcp_secret = "mcp-private-body-4791"
                    mcp_result = await hermes.call_tool("send_message", {
                        "recipient": "codex", "message_type": "task", "body": {"private": mcp_secret},
                    })
                    assert not mcp_result.isError
                    mcp_payload = mcp_result.structuredContent
                    if mcp_payload is None:
                        mcp_payload = json.loads(mcp_result.content[0].text)
                    mcp_id = mcp_payload["id"]
                    mcp_notice = await asyncio.wait_for(codex_events.get(), 3)
                    assert_recipient_only_notification(mcp_notice, "codex", secret=mcp_secret,
                                                       message_id=mcp_id)
                    assert await no_notification(hermes_events)
                    assert await no_notification(claude_events)

                    rest_secret = "rest-private-body-2859"
                    async with httpx.AsyncClient(timeout=5) as client:
                        response = await client.post(f"{base_url}/api/messages",
                            headers={"Authorization": f"Bearer {TOKENS.codex_token}"},
                            json={"recipient": "claude", "message_type": "task",
                                  "body": {"private": rest_secret}})
                    assert response.status_code == 201
                    rest_id = response.json()["id"]
                    rest_notice = await asyncio.wait_for(claude_events.get(), 3)
                    assert_recipient_only_notification(rest_notice, "claude", secret=rest_secret,
                                                       message_id=rest_id)
                    assert await no_notification(hermes_events)
                    assert await no_notification(codex_events)

                    process_id = str(uuid.uuid4())
                    process_secret = "independent-process-private-body"
                    process = multiprocessing.get_context("spawn").Process(
                        target=_independent_writer,
                        args=(str(db_path), "claude", "hermes", process_id, process_secret),
                    )
                    process.start()
                    await asyncio.to_thread(process.join, 10)
                    assert process.exitcode == 0
                    process_notice = await asyncio.wait_for(hermes_events.get(), 3)
                    assert_recipient_only_notification(process_notice, "hermes", secret=process_secret,
                                                       message_id=process_id)
                    assert await no_notification(codex_events)
                    assert await no_notification(claude_events)

                    # Simultaneous independent writers exercise native cross-process fanout.
                    mp = multiprocessing.get_context("spawn")
                    barrier = mp.Barrier(4)
                    concurrent = [
                        ("hermes", "codex"), ("codex", "claude"),
                        ("claude", "hermes"), ("hermes", "codex"),
                    ]
                    processes = []
                    concurrent_ids: dict[str, set[str]] = {name: set() for name in sessions}
                    for index, (sender, recipient) in enumerate(concurrent):
                        message_id = str(uuid.uuid4())
                        marker = f"concurrent-private-{index}"
                        concurrent_ids[recipient].add(message_id)
                        secrets[message_id] = marker
                        child = mp.Process(target=_independent_writer,
                            args=(str(db_path), sender, recipient, message_id, marker, barrier))
                        child.start()
                        processes.append(child)
                    await asyncio.gather(*(asyncio.to_thread(child.join, 10) for child in processes))
                    assert [child.exitcode for child in processes] == [0, 0, 0, 0]
                    latest_head = await asyncio.to_thread(repository.delivery_head)
                    assert source.wait_for_cursor(latest_head, timeout=5)

                    for recipient, expected_ids in concurrent_ids.items():
                        received = []
                        while True:
                            try:
                                notice = await asyncio.wait_for(queues[recipient].get(), 0.12)
                            except TimeoutError:
                                break
                            root = getattr(notice, "root", None)
                            if root is None or getattr(root, "method", None) != "notifications/resources/updated":
                                continue
                            wire = json.loads(root.model_dump_json(by_alias=True, exclude_none=True))
                            assert wire["params"] == {"uri": BASE_URI.format(recipient)}
                            serialized = json.dumps(wire)
                            assert all(secret not in serialized for secret in secrets.values())
                            assert all(message_id not in serialized for message_id in secrets)
                            received.append(notice)
                        assert received, f"missing concurrent invalidation for {recipient}"
                        page = resource_json(await sessions[recipient].read_resource(AnyUrl(
                            f"{BASE_URI.format(recipient)}?after={baseline}&limit=100"
                        )))
                        actual_ids = {message["id"] for message in page["messages"] if message["id"] in expected_ids}
                        assert actual_ids == expected_ids
                        assert all(message["recipient"] == recipient for message in page["messages"])

    assert source is not None
    assert source.stats["watcher_stopped"] is True


async def wait_for_notification_method(queue: asyncio.Queue, method: str, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for MCP notification {method}")
        notification = await asyncio.wait_for(queue.get(), remaining)
        root = getattr(notification, "root", None)
        if root is not None and getattr(root, "method", None) == method:
            return root


@pytest.mark.asyncio
async def test_database_replacement_fails_closed_and_is_observable_to_subscribed_client(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")
    db_path = tmp_path / "messages.sqlite3"
    app = create_app(db_path=db_path, audit_path=tmp_path / "audit.jsonl", auth=TOKENS,
                     file_root=tmp_path / "files", push_events_operational=True)
    async with loopback(app) as base_url:
        source = app.state.push_event_source
        assert source.healthy
        async with mcp_client(base_url, TOKENS.codex_token) as (client, events, _, initialized):
            assert initialized.capabilities.resources.subscribe is True
            await client.subscribe_resource(AnyUrl(BASE_URI.format("codex")))

            replacement = tmp_path / "replacement.sqlite3"
            source_db = app.state.repository.connect()
            destination = sqlite3.connect(replacement)
            try:
                source_db.backup(destination)
                destination.execute("PRAGMA journal_mode=WAL")
                destination.commit()
            finally:
                destination.close()
                source_db.close()
            os.replace(replacement, db_path)

            assert await asyncio.to_thread(source._thread_done.wait, 5)
            assert not source.healthy
            assert not app.state.push_publisher.enabled
            assert app.state.push_publisher.subscription_count == 0
            health_client = httpx.AsyncClient()
            try:
                health = (await health_client.get(f"{base_url}/health")).json()
            finally:
                await health_client.aclose()
            assert health["ok"] is False
            assert health["push"]["state"] == "failed"
            assert health["push"]["subscriptions_enabled"] is False

            notice = await wait_for_notification_method(events, "notifications/message")
            notice_json = json.loads(notice.model_dump_json(by_alias=True, exclude_none=True))
            assert notice_json["params"]["data"]["event"] == "resource_subscriptions_unavailable"
            with pytest.raises(McpError):
                await client.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
            assert await client.list_tools()
            async with mcp_client(base_url, TOKENS.hermes_token) as (new_client, _, _, new_init):
                assert new_init.capabilities.resources.subscribe is False
                assert await new_client.list_tools()
            assert "Native SQLite event source failed closed" in caplog.text
    assert source.stats["watcher_stopped"] is True


def test_inotify_overflow_is_a_fatal_source_failure(tmp_path):
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    reader = MessageCursorReader(page_size=8)
    failures = []
    source = SQLiteEventSource(repository.path, reader, lambda recipients, cursor: None,
                               on_error=failures.append)
    source.start()
    overflow = struct.pack("iIII", -1, IN_Q_OVERFLOW, 0, 0)
    with pytest.raises(WatcherFailed, match="IN_Q_OVERFLOW") as raised:
        source._process_data(overflow)
    source._fail(raised.value)
    thread_stopped = source._thread_done.wait(5)
    assert thread_stopped
    assert not source.healthy
    assert failures and isinstance(failures[0], WatcherFailed)
    assert source.stats["watcher_stopped"] is True


@pytest.mark.asyncio
async def test_auto_mode_disables_subscription_capability_on_unsupported_platform(tmp_path, monkeypatch):
    import agent_message_bus.sqlite_events as sqlite_events

    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setattr(sqlite_events, "sys_platform_linux", lambda: False)
    app = create_app(db_path=tmp_path / "messages.sqlite3", audit_path=tmp_path / "audit.jsonl",
                     auth=TOKENS, file_root=tmp_path / "files")
    async with loopback(app) as base_url:
        assert app.state.push_event_source is None
        assert not app.state.push_publisher.enabled
        async with mcp_client(base_url, TOKENS.codex_token) as (client, _, _, initialized):
            assert initialized.capabilities.resources.subscribe is False
            with pytest.raises(McpError):
                await client.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
        async with httpx.AsyncClient() as health_client:
            health = (await health_client.get(f"{base_url}/health")).json()
        assert health["push"]["state"] == "disabled_unsupported"
        assert health["push"]["subscriptions_enabled"] is False

    required_app = create_app(db_path=tmp_path / "required.sqlite3",
        audit_path=tmp_path / "required-audit.jsonl", auth=TOKENS,
        file_root=tmp_path / "required-files", push_events_operational=True)
    with pytest.raises(ExceptionGroup) as raised:
        async with required_app.router.lifespan_context(required_app):
            pytest.fail("required native push must reject unsupported platforms")
    assert raised.value.subgroup(RuntimeError) is not None
    assert any("native MCP subscriptions required but unavailable" in str(error)
               for error in raised.value.exceptions)
