"""Readonly canonical registry wire trust and bounded transport tests."""
import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import json

import httpx
import pytest
from pydantic import ValidationError

from engineering_hub.registry_client import (
    RegistryAccessDenied, RegistryClient, RegistryPointer, RegistrySnapshot,
    RegistrySnapshotRequired, RegistrySourceUnavailable, RegistryUnavailable,
    parse_event, validate_origin,
)

UUID1 = "00000000-0000-4000-8000-000000000001"
UUID2 = "00000000-0000-4000-8000-000000000002"
NOW = "2026-10-18T00:00:00Z"


def peer(**changes):
    return dict({"agent": "hermes", "status": "reachable", "reason": "session_ready",
                 "link_state": "linked", "transport_state": "attached", "availability_state": "available",
                 "evidence_level": "runtime_verified", "binding_revision": UUID1,
                 "attachment_revision": UUID2, "last_evidence_at": NOW,
                 "valid_until": "2026-10-18T00:02:00Z"}, **changes)


def snapshot(**changes):
    return dict({"topic": "engineering-hub", "registry_epoch": UUID1, "snapshot_revision": UUID2,
                 "as_of": NOW, "complete": True, "source_state": "active", "peers": [peer()]}, **changes)


def test_snapshot_is_immutable_and_actual_utc():
    obj = RegistrySnapshot.model_validate(snapshot(as_of="2026-10-18T00:00:00+00:00"))
    assert obj.as_of == datetime(2026, 10, 18, tzinfo=timezone.utc)
    assert isinstance(obj.peers, tuple)
    with pytest.raises(ValidationError):
        obj.complete = False
    with pytest.raises(ValidationError):
        obj.peers[0].status = "offline"


@pytest.mark.parametrize("bad", [
    {"private_session_ref": "secret"}, {"topic": "Topic"}, {"topic": "x/y"},
    {"registry_epoch": "abcdefab-0000-4000-8000-00000000000A"},
    {"registry_epoch": "{00000000-0000-4000-8000-000000000001}"},
    {"registry_epoch": None}, {"snapshot_revision": None}, {"complete": 1},
    {"as_of": "2026-10-18T00:00:00"}, {"as_of": "2026-10-18T01:00:00+01:00"},
    {"as_of": 1792281600}, {"as_of": "2026-10-18 00:00:00Z"},
    {"as_of": "2026-10-18T00:00:00-00:00"},
    {"peers": [peer(), peer()]},
    {"peers": [peer(agent="codex"), peer(agent="hermes"), peer(agent="claude"), peer()]},
    {"as_of": "2026-10-17T23:59:59Z"},
    {"as_of": "2026-10-18T00:02:00Z"},
    {"source_state": "degraded"},
])
def test_reject_bad_snapshot(bad):
    with pytest.raises(ValidationError):
        RegistrySnapshot.model_validate(snapshot(**bad))


@pytest.mark.parametrize("bad", [
    {"private_session_ref": "secret"}, {"agent": "other"}, {"reason": "anything"},
    {"binding_revision": None}, {"attachment_revision": None}, {"last_evidence_at": None},
    {"valid_until": None}, {"valid_until": "2026-10-18T00:02:01Z"},
    {"valid_until": "2026-10-17T23:59:59Z"}, {"last_evidence_at": "2026-10-18T00:00:01Z"},
    {"link_state": "unknown"}, {"transport_state": "unverified"},
    {"availability_state": "busy"}, {"evidence_level": "observed"},
    {"status": "linked_unverified"}, {"status": "unknown_stale"}, {"reason": "stream_only"},
])
def test_reject_bad_readiness(bad):
    with pytest.raises(ValidationError):
        RegistrySnapshot.model_validate(snapshot(peers=[peer(**bad)]))


def unknown(**changes):
    return peer(status="unknown_stale", reason="observer_unavailable", transport_state="unknown",
                availability_state="unknown", evidence_level="unverified", valid_until=None, **changes)


@pytest.mark.parametrize("source", ["degraded", "unconfigured", "stopped"])
def test_nonactive_source_has_only_unknown(source):
    assert RegistrySnapshot.model_validate(snapshot(source_state=source, registry_epoch=None,
                                                    snapshot_revision=None, peers=[unknown()]))
    for field, value in [("status", "linked_unverified"), ("reason", "stream_only"),
                         ("transport_state", "attached"), ("availability_state", "offline"),
                         ("evidence_level", "observed"), ("valid_until", "2026-10-18T00:01:00Z")]:
        bad = unknown()
        bad[field] = value
        with pytest.raises(ValidationError):
            RegistrySnapshot.model_validate(snapshot(source_state=source, peers=[bad]))


def test_unlinked_requires_complete_active_inventory():
    p = peer(status="unlinked", reason="no_exact_binding", link_state="unlinked",
             transport_state="unknown", availability_state="unknown", evidence_level="unverified",
             attachment_revision=None, last_evidence_at=None, valid_until=None)
    assert RegistrySnapshot.model_validate(snapshot(peers=[p]))
    with pytest.raises(ValidationError):
        RegistrySnapshot.model_validate(snapshot(complete=False, peers=[p]))
    p["binding_revision"] = None
    assert RegistrySnapshot.model_validate(snapshot(peers=[p]))


