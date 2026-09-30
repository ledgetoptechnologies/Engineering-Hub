#!/usr/bin/env python3
"""Live MCP/REST smoke test; run against a disposable local service."""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def mcp_call(url: str, token: str, name: str, arguments: dict):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                expected = ["send_message", "receive_messages", "claim_message", "ack_message", "reject_message", "get_message"]
                if [tool.name for tool in tools.tools] != expected:
                    raise RuntimeError(f"unexpected MCP tools: {[tool.name for tool in tools.tools]}")
                result = await session.call_tool(name, arguments)
                if getattr(result, "isError", False):
                    raise RuntimeError(f"MCP tool failed: {name}")
                return json.loads(result.content[0].text)


async def main(url: str) -> None:
    base = url.removesuffix("/mcp")
    hermes = "replace-with-a-long-random-token"
    codex = "replace-with-a-different-long-random-token"
    # The smoke script accepts tokens from environment via its CLI in deployment docs;
    # defaults make accidental use against a real service fail rather than guess secrets.
    import os
    hermes = os.environ.get("HERMES_TOKEN", hermes)
    codex = os.environ.get("CODEX_TOKEN", codex)
    marker = str(uuid.uuid4())
    sent = await mcp_call(url, hermes, "send_message", {"recipient": "codex", "message_type": "smoke", "body": {"marker": marker}})
    message_id = sent["id"]
    received = await mcp_call(url, codex, "receive_messages", {"limit": 100})
    message = next(item for item in received["messages"] if item["id"] == message_id)
    assert message["recipient"] == "codex" and message["body"]["marker"] == marker
    await mcp_call(url, codex, "claim_message", {"message_id": message_id})
    acknowledged = await mcp_call(url, codex, "ack_message", {"message_id": message_id})
    assert acknowledged["status"] == "acknowledged"
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{base}/api/messages/{message_id}", headers={"Authorization": f"Bearer {codex}"})
        assert response.status_code == 200 and response.json()["status"] == "acknowledged"
        assert (await client.get(f"{base}/api/messages", headers={"Authorization": "Bearer invalid"})).status_code == 401
    print("MCP smoke test passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.url))
