from __future__ import annotations

import json

import pytest
from pydantic import AnyUrl

from agent_message_bus.app import create_app
from agent_message_bus.database import MessageRepository
from tests.test_native_push_events import BASE_URI, TOKENS, loopback, mcp_client
from agent_message_bus.push import PushPublisher, presence_session_fingerprint
from agent_message_bus.topic_presence import TopicPresenceRepository


@pytest.fixture(autouse=True)
def allow_ephemeral_loopback_host(monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")


def live_app(tmp_path):
    return create_app(
        db_path=tmp_path / "messages.sqlite3",
        audit_path=tmp_path / "audit.jsonl",
        auth=TOKENS,
        file_root=tmp_path / "files",
        push_events_operational=True,
    )


def _payload(result) -> dict:
    assert not result.isError
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def _member(status: dict, agent: str) -> dict:
    return next(item for item in status["members"] if item["agent"] == agent)


@pytest.mark.asyncio
async def test_topic_presence_protocol_schema_and_same_agent_separate_connections(tmp_path):
    app = live_app(tmp_path)
    async with loopback(app) as url:
        async with mcp_client(url, TOKENS.codex_token) as (codex_a, _, session_a, _):
            await codex_a.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
            joined_a = _payload(await codex_a.call_tool("topic_presence_join", {"topic": "alpha"}))
            assert joined_a["agent"] == "codex"
            assert joined_a["topic"] == "alpha"
            assert len(joined_a["generation"]) == 32
            status_alpha = _payload(await codex_a.call_tool("topic_presence_status", {"topic": "alpha"}))
            assert _member(status_alpha, "codex")["state"] == "online"
            assert status_alpha["members"] == [_member(status_alpha, "codex")]
            assert status_alpha["topic"] == "alpha"
            assert session_a() not in json.dumps(status_alpha)
            assert "session_hash" not in json.dumps(status_alpha)
            assert "stream_generation" not in json.dumps(status_alpha)
            connection = app.state.repository.connect()
            try:
                persisted = connection.execute(
                    "SELECT * FROM topic_memberships WHERE topic='alpha'"
                ).fetchone()
            finally:
                connection.close()
            assert persisted["session_hash"] != session_a()
            assert len(persisted["session_hash"]) == 64
            assert persisted["subscription_generation"] != session_a()
            assert persisted["stream_generation"]
            assert len(persisted["stream_generation"]) == 64
            assert _payload(await codex_a.call_tool(
                "topic_presence_status", {"topic": "alpha-other"}
            ))["members"] == []

            # Same authenticated agent, different MCP connection, different exact topic.
            async with mcp_client(url, TOKENS.codex_token) as (codex_b, _, session_b, _):
                await codex_b.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
                joined_b = _payload(await codex_b.call_tool("topic_presence_join", {"topic": "beta"}))
                assert joined_b["agent"] == "codex"
                assert session_b() != session_a()
                assert _member(_payload(await codex_a.call_tool(
                    "topic_presence_status", {"topic": "beta"}
                )), "codex")["state"] == "online"

            # Closing connection B leaves its persisted membership disconnected, while A stays online.
            beta = _payload(await codex_a.call_tool("topic_presence_status", {"topic": "beta"}))
            alpha = _payload(await codex_a.call_tool("topic_presence_status", {"topic": "alpha"}))
            assert _member(beta, "codex")["state"] == "disconnected"
            assert _member(beta, "codex")["online"] is False
            assert _member(alpha, "codex")["state"] == "online"

            # An unrelated subscribed connection for the same identity cannot revive beta.
            async with mcp_client(url, TOKENS.codex_token) as (codex_c, _, _, _):
                await codex_c.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
                beta = _payload(await codex_c.call_tool("topic_presence_status", {"topic": "beta"}))
                assert _member(beta, "codex")["state"] == "disconnected"
                assert _member(_payload(await codex_c.call_tool(
                    "topic_presence_status", {"topic": "alpha"}
                )), "codex")["state"] == "online"

            # Auth identity is always derived by the server; another agent cannot remove Codex's row.
            async with mcp_client(url, TOKENS.hermes_token) as (hermes, _, _, _):
                await hermes.subscribe_resource(AnyUrl(BASE_URI.format("hermes")))
                denied = _payload(await hermes.call_tool("topic_presence_leave", {
                    "topic": "alpha", "generation": joined_a["generation"],
                }))
                assert denied == {"topic": "alpha", "removed": False}
                joined_h = _payload(await hermes.call_tool("topic_presence_join", {"topic": "alpha"}))
                assert joined_h["agent"] == "hermes"
                members = _payload(await hermes.call_tool("topic_presence_status", {"topic": "alpha"}))
                assert {member["agent"] for member in members["members"]} == {"codex", "hermes"}
                assert _member(members, "codex")["state"] == "online"
                assert _member(members, "hermes")["state"] == "online"

            # Caller-scoped global presence keeps its existing shape/API.
            global_status = _payload(await codex_a.call_tool("presence_status", {}))
            assert global_status["scope"] == "this_server_process"
            assert {row["agent"] for row in global_status["agents"]} == {"hermes", "codex", "claude"}


@pytest.mark.asyncio
async def test_join_requires_actual_current_inbox_subscription_and_active_sse(tmp_path):
    app = live_app(tmp_path)
    async with loopback(app) as url:
        async with mcp_client(url, TOKENS.codex_token) as (client, _, _, _):
            failed = await client.call_tool("topic_presence_join", {"topic": "no-stream"})
            assert failed.isError
            await client.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
            invalid = await client.call_tool("topic_presence_join", {"topic": "bad/topic"})
            assert invalid.isError
            joined = _payload(await client.call_tool("topic_presence_join", {"topic": "attached"}))
            assert joined["topic"] == "attached"

            # Unsubscribe invalidates the exact subscription generation attached at registration.
            await client.unsubscribe_resource(AnyUrl(BASE_URI.format("codex")))
            status = _payload(await client.call_tool("topic_presence_status", {"topic": "attached"}))
            assert _member(status, "codex")["state"] == "disconnected"
            await client.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
            status = _payload(await client.call_tool("topic_presence_status", {"topic": "attached"}))
            assert _member(status, "codex")["state"] == "disconnected"


@pytest.mark.asyncio
async def test_replacement_generation_fences_stale_leave_from_prior_connection(tmp_path):
    app = live_app(tmp_path)
    async with loopback(app) as url:
        async with mcp_client(url, TOKENS.codex_token) as (old_session, _, _, _):
            await old_session.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
            old_registration = _payload(await old_session.call_tool(
                "topic_presence_join", {"topic": "replacement"}
            ))
            async with mcp_client(url, TOKENS.codex_token) as (new_session, _, _, _):
                await new_session.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
                new_registration = _payload(await new_session.call_tool(
                    "topic_presence_join", {"topic": "replacement"}
                ))
                assert new_registration["generation"] != old_registration["generation"]
                stale = _payload(await old_session.call_tool("topic_presence_leave", {
                    "topic": "replacement", "generation": old_registration["generation"],
                }))
                assert stale == {"topic": "replacement", "removed": False}
                current = _payload(await new_session.call_tool(
                    "topic_presence_status", {"topic": "replacement"}
                ))
                assert len(current["members"]) == 1
                assert _member(current, "codex")["state"] == "online"

            # The old connection still has a global inbox stream, but cannot make the replacement live.
            disconnected = _payload(await old_session.call_tool(
                "topic_presence_status", {"topic": "replacement"}
            ))
            assert _member(disconnected, "codex")["state"] == "disconnected"
            removed = _payload(await old_session.call_tool("topic_presence_leave", {
                "topic": "replacement", "generation": new_registration["generation"],
            }))
            assert removed == {"topic": "replacement", "removed": False}


@pytest.mark.asyncio
async def test_restart_preserves_member_as_disconnected_and_leave_is_generation_fenced(tmp_path):
    db_path = tmp_path / "messages.sqlite3"
    first = live_app(tmp_path)
    async with loopback(first) as url:
        async with mcp_client(url, TOKENS.claude_token) as (client, _, _, _):
            await client.subscribe_resource(AnyUrl(BASE_URI.format("claude")))
            registration = _payload(await client.call_tool(
                "topic_presence_join", {"topic": "restart"}
            ))
            stale = _payload(await client.call_tool("topic_presence_join", {"topic": "restart"}))
            assert stale["generation"] != registration["generation"]
            stale_leave = _payload(await client.call_tool("topic_presence_leave", {
                "topic": "restart", "generation": registration["generation"],
            }))
            assert stale_leave["removed"] is False

    restarted = live_app(tmp_path)
    assert restarted.state.repository.path == db_path
    async with loopback(restarted) as url:
        async with mcp_client(url, TOKENS.claude_token) as (client, _, _, _):
            status = _payload(await client.call_tool("topic_presence_status", {"topic": "restart"}))
            assert status["native_push_available"] is True
            assert len(status["members"]) == 1
            assert _member(status, "claude")["state"] == "disconnected"
            assert "generation" not in json.dumps(status)
            assert "session_hash" not in json.dumps(status)
            assert "stream_generation" not in json.dumps(status)

            # A new session can replace the disconnected member and explicitly leave it.
            await client.subscribe_resource(AnyUrl(BASE_URI.format("claude")))
            replacement = _payload(await client.call_tool("topic_presence_join", {"topic": "restart"}))
            assert replacement["generation"] != stale["generation"]
            assert _member(_payload(await client.call_tool(
                "topic_presence_status", {"topic": "restart"}
            )), "claude")["state"] == "online"
            left = _payload(await client.call_tool("topic_presence_leave", {
                "topic": "restart", "generation": replacement["generation"],
            }))
            assert left["removed"] is True
            assert _payload(await client.call_tool(
                "topic_presence_status", {"topic": "restart"}
            ))["members"] == []


def test_existing_database_initialization_migrates_legacy_topic_memberships(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    original = MessageRepository(path)
    connection = original.connect()
    try:
        connection.execute("DROP TABLE topic_memberships")
        connection.execute("""CREATE TABLE topic_memberships (
            topic TEXT NOT NULL,
            agent TEXT NOT NULL,
            session_hash TEXT NOT NULL,
            subscription_generation TEXT NOT NULL,
            generation TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            PRIMARY KEY (topic, agent)
        )""")
        connection.execute("""INSERT INTO topic_memberships
            (topic, agent, session_hash, subscription_generation, generation, registered_at)
            VALUES ('legacy', 'codex', 'private-session-hash', 'old-subscription',
                    '0123456789abcdef0123456789abcdef', '2026-10-17T00:00:00Z')""")
    finally:
        connection.close()

    MessageRepository(path)
    connection = original.connect()
    try:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(topic_memberships)")}
        migrated = connection.execute(
            "SELECT * FROM topic_memberships WHERE topic='legacy'"
        ).fetchone()
    finally:
        connection.close()
    assert columns == {
        "topic", "agent", "session_hash", "subscription_generation", "stream_generation",
        "generation", "registered_at",
    }
    assert migrated["generation"] == "0123456789abcdef0123456789abcdef"
    assert migrated["stream_generation"] == ""


@pytest.mark.asyncio
async def test_reopening_sse_on_same_sdk_session_does_not_revive_topic_without_rejoin(tmp_path):
    publisher = PushPublisher(enabled=True)
    database_path = tmp_path / "presence.sqlite3"
    MessageRepository(database_path)
    repository = TopicPresenceRepository(database_path)

    class Session:
        async def send_resource_updated(self, _uri):
            pass

    await publisher.subscribe("same-sdk-session", "codex", Session())
    original_stream = await publisher.open_presence_stream("same-sdk-session", "codex")
    binding = await publisher.topic_subscription_binding("same-sdk-session", "codex")
    assert binding is not None
    subscription_generation, stream_generation = binding
    registration = repository.register(
        agent="codex", topic="stream-lifecycle", session_id="same-sdk-session",
        subscription_generation=subscription_generation, stream_generation=stream_generation,
    )
    old_live_binding = (
        presence_session_fingerprint("same-sdk-session"), "codex",
        subscription_generation, stream_generation,
    )
    assert old_live_binding in await publisher.live_topic_bindings()

    await publisher.close_presence_stream(original_stream)
    replacement_stream = await publisher.open_presence_stream("same-sdk-session", "codex")
    try:
        assert old_live_binding not in await publisher.live_topic_bindings(), (
            "a newly opened SSE stream must not revive the old topic registration"
        )
        replacement_binding = await publisher.topic_subscription_binding("same-sdk-session", "codex")
        assert replacement_binding is not None
        replacement_subscription, replacement_generation = replacement_binding
        assert replacement_subscription == subscription_generation
        replacement = repository.register(
            agent="codex", topic="stream-lifecycle", session_id="same-sdk-session",
            subscription_generation=replacement_subscription,
            stream_generation=replacement_generation,
        )
        assert replacement["generation"] != registration["generation"]
        assert (
            presence_session_fingerprint("same-sdk-session"), "codex",
            replacement_subscription, replacement_generation,
        ) in await publisher.live_topic_bindings()
        assert repository.leave(
            agent="codex", topic="stream-lifecycle", session_id="same-sdk-session",
            generation=registration["generation"],
        ) is False
        assert repository.leave(
            agent="codex", topic="stream-lifecycle", session_id="same-sdk-session",
            generation=replacement["generation"],
        ) is True
    finally:
        await publisher.close_presence_stream(replacement_stream)
