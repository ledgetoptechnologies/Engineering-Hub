"""Ordinary split-ingress shutdown with real disposable loopback clients."""
import asyncio
import sys

import httpx
import pytest
from fastapi import Request
from starlette.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings

from engineering_hub import server as runner
from engineering_hub.app import create_app
from engineering_hub.frontends import HubFrontends
from tests.test_hub_frontends import config, checker, Feed, CODEX, BASE


async def start(pair, stop):
    ready = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(runner.serve_frontends(lambda: pair,
        agent_host='127.0.0.1', agent_port=0, browser_port=0,
        stop_event=stop, install_signals=False,
        on_ready=lambda a, b: ready.set_result(a)))
    address = await asyncio.wait_for(ready, 5)
    return task, f'http://127.0.0.1:{address[1]}'


@pytest.mark.asyncio
async def test_ordinary_idle_hub_stream_stops_before_grace_expiry(config):
    feed, stop = Feed(), asyncio.Event()
    pair = HubFrontends(create_app(config, event_feed=feed))
    task, url = await start(pair, stop)
    try:
        async with httpx.AsyncClient(trust_env=False, headers={**CODEX, 'Host': 'hub.test'}) as client:
            async with client.stream('GET', url + BASE + '/events') as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                assert (await anext(lines)).startswith('data: ')
                assert feed.broker.subscriber_count == 1
                stop.set()
                await asyncio.wait_for(asyncio.shield(task), 3)
        assert pair.state == 'closed' and feed.stops == 1
        assert feed.broker.subscriber_count == 0
    finally:
        stop.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize('admission_failure', [False, True])
async def test_ordinary_held_post_survives_repeated_stop_and_keeps_owner(config, admission_failure):
    feed, stop, entered, release = Feed(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    core = create_app(config, event_feed=feed)
    pair = HubFrontends(core)
    if admission_failure:
        close = pair.close_admission

        def failed_close():
            close()
            raise RuntimeError('fixture admission failure')

        pair.close_admission = failed_close

    @core.middleware('http')
    async def held(request: Request, call_next):
        if request.method != 'POST' or request.url.path != BASE + '/tasks':
            return await call_next(request)
        assert await request.body() == b'accepted'
        entered.set()
        await release.wait()
        assert pair.state == 'stopping' and feed.stops == 0
        return JSONResponse({'accepted_completed': True})

    task, url = await start(pair, stop)
    try:
        async with httpx.AsyncClient(trust_env=False, headers={**CODEX, 'Host': 'hub.test'}) as client:
            mutation = asyncio.create_task(client.post(url + BASE + '/tasks', content=b'accepted'))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            await asyncio.sleep(.1)
            task.cancel()
            await asyncio.sleep(.1)
            assert not task.done() and not mutation.done() and feed.stops == 0
            assert pair.state == 'stopping'
            denied = await pair_request(pair)
            assert denied == 503
            release.set()
            assert (await mutation).json() == {'accepted_completed': True}
            # The SDK's AnyIO lifespan wraps ordinary callback errors, but
            # preserves their exact leaf after all accepted requests drain.
            expected = ExceptionGroup if admission_failure else asyncio.CancelledError
            with pytest.raises(expected) as stopped:
                await asyncio.wait_for(task, 3)
            if admission_failure:
                assert len(stopped.value.exceptions) == 1
                assert isinstance(stopped.value.exceptions[0], RuntimeError)
                assert str(stopped.value.exceptions[0]) == 'fixture admission failure'
        # Cancellation remains visible; the one-use pair must never reopen.
        assert pair.state == 'failed' and feed.stops == 1
    finally:
        release.set()
        stop.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


async def pair_request(pair):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.agents),
                                base_url='http://hub.test', headers=CODEX) as client:
        return (await client.get(BASE + '/tasks')).status_code


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != 'linux', reason='Real native bus event source')
async def test_ordinary_idle_native_mcp_get_stops_and_clears_presence(config, tmp_path):
    from agent_message_bus.app import create_app as create_bus
    bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=config.agent_auth, push_events_operational=True,
        transport_security_settings=TransportSecuritySettings(
            allowed_hosts=['hub.test'], allowed_origins=['https://hub.test']))
    feed, stop = Feed(), asyncio.Event()
    pair = HubFrontends(create_app(config, bus_app=bus, event_feed=feed))
    task, url = await start(pair, stop)
    headers = {**CODEX, 'Host': 'hub.test', 'Accept': 'application/json, text/event-stream'}
    try:
        async with httpx.AsyncClient(trust_env=False, headers=headers) as client:
            initialized = await client.post(url + '/mcp', json={
                'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                    'protocolVersion': '2025-06-18', 'capabilities': {},
                    'clientInfo': {'name': 'shutdown-fixture', 'version': '1'}}})
            assert initialized.status_code == 200
            client.headers['Mcp-Session-Id'] = initialized.headers['Mcp-Session-Id']
            client.headers['MCP-Protocol-Version'] = '2025-06-18'
            assert (await client.post(url + '/mcp', json={
                'jsonrpc': '2.0', 'method': 'notifications/initialized'})).status_code == 202
            async with client.stream('GET', url + '/mcp') as response:
                assert response.status_code == 200
                assert bus.state.push_publisher._presence_streams
                stop.set()
                await asyncio.wait_for(asyncio.shield(task), 3)
        assert not bus.state.push_publisher._presence_streams
        assert not bus.state.push_event_source.running
        assert pair.state == 'closed' and feed.stops == 1
    finally:
        stop.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
