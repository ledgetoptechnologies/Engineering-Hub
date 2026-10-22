"""Request-scoped canonical registry integration using synthetic transports only.

These are ASGI/MCP contract tests, not live registry or native wake acceptance.
"""
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.registry_bridge import (
    CanonicalRegistryBridge, HumanRegistryAccess, project_registry_snapshot,
)
from engineering_hub.registry_client import RegistryClient, RegistrySnapshot
from engineering_hub.registry_stream import RegistryStream

BASE = "/api/hub/v1/topics/engineering-hub"
ORIGIN = "https://registry.test"
EPOCH = "00000000-0000-4000-8000-000000000001"
REVISION = "00000000-0000-4000-8000-000000000002"


def wire_snapshot(now=None, **changes):
    now = now or datetime.now(timezone.utc)
    stamp = lambda value: value.isoformat().replace("+00:00", "Z")
    result = {"topic": "engineering-hub", "registry_epoch": EPOCH,
              "snapshot_revision": REVISION, "as_of": stamp(now), "complete": True,
              "source_state": "active", "peers": [{
                  "agent": "hermes", "status": "reachable", "reason": "session_ready",
                  "link_state": "linked", "transport_state": "attached",
                  "availability_state": "available", "evidence_level": "runtime_verified",
                  "binding_revision": EPOCH, "attachment_revision": REVISION,
                  "last_evidence_at": stamp(now), "valid_until": stamp(now + timedelta(seconds=120)),
              }]}
    return result | changes


def headers(agent="codex"):
    return {"Authorization": f"Bearer fixture-{agent}"}


@pytest.fixture
def make_hub(tmp_path):
    clients = []

    def make(handler=None, *, human_reader=None, stream_handler=None, configured=True):
        settings = Settings(tmp_path / f"hub-{len(clients)}.db",
                            AuthSettings("fixture-hermes", "fixture-codex", claude_token="fixture-claude"),
                            AuthConfig("https://hub.test", beau_github_id=42))
        bridge = None
        if configured:
            transport = httpx.MockTransport(handler or (lambda _: httpx.Response(200, json=wire_snapshot())))
            stream = RegistryStream(ORIGIN, transport=httpx.MockTransport(stream_handler)) if stream_handler else None
            bridge = CanonicalRegistryBridge(RegistryClient(ORIGIN, transport=transport),
                                             stream=stream, human_reader=human_reader)
        app = create_app(settings, registry_bridge=bridge)
        client = TestClient(app, base_url=settings.auth_config.origin)
        client.__enter__()
        clients.append(client)
        return client, app, settings

    yield make
    for client in reversed(clients):
        client.__exit__(None, None, None)


@pytest.mark.parametrize("route", ["peers", "preflight?recipient=hermes"])
@pytest.mark.parametrize("agent", ["codex", "hermes", "claude"])
def test_rest_preserves_original_bearer_ignoring_act_as(make_hub, route, agent):
    calls = []
    def remote(request):
        calls.append(request)
        return httpx.Response(200, json=wire_snapshot())
    client, _, _ = make_hub(remote)
    response = client.get(f"{BASE}/{route}", headers=headers(agent) | {"X-Act-As": "hermes"})
    assert response.status_code == 200, response.text
    assert calls[0].headers["authorization"] == f"Bearer fixture-{agent}"
    assert "x-act-as" not in calls[0].headers
    assert "cookie" not in calls[0].headers
    assert response.json()["delivery_guaranteed"] is False


def test_remote_acl_no_cross_agent_cache_or_response_leak(make_hub):
    calls = []
    def remote(request):
        calls.append(request.headers["authorization"])
        if calls[-1] == "Bearer fixture-claude":
            return httpx.Response(403, text="PRIVATE_OTHER_TOPIC")
        return httpx.Response(200, json=wire_snapshot())
    client, _, _ = make_hub(remote)
    assert client.get(BASE + "/peers", headers=headers()).status_code == 200
    denied = client.get(BASE + "/peers", headers=headers("claude"))
    assert denied.status_code == 403
    assert "PRIVATE_OTHER_TOPIC" not in denied.text and "reachable" not in denied.text
    assert calls == ["Bearer fixture-codex", "Bearer fixture-claude"]


