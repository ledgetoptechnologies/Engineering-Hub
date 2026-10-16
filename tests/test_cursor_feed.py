from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from agent_message_bus.api import Service
from agent_message_bus.app import create_app
from agent_message_bus.audit import AuditLog
from agent_message_bus.auth import AuthSettings
from agent_message_bus.database import MessageRepository
from agent_message_bus.retention import RetentionPolicy


def _insert_messages(path: str, sender: str, count: int, result_queue) -> None:
    repo = MessageRepository(path)
    ids = [repo.insert(sender=sender, recipient="codex", message_type="event", body={"n": i}).id
           for i in range(count)]
    result_queue.put(ids)


def _insert_once(path: str) -> None:
    MessageRepository(path).insert(sender="hermes", recipient="codex", message_type="arrived-during-wait",
                                   body={"subject": "cross process"})


def _insert_once_in_process(path: str) -> None:
    worker = multiprocessing.get_context("spawn").Process(target=_insert_once, args=(path,))
    worker.start()
    worker.join(15)
    assert worker.exitcode == 0


def _service(path, audit_path, auth=None):
    return Service(MessageRepository(path), AuditLog(audit_path),
                   auth or AuthSettings("h-token", "c-token", "a-token", claude_token="claude-token"),
                   RetentionPolicy())


