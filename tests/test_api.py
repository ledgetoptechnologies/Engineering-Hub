import asyncio
import json
import logging
from datetime import timedelta

import httpx
import pytest

from agent_message_bus.app import create_app
from agent_message_bus.auth import AuthSettings
from agent_message_bus.database import iso, now_utc


@pytest.fixture
def app(tmp_path):
    return create_app(db_path=tmp_path / "m.sqlite3", audit_path=tmp_path / "audit.jsonl",
                      auth=AuthSettings("h-token", "c-token", "a-token", claude_token="claude-token"))


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as value:
        yield value


@pytest.mark.asyncio
async def test_health_does_not_expose_secrets(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "h-token" not in response.text and "body" not in response.text


@pytest.mark.asyncio
async def test_request_logging_and_lifecycle_audit_are_structured_and_redacted(client, app, caplog):
    caplog.set_level(logging.INFO, logger="agent_message_bus.http")
    caplog.set_level(logging.INFO, logger="agent_message_bus.audit")
    response = await client.post(
        "/api/messages",
        headers={"Authorization": "Bearer h-token", "X-Request-ID": "request-123",
                 "X-Correlation-ID": "correlation-456"},
        json={"recipient": "codex", "message_type": "task", "correlation_id": "h-token",
              "body": {"secret": "body-secret-do-not-log"}},
    )

    assert response.status_code == 201
    assert response.headers["X-Request-ID"] == "request-123"
    assert response.headers["X-Correlation-ID"] == "correlation-456"
    records = [json.loads(record.message) for record in caplog.records if record.name.startswith("agent_message_bus.")]
    request_record = next(record for record in records if record["event"] == "http.request")
    assert request_record["request_id"] == "request-123"
    assert request_record["correlation_id"] == "correlation-456"
    assert request_record["method"] == "POST" and request_record["path"] == "/api/messages"
    assert request_record["status_code"] == 201 and request_record["latency_ms"] >= 0
    assert request_record["agent"] == "hermes"

    audit = json.loads(app.state.audit.path.read_text().splitlines()[0])
    assert audit["request_id"] == "request-123"
    assert audit["correlation_id"] == "correlation-456"
    collision = await client.get(
        "/api/messages", headers={"Authorization": "Bearer h-token", "X-Request-ID": "h-token",
                                  "X-Correlation-ID": "h-token"},
    )
    assert collision.status_code == 200
    assert collision.headers["X-Request-ID"] != "h-token"
    assert collision.headers["X-Correlation-ID"] != "h-token"
    logged = caplog.text + app.state.audit.path.read_text()
    assert "h-token" not in logged
    assert "body-secret-do-not-log" not in logged


@pytest.mark.asyncio
async def test_auth_send_receive_claim_ack_and_isolation(client):
    headers_h = {"Authorization": "Bearer h-token"}
    headers_c = {"Authorization": "Bearer c-token"}
    response = await client.post("/api/messages", headers=headers_h,
                           json={"recipient": "codex", "message_type": "task", "body": {"secret": "x"}})
    assert response.status_code == 201
    message_id = response.json()["id"]
    assert (await client.get("/api/messages", headers=headers_h)).json()["messages"] == []
    assert (await client.get("/api/messages", headers=headers_c)).json()["messages"][0]["id"] == message_id
    assert (await client.get(f"/api/messages/{message_id}", headers=headers_h)).status_code == 404
    assert (await client.post(f"/api/messages/{message_id}/claim", headers=headers_h)).status_code == 409
    claimed = await client.post(f"/api/messages/{message_id}/claim", headers=headers_c)
    assert claimed.status_code == 200 and claimed.json()["status"] == "claimed"
    assert (await client.post(f"/api/messages/{message_id}/ack", headers=headers_h)).status_code == 409
    done = await client.post(f"/api/messages/{message_id}/ack", headers=headers_c)
    assert done.status_code == 200 and done.json()["status"] == "acknowledged"


@pytest.mark.asyncio
async def test_invalid_auth_and_admin_cleanup(client):
    assert (await client.get("/api/messages")).status_code == 401
    assert (await client.get("/api/messages", headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (await client.post("/api/admin/cleanup", headers={"Authorization": "Bearer h-token"})).status_code == 401
    assert (await client.post("/api/admin/cleanup", headers={"Authorization": "Bearer a-token"})).status_code == 200


@pytest.mark.asyncio
async def test_limit_is_bounded_and_spoofed_sender_is_ignored(client):
    response = await client.post("/api/messages", headers={"Authorization": "Bearer h-token"},
                           json={"sender": "codex", "recipient": "codex", "message_type": "x", "body": {}})
    assert response.status_code == 201
    assert response.json()["sender"] == "hermes"


@pytest.mark.asyncio
async def test_authenticated_wait_times_out_and_returns_only_recipient_pending_messages(client):
    assert (await client.get("/api/messages/wait?timeout=0")).status_code == 401
    assert (await client.get("/api/messages/wait?timeout=61",
                             headers={"Authorization": "Bearer c-token"})).status_code == 422
    empty = await client.get("/api/messages/wait?timeout=0",
                             headers={"Authorization": "Bearer c-token"})
    assert empty.status_code == 200 and empty.json() == {"messages": []}

    waiter = asyncio.create_task(client.get(
        "/api/messages/wait?timeout=5", headers={"Authorization": "Bearer c-token"},
    ))
    await asyncio.sleep(0.05)
    probe = await asyncio.wait_for(client.get("/health"), timeout=0.5)
    assert probe.status_code == 200
    sent_to_codex = await client.post(
        "/api/messages", headers={"Authorization": "Bearer h-token"},
        json={"recipient": "codex", "message_type": "task", "body": {"work": 1}},
    )
    await client.post(
        "/api/messages", headers={"Authorization": "Bearer c-token"},
        json={"recipient": "hermes", "message_type": "reply", "body": {"work": 2}},
    )
    received = await asyncio.wait_for(waiter, timeout=2)
    assert sent_to_codex.status_code == 201
    assert received.status_code == 200
    assert [message["id"] for message in received.json()["messages"]] == [sent_to_codex.json()["id"]]
    assert received.json()["messages"][0]["recipient"] == "codex"
    assert received.json()["messages"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_claude_identity_is_bound_to_its_authenticated_inbox(client):
    headers = {"Authorization": "Bearer claude-token"}
    inbox = await client.get("/api/messages", headers=headers)
    assert inbox.status_code == 200 and inbox.json() == {"messages": []}
    assert (await client.get("/api/messages?recipient=codex", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_claude_can_send_and_correlate_a_direct_reply(client):
    sent = await client.post("/api/messages", headers={"Authorization": "Bearer h-token"}, json={
        "recipient": "claude", "message_type": "task", "body": {"work": "review"},
        "correlation_id": "legacy-trace-not-a-message-id",
    })
    assert sent.status_code == 201
    root = sent.json()
    assert root["sender"] == "hermes" and root["recipient"] == "claude"
    assert root["hop_count"] == 0 and root["in_reply_to"] is None
    assert root["correlation_id"] == "legacy-trace-not-a-message-id"

    reply = await client.post("/api/messages", headers={"Authorization": "Bearer claude-token"}, json={
        "recipient": "hermes", "message_type": "reply", "body": {"result": "done"},
        "in_reply_to": root["id"], "correlation_id": "unrelated-legacy-correlation",
    })
    assert reply.status_code == 201
    assert reply.json()["in_reply_to"] == root["id"]
    assert reply.json()["hop_count"] == 1


@pytest.mark.asyncio
async def test_rest_correlated_hops_enforce_server_limit_and_reject_invalid_parent(client):
    tokens = {"hermes": "h-token", "codex": "c-token", "claude": "claude-token"}
    headers = lambda identity: {"Authorization": f"Bearer {tokens[identity]}"}
    created = await client.post("/api/messages", headers=headers("hermes"), json={
        "recipient": "claude", "message_type": "task", "body": {}, "correlation_id": "thread",
    })
    assert created.status_code == 201
    parent = created.json()
    for expected_hop in range(1, 7):
        response = await client.post("/api/messages", headers=headers(parent["recipient"]), json={
            "recipient": parent["sender"], "message_type": "reply", "body": {},
            "in_reply_to": parent["id"], "correlation_id": "thread",
        })
        assert response.status_code == 201
        parent = response.json()
        assert parent["hop_count"] == expected_hop

    beyond_limit = await client.post("/api/messages", headers=headers(parent["recipient"]), json={
        "recipient": parent["sender"], "message_type": "reply", "body": {},
        "in_reply_to": parent["id"], "correlation_id": "different-correlation-id",
    })
    assert beyond_limit.status_code == 400
    assert "hop limit" in beyond_limit.json()["detail"]
    invalid_parent = await client.post("/api/messages", headers=headers("claude"), json={
        "recipient": "hermes", "message_type": "reply", "body": {},
        "in_reply_to": "missing-parent", "correlation_id": "otherwise-valid-trace",
    })
    assert invalid_parent.status_code == 400
    assert "parent" in invalid_parent.json()["detail"]


@pytest.mark.asyncio
async def test_wait_actionable_filter_is_applied_before_limit(client, app):
    repository = app.state.repository
    for index in range(55):
        repository.insert(sender="hermes", recipient="codex", message_type="event", body={"n": index})
    task = repository.insert(sender="hermes", recipient="codex", message_type="TaSk", body={})
    received = await client.get("/api/messages/wait?timeout=0&actionable_only=true",
                               headers={"Authorization": "Bearer c-token"})
    assert received.status_code == 200
    assert [message["id"] for message in received.json()["messages"]] == [task.id]


@pytest.mark.asyncio
async def test_wait_expires_claims_before_listing_pending_messages(client, app):
    repository = app.state.repository
    active = repository.insert(sender="hermes", recipient="codex", message_type="task", body={})
    expired = repository.insert(sender="hermes", recipient="codex", message_type="request", body={})
    repository.claim(active.id, "codex")
    repository.claim(expired.id, "codex")
    with repository.connect() as conn:
        conn.execute("UPDATE messages SET claim_expires_at=? WHERE id=?",
                     (iso(now_utc() - timedelta(seconds=1)), expired.id))
    received = await client.get("/api/messages/wait?timeout=0&actionable_only=true",
                               headers={"Authorization": "Bearer c-token"})
    assert received.status_code == 200
    assert [message["id"] for message in received.json()["messages"]] == [expired.id]
    assert received.json()["messages"][0]["status"] == "pending"
    assert repository.get(active.id).status == "claimed"


@pytest.mark.asyncio
async def test_reject_requires_claim_and_records_reason(client):
    headers_h = {"Authorization": "Bearer h-token"}
    headers_c = {"Authorization": "Bearer c-token"}
    sent = await client.post("/api/messages", headers=headers_h,
                             json={"recipient": "codex", "message_type": "task", "body": {}})
    message_id = sent.json()["id"]
    assert (await client.post(f"/api/messages/{message_id}/reject", headers=headers_c,
                              json={"reason": "not applicable"})).status_code == 409
    assert (await client.post(f"/api/messages/{message_id}/claim", headers=headers_c)).status_code == 200
    rejected = await client.post(f"/api/messages/{message_id}/reject", headers=headers_c,
                                 json={"reason": "not applicable"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