def test_busy_offline_and_stream_only_are_not_equivalent():
    assert RegistrySnapshot.model_validate(snapshot(peers=[peer(status="busy_queued", reason="session_busy_queue_verified", availability_state="busy")]))
    p = peer(status="offline", reason="session_disconnected", transport_state="disconnected",
             availability_state="offline", evidence_level="observed", valid_until=None)
    assert RegistrySnapshot.model_validate(snapshot(peers=[p]))
    p["transport_state"] = "attached"
    with pytest.raises(ValidationError):
        RegistrySnapshot.model_validate(snapshot(peers=[p]))
    p = peer(status="linked_unverified", reason="stream_only", availability_state="unverified",
             evidence_level="observed", valid_until=None)
    assert RegistrySnapshot.model_validate(snapshot(peers=[p]))


@pytest.mark.parametrize("origin", ["http://localhost:8765", "http://192.168.50.80:8765",
    "ftp://host", "https://host/path", "https://user:password@host", "https://host?q=x",
    "https://host#x", "https://host\\evil", "https://host:bad", "https://host\n", "", None])
def test_bad_origins(origin):
    with pytest.raises(ValueError, match="invalid registry origin"):
        RegistryClient(origin)


@pytest.mark.parametrize("origin", ["https://registry.example:8765", "http://127.0.0.1:8765", "http://[::1]:8765", "http://127.1.2.3:8765"])
def test_pinned_immutable_origin(origin):
    client = RegistryClient(origin + "/")
    assert client.base_url == origin
    with pytest.raises(FrozenInstanceError):
        client.base_url = "https://other.example"


async def test_fetch_uses_request_identity_without_cache_or_impersonation():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=snapshot())
    client = RegistryClient("https://registry.example", transport=httpx.MockTransport(handle))
    for bearer in ["first-reader", "second-reader"]:
        result = await client.fetch_snapshot("engineering-hub", bearer)
        assert result.topic == "engineering-hub"
    assert len(requests) == 2
    assert [r.headers["authorization"] for r in requests] == ["Bearer first-reader", "Bearer second-reader"]
    assert all(str(r.url) == "https://registry.example/api/topic-registry/v1/topics/engineering-hub/snapshot" for r in requests)
    assert all("x-act-as" not in r.headers and "cookie" not in r.headers for r in requests)
    assert "reader" not in repr(client)


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_are_not_unavailable_fallback(status):
    client = RegistryClient("https://registry.example", transport=httpx.MockTransport(lambda _: httpx.Response(status, text="secret-body")))
    with pytest.raises(RegistryAccessDenied) as error:
        await client.fetch_snapshot("engineering-hub", "secret-token")
    assert error.value.status_code == status
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 404, 429, 500, 503])
async def test_no_redirect_or_retry(status):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://evil.example"}, text="secret")
    client = RegistryClient("https://registry.example", transport=httpx.MockTransport(handle))
    with pytest.raises(RegistrySourceUnavailable):
        await client.fetch_snapshot("engineering-hub", "secret")
    assert len(calls) == 1


@pytest.mark.parametrize("content,headers", [
    (json.dumps(snapshot(topic="other")).encode(), {"content-type": "application/json"}),
    (b'{"topic":"engineering-hub","topic":"engineering-hub"}', {"content-type": "application/json"}),
    (b"x" * 65537, {"content-type": "application/json"}),
    (b"{}", {}), (b"{}", {"content-type": "text/html"}),
    (b"\xff", {"content-type": "application/json"}),
    (b'{"x":NaN}', {"content-type": "application/json"}),
], ids=["wrong-topic", "duplicate", "oversize", "no-type", "html", "utf8", "nan"])
async def test_bad_source_is_unavailable_without_response_reflection(content, headers):
    client = RegistryClient("https://registry.example", transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content, headers=headers)))
    with pytest.raises(RegistrySourceUnavailable) as error:
        await client.fetch_snapshot("engineering-hub", "private-token")
    assert str(error.value) == "topic registry source unavailable"
    assert error.value.__context__ is None or error.value.__suppress_context__


async def test_total_deadline_is_bounded(monkeypatch):
    import engineering_hub.registry_client as module
    monkeypatch.setattr(module, "DEADLINE_SECONDS", 0.01)
    calls = []
    async def handle(request):
        calls.append(request)
        await asyncio.sleep(1)
        return httpx.Response(200, json=snapshot())
    client = RegistryClient("https://registry.example", transport=httpx.MockTransport(handle))
    with pytest.raises(RegistrySourceUnavailable):
        await client.fetch_snapshot("engineering-hub", "secret")
    assert len(calls) == 1


