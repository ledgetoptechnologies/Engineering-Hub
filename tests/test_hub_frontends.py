"""One-core front doors: lifecycle and HTTP contracts, not network acceptance."""
import asyncio
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.cloudflare_access import AccessDenied, CloudflareIdentity
from engineering_hub.events import EventBroker
from engineering_hub.frontends import HubFrontends

BASE = "/api/hub/v1"
ORIGIN = "https://hub.test"
SIGNED = {"CF-Access-Jwt-Assertion": "signed-fixture"}
CODEX = {"Authorization": "Bearer fixture-codex"}


@pytest.fixture
def config(tmp_path):
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("private-dashboard")
    return Settings(tmp_path / "hub.db", AuthSettings("fixture-hermes", "fixture-codex",
                    claude_token="fixture-claude"), AuthConfig(ORIGIN,
                    human_auth_mode="cloudflare_access", cloudflare_issuer="https://hub-test.cloudflareaccess.com",
                    cloudflare_audience="hub-only", cloudflare_allowed_emails=("beau@example.com",)), ui)


@pytest.fixture(autouse=True)
def checker(monkeypatch):
    class Checker:
        def __init__(self, config):
            pass

        async def verify(self, token):
            if token != "signed-fixture":
                raise AccessDenied()
            return CloudflareIdentity("subject", "beau@example.com", "human:cf:" + "a" * 64,
                                      int(time.time()) + 600)

    monkeypatch.setattr("engineering_hub.ingress.CloudflareAccessVerifier", Checker)
    monkeypatch.setattr("engineering_hub.cloudflare_access.CloudflareAccessVerifier", Checker)


class Feed:
    def __init__(self, *, fail_start=False, fail_stop=False):
        self.broker = EventBroker()
        self.starts = self.stops = 0
        self.fail_start, self.fail_stop = fail_start, fail_stop

    @property
    def available(self):
        return self.broker.available

    async def start(self):
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("private startup failure")
        self.broker.activate(0)

    async def stop(self):
        self.stops += 1
        self.broker.fail()
        if self.fail_stop:
            raise RuntimeError("private shutdown failure")


def clients(pair):
    return (httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.browser), base_url=ORIGIN,
                              follow_redirects=False),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.agents), base_url=ORIGIN,
                              follow_redirects=False))


def test_constructor_requires_approved_existing_combined_core(config):
    with pytest.raises(ValueError, match="FastAPI"):
        HubFrontends(object())
    with pytest.raises(ValueError, match="configured Cloudflare"):
        HubFrontends(FastAPI())
    with pytest.raises(ValueError, match="configured Cloudflare"):
        HubFrontends(create_app(replace(config, ingress_mode="agents")))
    with pytest.raises(ValueError, match="configured Cloudflare"):
        HubFrontends(create_app(replace(config, auth_config=AuthConfig(ORIGIN))))
    core = create_app(config)
    pair = HubFrontends(core)
    assert pair.core is core and core.state.hub_frontends is pair
    with pytest.raises(ValueError, match="already has"):
        HubFrontends(core)


