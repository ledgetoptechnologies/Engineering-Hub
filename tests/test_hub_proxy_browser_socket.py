"""Actual wildcard listener and HTTP denials; no host/tunnel acceptance claim."""
import asyncio

import httpx
import pytest

from engineering_hub.app import create_app
from engineering_hub.frontends import HubFrontends
from engineering_hub import server as runner
from tests.test_hub_frontends import config, checker, Feed, SIGNED, CODEX, BASE


@pytest.mark.asyncio
async def test_actual_proxy_listener_retains_both_frontdoor_boundaries(config):
    feed = Feed()
    pair = HubFrontends(create_app(config, event_feed=feed))
    ready = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    task = asyncio.create_task(runner.serve_frontends(
        lambda: pair, agent_host="127.0.0.1", agent_port=0,
        browser_host="0.0.0.0", browser_port=0, stop_event=stop,
        install_signals=False, on_ready=lambda a, b: ready.set_result((a, b))))
    try:
        agent, browser = await asyncio.wait_for(ready, 5)
        assert browser[0] == "0.0.0.0" and agent[0] == "127.0.0.1"
        browser_url = f"http://127.0.0.1:{browser[1]}"
        agent_url = f"http://127.0.0.1:{agent[1]}"
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                     headers={"Host": "hub.test"}) as client:
            assert (await client.get(browser_url + "/health")).status_code == 200
            for path in ("/", "/index.html", "/bundle.js", BASE + "/overview"):
                assert (await client.get(browser_url + path)).status_code == 401
                spoof = {"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https",
                         "CF-Access-Authenticated-User-Email": "beau@example.com"}
                assert (await client.get(browser_url + path, headers=spoof)).status_code == 401
            assert (await client.get(browser_url + "/", headers=SIGNED)).text == "private-dashboard"
            assert (await client.get(browser_url + "/mcp/", headers=SIGNED)).status_code == 403
            assert (await client.get(browser_url + "/api/messages", headers=SIGNED)).status_code == 403
            assert (await client.get(browser_url + "/", headers=CODEX)).status_code == 403
            assert (await client.get(agent_url + "/", headers=SIGNED)).status_code == 403
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert feed.starts == feed.stops == 1 and pair.state == "closed"
