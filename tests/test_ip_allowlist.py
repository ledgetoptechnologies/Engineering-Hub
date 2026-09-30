import httpx
import pytest

from agent_message_bus.app import create_app
from agent_message_bus.auth import AuthSettings


@pytest.mark.asyncio
async def test_source_ip_allowlist_is_enforced(tmp_path):
    app = create_app(db_path=tmp_path / "m.sqlite3", audit_path=tmp_path / "audit.jsonl",
                     auth=AuthSettings("h", "c", "a", frozenset({"192.0.2.10"})))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("198.51.100.4", 1)),
                                 base_url="http://test") as client:
        response = await client.get("/api/messages", headers={"Authorization": "Bearer h"})
    assert response.status_code == 403
