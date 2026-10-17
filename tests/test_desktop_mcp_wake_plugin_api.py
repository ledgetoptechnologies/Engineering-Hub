from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_DASHBOARD = Path(__file__).resolve().parents[1] / "integrations" / "hermes-mcp-wake" / "dashboard"
sys.path.insert(0, str(PLUGIN_DASHBOARD))

from agent_bus_wake_runtime import StateStore, WakeManager  # noqa: E402


def message():
    return {
        "id": "m-1", "seq": 1, "sender": "codex", "recipient": "hermes",
        "message_type": "task", "status": "pending", "requires_user_approval": False,
        "created_at": "2026-10-17T00:00:00+00:00", "body": {"topic": "alpha"},
    }


def test_restart_marks_uncertain_dispatch_non_retriable(tmp_path):
    path = tmp_path / "wake.sqlite3"
    store = StateStore(path)
    store.add_binding(
        server_name="bus", recipient="hermes", topic="alpha", stored_session_id="stored-root",
        connection_id="local", profile="default", target_profile="default",
    )
    store.set_baseline("bus", "hermes", 0)
    ids = store.record_page("bus", "hermes", expected_cursor=0, messages=[message()], next_cursor=1)
    assert store.begin_claim(ids[0]) is not None
    assert store.set_dispatches_status(ids, "claimed", claim_token="private-in-test", expected=("claiming",))
    with store._connect() as db:
        db.execute("UPDATE dispatches SET message_json=? WHERE id=?", (json.dumps(message()), ids[0]))
        db.execute("UPDATE settings SET value='1' WHERE key='schema_version'")

    after_restart = StateStore(path)
    assert after_restart.counts() == {"uncertain": 1}
    assert after_restart.pending_dispatch_ids() == []
    with after_restart._connect() as db:
        saved = json.loads(db.execute("SELECT message_json FROM dispatches WHERE id=?", (ids[0],)).fetchone()[0])
    assert "body" not in saved


@pytest.mark.asyncio
async def test_plugin_lifespan_restores_saved_opt_in_without_renderer(monkeypatch):
    from contextlib import asynccontextmanager, nullcontext
    from fastapi import FastAPI
    import plugin_api
    monkeypatch.setattr(plugin_api, '_owner_scope', nullcontext)
    calls = []
    class State:
        def enabled(self): return True
    class Manager:
        state = State()
        async def start(self, **kwargs): calls.append('start')
        async def stop(self, **kwargs): calls.append(('stop', kwargs['persist']))
    monkeypatch.setattr(plugin_api, '_manager', Manager())
    monkeypatch.setattr(plugin_api, '_dispatcher', None)
    @asynccontextmanager
    async def core_lifespan(app):
        calls.append('core-start')
        yield
        calls.append('core-stop')
    app = FastAPI(lifespan=core_lifespan)
    app.include_router(plugin_api.router)
    async with app.router.lifespan_context(app):
        assert calls == ['core-start', 'start']
    assert ('stop', False) in calls
    assert 'core-stop' in calls


@pytest.mark.asyncio
async def test_plugin_api_hides_server_headers_and_fails_closed_on_owner(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from fastapi import FastAPI
    import plugin_api

    state = StateStore(tmp_path / "wake.sqlite3")
    manager = WakeManager(
        state,
        config_loader=lambda: {
            "bus": {"url": "http://bus.invalid/mcp", "headers": {"Authorization": "Bearer fake"}}
        },
        session_validator=lambda session_id: session_id == "saved-session",
    )
    manager._server_name = "bus"
    manager._recipient = "hermes"
    manager._connection_state = "subscribed"
    state.set_enabled(True)
    monkeypatch.setattr(plugin_api, "_manager", manager)
    app = FastAPI()
    app.include_router(plugin_api.router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        server_list = await client.get("/servers")
        assert server_list.status_code == 200
        assert server_list.json() == {"servers": [{"name": "bus"}]}
        assert "Bearer fake" not in server_list.text

        wrong_owner = await client.post("/bindings", json={
            "topic": "alpha", "stored_session_id": "saved-session",
            "owner": {"connection_id": "local", "profile": "research", "target_profile": "research"},
        })
        assert wrong_owner.status_code == 422

        missing_session = await client.post("/bindings", json={
            "topic": "alpha", "stored_session_id": "not-found",
            "owner": {"connection_id": "local", "profile": "default", "target_profile": "default"},
        })
        assert missing_session.status_code == 404

        linked = await client.post("/bindings", json={
            "topic": "alpha", "stored_session_id": "saved-session",
            "owner": {"connection_id": "local", "profile": "default", "target_profile": "default"},
        })
        assert linked.status_code == 200
        assert linked.json()["binding"]["stored_session_id"] == "saved-session"
        assert linked.json()["binding"]["owner"] == {
            "connection_id": "local", "profile": "default", "target_profile": "default"
        }
        linked_binding = (await client.get("/bindings")).json()["bindings"][0]
        assert linked_binding["transport_attachment"] == {
            "kind": "recipient_inbox_resource", "topic": "alpha",
            "uri": "agentbus://inbox/hermes", "attached": False,
        }
        assert (await client.get("/status")).json()["resource_subscription_attached"] is False
