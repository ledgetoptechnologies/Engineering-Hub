"""Disposable Linux listeners: transport/lifecycle evidence, not host deployment."""
import asyncio
import inspect
import socket
import sys
from types import SimpleNamespace

import httpx
import pytest

from engineering_hub.app import create_app
from engineering_hub.frontends import HubFrontends
from engineering_hub.private_browser_socket import create_private_browser_client
from engineering_hub import split_server as runner
from tests.test_hub_frontends import config, checker, Feed, SIGNED, CODEX, BASE, ORIGIN

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Real Linux private listener")


@pytest.fixture
def endpoint(tmp_path):
    directory = tmp_path / "s"
    directory.mkdir(mode=0o700)
    return directory / "browser.sock"


async def close_runner(task, stop):
    stop.set()
    await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_real_split_listeners_share_human_sessions_and_original_agent_identity(config, endpoint):
    feed = Feed()
    core = create_app(config, event_feed=feed)
    pair = HubFrontends(core)
    sources = []

    @core.middleware("http")
    async def source(request, call_next):
        if request.headers.get("authorization") == CODEX["Authorization"]:
            sources.append(request.scope.get("client"))
        return await call_next(request)

    loop = asyncio.get_running_loop()
    private_ready, public_ready = loop.create_future(), loop.create_future()
    private_stop, public_stop = asyncio.Event(), asyncio.Event()
    private = asyncio.create_task(runner.serve_private_coordinator(
        lambda: pair, socket_path=endpoint, agent_host="127.0.0.1", agent_port=0,
        stop_event=private_stop, install_signals=False,
        on_ready=lambda address, path: private_ready.set_result((address, path))))
    public = None
    clients = []
    try:
        address, path = await asyncio.wait_for(private_ready, 5)
        assert str(path).endswith("/" + endpoint.name) and endpoint.exists()
        assert address[0] == "127.0.0.1"

        def client_factory():
            client = create_private_browser_client(endpoint)
            clients.append(client)
            return client

        public = asyncio.create_task(runner.serve_public_browser(
            client_factory, browser_port=0, stop_event=public_stop, install_signals=False,
            on_ready=lambda address: public_ready.set_result(address)))
        ui_address = await asyncio.wait_for(public_ready, 5)
        assert ui_address[0] == "127.0.0.1"
        agent_url = f"http://127.0.0.1:{address[1]}"
        ui_url = f"http://127.0.0.1:{ui_address[1]}"
        async with httpx.AsyncClient(trust_env=False, headers={"Host": "hub.test"}) as client:
            assert (await client.get(ui_url + "/")).status_code == 401
            assert (await client.get(ui_url + "/", headers=SIGNED)).text == "private-dashboard"
            assert (await client.get(ui_url + "/mcp/", headers=CODEX)).status_code == 403
            assert (await client.get(agent_url + "/", headers=SIGNED)).status_code == 403
            assert (await client.get(ui_url + BASE + "/auth/login", headers=SIGNED)).status_code == 303
            cookie = client.cookies.get(config.auth_config.cookie_name)
            human = {**SIGNED, "Cookie": config.auth_config.cookie_name + "=" + cookie}
            me = (await client.get(ui_url + BASE + "/auth/me", headers=human)).json()
            csrf = {**human, "Origin": ORIGIN, "X-Hub-CSRF": me["csrf_token"]}
            made = await client.post(ui_url + BASE + "/tasks", headers=csrf, json={
                "project": "treedar", "title": "split listener task", "client_request_id": "split-1"})
            assert made.status_code == 201
            target = BASE + "/tasks/" + made.json()["id"]
            seen = await client.get(agent_url + target, headers={**CODEX, "X-Forwarded-For": "203.0.113.10"})
            assert seen.json() == made.json()
            assert sources and sources[-1][0] == "127.0.0.1"
            claimed = await client.post(agent_url + target + "/claim", headers=CODEX,
                                       json={"expected_revision": made.json()["revision"]})
            assert claimed.status_code == 200 and claimed.json()["owner"] == "codex"
            assert (await client.get(ui_url + target, headers=human)).json() == claimed.json()
            assert (await client.get(ui_url + target, headers=CODEX)).status_code == 403
            assert feed.starts == 1 and feed.stops == 0
    finally:
        if public is not None:
            await close_runner(public, public_stop)
        await close_runner(private, private_stop)
    assert feed.starts == feed.stops == 1 and pair.state == "closed"
    assert clients and clients[0].is_closed and not endpoint.exists()


def test_public_bind_has_no_host_override():
    assert "browser_host" not in inspect.signature(runner.serve_public_browser).parameters
    assert "host" not in inspect.signature(runner.serve_public_browser).parameters


