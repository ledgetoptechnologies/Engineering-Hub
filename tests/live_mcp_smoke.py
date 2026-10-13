#!/usr/bin/env python3
"""Live MCP/REST smoke test; run against a disposable local service."""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def mcp_call(url: str, token: str, name: str, arguments: dict, *, allow_error: bool = False) -> Any:
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                expected = ["send_message", "receive_messages", "claim_message", "ack_message", "reject_message", "get_message",
                            "knowledge_put", "knowledge_get", "knowledge_list", "knowledge_search"]
                if [tool.name for tool in tools.tools] != expected:
                    raise RuntimeError(f"unexpected MCP tools: {[tool.name for tool in tools.tools]}")
                result = await session.call_tool(name, arguments)
                if getattr(result, "isError", False):
                    detail = getattr(result.content[0], "text", "unknown tool error") if result.content else "unknown tool error"
                    if allow_error:
                        return {"error": detail}
                    raise RuntimeError(f"MCP tool failed: {name}: {detail}")
                return json.loads(result.content[0].text)


async def main(url: str) -> None:
    base = url.removesuffix("/mcp")
    import os
    hermes = os.environ.get("HERMES_TOKEN", "")
    codex = os.environ.get("CODEX_TOKEN", "")
    claude = os.environ.get("CLAUDE_TOKEN", "")
    admin = os.environ.get("ADMIN_TOKEN", "")
    if not all((hermes, codex, claude, admin)):
        raise RuntimeError("set HERMES_TOKEN, CODEX_TOKEN, CLAUDE_TOKEN, and ADMIN_TOKEN in the environment")
    marker = str(uuid.uuid4())
    sent = await mcp_call(url, hermes, "send_message", {"recipient": "codex", "message_type": "smoke", "body": {"marker": marker}})
    message_id = sent["id"]
    received = await mcp_call(url, codex, "receive_messages", {"limit": 100})
    message = next(item for item in received["messages"] if item["id"] == message_id)
    assert message["recipient"] == "codex" and message["body"]["marker"] == marker
    async with httpx.AsyncClient() as client:
        codex_headers = {"Authorization": f"Bearer {codex}"}
        ready = await client.get(f"{base}/api/messages/wait?timeout=0", headers=codex_headers)
        assert ready.status_code == 200
        assert any(item["id"] == message_id for item in ready.json()["messages"])
        hermes_wait = await client.get(
            f"{base}/api/messages/wait?timeout=0", headers={"Authorization": f"Bearer {hermes}"},
        )
        assert hermes_wait.status_code == 200
        assert all(item["id"] != message_id for item in hermes_wait.json()["messages"])
        assert (await client.get(f"{base}/api/messages/wait?timeout=61", headers=codex_headers)).status_code == 422
    await mcp_call(url, codex, "claim_message", {"message_id": message_id})
    acknowledged = await mcp_call(url, codex, "ack_message", {"message_id": message_id})
    assert acknowledged["status"] == "acknowledged"

    path = f"smoke/{marker}.md"
    created = await mcp_call(url, hermes, "knowledge_put", {
        "project": "live-smoke", "path": path, "title": "MCP smoke", "content": f"shared marker {marker}",
        "expected_version": 0,
    })
    assert created["version"] == 1 and created["created_by"] == "hermes"
    updated = await mcp_call(url, codex, "knowledge_put", {
        "project": "live-smoke", "path": path, "title": "MCP smoke", "content": f"updated marker {marker}",
        "expected_version": 1,
    })
    assert updated["version"] == 2 and updated["updated_by"] == "codex"
    read_by_claude = await mcp_call(url, claude, "knowledge_get", {"project": "live-smoke", "path": path})
    assert read_by_claude["content"] == f"updated marker {marker}"
    assert read_by_claude["sha256"] == updated["sha256"]
    found = await mcp_call(url, claude, "knowledge_search", {"query": marker, "project": "live-smoke"})
    assert any(item["path"] == path for item in found["results"])
    listed = await mcp_call(url, claude, "knowledge_list", {"project": "live-smoke", "path_prefix": "smoke"})
    assert any(item["path"] == path and "content" not in item for item in listed["documents"])
    claude_update = await mcp_call(url, claude, "knowledge_put", {
        "project": "live-smoke", "path": path, "title": "MCP smoke", "content": f"final marker {marker}",
        "expected_version": 2,
    })
    assert claude_update["version"] == 3 and claude_update["updated_by"] == "claude"
    conflict = await mcp_call(url, codex, "knowledge_put", {
        "project": "live-smoke", "path": path, "title": "Stale write", "content": "must not replace version 3",
        "expected_version": 2,
    }, allow_error=True)
    assert "version conflict" in conflict["error"]
    assert (await mcp_call(url, hermes, "knowledge_get", {"project": "live-smoke", "path": path})) == claude_update
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{base}/api/messages/{message_id}", headers={"Authorization": f"Bearer {codex}"})
        assert response.status_code == 200 and response.json()["status"] == "acknowledged"
        assert (await client.get(f"{base}/api/messages", headers={"Authorization": "Bearer invalid"})).status_code == 401
        admin_mcp = await client.post(url, headers={"Authorization": f"Bearer {admin}"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        })
        assert admin_mcp.status_code == 401
    print("MCP messaging and shared-knowledge smoke test passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.url))
