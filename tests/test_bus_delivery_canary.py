"""Delivery canary: every agent-to-agent route notifies its recipient and nobody else.

Scope, stated plainly: this proves the bus server side only, in-process on
loopback with synthetic tokens. A message sent over MCP or REST commits, the
recipient's subscribed MCP session receives between one and MAX_RECIPIENT_EVENTS
``notifications/resources/updated`` for its own inbox (invalidations are coalesced
but may legitimately repeat), nobody else is notified, and the recipient reads the
message back with the routing fields (``body.topic``, ``correlation_id``) that
host-side wake filters depend on.

Every check holds only within the observed windows (SEND_BUDGET_SECONDS per
message plus a final SETTLE_SECONDS quiet period); it is not an exactly-once
guarantee. It does NOT prove that a chat wakes: Claude's bus_wake listener, the Codex
native_push connector and the Hermes worker run on the agent hosts, outside this
repository, and their routing (topic/correlation filters, restarts) is untested
here. It is not a production probe and never polls anything recurring.

Depends on idempotent finish (PR #142), on which this branch is stacked.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from types import SimpleNamespace

import httpx
import pytest
from pydantic import AnyUrl

from tests.test_mcp_push import BASE_URI, TOKENS, live_app, loopback, mcp_client, resource_json

AGENTS = {"hermes": TOKENS.hermes_token, "codex": TOKENS.codex_token, "claude": TOKENS.claude_token}
# One absolute budget per message: the send, the recipient's first event and the settle window.
SEND_BUDGET_SECONDS = 2.0
# After the first notification, keep listening on every inbox this long (inside the budget).
SETTLE_SECONDS = 0.3
# Coalesced invalidations may repeat; more than this per commit is a regression.
MAX_RECIPIENT_EVENTS = 2


@pytest.fixture(autouse=True)
def allow_ephemeral_loopback_host(monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")


async def rest_send(base: str, token: str, payload: dict) -> dict:
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as client:
        response = await client.post(base.removesuffix("/mcp") + "/api/messages", json=payload)
        assert response.status_code == 201, response.text
        return response.json()


def uri_of(event) -> str:
    return str(event.root.params.uri)


def take_leftovers(queues: dict[str, asyncio.Queue]) -> dict[str, list]:
    """Everything that arrived after the previous settle window, attributed by inbox, never discarded."""
    leftovers = {name: [] for name in queues}
    for name, queue in queues.items():
        while not queue.empty():
            leftovers[name].append(queue.get_nowait())
    return leftovers


async def collect(queues: dict[str, asyncio.Queue], recipient: str, settle: float = SETTLE_SECONDS) -> dict[str, list]:
    """Wait for the recipient's first event, then gather every inbox until settled.

    The caller bounds the whole call with its absolute deadline (asyncio.timeout).
    """
    seen = {name: [] for name in queues}
    seen[recipient].append(await queues[recipient].get())
    settle_until = time.monotonic() + settle
    while (remaining := settle_until - time.monotonic()) > 0:
        getters = {asyncio.ensure_future(queue.get()): name for name, queue in queues.items()}
        done, pending = await asyncio.wait(getters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            seen[getters[task]].append(task.result())
    return seen


def assert_only_recipient(seen: dict[str, list], recipient: str, label) -> None:
    assert 1 <= len(seen[recipient]) <= MAX_RECIPIENT_EVENTS, (label, len(seen[recipient]))
    assert all(uri_of(event) == BASE_URI.format(recipient) for event in seen[recipient]), label
    for other in set(seen) - {recipient}:
        assert seen[other] == [], (label, f"{other} notified for a message to {recipient}")


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["mcp", "rest"])
async def test_every_route_notifies_only_its_recipient_within_budget(tmp_path, transport):
    app = live_app(tmp_path)
    async with loopback(app) as url:
        async with mcp_client(url, AGENTS["hermes"]) as (hermes, hermes_events, _, _):
            async with mcp_client(url, AGENTS["codex"]) as (codex, codex_events, _, _):
                async with mcp_client(url, AGENTS["claude"]) as (claude, claude_events, _, _):
                    sessions = {"hermes": hermes, "codex": codex, "claude": claude}
                    events = {"hermes": hermes_events, "codex": codex_events, "claude": claude_events}
                    for name, session in sessions.items():
                        await session.subscribe_resource(AnyUrl(BASE_URI.format(name)))
                    assert take_leftovers(events) == {name: [] for name in AGENTS}
                    cursors, previous = {name: 0 for name in AGENTS}, None
                    for sender, recipient in itertools.permutations(AGENTS, 2):
                        label = (transport, sender, recipient)
                        # Late arrivals since the last settle window may only belong to the previous
                        # message's recipient, and still count toward that message's bound.
                        late = take_leftovers(events)
                        if previous is not None:
                            prev_recipient, prev_count = previous
                            assert all(not late[name] for name in AGENTS if name != prev_recipient), (label, late)
                            assert all(uri_of(event) == BASE_URI.format(prev_recipient) for event in late[prev_recipient]), label
                            assert prev_count + len(late[prev_recipient]) <= MAX_RECIPIENT_EVENTS, (label, late)
                        else:
                            assert all(not items for items in late.values()), (label, late)
                        topic = f"canary-{sender}-to-{recipient}"
                        payload = {"recipient": recipient, "message_type": "canary",
                                   "body": {"topic": topic, "from_role": sender}, "correlation_id": topic}
                        async with asyncio.timeout(SEND_BUDGET_SECONDS):
                            if transport == "mcp":
                                result = await sessions[sender].call_tool("send_message", payload)
                                assert not result.isError
                            else:
                                await rest_send(url, AGENTS[sender], payload)
                            seen = await collect(events, recipient)
                        assert_only_recipient(seen, recipient, label)
                        previous = (recipient, len(seen[recipient]))
                        page = resource_json(await sessions[recipient].read_resource(
                            AnyUrl(BASE_URI.format(recipient) + f"?after={cursors[recipient]}&limit=10")))
                        assert [item["sender"] for item in page["messages"]] == [sender]
                        message = page["messages"][0]
                        assert message["body"] == {"topic": topic, "from_role": sender}
                        assert message["correlation_id"] == topic
                        cursors[recipient] = page["next_cursor"]
                    # Final bounded quiet check: after the last read-back, within SETTLE_SECONDS, only
                    # late events for the last recipient (still within its bound) may arrive.
                    await asyncio.sleep(SETTLE_SECONDS)
                    late = take_leftovers(events)
                    last_recipient, last_count = previous
                    assert all(not late[name] for name in AGENTS if name != last_recipient), late
                    assert all(uri_of(event) == BASE_URI.format(last_recipient) for event in late[last_recipient])
                    assert last_count + len(late[last_recipient]) <= MAX_RECIPIENT_EVENTS


def fake_event(recipient: str):
    return SimpleNamespace(root=SimpleNamespace(params=SimpleNamespace(uri=BASE_URI.format(recipient))))


@pytest.mark.asyncio
@pytest.mark.parametrize("late_inbox,late_target", [("hermes", "codex"), ("codex", "codex")])
async def test_collect_attributes_delayed_foreign_and_duplicate_events(late_inbox, late_target):
    """The settle window must catch events that arrive after the first one, not hide them."""
    queues = {name: asyncio.Queue() for name in AGENTS}

    async def deliver():
        queues["codex"].put_nowait(fake_event("codex"))
        await asyncio.sleep(0.05)
        queues[late_inbox].put_nowait(fake_event(late_target))
        if late_inbox == "codex":
            queues["codex"].put_nowait(fake_event("codex"))  # a third event for one commit

    producer = asyncio.create_task(deliver())
    async with asyncio.timeout(SEND_BUDGET_SECONDS):
        seen = await collect(queues, "codex", settle=0.2)
    await producer
    with pytest.raises(AssertionError):
        assert_only_recipient(seen, "codex", "injected")


@pytest.mark.asyncio
async def test_ack_retried_from_a_new_session_after_its_response_was_ignored(tmp_path):
    """The recipient acks, discards the result, opens a new MCP session and acks again.

    This models a client that cannot tell whether its ack landed. It is not a dropped
    wire response and not a server restart: the server keeps running throughout.
    With idempotent finish (PR #142) the retry returns the committed message unchanged.
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
            assert not retried.isError
            assert retried.structuredContent == first.structuredContent
    stored = app.state.repository.get(message_id)
    assert stored.status == "acknowledged" and stored.claimed_by == "codex"
