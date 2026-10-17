from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

PLUGIN_DASHBOARD = Path(__file__).resolve().parents[1] / "integrations" / "hermes-mcp-wake" / "dashboard"
sys.path.insert(0, str(PLUGIN_DASHBOARD))

from agent_bus_wake_runtime import StateStore, WakeError, WakeManager  # noqa: E402


class FakeSession:
    def __init__(self, *, baseline_cursor=0, baseline_messages=None):
        self.handler = None
        self.subscribed = []
        self.calls = []
        self.reads = []
        self.pages = []
        self.baseline_cursor = baseline_cursor
        self.baseline_messages = baseline_messages or []
        self.message_by_id = {}

    async def initialize(self):
        return SimpleNamespace(capabilities=SimpleNamespace(resources=SimpleNamespace(subscribe=True)))

    async def list_resources(self):
        return SimpleNamespace(resources=[SimpleNamespace(uri="agentbus://inbox/hermes")])

    async def subscribe_resource(self, uri):
        self.subscribed.append(str(uri))

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "wait_for_messages":
            assert arguments == {"since_now": True, "timeout": 0, "fields": "full", "limit": 100}
            next_cursor = self.baseline_messages[-1]["seq"] if self.baseline_messages else self.baseline_cursor
            return {"messages": self.baseline_messages, "next_cursor": next_cursor}
        if name == "claim_message":
            message = dict(self.message_by_id[arguments["message_id"]])
            message["status"] = "claimed"
            return message
        if name == "ack_message":
            return {"status": "acknowledged"}
        raise AssertionError(f"unexpected tool: {name}")

    async def read_resource(self, uri):
        uri = str(uri)
        self.reads.append(uri)
        if self.pages:
            page = self.pages.pop(0)
        else:
            after = int(parse_qs(urlsplit(uri).query)["after"][0])
            page = {"messages": [], "next_cursor": after}
        return SimpleNamespace(contents=[SimpleNamespace(text=json.dumps(page))])

    async def notify(self, uri="agentbus://inbox/hermes"):
        await self.handler(SimpleNamespace(params=SimpleNamespace(uri=uri)))


@asynccontextmanager
async def fake_connector(session, config, message_handler):
    assert config["url"] == "http://bus.invalid/mcp"
    session.handler = message_handler
    yield session


def make_message(seq, message_id, *, topic="alpha", status="pending", requires_user_approval=False, body=None):
    return {
        "id": message_id,
        "seq": seq,
        "sender": "codex",
        "recipient": "hermes",
        "message_type": "task",
        "priority": "normal",
        "correlation_id": None,
        "in_reply_to": None,
        "hop_count": 0,
        "requires_user_approval": requires_user_approval,
        "created_at": "2026-10-17T00:00:00+00:00",
        "status": status,
        "body": body if body is not None else {"topic": topic, "text": f"task-{seq}"},
    }


def make_manager(tmp_path, session, notifications, *, cursor=None):
    state = StateStore(tmp_path / "wake.sqlite3")
    if cursor is not None:
        state.set_baseline("bus", "hermes", cursor)
    @asynccontextmanager
    async def connector(config, handler):
        async with fake_connector(session, config, handler):
            yield session
    manager = WakeManager(
        state,
        config_loader=lambda: {"bus": {"url": "http://bus.invalid/mcp", "headers": {"Authorization": "Bearer fake"}}},
        connector=connector,
        notifier=lambda dispatch_id: notifications.append(dispatch_id),
        session_validator=lambda _session_id: True,
    )
    return manager, state


def add_binding(state, *, session_id="stored-root", topic="alpha"):
    return state.add_binding(
        server_name="bus", recipient="hermes", topic=topic,
        stored_session_id=session_id, connection_id="local",
        profile="default", target_profile="default",
    )


@pytest.mark.asyncio
async def test_resource_drain_failure_tears_down_connection_instead_of_silent_death(tmp_path):
    class BrokenRead(FakeSession):
        async def read_resource(self, uri):
            raise RuntimeError("resource read failed")

    manager, _state = make_manager(tmp_path, BrokenRead(), [], cursor=0)
    manager._server_name = "bus"
    with pytest.raises(RuntimeError, match="resource read failed"):
        await asyncio.wait_for(manager._connect_once(), timeout=0.2)
    assert manager._session is None
    assert manager._subscribed_uri is None


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['reply', 'info', 'error'])
async def test_bound_peer_continuations_can_be_claimed_without_new_worker(tmp_path, kind):
    message = {**make_message(1, 'continuation'), 'message_type': kind}
    session = FakeSession()
    session.message_by_id = {'continuation': message}
    manager, state = make_manager(tmp_path, session, [])
    add_binding(state)
    try:
        await manager.start('bus', wait_seconds=1)
        ids = state.record_page('bus', 'hermes', expected_cursor=0, messages=[message], next_cursor=1)
        assert len(ids) == 1
        claim = await manager.claim_dispatch(ids[0])
        assert claim['messages'][0]['message_type'] == kind
    finally:
        await manager.stop()