@pytest.mark.asyncio
@pytest.mark.parametrize("host,port", [("localhost", 8770), ("127.0.0.1", True), ("127.0.0.1", -1), ("127.0.0.1", 65536)])
async def test_bad_private_bind_plan_precedes_factory(endpoint, host, port):
    called = []
    with pytest.raises(ValueError):
        await runner.serve_private_coordinator(lambda: called.append(True), socket_path=endpoint,
            agent_host=host, agent_port=port, install_signals=False)
    assert not called and not endpoint.exists()


@pytest.mark.asyncio
async def test_private_busy_tcp_precedes_factory(endpoint):
    called = []
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        with pytest.raises(runner.SplitServerError):
            await runner.serve_private_coordinator(lambda: called.append(True), socket_path=endpoint,
                agent_host="127.0.0.1", agent_port=busy.getsockname()[1], install_signals=False)
    assert not called and not endpoint.exists()


@pytest.mark.asyncio
async def test_private_existing_socket_entry_preserved_before_factory(endpoint):
    endpoint.write_text("operator-owned")
    called = []
    with pytest.raises(RuntimeError):
        await runner.serve_private_coordinator(lambda: called.append(True), socket_path=endpoint,
            agent_host="127.0.0.1", agent_port=0, install_signals=False)
    assert not called and endpoint.read_text() == "operator-owned"


@pytest.mark.asyncio
@pytest.mark.parametrize("port", [True, -1, 65536])
async def test_public_invalid_port_precedes_client_factory(port):
    called = []
    with pytest.raises(ValueError):
        await runner.serve_public_browser(lambda: called.append(True), browser_port=port, install_signals=False)
    assert not called


@pytest.mark.asyncio
async def test_public_busy_port_precedes_client_factory():
    called = []
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        with pytest.raises(runner.SplitServerError):
            await runner.serve_public_browser(lambda: called.append(True), browser_port=busy.getsockname()[1], install_signals=False)
    assert not called


@pytest.mark.asyncio
async def test_private_wrong_factory_type_unlinks_owned_socket(endpoint):
    with pytest.raises(runner.SplitServerError):
        await runner.serve_private_coordinator(lambda: object(), socket_path=endpoint,
            agent_host="127.0.0.1", agent_port=0, install_signals=False)
    assert not endpoint.exists()


@pytest.mark.asyncio
async def test_public_wrong_factory_type_denied():
    with pytest.raises(runner.SplitServerError):
        await runner.serve_public_browser(lambda: object(), browser_port=0, install_signals=False)


@pytest.mark.asyncio
async def test_private_startup_callback_failure_drains_owner(config, endpoint):
    feed = Feed()
    pair = HubFrontends(create_app(config, event_feed=feed))

    def fail(*args):
        raise RuntimeError("readiness callback failed")

    with pytest.raises(runner.SplitServerError):
        await runner.serve_private_coordinator(lambda: pair, socket_path=endpoint,
            agent_host="127.0.0.1", agent_port=0, install_signals=False, on_ready=fail)
    assert feed.starts == feed.stops == 1 and not endpoint.exists()


@pytest.mark.asyncio
async def test_public_callback_failure_closes_owned_client():
    client = httpx.AsyncClient(trust_env=False)

    def fail(*args):
        raise RuntimeError("public readiness failed")

    with pytest.raises(runner.SplitServerError):
        await runner.serve_public_browser(lambda: client, browser_port=0, install_signals=False, on_ready=fail)
    assert client.is_closed


@pytest.mark.asyncio
async def test_public_upstream_failure_is_not_retried_and_client_closes(endpoint):
    attempts = []

    async def unavailable(request):
        attempts.append(request)
        raise httpx.ConnectError("private endpoint absent")

    client = httpx.AsyncClient(transport=httpx.MockTransport(unavailable), trust_env=False)
    ready, stop = asyncio.get_running_loop().create_future(), asyncio.Event()
    task = asyncio.create_task(runner.serve_public_browser(lambda: client, browser_port=0,
        stop_event=stop, install_signals=False, on_ready=lambda address: ready.set_result(address)))
    try:
        address = await asyncio.wait_for(ready, 5)
        async with httpx.AsyncClient(trust_env=False) as browser:
            response = await browser.get(f"http://127.0.0.1:{address[1]}/", headers={"Host": "hub.test"})
            assert response.status_code == 502
        assert len(attempts) == 1
    finally:
        await close_runner(task, stop)
    assert client.is_closed


