"""Delivery canary: every agent-to-agent route raises exactly the recipient's inbox notification.

Scope, stated plainly: this proves the bus server side only. A message sent over
MCP or REST commits, the recipient's subscribed MCP session receives one
``notifications/resources/updated`` for its own inbox within a bound, nobody else
is notified, and the recipient can read the message back with the routing fields
(``body.topic``, ``correlation_id``) that host-side wake filters depend on.

It does NOT prove that a chat wakes: Claude's bus_wake listener, the Codex
native_push connector and the Hermes worker run on the agent hosts, outside this
repository, and their routing (topic/correlation filters, restarts) is untested
here. It also runs only in-process on loopback with synthetic tokens; it is not a
production probe and never polls anything recurring.
"""
from __future__ import annotations

import asyncio
import itertools
import time

import httpx
import pytest
from pydantic import AnyUrl

from tests.test_mcp_push import BASE_URI, TOKENS, live_app, loopback, mcp_client, no_notification, resource_json

AGENTS = {"hermes": TOKENS.hermes_token, "codex": TOKENS.codex_token, "claude": TOKENS.claude_token}
# Generous for CI; the canary records the observed latency so regressions are visible.
DELIVERY_BOUND_SECONDS = 2.0


@pytest.fixture(autouse=True)
def allow_ephemeral_loopback_host(monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")


async def rest_send(base: str, token: str, payload: dict) -> dict:
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as client:
        response = await client.post(base.removesuffix("/mcp") + "/api/messages", json=payload)
        assert response.status_code == 201, response.text
        return response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["mcp", "rest"])
async def test_every_route_notifies_only_its_recipient_within_bound(tmp_path, transport):
    app = live_app(tmp_path)
    latencies = {}
    async with loopback(app) as url:
        async with mcp_client(url, AGENTS["hermes"]) as (hermes, hermes_events, _, _):
            async with mcp_client(url, AGENTS["codex"]) as (codex, codex_events, _, _):
                async with mcp_client(url, AGENTS["claude"]) as (claude, claude_events, _, _):
                    sessions = {"hermes": hermes, "codex": codex, "claude": claude}
                    events = {"hermes": hermes_events, "codex": codex_events, "claude": claude_events}
                    for name, session in sessions.items():
                        await session.subscribe_resource(AnyUrl(BASE_URI.format(name)))
                    cursors = {name: 0 for name in AGENTS}
                    for sender, recipient in itertools.permutations(AGENTS, 2):
                        topic = f"canary-{sender}-to-{recipient}"
                        payload = {"recipient": recipient, "message_type": "canary",
                                   "body": {"topic": topic, "from_role": sender}, "correlation_id": topic}
                        started = time.monotonic()
                        if transport == "mcp":
                            result = await sessions[sender].call_tool("send_message", payload)
                            assert not result.isError
                        else:
                            await rest_send(url, AGENTS[sender], payload)
                        notification = await asyncio.wait_for(events[recipient].get(), DELIVERY_BOUND_SECONDS)
                        latencies[(sender, recipient)] = time.monotonic() - started
                        assert str(notification.root.params.uri) == BASE_URI.format(recipient)
                        for other in set(AGENTS) - {recipient}:
                            assert await no_notification(events[other], 0.05), f"{other} notified for {recipient}"
                        page = resource_json(await sessions[recipient].read_resource(
                            AnyUrl(BASE_URI.format(recipient) + f"?after={cursors[recipient]}&limit=10")))
                        assert [item["sender"] for item in page["messages"]] == [sender]
                        message = page["messages"][0]
                        assert message["body"] == {"topic": topic, "from_role": sender}
                        assert message["correlation_id"] == topic
                        cursors[recipient] = page["next_cursor"]
    assert len(latencies) == 6
    assert max(latencies.values()) < DELIVERY_BOUND_SECONDS


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="retried ack after a lost response fails until PR #142 (idempotent ack) merges")
async def test_canary_survives_a_lost_ack_response_and_a_reconnect(tmp_path):
    """The recipient reconnects (restart) and repeats claim/ack handling without losing the message."""
    app = live_app(tmp_path)
    async with loopback(app) as url:
        async with mcp_client(url, AGENTS["claude"]) as (claude, _, _, _):
            sent = await claude.call_tool("send_message", {
                "recipient": "codex", "message_type": "canary", "body": {"topic": "canary-restart"}})
            assert not sent.isError
        async with mcp_client(url, AGENTS["codex"]) as (codex, events, _, _):
            await codex.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
            page = resource_json(await codex.read_resource(AnyUrl(BASE_URI.format("codex") + "?after=0&limit=10")))
            pending = [item for item in page["messages"] if item["body"].get("topic") == "canary-restart"]
            assert len(pending) == 1
            claimed = await codex.call_tool("claim_message", {"message_id": pending[0]["id"]})
            assert not claimed.isError
            first = await codex.call_tool("ack_message", {"message_id": pending[0]["id"]})
        # The ack response is "lost" with the session; a new session retries it.
        async with mcp_client(url, AGENTS["codex"]) as (codex, _, _, _):
            retried = await codex.call_tool("ack_message", {"message_id": pending[0]["id"]})
            assert not first.isError
            assert not retried.isError, "a retried ack after reconnect must return the committed result"
