"""One core and one agent server across primary/legacy compatibility sockets."""
import asyncio
import json
import socket
import sys
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import TransportSecuritySettings

from agent_message_bus.auth import AuthSettings
from engineering_hub import server as runner
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.events import EventBroker
from engineering_hub.frontends import HubFrontends


AUTH = AuthSettings('alias-hermes', 'alias-codex', claude_token='alias-claude')
BASE = '/api/hub/v1'
CODEX = {'Authorization': 'Bearer ' + AUTH.codex_token}


class Feed:
    def __init__(self):
        self.broker = EventBroker()
        self.starts = self.stops = 0

    @property
    def available(self):
        return self.broker.available

    async def start(self):
        self.starts += 1
        self.broker.activate(0)

    async def stop(self):
        self.stops += 1
        self.broker.fail()


def config(tmp_path):
    return Settings(tmp_path / 'hub.db', AUTH, AuthConfig(
        'https://hub.test', human_auth_mode='cloudflare_access',
        cloudflare_issuer='https://fixture.cloudflareaccess.com', cloudflare_audience='fixture-hub',
        cloudflare_allowed_emails=('beau@example.invalid',)))


def tracked_binds(monkeypatch):
    captured = []
    original = runner._bind

    def track(host, port):
        sock = original(host, port)
        captured.append((sock, sock.getsockname()))
        return sock

    monkeypatch.setattr(runner, '_bind', track)
    return captured


def url(address):
    return f'http://127.0.0.1:{address[1]}'


@pytest.mark.asyncio
async def test_actual_primary_alias_browser_share_one_core_and_feed(tmp_path, monkeypatch):
    feed = Feed()
    core = create_app(config(tmp_path), event_feed=feed)
    pair = HubFrontends(core)
    captured = tracked_binds(monkeypatch)
    listeners = []
    original_listener = runner._listener

    def listener(app):
        result = original_listener(app)
        listeners.append(result)
        return result

    monkeypatch.setattr(runner, '_listener', listener)
    ready = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    constructions = []

    def factory():
        constructions.append(True)
        return pair

    task = asyncio.create_task(runner.serve_frontends(factory, agent_host='127.0.0.1',
        agent_port=0, browser_port=0, agent_alias_port=0, stop_event=stop, install_signals=False,
        on_ready=lambda primary, browser: ready.set_result((primary, browser))))
    try:
        primary, browser = await asyncio.wait_for(ready, 5)
        assert len(captured) == 3 and len({address for _, address in captured}) == 3
        alias = next(address for _, address in captured if address not in (primary, browser))
        assert len(listeners) == 2
        assert listeners[0].config.app is pair.agents and listeners[1].config.app is pair.browser
        task_record = core.state.repository.create_task('human:123', 'engineering-hub', 'Alias task', 'alias-task')
        async with httpx.AsyncClient(headers={'Host': 'hub.test'}, trust_env=False) as client:
            for address in (primary, alias):
                assert (await client.get(url(address) + '/')).status_code == 403
                assert (await client.get(url(address) + BASE + '/auth/login', headers=CODEX)).status_code == 403
                assert (await client.get(url(address) + BASE + '/overview')).status_code == 401
                seen = await client.get(url(address) + BASE + '/tasks/' + task_record['id'], headers=CODEX)
                assert seen.status_code == 200 and seen.json() == task_record
            claimed = await client.post(url(alias) + BASE + '/tasks/' + task_record['id'] + '/claim',
                headers=CODEX, json={'expected_revision': task_record['revision']})
            assert claimed.status_code == 200 and claimed.json()['owner'] == 'codex'
            seen = await client.get(url(primary) + BASE + '/tasks/' + task_record['id'], headers=CODEX)
            assert seen.json() == claimed.json()
            assert (await client.get(url(browser) + '/mcp', headers=CODEX)).status_code == 403
            assert (await client.get(url(browser) + '/mcp/')).status_code == 403
            assert constructions == [True] and feed.starts == 1 and feed.stops == 0
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert feed.starts == feed.stops == 1 and pair.state == 'closed'
    assert all(sock.fileno() == -1 for sock, _ in captured)


