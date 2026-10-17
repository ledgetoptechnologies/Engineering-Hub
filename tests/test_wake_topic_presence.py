from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DASHBOARD = Path(__file__).resolve().parents[1] / "integrations" / "hermes-mcp-wake" / "dashboard"
sys.path.insert(0, str(PLUGIN_DASHBOARD))

from agent_bus_wake_runtime import StateStore, WakeManager  # noqa: E402


class Session:
    def __init__(self):
        self.handler = None
        self.calls = []
        self.joined = {}
        self.list_tools_calls = 0

    async def initialize(self):
        return SimpleNamespace(capabilities=SimpleNamespace(resources=SimpleNamespace(subscribe=True)))

    async def list_resources(self):
        return SimpleNamespace(resources=[SimpleNamespace(uri="agentbus://inbox/hermes")])

    async def subscribe_resource(self, _uri):
        pass

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "wait_for_messages":
            return {"messages": [], "next_cursor": 0}
        if name == "topic_presence_join":
            result = {"topic": arguments["topic"], "agent": "hermes",
                      "generation": f"generation-{arguments['topic']}", "registered_at": "now"}
            self.joined[arguments["topic"]] = result
            return result
        if name == "topic_presence_status":
            return {"topic": arguments["topic"], "observed_at": "now", "members": [
                {"agent": "hermes", "state": "online", "online": True, "registered_at": "now"}
            ]}
        if name == "topic_presence_leave":
            return {"topic": arguments["topic"], "removed": True}
        raise AssertionError(f"unexpected tool: {name}")

    async def list_tools(self):
        self.list_tools_calls += 1
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in (
            "topic_presence_join", "topic_presence_status", "topic_presence_leave"
        )])

    async def read_resource(self, uri):
        from urllib.parse import parse_qs, urlsplit
        cursor = int(parse_qs(urlsplit(str(uri)).query)["after"][0])
        return SimpleNamespace(contents=[SimpleNamespace(text=json.dumps({"messages": [], "next_cursor": cursor}))])

    async def disconnect(self):
        await self.handler(RuntimeError("transport closed"))


class OldSession(Session):
    # Production session predates list_tools; missing introspection is unsupported, not fatal.
    list_tools = None


def make_manager(tmp_path, sessions):
    state = StateStore(tmp_path / "wake.sqlite3")
    state.set_baseline("bus", "hermes", 0)
    yielded = []

    @asynccontextmanager
    async def connector(config, handler):
        assert config["url"] == "http://bus.invalid/mcp"
        session = sessions[len(yielded)]
        yielded.append(session)
        session.handler = handler
        yield session

    manager = WakeManager(
        state, config_loader=lambda: {"bus": {"url": "http://bus.invalid/mcp"}},
        connector=connector, session_validator=lambda _session_id: True,
    )
    manager._server_name = "bus"
    return manager, state, yielded


def add_binding(state, topic):
    return state.add_binding(
        server_name="bus", recipient="hermes", topic=topic,
        stored_session_id=f"stored-{topic}", connection_id="local",
        profile="default", target_profile="default",
    )


async def wait_until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    assert predicate(), "condition was not reached"


@pytest.mark.asyncio
async def test_old_server_without_tool_discovery_keeps_wake_and_reports_unsupported(tmp_path):
    session = OldSession()
    manager, state, _yielded = make_manager(tmp_path, [session])
    add_binding(state, "alpha")
    try:
        status = await manager.start("bus", wait_seconds=1)
        assert status["state"] == "subscribed"
        assert status["resource_subscription_attached"] is True
        assert manager.topic_presence_status()["availability"] == "unsupported"
        assert manager.binding_statuses()[0]["topic_presence"]["state"] == "unsupported"
        assert not [name for name, _ in session.calls if name.startswith("topic_presence_")]
    finally:
        await manager.stop(persist=False)


@pytest.mark.asyncio
async def test_join_two_topics_and_binding_add_unlink_use_same_native_session(tmp_path, monkeypatch):
    session = Session()
    manager, state, yielded = make_manager(tmp_path, [session])
    alpha = add_binding(state, "alpha")
    add_binding(state, "beta")
    try:
        await manager.start("bus", wait_seconds=1)
        assert session.list_tools_calls == 1
        assert [args["topic"] for name, args in session.calls if name == "topic_presence_join"] == ["alpha", "beta"]
        assert {item["topic_presence"]["state"] for item in manager.binding_statuses()} == {"joined"}
        assert all(item["topic_presence"]["server_status"]["members"][0]["online"]
                   for item in manager.binding_statuses())

        import plugin_api
        monkeypatch.setattr(plugin_api, "_manager", manager)
        linked = await plugin_api.add_binding({
            "topic": "gamma", "stored_session_id": "stored-gamma",
            "owner": {"connection_id": "local", "profile": "default", "target_profile": "default"},
        })
        gamma = linked["binding"]
        assert linked["topic_presence"]["state"] == "joined"
        assert session.joined["gamma"]["topic"] == "gamma"
        removed = await plugin_api.remove_binding(alpha["id"])
        assert removed["removed"] is True
        leave = [args for name, args in session.calls if name == "topic_presence_leave"]
        assert leave == [{"topic": "alpha", "generation": "generation-alpha"}]
        assert len(yielded) == 1
        assert session.list_tools_calls == 1
        assert {b["topic"] for b in state.bindings()} == {"beta", "gamma"}
        assert gamma["topic"] == "gamma"
    finally:
        await manager.stop(persist=False)


@pytest.mark.asyncio
async def test_disconnect_clears_local_generation_without_leaving_and_rejoins(tmp_path):
    first, second = Session(), Session()
    manager, state, yielded = make_manager(tmp_path, [first, second])
    add_binding(state, "alpha")
    try:
        task = asyncio.create_task(manager._connect_once())
        await wait_until(lambda: manager._connection_state == "subscribed")
        assert manager.topic_presence_status()["topics"]["alpha"]["state"] == "joined"
        await first.disconnect()
        await asyncio.wait_for(task, timeout=1)
        disconnected = manager.binding_statuses()[0]["topic_presence"]
        assert disconnected["state"] == "disconnected"
        assert "generation" not in disconnected
        assert not [args for name, args in first.calls if name == "topic_presence_leave"]
        assert manager._topic_presence_generations == {}

        task = asyncio.create_task(manager._connect_once())
        await wait_until(lambda: manager._connection_state == "subscribed" and len(yielded) == 2)
        assert [args["topic"] for name, args in second.calls if name == "topic_presence_join"] == ["alpha"]
        assert manager.binding_statuses()[0]["topic_presence"]["state"] == "joined"
        await second.disconnect()
        await asyncio.wait_for(task, timeout=1)
        assert manager.binding_statuses()[0]["topic_presence"]["state"] == "disconnected"
        assert not [args for name, args in second.calls if name == "topic_presence_leave"]
    finally:
        await manager.stop(persist=False)