def test_binding_requires_exact_default_profile_and_survives_store_reopen(tmp_path):
    path = tmp_path / "wake.sqlite3"
    state = StateStore(path)
    binding = add_binding(state)
    with pytest.raises(WakeError, match="local/default-profile"):
        state.add_binding(
            server_name="bus", recipient="hermes", topic="other",
            stored_session_id="stored-child", connection_id="local",
            profile="research", target_profile="research",
        )
    with pytest.raises(WakeError, match="surrounding whitespace"):
        add_binding(state, topic=" alpha ")
    state.set_baseline("bus", "hermes", 81)

    reopened = StateStore(path)
    assert reopened.cursor("bus", "hermes") == 81
    assert reopened.bindings(server_name="bus")[0]["id"] == binding["id"]
    assert reopened.bindings(server_name="bus")[0]["stored_session_id"] == "stored-root"


def test_unlinked_topic_can_be_explicitly_reassigned(tmp_path):
    state = StateStore(tmp_path / "wake.sqlite3")
    original = add_binding(state, session_id="old-chat")
    assert state.remove_binding(original["id"])
    replacement = add_binding(state, session_id="new-chat")
    assert replacement["id"] != original["id"]
    assert replacement["stored_session_id"] == "new-chat"


@pytest.mark.asyncio
async def test_fresh_subscribe_uses_head_snapshot_without_reading_history(tmp_path):
    session = FakeSession(baseline_cursor=900)
    notifications = []
    manager, state = make_manager(tmp_path, session, notifications)
    try:
        status = await manager.start("bus", wait_seconds=1)
        assert status["state"] == "subscribed"
        assert status["resource_subscription_attached"] is True
        assert status["resource_uri"] == "agentbus://inbox/hermes"
        assert session.subscribed == ["agentbus://inbox/hermes"]
        assert state.cursor("bus", "hermes") == 900
        assert [name for name, _ in session.calls] == ["wait_for_messages"]
        assert session.calls[0][1]["timeout"] == 0
        assert session.reads == []
        assert notifications == []
    finally:
        stopped = await manager.stop(persist=False)
        assert stopped["resource_subscription_attached"] is False
        assert stopped["resource_uri"] is None


@pytest.mark.asyncio
async def test_fresh_snapshot_dispatches_only_post_head_message_without_storing_body(tmp_path):
    session = FakeSession(baseline_cursor=6, baseline_messages=[make_message(7, "m-7")])
    session.message_by_id = {"m-7": make_message(7, "m-7")}
    notifications = []
    manager, state = make_manager(tmp_path, session, notifications)
    add_binding(state, topic="alpha")
    try:
        await manager.start("bus", wait_seconds=1)
        assert state.cursor("bus", "hermes") == 7
        assert len(notifications) == 1
        with state._connect() as db:
            saved = json.loads(db.execute(
                "SELECT message_json FROM dispatches WHERE id=?", (notifications[0],)
            ).fetchone()[0])
        assert saved["id"] == "m-7"
        assert "body" not in saved
        dispatch = await manager.claim_dispatch(notifications[0])
        assert dispatch["messages"][0]["body"]["text"] == "task-7"
    finally:
        await manager.stop(persist=False)


