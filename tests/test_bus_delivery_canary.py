"""Delivery canary: every agent-to-agent route raises exactly the recipient's inbox notification.

Scope, stated plainly: this proves the bus server side only, in-process on
loopback with synthetic tokens. A message sent over MCP or REST commits, the
recipient's subscribed MCP session receives its own ``notifications/resources/updated``
within a bound, nobody else is notified, and the recipient reads the message back
with the routing fields (``body.topic``, ``correlation_id``) that host-side wake
filters depend on.

It does NOT prove that a chat wakes: Claude's bus_wake listener, the Codex
native_push connector and the Hermes worker run on the agent hosts, outside this
repository, and their routing (topic/correlation filters, restarts) is untested
here. It is not a production probe and never polls anything recurring.
"""
from __future__ import annotations

import asyncio
import itertools
import time

import httpx
import pytest
from pydantic import AnyUrl

from agent_message_bus.database import MessageRepository
from tests.test_mcp_push import BASE_URI, TOKENS, live_app, loopback, mcp_client, resource_json

AGENTS = {"hermes": TOKENS.hermes_token, "codex": TOKENS.codex_token, "claude": TOKENS.claude_token}
# One absolute budget per send, covering delivery and the silence/duplicate window.
SEND_BUDGET_SECONDS = 2.0
# After the first notification, keep listening this long (inside the budget) for late events.
SETTLE_SECONDS = 0.3
# Invalidations are coalesced but may legitimately repeat; more than this per commit is a regression.
MAX_RECIPIENT_EVENTS = 2


@pytest.fixture(autouse=True)
def allow_ephemeral_loopback_host(monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")


async def rest_send(base: str, token: str, payload: dict) -> dict:
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as client:
        response = await client.post(base.removesuffix("/mcp") + "/api/messages", json=payload)
        assert response.status_code == 201, response.text
        return response.json()


def drain(queues: dict[str, asyncio.Queue]) -> None:
    for queue in queues.values():
        while not queue.empty():
            queue.get_nowait()


async def collect(queues: dict[str, asyncio.Queue], recipient: str, deadline: float) -> dict[str, list]:
    """Wait for the recipient's first event before the deadline, then gather every queue until settled."""
    seen = {name: [] for name in queues}
    first = await asyncio.wait_for(queues[recipient].get(), max(0.0, deadline - time.monotonic()))
    seen[recipient].append(first)
    settle_until = min(deadline, time.monotonic() + SETTLE_SECONDS)
    while (remaining := settle_until - time.monotonic()) > 0:
        getters = {asyncio.ensure_future(queue.get()): name for name, queue in queues.items()}
        done, pending = await asyncio.wait(getters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            seen[getters[task]].append(task.result())
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["mcp", "rest"])
async def test_every_route_notifies_only_its_recipient_within_budget(tmp_path, transport):
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
                        drain(events)
                        topic = f"canary-{sender}-to-{recipient}"
                        payload = {"recipient": recipient, "message_type": "canary",
                                   "body": {"topic": topic, "from_role": sender}, "correlation_id": topic}
                        started = time.monotonic()
                        deadline = started + SEND_BUDGET_SECONDS
                        if transport == "mcp":
                            result = await sessions[sender].call_tool("send_message", payload)
                            assert not result.isError
                        else:
                            await rest_send(url, AGENTS[sender], payload)
                        seen = await collect(events, recipient, deadline)
                        latencies[(sender, recipient)] = time.monotonic() - started
                        assert 1 <= len(seen[recipient]) <= MAX_RECIPIENT_EVENTS, (sender, recipient, len(seen[recipient]))
                        assert all(str(item.root.params.uri) == BASE_URI.format(recipient) for item in seen[recipient])
                        for other in set(AGENTS) - {recipient}:
                            assert seen[other] == [], f"{other} notified for a message to {recipient}"
                        assert time.monotonic() <= deadline + 0.05
                        page = resource_json(await sessions[recipient].read_resource(
                            AnyUrl(BASE_URI.format(recipient) + f"?after={cursors[recipient]}&limit=10")))
                        assert [item["sender"] for item in page["messages"]] == [sender]
                        message = page["messages"][0]
                        assert message["body"] == {"topic": topic, "from_role": sender}
                        assert message["correlation_id"] == topic
                        cursors[recipient] = page["next_cursor"]
    assert len(latencies) == 6


IDEMPOTENT_ACK = hasattr(MessageRepository, "acknowledge_with_outcome")


@pytest.mark.asyncio
async def test_ack_retried_from_a_new_session_after_its_response_was_ignored(tmp_path):
    """The recipient acks, discards the result, opens a new MCP session and acks again.

    This models a client that cannot tell whether its ack landed. It is not a dropped
    wire response and not a server restart: the server keeps running throughout.
    Before idempotent finish (PR #142) the retry is refused with an exact error; after
    it, the retry returns the committed message. Either way, nothing else may change.
    """
    app = live_app(tmp_path)
    async with loopback(app) as url:
        async with mcp_client(url, AGENTS["claude"]) as (claude, _, _, _):
            sent = await claude.call_tool("send_message", {
                "recipient": "codex", "message_type": "canary", "body": {"topic": "canary-retry"}})
            assert not sent.isError
        async with mcp_client(url, AGENTS["codex"]) as (codex, _, _, _):
            page = resource_json(await codex.read_resource(AnyUrl(BASE_URI.format("codex") + "?after=0&limit=10")))
            pending = [item for item in page["messages"] if item["body"].get("topic") == "canary-retry"]
            assert len(pending) == 1
            message_id = pending[0]["id"]
            assert not (await codex.call_tool("claim_message", {"message_id": message_id})).isError
            first = await codex.call_tool("ack_message", {"message_id": message_id})
            assert not first.isError
        async with mcp_client(url, AGENTS["codex"]) as (codex, _, _, _):
            retried = await codex.call_tool("ack_message", {"message_id": message_id})
            if IDEMPOTENT_ACK:
                assert not retried.isError
                assert retried.structuredContent["status"] == "acknowledged"
            else:
                assert retried.isError
                assert "message is not claimed by this agent" in retried.content[0].text
    stored = app.state.repository.get(message_id)
    assert stored.status == "acknowledged" and stored.claimed_by == "codex"