@pytest.mark.asyncio
async def test_private_repeated_cancel_retains_feed_and_socket_until_accepted_request_drains(config, endpoint):
    feed = Feed()
    core = create_app(config, event_feed=feed)
    entered, release = asyncio.Event(), asyncio.Event()

    @core.middleware("http")
    async def hold_committed_response(request, call_next):
        response = await call_next(request)
        if request.method == "POST" and request.url.path == BASE + "/decisions":
            assert response.status_code == 201
            entered.set()
            await release.wait()
        return response

    pair = HubFrontends(core)
    ready, stop = asyncio.get_running_loop().create_future(), asyncio.Event()
    task = asyncio.create_task(runner.serve_private_coordinator(lambda: pair,
        socket_path=endpoint, agent_host="127.0.0.1", agent_port=0,
        stop_event=stop, install_signals=False, on_ready=lambda a, b: ready.set_result(a)))
    request = None
    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        try:
            address = await asyncio.wait_for(ready, 5)
            request = asyncio.create_task(client.post(f"http://127.0.0.1:{address[1]}" + BASE + "/decisions",
                headers={**CODEX, "Host": "hub.test"}, json={"project": "treedar", "title": "drain accepted mutation", "options": ["approve", "decline"]}))
            await asyncio.wait_for(entered.wait(), 3)
            decisions = core.state.repository.list_decisions()
            assert len(decisions) == 1 and decisions[0]["title"] == "drain accepted mutation"
            task.cancel()
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done() and endpoint.exists()
            assert feed.starts == 1 and feed.stops == 0 and pair.state == "stopping"
            release.set()
            assert (await request).status_code == 201
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        finally:
            release.set()
            stop.set()
            if request is not None:
                await asyncio.gather(request, return_exceptions=True)
            await asyncio.gather(task, return_exceptions=True)
    assert feed.stops == 1 and not endpoint.exists()


@pytest.mark.asyncio
async def test_public_repeated_cancel_waits_for_upstream_response_before_client_closes():
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, text="accepted response")

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(held), trust_env=False)
    ready, stop = asyncio.get_running_loop().create_future(), asyncio.Event()
    task = asyncio.create_task(runner.serve_public_browser(lambda: upstream, browser_port=0,
        stop_event=stop, install_signals=False, on_ready=lambda a: ready.set_result(a)))
    request = None
    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        try:
            address = await asyncio.wait_for(ready, 5)
            request = asyncio.create_task(client.get(f"http://127.0.0.1:{address[1]}/", headers={"Host": "hub.test"}))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done() and not upstream.is_closed
            release.set()
            assert (await request).text == "accepted response"
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        finally:
            release.set()
            stop.set()
            if request is not None:
                await asyncio.gather(request, return_exceptions=True)
            await asyncio.gather(task, return_exceptions=True)
    assert upstream.is_closed


class FakeListener:
    def __init__(self, *, exits=False):
        self.exits = exits
        self.started = self.should_exit = self.closed = False
        self.config = SimpleNamespace(timeout_graceful_shutdown=30)

    async def serve(self, sockets):
        self.started = True
        try:
            if self.exits:
                return
            while not self.should_exit:
                await asyncio.sleep(0.01)
        finally:
            self.closed = True


@pytest.mark.asyncio
async def test_private_unexpected_listener_exit_drains_other_before_feed_stop(config, endpoint, monkeypatch):
    feed = Feed()
    pair = HubFrontends(create_app(config, event_feed=feed))
    listeners = [FakeListener(exits=True), FakeListener()]
    pending = iter(listeners)
    monkeypatch.setattr(runner, "_listener", lambda app: next(pending))
    with pytest.raises(runner.SplitServerError):
        await runner.serve_private_coordinator(lambda: pair, socket_path=endpoint,
            agent_host="127.0.0.1", agent_port=0, install_signals=False)
    assert all(listener.closed for listener in listeners)
    assert feed.starts == feed.stops == 1 and not endpoint.exists()


