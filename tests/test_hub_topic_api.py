"""Advisory topic reachability and authenticated event-feed integration."""
from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.events import EventBroker, NativeHubEventFeed
from engineering_hub.reachability import ObserverContext, RouteObservation, TopicBinding


BASE = "/api/hub/v1"
TOPIC = "hub-build"
NOW = datetime.now(timezone.utc)
AUTH = {"Authorization": "Bearer fixture-codex"}
PRIVATE = "private-session-observer-material"


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "hub.sqlite3",
                    AuthSettings("fixture-hermes", "fixture-codex", claude_token="fixture-claude"),
                    AuthConfig("https://hub.test", beau_github_id=42))


def disabled_feed(settings):
    return NativeHubEventFeed(settings.database, support_check=lambda _: (False, "fixture-unsupported"))


def make_app(settings, **kwargs):
    return create_app(settings, event_feed=kwargs.pop("event_feed", disabled_feed(settings)), **kwargs)


def evidence_for(topic=TOPIC, *, event="ready"):
    binding = TopicBinding(topic=topic, agent="hermes", session_id=PRIVATE,
                           session_generation=1, connector_generation=1, observer_id=PRIVATE)
    observer = ObserverContext(observer_id=PRIVATE, epoch=PRIVATE, now=NOW)
    observation = RouteObservation(
        **binding.model_dump(exclude={"binding_revision"}), observer_epoch=PRIVATE,
        sequence=1, observed_at=NOW, provenance="runtime_control", event=event,
        native_attachment="verified", runtime_acceptance="idle",
    )
    return {"bindings": (binding,), "observer": observer, "observations": (observation,),
            "binding_snapshot_complete": True}


def rpc(client, name, arguments, *, authorization=AUTH):
    response = client.post("/mcp/", headers={
        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18",
        **authorization,
    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}})
    return response


def structured(response):
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("suffix", [f"/topics/{TOPIC}/peers", f"/topics/{TOPIC}/preflight?recipient=hermes",
                                    "/events/status", "/events"])
def test_topic_and_event_reads_require_authentication(settings, suffix):
    with TestClient(make_app(settings), base_url="https://hub.test") as client:
        for headers in ({}, {"Authorization": "Bearer invalid"}):
            assert client.get(BASE + suffix, headers=headers).status_code == 401


def test_unconfigured_topic_is_unknown_not_authoritatively_unlinked(settings):
    with TestClient(make_app(settings), base_url="https://hub.test") as client:
        response = client.get(f"{BASE}/topics/{TOPIC}/peers", headers=AUTH)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["topic"] == TOPIC
        assert {peer["agent"] for peer in data["items"]} == {"codex", "hermes", "claude"}
        assert {peer["status"] for peer in data["items"]} == {"unknown_stale"}
        assert data["advisory"] is True and data["delivery_guaranteed"] is False
        for recipient in ("codex", "hermes", "claude"):
            response = client.get(f"{BASE}/topics/{TOPIC}/preflight", headers=AUTH,
                                  params={"recipient": recipient})
            assert response.status_code == 200, response.text
            preflight = response.json()
            assert preflight["topic"] == TOPIC and preflight["recipient"] == recipient
            assert preflight["peer"]["agent"] == recipient
            assert preflight["peer"]["status"] == "unknown_stale"
            assert preflight["advisory"] is True and preflight["delivery_guaranteed"] is False
            assert preflight['guidance'] == {
                'recipient_state': 'unknown', 'send_mode': 'queue_only',
                'response_expectation': 'not_verified',
                'durable_delivery_required': True, 'receipt_required': True}


@pytest.mark.parametrize("topic", ["bad topic", "$private-invalid", "x" * 129, ".invalid", "bad%value"])
def test_invalid_topics_rejected_by_rest_and_mcp_without_echo(settings, topic):
    with TestClient(make_app(settings), base_url="https://hub.test") as client:
        response = client.get(f"{BASE}/topics/{topic}/peers", headers=AUTH)
        assert response.status_code == 422, response.text
        assert response.json() == {"detail": {"code": "invalid_topic"}}
        response = rpc(client, "hub_topic_peers", {"topic": topic})
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is True and topic not in str(result)


def test_invalid_recipient_never_falls_back_to_another_peer(settings):
    with TestClient(make_app(settings), base_url="https://hub.test") as client:
        response = client.get(f"{BASE}/topics/{TOPIC}/preflight", headers=AUTH,
                              params={"recipient": "admin-private"})
        assert response.status_code == 422
        response = rpc(client, "hub_topic_preflight", {"topic": TOPIC, "recipient": "admin-private"})
        assert response.status_code == 200
        assert response.json()["result"]["isError"] is True
        assert "admin-private" not in response.text