@pytest.mark.asyncio
@pytest.mark.parametrize('options', [
    {'agent_alias_port': True}, {'agent_alias_port': False}, {'agent_alias_port': -1},
    {'agent_alias_port': 65536}, {'agent_alias_port': '8765'}, {'agent_alias_port': 1.0},
    {'agent_port': 8765, 'agent_alias_port': 8765},
    {'browser_port': 8765, 'agent_alias_port': 8765},
])
async def test_invalid_alias_plan_never_binds_or_constructs(monkeypatch, options):
    def unexpected(*args, **kwargs):
        pytest.fail('Invalid alias plan must fail before socket/core construction')
    monkeypatch.setattr(runner, '_bind', unexpected)
    values = {'agent_host': '127.0.0.1', 'agent_port': 8770, 'browser_port': 8771,
              'agent_alias_port': None, 'install_signals': False, **options}
    with pytest.raises(ValueError):
        await runner.serve_frontends(unexpected, **values)


@pytest.mark.asyncio
async def test_alias_collision_never_constructs_core_and_closes_prior_sockets(monkeypatch):
    captured = tracked_binds(monkeypatch)
    def forbidden():
        pytest.fail('Alias collision must precede core construction')
    with socket.socket() as busy:
        busy.bind(('127.0.0.1', 0))
        busy.listen()
        with pytest.raises(OSError):
            await runner.serve_frontends(forbidden, agent_host='127.0.0.1', agent_port=0,
                browser_port=0, agent_alias_port=busy.getsockname()[1], install_signals=False)
    assert captured and all(sock.fileno() == -1 for sock, _ in captured)


class Pair:
    def __init__(self):
        self.agents, self.browser = object(), object()
        self.starts = self.stops = 0

    @asynccontextmanager
    async def lifespan(self):
        self.starts += 1
        try:
            yield self
        finally:
            self.stops += 1


class DrainingListener:
    def __init__(self, release):
        self.started = self.should_exit = self.closed = False
        self.release = release
        self.sockets = []

    async def serve(self, sockets):
        self.sockets = list(sockets)
        self.started = True
        try:
            while not self.should_exit:
                await asyncio.sleep(.01)
            await self.release.wait()
        finally:
            self.closed = True


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_both_agent_sockets_before_one_owner_release(monkeypatch):
    pair = Pair()
    release, ready = asyncio.Event(), asyncio.Event()
    listeners = [DrainingListener(release), DrainingListener(release)]
    pending = iter(listeners)
    monkeypatch.setattr(runner, '_listener', lambda app: next(pending))
    captured = tracked_binds(monkeypatch)
    task = asyncio.create_task(runner.serve_frontends(lambda: pair, agent_host='127.0.0.1',
        agent_port=0, browser_port=0, agent_alias_port=0, install_signals=False,
        on_ready=lambda primary, browser: ready.set()))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        assert len(listeners[0].sockets) == 2 and len(listeners[1].sockets) == 1
        assert pair.starts == 1
        task.cancel()
        await asyncio.sleep(.04)
        task.cancel()
        await asyncio.sleep(.04)
        assert pair.stops == 0 and not task.done()
        assert all(sock.fileno() >= 0 for sock, _ in captured)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert pair.starts == pair.stops == 1 and all(listener.closed for listener in listeners)
    assert all(sock.fileno() == -1 for sock, _ in captured)