def human_cookie(client, app, settings):
    session = app.state.sessions.create_session(42)
    client.cookies.set(settings.auth_config.cookie_name, session["cookie_token"])


def test_unconfigured_human_reader_never_borrows_agent_token(make_hub):
    def forbidden(_):
        pytest.fail("unconfigured human must not call upstream")
    client, app, settings = make_hub(forbidden)
    human_cookie(client, app, settings)
    response = client.get(BASE + "/peers", headers={"X-Act-As": "codex"})
    assert response.status_code == 503


def test_human_fixed_topic_service_reader_is_separate_scope(make_hub):
    calls, resolutions = [], []
    def reader(principal, topic):
        resolutions.append((principal, topic))
        return HumanRegistryAccess("fixture-readonly-service", frozenset({"engineering-hub"}))
    def remote(request):
        calls.append(request)
        return httpx.Response(200, json=wire_snapshot())
    client, app, settings = make_hub(remote, human_reader=reader)
    human_cookie(client, app, settings)
    assert client.get(BASE + "/peers").status_code == 200
    assert calls[0].headers["authorization"] == "Bearer fixture-readonly-service"
    denied = client.get("/api/hub/v1/topics/private-topic/peers")
    assert denied.status_code == 403 and len(calls) == 1
    assert resolutions == [("human:42", "engineering-hub"), ("human:42", "private-topic")]


@pytest.mark.parametrize("mode", ["unavailable", "private", "wrong-topic", "malformed"])
def test_invalid_source_is_safe_unavailable(make_hub, mode):
    value = wire_snapshot()
    if mode == "private":
        value["private_session_ref"] = "PRIVATE_TOKEN_AND_SESSION"
    elif mode == "wrong-topic":
        value["topic"] = "private-topic"
    elif mode == "malformed":
        value["peers"][0]["status"] = "arbitrary"
    response = httpx.Response(503, text="PRIVATE_TOKEN_AND_SESSION") if mode == "unavailable" else httpx.Response(200, json=value)
    client, _, _ = make_hub(lambda _: response)
    result = client.get(BASE + "/peers", headers=headers())
    assert result.status_code == 503
    assert "PRIVATE" not in result.text and "private-topic" not in result.text


def test_default_is_unknown_and_never_opens_transport(make_hub, monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "send", lambda *a, **k: pytest.fail("unexpected network"))
    client, _, _ = make_hub(configured=False)
    response = client.get(BASE + "/peers", headers=headers())
    assert response.status_code == 200
    assert all(peer["status"] == "unknown_stale" for peer in response.json()["items"])
    assert client.get(BASE + "/events", headers=headers()).status_code == 503


def test_projection_expiry_future_and_missing_peer_never_infers_unlinked():
    now = datetime.now(timezone.utc)
    snapshot = RegistrySnapshot.model_validate(wire_snapshot(now))
    first = project_registry_snapshot(snapshot, topic="engineering-hub", now=now)
    hermes = next(item for item in first["items"] if item["agent"] == "hermes")
    assert hermes["status"] == "reachable"
    assert all(item["status"] == "unknown_stale" for item in first["items"] if item["agent"] != "hermes")
    later = project_registry_snapshot(snapshot, topic="engineering-hub", now=now + timedelta(seconds=121))
    expired = next(item for item in later["items"] if item["agent"] == "hermes")
    assert expired["status"] == "unknown_stale" and expired["reason"] == "evidence_expired"
    assert expired["valid_until"] is None
    assert expired["last_evidence_at"] == hermes["last_evidence_at"]
    future = project_registry_snapshot(snapshot, topic="engineering-hub", now=now - timedelta(seconds=1))
    assert next(item for item in future["items"] if item["agent"] == "hermes")["reason"] == "evidence_in_future"


def test_mixed_sources_rejected(tmp_path):
    with pytest.raises(ValueError, match="registry_source_identity_mismatch"):
        CanonicalRegistryBridge(RegistryClient(ORIGIN), stream=RegistryStream("https://different.test"))
    settings = Settings(tmp_path / "hub.db", AuthSettings("hermes", "codex"), AuthConfig("https://hub.test"))
    with pytest.raises(ValueError, match="one_reachability_source_required"):
        create_app(settings, registry_bridge=CanonicalRegistryBridge(RegistryClient(ORIGIN)),
                   reachability_provider=lambda _: {})