@pytest.mark.asyncio
async def test_both_frontdoors_share_repository_sessions_feed_and_task_claim(config):
    feed = Feed()
    core = create_app(config, event_feed=feed)
    pair = HubFrontends(core)
    repo, sessions = core.state.repository, core.state.sessions
    async with pair.lifespan():
        assert pair.state == "active" and feed.starts == 1
        async with clients(pair)[0] as browser, clients(pair)[1] as agent:
            assert (await browser.get("/")).status_code == 401
            assert (await browser.get("/", headers=SIGNED)).text == "private-dashboard"
            assert (await agent.get("/", headers=CODEX)).status_code == 403
            assert (await agent.get(BASE + "/auth/login", headers=CODEX)).status_code == 403
            assert (await browser.get("/mcp/", headers=SIGNED)).status_code == 403
            assert (await browser.get(BASE + "/overview", headers=CODEX)).status_code == 403
            assert (await browser.get(BASE + "/auth/login", headers=SIGNED)).status_code == 303
            me = (await browser.get(BASE + "/auth/me", headers=SIGNED)).json()
            csrf = {**SIGNED, "Origin": ORIGIN, "X-Hub-CSRF": me["csrf_token"]}
            payload = {"project": "treedar", "title": "shared task", "client_request_id": "shared-1"}
            assert (await browser.post(BASE + "/tasks", headers=SIGNED, json=payload)).status_code == 403
            created = await browser.post(BASE + "/tasks", headers=csrf, json=payload)
            assert created.status_code == 201
            task = created.json()
            seen = await agent.get(BASE + "/tasks/" + task["id"], headers=CODEX)
            assert seen.json() == task
            claimed = await agent.post(BASE + "/tasks/" + task["id"] + "/claim", headers=CODEX,
                                       json={"expected_revision": task["revision"]})
            assert claimed.status_code == 200 and claimed.json()["owner"] == "codex"
            human_read = await browser.get(BASE + "/tasks/" + task["id"], headers=SIGNED)
            assert human_read.json()["owner"] == "codex"
            assert repo is core.state.repository and sessions is core.state.sessions
            assert pair.agents.ingress.app.state.event_feed is feed
            assert pair.browser.ingress.app.state.event_feed is feed
            assert (await browser.get("/health")).json() == (await agent.get("/health")).json()
            assert (await browser.post(BASE + "/auth/logout", headers=csrf)).status_code == 200
            assert (await browser.get(BASE + "/overview", headers=SIGNED)).status_code == 401
            assert (await agent.get(BASE + "/overview", headers=CODEX)).status_code == 200
    assert feed.starts == feed.stops == 1 and pair.state == "closed"


@pytest.mark.asyncio
async def test_frontdoors_unavailable_before_start_and_after_shutdown(config):
    pair = HubFrontends(create_app(config, event_feed=Feed()))
    async with clients(pair)[0] as browser, clients(pair)[1] as agent:
        for client in (browser, agent):
            assert (await client.get("/health")).status_code == 503
            assert (await client.get(BASE + "/overview", headers=CODEX)).status_code == 503
        async with pair.lifespan():
            assert (await browser.get("/health")).status_code == 200
        for client in (browser, agent):
            response = await client.get("/", headers=SIGNED)
            assert response.status_code == 503 and "private" not in response.text


@pytest.mark.asyncio
async def test_frontdoors_unavailable_during_startup_and_teardown(config):
    entered_start, release_start = asyncio.Event(), asyncio.Event()
    entered_stop, release_stop = asyncio.Event(), asyncio.Event()
    release_body = asyncio.Event()
    core = create_app(config)

    @asynccontextmanager
    async def slow_lifespan(app):
        entered_start.set()
        await release_start.wait()
        try:
            yield
        finally:
            entered_stop.set()
            await release_stop.wait()

    core.router.lifespan_context = slow_lifespan
    pair = HubFrontends(core)

    async def owner():
        async with pair.lifespan():
            await release_body.wait()

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(entered_start.wait(), 2)
        assert pair.state == "starting"
        async with clients(pair)[0] as browser:
            assert (await browser.get("/health")).status_code == 503
            with pytest.raises(RuntimeError, match="one-use"):
                async with pair.lifespan():
                    pytest.fail("concurrent startup")
            release_start.set()
            release_body.set()
            await asyncio.wait_for(entered_stop.wait(), 2)
            assert pair.state == "stopping"
            assert (await browser.get("/health")).status_code == 503
        release_stop.set()
        await task
        assert pair.state == "closed"
    finally:
        release_start.set()
        release_body.set()
        release_stop.set()
        await task


@pytest.mark.asyncio
async def test_lifecycle_one_use_nested_and_concurrent_rejected(config):
    feed = Feed()
    pair = HubFrontends(create_app(config, event_feed=feed))
    async with pair.lifespan():
        with pytest.raises(RuntimeError, match="one-use"):
            async with pair.lifespan():
                pytest.fail("nested lifecycle")

        async def competing():
            async with pair.lifespan():
                pytest.fail("competing lifecycle")

        with pytest.raises(RuntimeError, match="one-use"):
            await asyncio.create_task(competing())
    with pytest.raises(RuntimeError, match="one-use"):
        async with pair.lifespan():
            pytest.fail("restarted lifecycle")
    assert feed.starts == feed.stops == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_start,fail_stop", [(True, False), (False, True)])