@asynccontextmanager
async def mcp_client(address, token):
    events = asyncio.Queue()
    async def incoming(message):
        if isinstance(message, types.ServerNotification):
            await events.put(message)
    async with httpx.AsyncClient(headers={'Host': 'hub.test', 'Authorization': 'Bearer ' + token},
                                  timeout=httpx.Timeout(10, read=None), trust_env=False) as http:
        async with streamable_http_client(url(address) + '/mcp', http_client=http) as (read, write, sid):
            async with ClientSession(read, write, message_handler=incoming) as session:
                initialized = await session.initialize()
                yield session, events, sid, initialized


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux native inotify publisher')
async def test_unified_stateful_session_and_native_notifications_cross_agent_alias(tmp_path, monkeypatch):
    from agent_message_bus.app import create_app as create_bus
    bus = create_bus(db_path=tmp_path / 'bus.db', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=AUTH, push_events_operational=True,
        transport_security_settings=TransportSecuritySettings(allowed_hosts=['hub.test'], allowed_origins=['https://hub.test']))
    feed = Feed()
    core = create_app(config(tmp_path), bus_app=bus, event_feed=feed)
    pair = HubFrontends(core)
    original_lifespan = bus.router.lifespan_context
    lifecycle = []
    @asynccontextmanager
    async def counted_lifespan(app):
        lifecycle.append('start')
        async with original_lifespan(app):
            try:
                yield
            finally:
                lifecycle.append('stop')
    bus.router.lifespan_context = counted_lifespan
    captured = tracked_binds(monkeypatch)
    ready = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    previous_tasks = set(asyncio.all_tasks())
    task = asyncio.create_task(runner.serve_frontends(lambda: pair, agent_host='127.0.0.1',
        agent_port=0, browser_port=0, agent_alias_port=0, stop_event=stop, install_signals=False,
        on_ready=lambda primary, browser: ready.set_result((primary, browser))))
    try:
        primary, browser = await asyncio.wait_for(ready, 5)
        alias = next(address for _, address in captured if address not in (primary, browser))
        async with mcp_client(primary, AUTH.codex_token) as (codex, events, sid, initialized):
            assert initialized.capabilities.resources.subscribe is True
            tools = (await codex.list_tools()).tools
            assert len(tools) == 33 and 'hub_overview' in {tool.name for tool in tools}
            await codex.subscribe_resource('agentbus://inbox/codex')
            async with httpx.AsyncClient(headers={'Host': 'hub.test'}, trust_env=False) as http:
                rpc = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
                headers = {**CODEX, 'Mcp-Session-Id': sid(), 'Accept': 'application/json, text/event-stream',
                           'MCP-Protocol-Version': '2025-06-18'}
                response = await http.post(url(alias) + '/mcp', headers=headers, json=rpc)
                assert response.status_code == 200
                body = json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: '))) if response.headers['content-type'].startswith('text/event-stream') else response.json()
                assert len(body['result']['tools']) == 33
                foreign = {**headers, 'Authorization': 'Bearer ' + AUTH.hermes_token}
                assert (await http.post(url(alias) + '/mcp', headers=foreign, json=rpc)).status_code == 404
            async with mcp_client(alias, AUTH.hermes_token) as (hermes, _, _, _):
                sent = await hermes.call_tool('send_message', {'recipient': 'codex', 'message_type': 'info',
                    'body': {'topic': 'same-owner-alias'}})
                assert not sent.isError
                event = await asyncio.wait_for(events.get(), 3)
                assert str(event.root.params.uri) == 'agentbus://inbox/codex'
                pending = await codex.call_tool('receive_messages')
                payload = pending.structuredContent or json.loads(pending.content[0].text)
                assert payload['messages'][0]['body'] == {'topic': 'same-owner-alias'}
            await codex.unsubscribe_resource('agentbus://inbox/codex')
        assert lifecycle == ['start'] and feed.starts == 1 and feed.stops == 0
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, 5)
        finally:
            # sse-starlette's process-exit notifier outlives a server on this
            # test loop. Retire only notifier tasks created by this test.
            watchers = [pending for pending in asyncio.all_tasks()
                        if pending not in previous_tasks and pending is not asyncio.current_task()
                        and not pending.done() and pending.get_coro().__qualname__ == '_shutdown_watcher']
            for pending in watchers:
                pending.cancel()
            if watchers:
                await asyncio.gather(*watchers, return_exceptions=True)
    assert lifecycle == ['start', 'stop'] and feed.starts == feed.stops == 1
    assert all(sock.fileno() == -1 for sock, _ in captured)


def test_cli_alias_requires_split_ingress_before_startup(monkeypatch):
    from engineering_hub import __main__ as cli
    monkeypatch.setattr(sys, 'argv', ['engineering_hub', '--agent-alias-port', '8765'])
    monkeypatch.setattr(cli.uvicorn, 'run', lambda *a, **kw: pytest.fail('Invalid CLI must not start a server'))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


@pytest.mark.parametrize('alias', [0, 8765])
def test_cli_passes_explicit_alias_to_one_shared_runner_without_constructing_owner(monkeypatch, alias):
    from engineering_hub import __main__ as cli
    received = []
    consumed = []
    def runner_only(factory, **kwargs):
        assert callable(factory)
        received.append(kwargs)
        async def inert_runner():
            return None
        return inert_runner()
    def consume_without_event_loop(coroutine):
        # This is an argument-forwarding test, not another server run. Do not
        # replace pytest's event-loop policy with asyncio.run from a sync test.
        consumed.append(True)
        coroutine.close()
    monkeypatch.setattr(runner, 'serve_frontends', runner_only)
    monkeypatch.setattr(cli.asyncio, 'run', consume_without_event_loop)
    monkeypatch.setattr(sys, 'argv', ['engineering_hub', '--split-ingress', '--agent-alias-port', str(alias)])
    cli.main()
    assert consumed == [True] and len(received) == 1 and received[0]['agent_alias_port'] == alias