def frame(name, payload, identity=None):
    return ((f"id: {identity}\n" if identity else "") + f"event: {name}\ndata: {json.dumps(payload)}\n\n")


def test_sse_uses_canonical_cursor_and_original_identity(make_hub):
    calls = []
    def snapshot(request):
        calls.append(request)
        return httpx.Response(200, json=wire_snapshot())
    def stream(request):
        calls.append(request)
        pointer = {"topic": "engineering-hub", "registry_epoch": EPOCH, "snapshot_revision": REVISION, "cursor": 8}
        unavailable = {"topic": "engineering-hub", "registry_epoch": EPOCH, "reason": "observer_unavailable"}
        content = frame("registry.invalidated", pointer, f"{EPOCH}:engineering-hub:8") + frame("registry.unavailable", unavailable)
        return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})
    client, _, _ = make_hub(snapshot, stream_handler=stream)
    response = client.get(BASE + f"/events?after=7&epoch={EPOCH}", headers=headers("claude") | {"X-Act-As": "codex"})
    assert response.status_code == 200
    assert len(calls) == 2
    assert all(request.headers["authorization"] == "Bearer fixture-claude" for request in calls)
    assert calls[1].url.params == httpx.QueryParams(after="7", epoch=EPOCH)
    assert calls[1].headers["last-event-id"] == f"{EPOCH}:engineering-hub:7"
    assert f"id: {EPOCH}:engineering-hub:8" in response.text
    assert "event: registry.invalidated" in response.text
    assert response.text.count("event: registry.unavailable") == 1
    assert "hub.outbox" not in response.text


@pytest.mark.parametrize("query", ["after=1", f"epoch={EPOCH}", f"after=-1&epoch={EPOCH}",
    f"after=9223372036854775808&epoch={EPOCH}", f"after=true&epoch={EPOCH}",
    f"after=1.5&epoch={EPOCH}", "after=1&epoch=not-a-uuid"])
def test_sse_query_rejected_before_subscription(make_hub, query):
    client, _, _ = make_hub(lambda _: pytest.fail("invalid cursor fetched snapshot"),
                           stream_handler=lambda _: pytest.fail("invalid cursor subscribed"))
    assert client.get(BASE + "/events?" + query, headers=headers()).status_code == 422


def test_sse_last_event_id_is_not_a_hub_cursor(make_hub):
    client, _, _ = make_hub(lambda _: pytest.fail("legacy cursor fetched snapshot"),
                           stream_handler=lambda _: pytest.fail("legacy cursor subscribed"))
    assert client.get(BASE + "/events", headers=headers() | {"Last-Event-ID": "99"}).status_code == 422


def test_sse_acl_denial_never_subscribes(make_hub):
    client, _, _ = make_hub(lambda _: httpx.Response(403, text="PRIVATE"),
                           stream_handler=lambda _: pytest.fail("denied subscription"))
    response = client.get(BASE + "/events", headers=headers())
    assert response.status_code == 403 and "PRIVATE" not in response.text


@pytest.mark.parametrize("mode", ["disconnect", "wrong-topic", "private"])
def test_sse_remote_failure_is_terminal_fixed_safe(make_hub, mode):
    def remote(request):
        if mode == "disconnect":
            raise httpx.ConnectError("PRIVATE_TOKEN_AND_HOST", request=request)
        payload = {"topic": "wrong" if mode == "wrong-topic" else "engineering-hub",
                   "registry_epoch": None, "reason": "observer_unavailable"}
        if mode == "private":
            payload["private_session_ref"] = "PRIVATE_TOKEN_AND_HOST"
        return httpx.Response(200, text=frame("registry.unavailable", payload),
                              headers={"content-type": "text/event-stream"})
    client, _, _ = make_hub(stream_handler=remote)
    response = client.get(BASE + "/events", headers=headers())
    assert response.status_code == 200
    assert response.text.count("event: registry.unavailable") == 1
    assert "PRIVATE" not in response.text and '"topic": "wrong"' not in response.text
    assert "observer_unavailable" in response.text


