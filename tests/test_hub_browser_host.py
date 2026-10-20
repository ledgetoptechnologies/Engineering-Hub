"""Explicit proxy bind selection preserves the authenticated split front door."""
import asyncio
from contextlib import contextmanager
import sys

import pytest

from engineering_hub import __main__ as entrypoint
from engineering_hub import server as runner
from engineering_hub.app import create_app
from engineering_hub.frontends import HubFrontends
from tests.test_hub_frontends import config, checker, Feed
from tests.test_hub_server import FakeListener, Pair


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["localhost", "hub.test", "", "http://127.0.0.1", None,
                                 1, True, "fe80::1%eth0", "999.1.1.1"])
async def test_invalid_browser_host_precedes_all_binds_and_factory(monkeypatch, host):
    def forbidden(*args):
        pytest.fail("Invalid browser address reached resource construction")
    monkeypatch.setattr(runner, "_bind", forbidden)
    with pytest.raises(ValueError):
        await runner.serve_frontends(forbidden, browser_host=host, install_signals=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.50.80", "::", "2001:db8::1"])
async def test_nonloopback_rejects_bare_pair_before_lifespan_and_serving(monkeypatch, host):
    binds, closed = [], []
    @contextmanager
    def bind(address, port):
        binds.append((address, port))
        try:
            yield object()
        finally:
            closed.append(address)
    monkeypatch.setattr(runner, "_bind", bind)
    monkeypatch.setattr(runner, "_listener", lambda *_: pytest.fail("Unprotected pair served"))
    pair = Pair()
    with pytest.raises(ValueError, match="authenticated HubFrontends"):
        await runner.serve_frontends(lambda: pair, browser_host=host, install_signals=False)
    assert binds == [("0.0.0.0", 8770), (host, 8771)]
    assert closed == [host, "0.0.0.0"]
    assert pair.starts == pair.stops == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("host", [None, "0.0.0.0", "192.168.50.80", "::", "::1"])
async def test_configured_pair_uses_selected_numeric_browser_bind(monkeypatch, config, host):
    binds = []
    class Socket:
        def __init__(self, address, port):
            self.address = (address, port)
        def getsockname(self):
            return self.address
    @contextmanager
    def bind(address, port):
        binds.append((address, port))
        yield Socket(address, port)
    monkeypatch.setattr(runner, "_bind", bind)
    monkeypatch.setattr(runner, "_listener", lambda *_: FakeListener())
    feed = Feed()
    pair = HubFrontends(create_app(config, event_feed=feed))
    stop = asyncio.Event()
    options = {} if host is None else {"browser_host": host}
    await runner.serve_frontends(lambda: pair, stop_event=stop, install_signals=False,
                                on_ready=lambda *_: stop.set(), **options)
    assert binds == [("0.0.0.0", 8770), ("127.0.0.1" if host is None else host, 8771)]
    assert feed.starts == feed.stops == 1
    assert pair.state == "closed"


@pytest.mark.parametrize("extra,expected", [([], "127.0.0.1"),
                                         (["--browser-host", "0.0.0.0"], "0.0.0.0"),
                                         (["--browser-host", "::"], "::"),
                                         (["--browser-host", ""], "")])
def test_cli_forwards_browser_host_without_alias(monkeypatch, extra, expected):
    calls = []
    coroutine = object()
    completed = []
    def serve(factory, **kwargs):
        calls.append(kwargs)
        return coroutine
    def run(value):
        assert value is coroutine
        completed.append(True)
    monkeypatch.setattr(runner, "serve_frontends", serve)
    # This test checks CLI argument forwarding, not asyncio.run. Creating a real
    # loop here would replace pytest-asyncio's policy loop in this sync test.
    monkeypatch.setattr(entrypoint.asyncio, "run", run)
    monkeypatch.setattr(sys, "argv", ["hub", "--split-ingress", *extra])
    entrypoint.main()
    assert len(calls) == 1
    assert completed == [True]
    assert calls[0]["browser_host"] == expected
    assert calls[0]["agent_port"] == 8770
    assert calls[0]["browser_port"] == 8771
    assert calls[0]["agent_alias_port"] is None


def test_cli_rejects_browser_host_without_split_ingress(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["hub", "--browser-host", "0.0.0.0"])
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda *_a, **_k: pytest.fail("Combined app started"))
    with pytest.raises(SystemExit) as error:
        entrypoint.main()
    assert error.value.code == 2
    assert "--browser-host requires --split-ingress" in capsys.readouterr().err
