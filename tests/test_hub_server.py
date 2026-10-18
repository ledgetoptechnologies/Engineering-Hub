"""Real loopback sockets and failure cleanup, never external hosts or secrets."""
import asyncio
from contextlib import asynccontextmanager
import socket

import httpx
import pytest

from engineering_hub.app import create_app
from engineering_hub.frontends import HubFrontends
from engineering_hub import server as runner
from tests.test_hub_frontends import config, checker, Feed, SIGNED, CODEX, BASE, ORIGIN


@pytest.mark.asyncio
async def test_real_sockets_share_one_core_and_enforce_roles(config):
    feed = Feed()
    core = create_app(config, event_feed=feed)
    pair = HubFrontends(core)
    stop = asyncio.Event()
    addresses = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(runner.serve_frontends(
        lambda: pair, agent_host="127.0.0.1", agent_port=0, browser_port=0,
        stop_event=stop, install_signals=False,
        on_ready=lambda a, b: addresses.set_result((a, b))))
    try:
        agent_address, browser_address = await asyncio.wait_for(addresses, 5)
        assert agent_address[0] == browser_address[0] == "127.0.0.1"
        assert agent_address[1] != browser_address[1]
        agent_url = f"http://127.0.0.1:{agent_address[1]}"
        browser_url = f"http://127.0.0.1:{browser_address[1]}"
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                     headers={"Host": "hub.test"}) as client:
            assert (await client.get(agent_url + "/")).status_code == 403
            assert (await client.get(agent_url + "/", headers={**CODEX, **SIGNED})).status_code == 403
            assert (await client.get(browser_url + "/")).status_code == 401
            assert (await client.get(browser_url + "/", headers=SIGNED)).text == "private-dashboard"
            assert (await client.get(browser_url + "/mcp/", headers=CODEX)).status_code == 403
            login = await client.get(browser_url + BASE + "/auth/login", headers=SIGNED)
            assert login.status_code == 303
            # The local HTTP hop represents cloudflared. A browser sends this
            # Secure cookie to public HTTPS, not directly to the test HTTP hop.
            cookie = client.cookies.get(config.auth_config.cookie_name)
            browser_headers = {**SIGNED, "Cookie": config.auth_config.cookie_name + "=" + cookie}
            topic = await client.get(browser_url + BASE + "/topics/ops:alerts/peers", headers=browser_headers)
            assert topic.status_code == 200 and topic.json()["topic"] == "ops:alerts"
            encoded = await client.get(browser_url + BASE + "/topics/ops%3Aalerts/peers", headers=browser_headers)
            assert encoded.status_code == 403  # Keep the raw-path boundary strict.
            me = (await client.get(browser_url + BASE + "/auth/me", headers=browser_headers)).json()
            csrf = {**browser_headers, "Origin": ORIGIN, "X-Hub-CSRF": me["csrf_token"]}
            created = await client.post(browser_url + BASE + "/tasks", headers=csrf, json={
                "project": "treedar", "title": "one socket owner", "client_request_id": "socket-one"})
            assert created.status_code == 201
            seen = await client.get(agent_url + BASE + "/tasks/" + created.json()["id"], headers=CODEX)
            assert seen.json() == created.json()
            assert (await client.get(agent_url + BASE + "/tasks", headers=browser_headers)).status_code == 401
            assert (await client.get(browser_url + BASE + "/tasks", headers=CODEX)).status_code == 403
            assert feed.starts == 1 and feed.stops == 0
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert feed.starts == feed.stops == 1 and pair.state == "closed"


class Pair:
    def __init__(self):
        self.starts = self.stops = 0
        self.agents, self.browser = object(), object()

    @asynccontextmanager
    async def lifespan(self):
        self.starts += 1
        try:
            yield self
        finally:
            self.stops += 1


@pytest.mark.asyncio
async def test_second_bind_failure_never_starts_core_and_closes_first_socket(monkeypatch):
    pair = Pair()
    constructions = []
    def factory():
        constructions.append(True)
        return pair
    created = []
    original = runner._bind

    def track(host, port):
        result = original(host, port)
        created.append(result)
        return result

    monkeypatch.setattr(runner, "_bind", track)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        with pytest.raises(OSError):
            await runner.serve_frontends(factory, agent_host="127.0.0.1", agent_port=0,
                browser_port=busy.getsockname()[1], install_signals=False)
    assert pair.starts == pair.stops == 0
    assert constructions == []
    assert len(created) == 1 and created[0].fileno() == -1


@pytest.mark.asyncio
@pytest.mark.parametrize("host,agent,browser", [
    ("localhost", 8770, 8771), ("127.0.0.1", 8770, 8770),
    ("127.0.0.1", True, 8771), ("127.0.0.1", 8770, 65536),
])
async def test_bad_bind_plan_does_not_start_owner(host, agent, browser):
    pair = Pair()
    with pytest.raises(ValueError):
        await runner.serve_frontends(lambda: pair, agent_host=host, agent_port=agent,
                                     browser_port=browser, install_signals=False)
    assert pair.starts == pair.stops == 0


class FakeListener:
    def __init__(self, *, exits=False, release=None):
        self.should_exit = self.started = self.closed = False
        self.exits, self.release = exits, release

    async def serve(self, sockets):
        self.started = True
        try:
            if self.exits:
                return
            while not self.should_exit:
                await asyncio.sleep(0.01)
            if self.release:
                await self.release.wait()
        finally:
            self.closed = True


@pytest.mark.asyncio
async def test_one_listener_exit_drains_other_before_owner_stops(monkeypatch):
    pair = Pair()
    listeners = [FakeListener(exits=True), FakeListener()]
    pending = iter(listeners)
    monkeypatch.setattr(runner, "_listener", lambda app: next(pending))
    with pytest.raises(RuntimeError, match="stopped unexpectedly"):
        await runner.serve_frontends(lambda: pair, agent_host="127.0.0.1", agent_port=0,
                                     browser_port=0, install_signals=False)
    assert pair.starts == pair.stops == 1 and all(x.closed for x in listeners)


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_listeners_before_owner_release(monkeypatch):
    pair = Pair()
    release, ready = asyncio.Event(), asyncio.Event()
    listeners = [FakeListener(release=release), FakeListener(release=release)]
    pending = iter(listeners)
    monkeypatch.setattr(runner, "_listener", lambda app: next(pending))
    task = asyncio.create_task(runner.serve_frontends(lambda: pair, agent_host="127.0.0.1",
        agent_port=0, browser_port=0, install_signals=False, on_ready=lambda *_: ready.set()))
    await asyncio.wait_for(ready.wait(), 3)
    task.cancel()
    await asyncio.sleep(0.04)
    task.cancel()
    await asyncio.sleep(0.04)
    assert pair.stops == 0 and not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert pair.stops == 1 and all(x.closed for x in listeners)


@pytest.mark.asyncio
async def test_listener_construction_failure_releases_started_owner(monkeypatch):
    pair = Pair()
    def fail(app):
        raise ValueError("invalid listener configuration")
    monkeypatch.setattr(runner, "_listener", fail)
    with pytest.raises(ValueError, match="invalid listener configuration"):
        await runner.serve_frontends(lambda: pair, agent_host="127.0.0.1",
            agent_port=0, browser_port=0, install_signals=False)
    assert pair.starts == pair.stops == 1


def test_listener_configuration_has_no_independent_owner_or_proxy_trust(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "not-an-integer")
    listener = runner._listener(object())
    assert listener.config.lifespan == "off"
    assert listener.config.proxy_headers is False
    assert listener.config.access_log is False
    assert listener.config.workers == 1 and listener.config.reload is False
