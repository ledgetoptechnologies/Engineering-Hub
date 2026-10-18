"""The socket peer, not forwarded headers, governs agent IP restrictions."""
import sys

from engineering_hub import __main__ as entrypoint


def test_entrypoint_does_not_trust_forwarded_headers(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["engineering-hub", "--host", "127.0.0.1", "--port", "8771"])
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    entrypoint.main()
    assert calls == [(("engineering_hub.app:create_app",), {
        "factory": True, "host": "127.0.0.1", "port": 8771,
        "access_log": False, "proxy_headers": False,
    })]


def test_split_entrypoint_builds_one_core_and_uses_shared_runner(monkeypatch):
    from engineering_hub import app, frontends, server

    calls = []
    core, pair = object(), object()
    monkeypatch.setattr(sys, "argv", ["engineering-hub", "--host", "0.0.0.0", "--port", "8770",
                                     "--split-ingress", "--browser-port", "8771"])
    monkeypatch.setattr(app, "create_app", lambda: calls.append("create") or core)
    monkeypatch.setattr(frontends, "HubFrontends", lambda value: calls.append(("pair", value)) or pair)

    async def run(value, **kwargs):
        assert calls == []  # Construction occurs only after the runner binds.
        calls.append(("serve", value(), kwargs))

    monkeypatch.setattr(server, "serve_frontends", run)
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda *args, **kwargs: calls.append("unexpected"))
    entrypoint.main()
    assert calls == ["create", ("pair", core), ("serve", pair, {
        "agent_host": "0.0.0.0", "agent_port": 8770, "browser_port": 8771,
    })]