def test_mcp_discovery_and_topic_tools_preserve_original_identity(make_hub):
    calls = []
    def remote(request):
        calls.append(request.headers["authorization"])
        return httpx.Response(200, json=wire_snapshot())
    client, _, _ = make_hub(remote)
    def rpc(method, params):
        return client.post("/mcp/", headers=headers("claude") | {
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18",
            "X-Act-As": "codex"}, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    response = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "fixture", "version": "1"}})
    assert response.status_code == 200, response.text
    tools = rpc("tools/list", {}).json()["result"]["tools"]
    assert len(tools) == 19
    for tool in ("hub_topic_peers", "hub_topic_preflight"):
        arguments = {"topic": "engineering-hub"}
        if tool.endswith("preflight"):
            arguments["recipient"] = "hermes"
        result = rpc("tools/call", {"name": tool, "arguments": arguments}).json()["result"]
        assert not result.get("isError"), result
    assert calls == ["Bearer fixture-claude", "Bearer fixture-claude"]


def test_stream_rechecks_human_session_before_emitting_pointer(make_hub):
    shared = {}
    closed = []

    class RevokingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            shared["app"].state.sessions.delete_session(shared["token"])
            pointer = {"topic": "engineering-hub", "registry_epoch": EPOCH,
                       "snapshot_revision": REVISION, "cursor": 8}
            yield frame("registry.invalidated", pointer, f"{EPOCH}:engineering-hub:8").encode()

        async def aclose(self):
            closed.append(True)

    client, app, settings = make_hub(
        human_reader=lambda principal, topic: HumanRegistryAccess("fixture-service", frozenset({"engineering-hub"})),
        stream_handler=lambda _: httpx.Response(200, stream=RevokingBody(),
                                                headers={"content-type": "text/event-stream"}),
    )
    session = app.state.sessions.create_session(42)
    client.cookies.set(settings.auth_config.cookie_name, session["cookie_token"])
    shared.update(app=app, token=session["cookie_token"])
    response = client.get(BASE + "/events")
    assert response.status_code == 200
    assert "registry.invalidated" not in response.text
    assert closed == [True]


def test_comment_only_stream_closes_on_local_human_session_revocation(make_hub):
    shared, closed, consumed_after_revocation = {}, [], []

    class RevokingComments(httpx.AsyncByteStream):
        async def __aiter__(self):
            # No public registry event follows revocation: the chunk-level guard
            # must close before requesting another chunk or retaining the reader.
            yield b": still connected\n\n"
            shared["app"].state.sessions.delete_session(shared["token"])
            yield b": PRIVATE_KEEPALIVE\n\n"
            consumed_after_revocation.append(True)
            raise AssertionError("PRIVATE_UPSTREAM_DETAILS")

        async def aclose(self):
            closed.append(True)

    client, app, settings = make_hub(
        human_reader=lambda principal, topic: HumanRegistryAccess("fixture-service", frozenset({"engineering-hub"})),
        stream_handler=lambda _: httpx.Response(200, stream=RevokingComments(),
                                                headers={"content-type": "text/event-stream"}),
    )
    session = app.state.sessions.create_session(42)
    client.cookies.set(settings.auth_config.cookie_name, session["cookie_token"])
    shared.update(app=app, token=session["cookie_token"])
    response = client.get(BASE + "/events")
    assert response.status_code == 200
    assert response.text.count("event: registry.unavailable") == 1
    assert "registry.invalidated" not in response.text
    assert "PRIVATE" not in response.text and "fixture-service" not in response.text
    assert not consumed_after_revocation
    assert closed == [True]


def test_mcp_acl_denial_does_not_fallback_or_reflect_remote_data(make_hub):
    client, _, _ = make_hub(lambda _: httpx.Response(403, text="PRIVATE_OTHER_TOPIC"))
    response = client.post("/mcp/", headers=headers() | {
        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "hub_topic_peers", "arguments": {"topic": "engineering-hub"}}})
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert "topic_read_forbidden" in str(result)
    assert "PRIVATE_OTHER_TOPIC" not in str(result)
