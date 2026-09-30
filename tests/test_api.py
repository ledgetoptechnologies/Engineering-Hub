import httpx
import pytest

from agent_message_bus.app import create_app
from agent_message_bus.auth import AuthSettings


@pytest.fixture
def app(tmp_path):
    return create_app(db_path=tmp_path / "m.sqlite3", audit_path=tmp_path / "audit.jsonl",
                      auth=AuthSettings("h-token", "c-token", "a-token"))


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