def test_trusted_provider_ready_disconnect_and_exact_topic_projection(settings):
    state = {"event": "ready"}
    calls = []

    def provider(topic):
        calls.append(topic)
        return evidence_for(event=state["event"])

    with TestClient(make_app(settings, reachability_provider=provider), base_url="https://hub.test") as client:
        path = f"{BASE}/topics/{TOPIC}/preflight"
        first = client.get(path, headers=AUTH, params={"recipient": "hermes"})
        assert first.json()["peer"]["status"] == "reachable"
        assert first.json()["peer"]["evidence_level"] == "runtime_verified"
        assert first.json()["peer"]["valid_until"] is not None
        assert first.json()["delivery_guaranteed"] is False
        assert first.json()['guidance']['recipient_state'] == 'online'
        assert first.json()['guidance']['send_mode'] == 'notify_if_still_reachable'
        assert PRIVATE not in first.text and "session_id" not in first.text
        state["event"] = "disconnected"
        second = client.get(path, headers=AUTH, params={"recipient": "hermes"})
        assert second.json()["peer"]["status"] == "offline"
        assert second.json()["peer"]["reason"] == "session_disconnected"
        assert second.json()['guidance']['recipient_state'] == 'offline'
        assert second.json()['guidance']['send_mode'] == 'queue_only'
        other = client.get(f"{BASE}/topics/different-topic/peers", headers=AUTH).json()
        assert all(peer["status"] == "unlinked" for peer in other["items"])
        assert calls == [TOPIC, TOPIC, "different-topic"]


def test_cached_provider_clock_cannot_freeze_reachability_as_ready(settings):
    old = datetime.now(timezone.utc) - timedelta(days=1)
    evidence = evidence_for()
    evidence["observer"] = evidence["observer"].model_copy(update={"now": old})
    evidence["observations"] = tuple(item.model_copy(update={"observed_at": old})
                                      for item in evidence["observations"])
    with TestClient(make_app(settings, reachability_provider=lambda _: evidence), base_url="https://hub.test") as client:
        response = client.get(f"{BASE}/topics/{TOPIC}/preflight", headers=AUTH,
                              params={"recipient": "hermes"})
        assert response.status_code == 200
        assert response.json()["peer"]["status"] == "unknown_stale"
        assert response.json()["peer"]["reason"] == "evidence_expired"
        assert response.json()["peer"]["valid_until"] is None


@pytest.mark.parametrize("kind", ["exception", "invalid-kwargs", "invalid-evidence"])
def test_provider_failure_degrades_to_unknown_without_private_errors(settings, kind):
    def provider(topic):
        if kind == "exception":
            raise RuntimeError(PRIVATE)
        if kind == "invalid-kwargs":
            return {"private": PRIVATE}
        return {"bindings": ({"topic": topic, "session_id": PRIVATE},)}

    with TestClient(make_app(settings, reachability_provider=provider), base_url="https://hub.test") as client:
        response = client.get(f"{BASE}/topics/{TOPIC}/peers", headers=AUTH)
        assert response.status_code == 200
        assert all(peer["status"] == "unknown_stale" for peer in response.json()["items"])
        assert PRIVATE not in response.text
        data = structured(rpc(client, "hub_topic_peers", {"topic": TOPIC}))
        assert all(peer["status"] == "unknown_stale" for peer in data["items"])
        assert PRIVATE not in str(data)


def test_mcp_topic_tools_match_rest_and_use_existing_bearer_auth(settings):
    with TestClient(make_app(settings, reachability_provider=lambda _: evidence_for()), base_url="https://hub.test") as client:
        rest = client.get(f"{BASE}/topics/{TOPIC}/peers", headers=AUTH).json()
        assert structured(rpc(client, "hub_topic_peers", {"topic": TOPIC})) == rest
        preflight = structured(rpc(client, "hub_topic_preflight", {"topic": TOPIC, "recipient": "hermes"}))
        assert preflight["peer"]["status"] == "reachable" and preflight["delivery_guaranteed"] is False
        rest_preflight = client.get(f'{BASE}/topics/{TOPIC}/preflight', headers=AUTH,
                                    params={'recipient': 'hermes'}).json()
        assert preflight['guidance'] == rest_preflight['guidance']
        assert preflight['guidance']['recipient_state'] == 'online'
        for headers in ({}, {"Authorization": "Bearer invalid"}):
            assert rpc(client, "hub_topic_peers", {"topic": TOPIC}, authorization=headers).status_code == 401