async def test_network_errors_are_sanitized():
    def handle(request):
        raise httpx.ConnectError("secret-token https://internal.example", request=request)
    client = RegistryClient("https://registry.example", transport=httpx.MockTransport(handle))
    with pytest.raises(RegistrySourceUnavailable) as error:
        await client.fetch_snapshot("engineering-hub", "secret")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("topic", ["../private", "a/b", "a?x=y", "a" * 129, "Upper", "", None])
async def test_bad_topic_never_sent(topic):
    def handle(_):
        pytest.fail("invalid topic sent")
    client = RegistryClient("https://registry.example", transport=httpx.MockTransport(handle))
    with pytest.raises(ValueError):
        await client.fetch_snapshot(topic, "secret")


@pytest.mark.parametrize("bearer", ["", None, "a\r\nb", "a b", "x" * 8193])
async def test_invalid_bearer_never_sent(bearer):
    def handle(_):
        pytest.fail("invalid credential sent")
    client = RegistryClient("https://registry.example", transport=httpx.MockTransport(handle))
    with pytest.raises(RegistryAccessDenied):
        await client.fetch_snapshot("engineering-hub", bearer)


@pytest.mark.parametrize("event,payload,model", [
    ("registry.invalidated", {"topic": "engineering-hub", "registry_epoch": UUID1, "snapshot_revision": UUID2, "cursor": 0}, RegistryPointer),
    ("registry.snapshot_required", {"topic": "engineering-hub", "registry_epoch": UUID1, "head": 2, "reason": "initial_snapshot"}, RegistrySnapshotRequired),
    ("registry.unavailable", {"topic": "engineering-hub", "registry_epoch": None, "reason": "observer_unavailable"}, RegistryUnavailable),
])
def test_event_models(event, payload, model):
    assert isinstance(parse_event(event, json.dumps(payload)), model)
    with pytest.raises(RegistrySourceUnavailable):
        parse_event(event, json.dumps(dict(payload, private_session_ref="secret")))
    for key in payload:
        incomplete = payload.copy()
        del incomplete[key]
        with pytest.raises(RegistrySourceUnavailable):
            parse_event(event, json.dumps(incomplete))


@pytest.mark.parametrize("cursor", [-1, 2**63, True, "1", 1.0])
def test_cursor_is_strict_bounded_integer(cursor):
    with pytest.raises(RegistrySourceUnavailable):
        parse_event("registry.invalidated", json.dumps({"topic": "x", "registry_epoch": UUID1, "snapshot_revision": UUID2, "cursor": cursor}))


def test_unknown_and_duplicate_events_fail_closed():
    with pytest.raises(RegistrySourceUnavailable):
        parse_event("other", "{}")
    with pytest.raises(RegistrySourceUnavailable):
        parse_event("registry.unavailable", '{"topic":"x","topic":"y","registry_epoch":null,"reason":"observer_unavailable"}')


async def test_no_environment_proxy_and_client_closed_each_request(monkeypatch):
    import engineering_hub.registry_client as module
    original = httpx.AsyncClient
    made = []
    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        result = original(**kwargs)
        made.append(result)
        return result
    monkeypatch.setattr(module.httpx, "AsyncClient", factory)
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.example")
    client = RegistryClient("https://registry.example", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=snapshot())))
    await client.fetch_snapshot("engineering-hub", "reader")
    await client.fetch_snapshot("engineering-hub", "reader")
    assert len(made) == 2 and all(c.is_closed for c in made)
    assert client.source_identity == "https://registry.example"
    await client.aclose()


@pytest.mark.parametrize("mode", ["oversize", "slow", "disconnect"])
async def test_bounded_body_stream_and_deadline(monkeypatch, mode):
    import engineering_hub.registry_client as module
    monkeypatch.setattr(module, "DEADLINE_SECONDS", .01)
    closed = []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b" " * 32768
            if mode == "slow":
                await asyncio.sleep(1)
            elif mode == "disconnect":
                raise httpx.ReadError("private source path and token")
            yield b" " * 32768
            yield b" "
        async def aclose(self):
            closed.append(True)
    client = RegistryClient("https://registry.example", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "application/json"}, stream=Body())))
    with pytest.raises(RegistrySourceUnavailable):
        await client.fetch_snapshot("engineering-hub", "reader")
    assert closed == [True]


async def test_compressed_body_refused_before_read():
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("compressed response consumed")
            yield b""
    client = RegistryClient("https://registry.example", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "application/json", "content-encoding": "gzip"}, stream=Body())))
    with pytest.raises(RegistrySourceUnavailable):
        await client.fetch_snapshot("engineering-hub", "reader")


@pytest.mark.parametrize("payload", [
    b'{"peers":[{"agent":"hermes","agent":"codex"}]}',
    b"[" * 2000 + b"]" * 2000,
    b"x" * 65537,
], ids=["nested-duplicate", "recursion", "oversize"])
def test_event_bounded_strict_json(payload):
    with pytest.raises(RegistrySourceUnavailable):
        parse_event("registry.invalidated", payload)