@pytest.mark.asyncio
async def test_private_idle_event_subscription_does_not_deadlock_shutdown(config, endpoint):
    feed = Feed()
    pair = HubFrontends(create_app(config, event_feed=feed))
    ready, stop = asyncio.get_running_loop().create_future(), asyncio.Event()
    task = asyncio.create_task(runner.serve_private_coordinator(lambda: pair,
        socket_path=endpoint, agent_host="127.0.0.1", agent_port=0,
        stop_event=stop, install_signals=False, on_ready=lambda a, b: ready.set_result(a)))
    try:
        address = await asyncio.wait_for(ready, 5)
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            async with client.stream("GET", f"http://127.0.0.1:{address[1]}" + BASE + "/events",
                                     headers={**CODEX, "Host": "hub.test"}) as response:
                assert response.status_code == 200
                first = await asyncio.wait_for(anext(response.aiter_lines()), 2)
                assert first.startswith("data: ")
                assert feed.stops == 0
                stop.set()
                await asyncio.wait_for(task, 5)
        assert feed.stops == 1 and not endpoint.exists()
    finally:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_public_idle_human_event_stream_disconnects_without_stopping_private_owner(config, endpoint):
    feed = Feed()
    pair = HubFrontends(create_app(config, event_feed=feed))
    loop = asyncio.get_running_loop()
    private_ready, public_ready = loop.create_future(), loop.create_future()
    private_stop, public_stop = asyncio.Event(), asyncio.Event()
    private = asyncio.create_task(runner.serve_private_coordinator(lambda: pair,
        socket_path=endpoint, agent_host="127.0.0.1", agent_port=0,
        stop_event=private_stop, install_signals=False, on_ready=lambda *a: private_ready.set_result(True)))
    public = None
    upstream = None
    try:
        await asyncio.wait_for(private_ready, 5)
        upstream = create_private_browser_client(endpoint)
        public = asyncio.create_task(runner.serve_public_browser(lambda: upstream,
            browser_port=0, stop_event=public_stop, install_signals=False,
            on_ready=lambda a: public_ready.set_result(a)))
        address = await asyncio.wait_for(public_ready, 5)
        url = f"http://127.0.0.1:{address[1]}"
        async with httpx.AsyncClient(trust_env=False, timeout=5, headers={"Host": "hub.test"}) as client:
            assert (await client.get(url + BASE + "/auth/login", headers=SIGNED)).status_code == 303
            cookie = client.cookies.get(config.auth_config.cookie_name)
            human = {**SIGNED, "Cookie": config.auth_config.cookie_name + "=" + cookie}
            async with client.stream("GET", url + BASE + "/events", headers=human) as response:
                assert response.status_code == 200
                assert (await asyncio.wait_for(anext(response.aiter_lines()), 2)).startswith("data: ")
                public_stop.set()
                await asyncio.wait_for(public, 5)
                assert upstream.is_closed
                assert feed.starts == 1 and feed.stops == 0 and pair.state == "active"
    finally:
        if public is not None:
            public_stop.set()
            await asyncio.gather(public, return_exceptions=True)
        await close_runner(private, private_stop)
    assert feed.stops == 1 and not endpoint.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,disconnect", [
    ("GET", BASE + "/events", True),
    ("GET", BASE + "/topics/ops:alerts/events", True),
    ("GET", "/mcp", True),
    ("GET", "/mcp/", True),
    ("POST", BASE + "/events", False),
    ("POST", "/mcp/", False),
    ("POST", BASE + "/tasks", False),
    ("GET", BASE + "/tasks", False),
])
async def test_stop_disconnect_is_exact_read_only_stream_scope(method, path, disconnect):
    stop = asyncio.Event()
    stop.set()
    received = []
    original = {"type": "http.request", "body": b"accepted", "more_body": False}
    calls = []

    async def receive():
        calls.append(True)
        return original

    async def app(scope, receive, send):
        received.append(await receive())

    async def send(message):
        raise AssertionError("receive wrapper must not send or create a response")

    await runner._StreamDisconnect(app, stop)({"type": "http", "method": method, "path": path}, receive, send)
    assert received == ([{"type": "http.disconnect"}] if disconnect else [original])
    assert calls == ([] if disconnect else [True])


@pytest.mark.asyncio
async def test_private_listener_construction_failure_closes_started_owner(config, endpoint, monkeypatch):
    feed = Feed()
    pair = HubFrontends(create_app(config, event_feed=feed))

    def invalid(app):
        raise ValueError("invalid listener configuration")

    monkeypatch.setattr(runner, "_listener", invalid)
    with pytest.raises(runner.SplitServerError):
        await runner.serve_private_coordinator(lambda: pair, socket_path=endpoint,
            agent_host="127.0.0.1", agent_port=0, install_signals=False)
    assert feed.starts == feed.stops == 1 and not endpoint.exists()


@pytest.mark.asyncio
async def test_public_gateway_configuration_failure_closes_accepted_client():
    async def forbidden_hook(request):
        pass

    client = httpx.AsyncClient(trust_env=False, event_hooks={"request": [forbidden_hook]})
    with pytest.raises(runner.SplitServerError):
        await runner.serve_public_browser(lambda: client, browser_port=0, install_signals=False)
    assert client.is_closed


@pytest.mark.asyncio
async def test_preexisting_stop_never_binds_or_constructs_factories(endpoint):
    stop, called = asyncio.Event(), []
    stop.set()
    await runner.serve_private_coordinator(lambda: called.append("private"), socket_path=endpoint,
        agent_host="127.0.0.1", agent_port=0, stop_event=stop, install_signals=False)
    await runner.serve_public_browser(lambda: called.append("public"), browser_port=0,
        stop_event=stop, install_signals=False)
    assert not called and not endpoint.exists()