def test_peer_readiness_has_no_self_report_or_write_route(settings):
    with TestClient(make_app(settings), base_url="https://hub.test") as client:
        payload = {"agent": "codex", "status": "reachable", "session_id": PRIVATE}
        for suffix in ("peers", "preflight"):
            response = client.post(f"{BASE}/topics/{TOPIC}/{suffix}", headers=AUTH, json=payload)
            assert response.status_code == 405
        assert client.post(BASE + "/events/status", headers=AUTH, json=payload).status_code == 405
        data = client.get(f"{BASE}/topics/{TOPIC}/peers", headers=AUTH).json()
        assert all(peer["status"] == "unknown_stale" for peer in data["items"])


def test_unsupported_event_feed_returns_authenticated_503_without_claiming_poll_fallback(settings):
    feed = disabled_feed(settings)
    with TestClient(make_app(settings, event_feed=feed), base_url="https://hub.test") as client:
        status = client.get(BASE + "/events/status", headers=AUTH)
        assert status.status_code == 200
        assert status.json()["available"] is False
        assert status.json()["reason"] == "native_source_unsupported"
        response = client.get(BASE + "/events", headers=AUTH)
        assert response.status_code == 503
        assert "fixture-unsupported" not in response.text


class FiniteBroker(EventBroker):
    """Finite test transport; production EventBroker remains event driven."""

    async def events(self):
        stream = super().events()
        try:
            yield await anext(stream)
        finally:
            await stream.aclose()


class FixtureFeed:
    def __init__(self):
        self.broker = FiniteBroker()
        self.started = self.stopped = False
        self.reason = "not_started"

    @property
    def available(self):
        return self.broker.available

    @property
    def status(self):
        return {"available": self.available, "reason": self.reason,
                "observer_epoch": self.broker.observer_epoch, "cursor": self.broker.cursor}

    async def start(self):
        self.started = True
        self.broker.activate(5)
        self.reason = "native_source_active"
        return self.status

    async def stop(self):
        self.stopped = True
        self.broker.fail()


def test_event_feed_lifecycle_auth_snapshot_only_and_disconnect_status(settings):
    feed = FixtureFeed()
    with TestClient(make_app(settings, event_feed=feed), base_url="https://hub.test") as client:
        assert feed.started and not feed.stopped
        assert client.get(BASE + "/events/status", headers=AUTH).json()["available"] is True
        response = client.get(BASE + "/events", headers=AUTH)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text.startswith("data: ")
        pointer = json.loads(response.text.removeprefix("data: ").strip())
        assert set(pointer) == {"observer_epoch", "cursor", "kind"}
        assert pointer["cursor"] == 5 and pointer["kind"] == "invalidate"
        assert feed.broker.subscriber_count == 0
        # A disconnected invalidation source does not prove peers are offline;
        # route evidence remains unknown until an authoritative observer reports.
        feed.broker.fail()
        feed.reason = "native_source_failed"
        assert client.get(BASE + "/events/status", headers=AUTH).json()["available"] is False
        assert client.get(BASE + "/events", headers=AUTH).status_code == 503
        assert all(peer["status"] == "unknown_stale" for peer in client.get(
            f"{BASE}/topics/{TOPIC}/peers", headers=AUTH).json()["items"])
    assert feed.stopped


def test_human_session_revocation_stops_stream_before_next_pointer(settings):
    feed = FixtureFeed()
    app = make_app(settings, event_feed=feed)
    session = app.state.sessions.create_session(42)

    class RevokingBroker(EventBroker):
        async def events(self):
            yield {"observer_epoch": self.observer_epoch, "cursor": 5, "kind": "invalidate"}
            app.state.sessions.delete_session(session["cookie_token"])
            yield {"observer_epoch": self.observer_epoch, "cursor": 6, "kind": "invalidate"}

    feed.broker = RevokingBroker()
    with TestClient(app, base_url="https://hub.test") as client:
        client.cookies.set(settings.auth_config.cookie_name, session["cookie_token"])
        response = client.get(BASE + "/events")
        assert response.status_code == 200
        frames = [frame for frame in response.text.split("\n\n") if frame]
        assert len(frames) == 1
        assert json.loads(frames[0].removeprefix("data: "))["cursor"] == 5
        assert client.get(BASE + "/events/status").status_code == 401
