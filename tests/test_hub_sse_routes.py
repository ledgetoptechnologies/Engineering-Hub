"""Actual SSE endpoint iterators; synthetic verified identity, not live JWT proof.

No ASGI lifespan, remote registry, native OS watcher or processing worker starts.
"""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.cloudflare_access import CloudflareIdentity
from engineering_hub.events import EventBroker
from engineering_hub.registry_bridge import CanonicalRegistryBridge, HumanRegistryAccess
from engineering_hub.registry_client import RegistryPointer, RegistrySnapshot
from engineering_hub.sse_keepalive import with_keepalive


TOPIC = "engineering-hub"
EPOCH = "00000000-0000-4000-8000-000000000001"
REVISION = "00000000-0000-4000-8000-000000000002"


class RegistryFixture:
    source_identity = "synthetic-registry"

    def __init__(self):
        self.snapshots = 0
        self.subscriptions = 0
        self.reads = 0
        self.active = False
        self.closed = False
        self.queue = asyncio.Queue()

    async def fetch_snapshot(self, topic, bearer):
        assert topic == TOPIC and bearer == "synthetic-reader"
        self.snapshots += 1
        return RegistrySnapshot.model_validate_json(json.dumps({
            "topic": TOPIC, "registry_epoch": EPOCH, "snapshot_revision": REVISION,
            "as_of": datetime.now(timezone.utc).isoformat(), "complete": True,
            "source_state": "active", "peers": [],
        }))

    async def events(self, topic, bearer, *, after, epoch, check_access):
        assert topic == TOPIC and bearer == "synthetic-reader"
        assert after is None and epoch is None
        self.subscriptions += 1
        self.active = True
        try:
            check_access()
            yield RegistryPointer(topic=TOPIC, registry_epoch=EPOCH,
                                  snapshot_revision=REVISION, cursor=7)
            while True:
                self.reads += 1
                event = await self.queue.get()
                check_access()
                yield event
        finally:
            self.active = False
            self.closed = True


@pytest.fixture
def setup_hub(tmp_path, monkeypatch):
    monkeypatch.setattr("engineering_hub.app.with_keepalive",
                        lambda source: with_keepalive(source, interval=.005))

    def make(kind):
        config = AuthConfig(
            "https://hub.example", human_auth_mode="cloudflare_access",
            cloudflare_issuer="https://synthetic-team.cloudflareaccess.com",
            cloudflare_audience="synthetic-eh-audience",
            cloudflare_allowed_emails=("operator@first.example", "operator@second.example"),
        )
        subject = "synthetic-human-subject"
        actor = "human:cf:" + hashlib.sha256("\0".join((
            config.cloudflare_issuer, config.cloudflare_audience, subject,
        )).encode()).hexdigest()
        identity = CloudflareIdentity(subject, config.cloudflare_allowed_emails[0], actor, 2000)
        broker = EventBroker()
        broker.activate(11)
        feed = SimpleNamespace(broker=broker, available=True)
        registry = RegistryFixture()
        reader_calls = []

        def reader(principal, topic):
            assert principal == actor and topic == TOPIC
            reader_calls.append((principal, topic))
            return HumanRegistryAccess("synthetic-reader", frozenset({TOPIC}))

        bridge = CanonicalRegistryBridge(registry, stream=registry, human_reader=reader)
        settings = Settings(tmp_path / (kind + ".db"),
                            AuthSettings("fixture-hermes", "fixture-codex", claude_token="fixture-claude"), config)
        app = create_app(settings, event_feed=feed, registry_bridge=bridge)
        store = app.state.sessions
        clock = [1000]
        store.clock = lambda: clock[0]
        session = store.create_cloudflare_session(config, identity)
        path = "/api/hub/v1/events" if kind == "native" else "/api/hub/v1/topics/{topic}/events"
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == path)
        request = Request({"type": "http", "method": "GET", "path": path,
                           "scheme": "https", "server": ("hub.example", 443),
                           "headers": [(b"cookie", (config.cookie_name + "=" + session["cookie_token"]).encode())],
                           "query_string": b"", "state": {"cloudflare_identity": identity}})
        return SimpleNamespace(app=app, store=store, clock=clock, session=session,
                               endpoint=endpoint, request=request, broker=broker,
                               registry=registry, readers=reader_calls, kind=kind)

    return make


async def open_response(hub):
    if hub.kind == "native":
        return await hub.endpoint(hub.request)
    return await hub.endpoint(TOPIC, hub.request, after=None, epoch=None)


def assert_unsubscribed(hub):
    if hub.kind == "native":
        assert hub.broker.subscriber_count == 0
    else:
        assert hub.registry.closed and not hub.registry.active


@pytest.mark.parametrize("kind", ["native", "registry"])
@pytest.mark.parametrize("revoke", ["delete", "expire"])
@pytest.mark.asyncio
async def test_actual_idle_route_rechecks_session_and_closes(kind, revoke, setup_hub):
    hub = setup_hub(kind)
    response = await open_response(hub)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    body = response.body_iterator
    try:
        initial = await anext(body)
        assert "data:" in initial
        if kind == "native":
            assert json.loads(initial[6:])["cursor"] == 11
            assert hub.broker.subscriber_count == 1
        else:
            assert f"id: {EPOCH}:{TOPIC}:7\n" in initial
        for _ in range(3):
            assert await anext(body) == ": keepalive\n\n"
        if kind == "native":
            assert hub.broker.cursor == 11 and hub.broker.subscriber_count == 1
        else:
            assert hub.registry.snapshots == 1 and hub.registry.subscriptions == 1
            assert hub.registry.reads == 1 and len(hub.readers) == 2
        if revoke == "delete":
            hub.store.delete_cloudflare_session(hub.session["cookie_token"])
        else:
            hub.clock[0] = 2001
        with pytest.raises(StopAsyncIteration):
            await anext(body)
        assert_unsubscribed(hub)
    finally:
        await body.aclose()


@pytest.mark.parametrize("kind", ["native", "registry"])
@pytest.mark.asyncio
async def test_actual_route_cancellation_closes_original_subscription(kind, setup_hub, monkeypatch):
    # Keep cancellation independent of machine scheduling speed; no five-second
    # wait actually occurs because this test explicitly cancels the read.
    monkeypatch.setattr("engineering_hub.app.with_keepalive",
                        lambda source: with_keepalive(source, interval=5))
    hub = setup_hub(kind)
    body = (await open_response(hub)).body_iterator
    await anext(body)
    pending = asyncio.create_task(anext(body))
    # No timer needed: allow route/helper/source tasks to reach their await.
    for _ in range(10):
        await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await body.aclose()
    assert_unsubscribed(hub)


@pytest.mark.parametrize("kind", ["native", "registry"])
@pytest.mark.asyncio
async def test_heartbeat_then_real_event_preserves_only_real_cursor(kind, setup_hub):
    hub = setup_hub(kind)
    body = (await open_response(hub)).body_iterator
    try:
        await anext(body)
        assert await anext(body) == ": keepalive\n\n"
        if kind == "native":
            hub.broker.publish(12)
        else:
            await hub.registry.queue.put(RegistryPointer(
                topic=TOPIC, registry_epoch=EPOCH, snapshot_revision=REVISION, cursor=8))
        frame = await anext(body)
        if kind == "native":
            assert json.loads(frame[6:])["cursor"] == 12
        else:
            assert f"id: {EPOCH}:{TOPIC}:8\n" in frame
            assert "event: registry.invalidated\n" in frame
            assert hub.registry.snapshots == 1 and hub.registry.subscriptions == 1
    finally:
        await body.aclose()
    assert_unsubscribed(hub)