@pytest.mark.asyncio
async def test_resource_updates_route_topic_batch_to_one_exact_binding(tmp_path):
    session = FakeSession()
    notifications = []
    manager, state = make_manager(tmp_path, session, notifications, cursor=1)
    binding = add_binding(state, session_id="stored-owner-session")
    try:
        await manager.start("bus", wait_seconds=1)
        # Startup recovery is bounded to the durable cursor and is empty here.
        await asyncio.sleep(0)
        initial_reads = len(session.reads)
        session.message_by_id = {"m-2": make_message(2, "m-2"), "m-3": make_message(3, "m-3")}
        session.pages.extend([
            {"messages": [make_message(2, "m-2"), make_message(3, "m-3")], "next_cursor": 3},
            {"messages": [], "next_cursor": 3},
        ])

        # URI invalidations are hints only; a different inbox URI is ignored.
        await session.notify("agentbus://inbox/someone-else")
        await asyncio.sleep(0)
        assert len(session.reads) == initial_reads

        event = asyncio.Event()
        manager._notifier = lambda dispatch_id: (notifications.append(dispatch_id), event.set())
        await session.notify()
        await session.notify()  # coalesces while the page is being reconciled
        await asyncio.wait_for(event.wait(), timeout=1)

        assert state.cursor("bus", "hermes") == 3
        assert len(notifications) == 1
        with state._connect() as db:
            saved = json.loads(db.execute("SELECT message_json FROM dispatches WHERE id=?", (notifications[-1],)).fetchone()[0])
        assert "body" not in saved
        assert saved["topic"] == "alpha"
        dispatch = await manager.claim_dispatch(notifications[-1])
        assert dispatch["binding"]["id"] == binding["id"]
        assert dispatch["binding"]["stored_session_id"] == "stored-owner-session"
        assert dispatch["binding"]["owner"] == {
            "connection_id": "local", "profile": "default", "target_profile": "default"
        }
        attached = manager.binding_statuses()[0]["transport_attachment"]
        assert attached == {
            "kind": "recipient_inbox_resource", "topic": "alpha",
            "uri": "agentbus://inbox/hermes", "attached": True,
        }
        assert [message["id"] for message in dispatch["messages"]] == ["m-2", "m-3"]
        assert dispatch["messages"][0]["body"]["text"] == "task-2"
        assert len([call for call in session.calls if call[0] == "claim_message"]) == 2
        with pytest.raises(WakeError, match="no longer pending"):
            await manager.claim_dispatch(notifications[-1])

        await manager.complete_dispatch(notifications[-1], dispatch["claim_token"])
        assert len([call for call in session.calls if call[0] == "ack_message"]) == 2
        assert state.counts()["submitted"] == 2
        assert manager.pending()["dispatch_ids"] == []
    finally:
        await manager.stop(persist=False)


@pytest.mark.asyncio
async def test_restart_catches_up_only_after_saved_cursor(tmp_path):
    session = FakeSession()
    notifications = []
    manager, state = make_manager(tmp_path, session, notifications, cursor=41)
    add_binding(state)
    session.pages.extend([
        {"messages": [make_message(46, "new-46")], "next_cursor": 46},
        {"messages": [], "next_cursor": 46},
    ])
    try:
        await manager.start("bus", wait_seconds=1)
        # Catch-up begins after the saved cursor, not at zero or history head.
        for _ in range(20):
            if notifications:
                break
            await asyncio.sleep(0.01)
        assert notifications
        assert "after=41" in session.reads[0]
        assert state.cursor("bus", "hermes") == 46
        assert len(notifications) == 1
    finally:
        await manager.stop(persist=False)


@pytest.mark.asyncio
async def test_approval_required_and_nonmatching_messages_advance_without_dispatch(tmp_path):
    session = FakeSession()
    notifications = []
    manager, state = make_manager(tmp_path, session, notifications, cursor=10)
    add_binding(state, topic="alpha")
    session.pages.extend([
        {"messages": [
            make_message(11, "wrong-topic", topic="beta"),
            make_message(12, "needs-approval", requires_user_approval=True),
            {**make_message(13, "status-message"), "message_type": "status"},
        ], "next_cursor": 13},
        {"messages": [], "next_cursor": 13},
    ])
    try:
        await manager.start("bus", wait_seconds=1)
        for _ in range(20):
            if state.cursor("bus", "hermes") == 13:
                break
            await asyncio.sleep(0.01)
        assert state.cursor("bus", "hermes") == 13
        assert notifications == []
        assert manager.pending()["dispatch_ids"] == []
    finally:
        await manager.stop(persist=False)


@pytest.mark.asyncio
async def test_server_without_native_subscription_fails_closed(tmp_path):
    class Unsupported(FakeSession):
        async def initialize(self):
            return SimpleNamespace(capabilities=SimpleNamespace(resources=SimpleNamespace(subscribe=False)))

    session = Unsupported()
    notifications = []
    manager, _state = make_manager(tmp_path, session, notifications)
    try:
        status = await manager.start("bus", wait_seconds=1)
        assert status["state"] == "unavailable"
        assert status["error"] == "WakeError"
        assert session.subscribed == []
        assert session.calls == []
        assert notifications == []
    finally:
        await manager.stop(persist=False)