async def test_startup_shutdown_failure_never_retries_or_serves(config, fail_start, fail_stop):
    feed = Feed(fail_start=fail_start, fail_stop=fail_stop)
    pair = HubFrontends(create_app(config, event_feed=feed))
    with pytest.raises(RuntimeError, match="private"):
        async with pair.lifespan():
            pass
    assert pair.state == "failed" and feed.starts == feed.stops == 1
    async with clients(pair)[0] as browser:
        response = await browser.get("/health")
        assert response.status_code == 503 and "private" not in response.text
    with pytest.raises(RuntimeError, match="one-use"):
        async with pair.lifespan():
            pytest.fail("failure restart")


@pytest.mark.asyncio
async def test_cancellation_propagates_and_closes_exactly_once(config):
    feed = Feed()
    pair = HubFrontends(create_app(config, event_feed=feed))
    entered = asyncio.Event()

    async def run():
        async with pair.lifespan():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pair.state == "failed" and feed.starts == feed.stops == 1


@pytest.mark.asyncio
async def test_frontdoor_lifespans_are_rejected_and_websockets_closed(config):
    feed = Feed()
    pair = HubFrontends(create_app(config, event_feed=feed))
    sent = []

    async def send(message):
        sent.append(message)

    for door in (pair.agents, pair.browser):
        with pytest.raises(RuntimeError, match="disable server lifespans"):
            await door({"type": "lifespan"}, None, send)
        await door({"type": "websocket"}, None, send)
    assert sent == [{"type": "websocket.close", "code": 1008}] * 2
    assert feed.starts == feed.stops == 0


@pytest.mark.asyncio
async def test_single_shared_lifespan_returns_shared_state(config):
    core = create_app(config)
    calls = []

    @asynccontextmanager
    async def lifespan(app):
        assert app is core
        calls.append("start")
        try:
            yield {"core": "one"}
        finally:
            calls.append("stop")

    core.router.lifespan_context = lifespan
    pair = HubFrontends(core)
    async with pair.lifespan() as active:
        assert active is pair
    assert calls == ["start", "stop"]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Actual Linux ownership lifecycle")
async def test_two_frontdoors_one_actual_pilot_owner_service_and_feed(config, tmp_path):
    from engineering_hub.container_pilot import ContainerPilotTarget
    from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError
    from engineering_hub.pilot_service import create_owned_pilot_app
    from tests.test_hub_container_pilot import pilot_manifest
    from tests.test_hub_owned_service import Client

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    config = replace(config, database=private / "hub.sqlite")
    owner = PilotOwner(config.database, allow_ephemeral_test_storage=True)
    client, feed = Client(), Feed()
    target = ContainerPilotTarget.from_manifest(pilot_manifest())
    try:
        core = create_owned_pilot_app(config, client, [target], owner=owner, event_feed=feed,
                                     supervisor_stop_grace_seconds=1200)
        pair = HubFrontends(core)
        service = core.state.owned_pilot_service
        async with pair.lifespan():
            async with clients(pair)[0] as browser, clients(pair)[1] as agent:
                assert (await browser.get("/health")).status_code == 200
                assert (await agent.get("/health")).status_code == 200
                owner.assert_active(config.database)
                with pytest.raises(PilotOwnershipError):
                    PilotOwner(config.database, allow_ephemeral_test_storage=True)
                assert service.status["state"] == "active"
                assert pair.agents.ingress.app.state.owned_pilot_service is service
                assert pair.browser.ingress.app.state.owned_pilot_service is service
                assert feed.starts == 1
        assert feed.stops == 1 and client.closed and service.status["state"] == "stopped"
        with PilotOwner(config.database, allow_ephemeral_test_storage=True):
            pass
    finally:
        owner.close()
