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