@pytest.fixture
async def cursor_client(tmp_path):
    app = create_app(db_path=tmp_path / "messages.sqlite3", audit_path=tmp_path / "audit.jsonl",
                     auth=AuthSettings("h-token", "c-token", "a-token", claude_token="claude-token"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield app, client


def test_legacy_migration_backfills_deterministically_without_firing_message_update_triggers(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            sender TEXT NOT NULL CHECK (sender IN ('hermes', 'codex')),
            recipient TEXT NOT NULL CHECK (recipient IN ('hermes', 'codex')),
            message_type TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal', body_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', correlation_id TEXT,
            requires_user_approval INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            claimed_by TEXT, claim_expires_at TEXT, acknowledged_at TEXT, rejected_at TEXT,
            rejection_reason TEXT
        );
        CREATE TABLE update_events (n INTEGER NOT NULL);
        CREATE TRIGGER legacy_message_update AFTER UPDATE ON messages
        BEGIN INSERT INTO update_events VALUES (1); END;
        INSERT INTO messages(id,sender,recipient,message_type,body_json,created_at)
            VALUES ('z','hermes','codex','legacy','{}','2024-01-02T00:00:00Z');
        INSERT INTO messages(id,sender,recipient,message_type,body_json,created_at)
            VALUES ('b','hermes','codex','legacy','{}','2024-01-01T00:00:00Z');
        INSERT INTO messages(id,sender,recipient,message_type,body_json,created_at)
            VALUES ('a','hermes','codex','legacy','{}','2024-01-01T00:00:00Z');
    """)
    conn.commit()
    conn.close()

    repo = MessageRepository(path)
    with repo.connect() as conn:
        assert [tuple(row) for row in conn.execute(
            "SELECT message_id, seq FROM message_delivery_sequence ORDER BY seq"
        )] == [("a", 1), ("b", 2), ("z", 3)]
        assert conn.execute("SELECT COUNT(*) FROM update_events").fetchone()[0] == 0
        conn.execute("UPDATE messages SET status=status WHERE id='a'")
        assert conn.execute("SELECT COUNT(*) FROM update_events").fetchone()[0] == 1
    repo.initialize()
    assert repo.delivery_head() == 3


def test_sequence_allocation_is_transactional_and_survives_retention_restart(tmp_path):
    path = tmp_path / "messages.sqlite3"
    repo = MessageRepository(path)
    first = repo.insert(sender="hermes", recipient="codex", message_type="event", body={})
    with repo.connect() as conn:
        conn.execute("""CREATE TRIGGER reject_delivery_seq BEFORE INSERT ON message_delivery_sequence
            BEGIN SELECT RAISE(ABORT, 'blocked for test'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="blocked for test"):
        repo.insert(sender="hermes", recipient="codex", message_type="event", body={})
    with repo.connect() as conn:
        conn.execute("DROP TRIGGER reject_delivery_seq")
        conn.execute("UPDATE messages SET status='acknowledged', acknowledged_at='2000-01-01T00:00:00Z' WHERE id=?",
                     (first.id,))
    assert repo.cleanup(acknowledged_days=1) == {"acknowledged_deleted": 1, "rejected_deleted": 0}
    assert repo.delivery_head() == 1
    restarted = MessageRepository(path)
    second = restarted.insert(sender="hermes", recipient="codex", message_type="event", body={})
    assert restarted.sequence_for(second.id) == 2
    assert restarted.delivery_head() == 2


def test_delivery_head_detects_reset_below_existing_sequence(tmp_path):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    repo.insert(sender="hermes", recipient="codex", message_type="event", body={})
    with repo.connect() as conn:
        conn.execute("UPDATE sqlite_sequence SET seq=0 WHERE name='message_delivery_sequence'")
    with pytest.raises(ValueError, match="high-water mark is inconsistent"):
        repo.delivery_head()
    with repo.connect() as conn:
        conn.execute("UPDATE sqlite_sequence SET seq=1 WHERE name='message_delivery_sequence'")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='message_delivery_sequence'")
    with pytest.raises(ValueError, match="high-water mark is inconsistent"):
        repo.delivery_head()


def test_two_process_repository_writers_allocate_unique_monotonic_sequences(tmp_path):
    path = str(tmp_path / "writers.sqlite3")
    MessageRepository(path)
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    workers = [context.Process(target=_insert_messages, args=(path, sender, 8, results))
               for sender in ("hermes", "claude")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0
    for _ in workers:
        results.get(timeout=2)
    repo = MessageRepository(path)
    with repo.connect() as conn:
        seqs = [row[0] for row in conn.execute("SELECT seq FROM message_delivery_sequence ORDER BY seq")]
    assert seqs == list(range(1, 17))
    assert repo.delivery_head() == 16


def test_recipient_cursor_pagination_ignores_status_and_message_type(tmp_path):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    messages = [repo.insert(sender="hermes", recipient="codex", message_type=f"type-{i}", body={"n": i})
                for i in range(5)]
    repo.insert(sender="hermes", recipient="claude", message_type="other", body={})
    repo.claim(messages[1].id, "codex")
    repo.claim(messages[2].id, "codex")
    repo.acknowledge(messages[2].id, "codex")
    repo.claim(messages[3].id, "codex")
    repo.reject(messages[3].id, "codex", "rejected")
    first, next_cursor = repo.list_after("codex", after=0, limit=2)
    assert [item["id"] for item in first] == [messages[0].id, messages[1].id]
    assert next_cursor == repo.sequence_for(messages[1].id)
    second, cursor2 = repo.list_after("codex", after=next_cursor, limit=2)
    assert [item["id"] for item in second] == [messages[2].id, messages[3].id]
    assert cursor2 == repo.sequence_for(messages[3].id)
    third, cursor3 = repo.list_after("codex", after=cursor2, limit=2)
    assert [item["id"] for item in third] == [messages[4].id]
    assert cursor3 == repo.sequence_for(messages[4].id)
    assert {item["status"] for item in first + second + third} == {"pending", "claimed", "acknowledged", "rejected"}
    assert repo.list_after("codex", after=cursor3, limit=2) == ([], cursor3)


@pytest.mark.asyncio
async def test_rest_cursor_pointer_is_private_and_suppresses_known_secrets(cursor_client):
    app, client = cursor_client
    sent = await client.post("/api/messages", headers={"Authorization": "Bearer h-token"}, json={
        "recipient": "codex", "message_type": "task", "priority": "urgent",
        "correlation_id": "corr", "in_reply_to": None, "requires_user_approval": True,
        "body": {"subject": "leak c-token", "private": "BODY-DO-NOT-RETURN"},
    })
    msg = sent.json()
    response = await client.get("/api/messages/wait?after=0&timeout=0&fields=pointer&limit=1",
                               headers={"Authorization": "Bearer c-token"})
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"messages", "next_cursor", "oldest_available_seq"}
    pointer = data["messages"][0]
    assert set(pointer) == {"id", "seq", "sender", "recipient", "message_type", "priority",
                            "correlation_id", "in_reply_to", "hop_count", "requires_user_approval",
                            "created_at", "subject"}
    assert pointer["subject"] is None
    assert pointer["id"] == msg["id"] and pointer["seq"] == 1
    assert "BODY-DO-NOT-RETURN" not in response.text and "body" not in pointer
    assert data["next_cursor"] == 1 and data["oldest_available_seq"] == 1

    safe = await client.post("/api/messages", headers={"Authorization": "Bearer h-token"}, json={
        "recipient": "codex", "message_type": "note", "body": {"subject": "short safe subject"},
    })
    assert safe.status_code == 201
    full = await client.get("/api/messages/wait?after=1&timeout=0&fields=full",
                            headers={"Authorization": "Bearer c-token"})
    assert full.status_code == 200
    item = full.json()["messages"][0]
    assert item["seq"] == 2 and item["body"]["subject"] == "short safe subject"
    assert "BODY-DO-NOT-RETURN" not in full.text
    safe_pointer = await client.get("/api/messages/wait?after=1&timeout=0&fields=pointer",
                                    headers={"Authorization": "Bearer c-token"})
    assert safe_pointer.json()["messages"][0]["subject"] == "short safe subject"


@pytest.mark.asyncio
async def test_cursor_rest_validates_auth_args_and_future_head(cursor_client):
    _, client = cursor_client
    assert (await client.get("/api/messages/wait?after=0&timeout=0")).status_code == 401
    assert (await client.get("/api/messages/wait?after=-1", headers={"Authorization": "Bearer c-token"})).status_code == 422
    assert (await client.get("/api/messages/wait?after=0&since_now=true",
                             headers={"Authorization": "Bearer c-token"})).status_code == 422
    assert (await client.get("/api/messages/wait?after=0&limit=101",
                             headers={"Authorization": "Bearer c-token"})).status_code == 422
    assert (await client.get("/api/messages/wait?after=0&fields=verbose",
                             headers={"Authorization": "Bearer c-token"})).status_code == 422
    assert (await client.get("/api/messages/wait?since_now=true&timeout=61",
                             headers={"Authorization": "Bearer c-token"})).status_code == 422
    assert (await client.get("/api/messages/wait?after=1&timeout=0",
                             headers={"Authorization": "Bearer c-token"})).status_code == 400
    wrong = await client.get("/api/messages/wait?after=0&timeout=0",
                             headers={"Authorization": "Bearer bad-token"})
    assert wrong.status_code == 401


@pytest.mark.asyncio
async def test_since_now_uses_one_head_snapshot_and_observes_concurrent_commit(cursor_client):
    app, client = cursor_client
    headers_h = {"Authorization": "Bearer h-token"}
    headers_c = {"Authorization": "Bearer c-token"}
    old = await client.post("/api/messages", headers=headers_h, json={
        "recipient": "codex", "message_type": "already-here", "body": {},
    })
    waiting = asyncio.create_task(client.get(
        "/api/messages/wait?since_now=true&timeout=2&fields=pointer",
        headers=headers_c,
    ))
    await asyncio.sleep(0.15)
    await asyncio.to_thread(_insert_once_in_process, str(app.state.repository.path))
    response = await asyncio.wait_for(waiting, timeout=2)
    assert response.status_code == 200
    assert [item["message_type"] for item in response.json()["messages"]] == ["arrived-during-wait"]
    assert response.json()["messages"][0]["id"] != old.json()["id"]
    assert response.json()["next_cursor"] == 2
    assert app.state.repository.get(old.json()["id"]).status == "pending"


@pytest.mark.asyncio
async def test_cursor_timeout_preserves_cursor_and_legacy_wait_shape(cursor_client):
    _, client = cursor_client
    headers = {"Authorization": "Bearer c-token"}
    timed = await client.get("/api/messages/wait?after=0&timeout=0&fields=pointer", headers=headers)
    assert timed.status_code == 200
    assert timed.json() == {"messages": [], "next_cursor": 0, "oldest_available_seq": None}
    legacy = await client.get("/api/messages/wait?timeout=0", headers=headers)
    assert legacy.status_code == 200 and legacy.json() == {"messages": []}


def test_mcp_exposes_async_cursor_wait_without_removing_existing_tools(tmp_path):
    service = _service(tmp_path / "m.sqlite3", tmp_path / "audit.jsonl")
    tools = build_mcp_tools(service)
    names = {tool.name for tool in tools}
    assert {"feedback_submit", "feedback_get", "feedback_list", "feedback_update", "wait_for_messages"} <= names
    assert len(names) == 21
    assert "wait_for_messages" in names
    tool = next(tool for tool in tools if tool.name == "wait_for_messages")
    assert set(tool.parameters["properties"]) >= {"after", "since_now", "fields", "limit", "timeout"}
    assert asyncio.iscoroutinefunction(tool.fn)


@pytest.mark.asyncio
async def test_mcp_cursor_wait_authenticates_and_returns_only_pointer_fields(tmp_path):
    service = _service(tmp_path / "m.sqlite3", tmp_path / "audit.jsonl")
    message = service.repository.insert(sender="hermes", recipient="codex", message_type="task",
                                        body={"subject": "private c-token", "body": "must stay private"})
    tool = next(tool for tool in build_mcp_tools(service) if tool.name == "wait_for_messages")

    def context(token):
        request = Request({
            "type": "http", "method": "POST", "scheme": "http", "server": ("test", 80),
            "client": ("127.0.0.1", 1234), "root_path": "", "path": "/mcp", "raw_path": b"/mcp",
            "query_string": b"", "headers": [(b"authorization", f"Bearer {token}".encode())], "state": {},
        })
        return SimpleNamespace(request_context=SimpleNamespace(request=request))

    pointer = await tool.fn(after=0, timeout=0, fields="pointer", ctx=context("c-token"))
    assert pointer["messages"][0]["id"] == message.id
    assert pointer["messages"][0]["subject"] is None
    assert "body" not in pointer["messages"][0]
    assert service.repository.get(message.id).status == "pending"
    with pytest.raises(HTTPException) as error:
        await tool.fn(after=0, timeout=0, ctx=context("wrong-token"))
    assert error.value.status_code == 401
    with pytest.raises(ValueError, match="cannot be combined"):
        await tool.fn(after=0, since_now=True, timeout=0, ctx=context("c-token"))
    with pytest.raises(ValueError, match="limit must be"):
        await tool.fn(after=0, limit=101, timeout=0, ctx=context("c-token"))
    with pytest.raises(ValueError, match="timeout must be"):
        await tool.fn(after=0, timeout=61, ctx=context("c-token"))


def build_mcp_tools(service):
    from agent_message_bus.mcp_server import build_mcp
    return build_mcp(service)._tool_manager.list_tools()
