"""Source-only registry bootstrap checks; all upstream traffic is synthetic."""
import httpx
import pytest
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.registry_bridge import CanonicalRegistryBridge
from engineering_hub.registry_client import RegistryClient
from engineering_hub.registry_stream import RegistryStream
from tests.test_hub_registry_bridge import BASE, ORIGIN, EPOCH, REVISION, frame, wire_snapshot


def config(tmp_path, **changes):
    return Settings(tmp_path / "hub.db",
                    AuthSettings("fixture-hermes", "fixture-codex", claude_token="fixture-claude"),
                    AuthConfig("https://hub.test", beau_github_id=42), **changes)


def bearer(agent="codex"):
    return {"Authorization": f"Bearer fixture-{agent}"}


@pytest.fixture
def factories(monkeypatch):
    """Patch only transport construction, retaining production bridge/client logic."""
    made, calls = [], []
    response = [lambda _: httpx.Response(200, json=wire_snapshot())]

    def remote(request):
        calls.append(request)
        return response[0](request)

    def client(origin):
        made.append(("client", origin))
        return RegistryClient(origin, transport=httpx.MockTransport(remote))

    def stream(origin):
        made.append(("stream", origin))
        return RegistryStream(origin, transport=httpx.MockTransport(remote))

    monkeypatch.setattr("engineering_hub.registry_client.RegistryClient", client)
    monkeypatch.setattr("engineering_hub.registry_stream.RegistryStream", stream)
    return made, calls, response


@pytest.mark.parametrize("field", ["registry_enabled", "registry_events_enabled"])
@pytest.mark.parametrize("value", [0, 1, "1", "false", None])
def test_flags_are_explicit_booleans(tmp_path, field, value):
    with pytest.raises(ValueError):
        config(tmp_path, **{field: value})


@pytest.mark.parametrize("origin", ["https://registry.test", "https://registry.test:8443/",
                                         "http://127.0.0.1:8765", "http://[::1]:8765"])
def test_explicit_safe_origins(tmp_path, origin):
    assert config(tmp_path, registry_enabled=True, registry_origin=origin).registry_origin == origin


@pytest.mark.parametrize("origin", ["", "http://localhost:8765", "http://192.168.50.80:8765",
    "https://user:secret@registry.test", "https://registry.test/path", "https://registry.test?x=1",
    "https://registry.test/#fragment", "https://registry.test\n", "https://registry.test:bad", 123])
def test_invalid_origins_rejected_even_when_disabled(tmp_path, origin):
    with pytest.raises(ValueError):
        config(tmp_path, registry_origin=origin)


def test_dependent_flags_require_explicit_configuration(tmp_path):
    with pytest.raises(ValueError):
        config(tmp_path, registry_enabled=True)
    with pytest.raises(ValueError):
        config(tmp_path, registry_events_enabled=True, registry_origin=ORIGIN)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in tuple(__import__("os").environ):
        if name.startswith("HUB_") or name in {"AGENT_BUS_DB", "AGENT_BUS_ALLOWED_IPS", "ADMIN_TOKEN"}:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_TOKEN", "fixture-hermes")
    monkeypatch.setenv("CODEX_TOKEN", "fixture-codex")
    monkeypatch.setenv("CLAUDE_TOKEN", "fixture-claude")
    monkeypatch.setenv("HUB_DB", str(tmp_path / "env.db"))


def test_environment_default_and_explicit_flags(clean_env, monkeypatch):
    default = Settings.from_env()
    assert (default.registry_enabled, default.registry_events_enabled, default.registry_origin) == (False, False, None)
    monkeypatch.setenv("HUB_REGISTRY_ENABLED", "1")
    monkeypatch.setenv("HUB_REGISTRY_ORIGIN", ORIGIN)
    assert Settings.from_env().registry_enabled is True
    assert Settings.from_env().registry_events_enabled is False
    monkeypatch.setenv("HUB_REGISTRY_EVENTS_ENABLED", "1")
    assert Settings.from_env().registry_events_enabled is True


@pytest.mark.parametrize("name", ["HUB_REGISTRY_ENABLED", "HUB_REGISTRY_EVENTS_ENABLED"])
@pytest.mark.parametrize("value", ["", "true", "false", "2", " 1", "01"])
def test_environment_flags_fail_closed(clean_env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_environment_dependencies_and_origin_are_validated(clean_env, monkeypatch):
    monkeypatch.setenv("HUB_REGISTRY_ENABLED", "1")
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.setenv("HUB_REGISTRY_ORIGIN", "http://192.168.50.80")
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.setenv("HUB_REGISTRY_ORIGIN", ORIGIN)
    monkeypatch.setenv("HUB_REGISTRY_ENABLED", "0")
    monkeypatch.setenv("HUB_REGISTRY_EVENTS_ENABLED", "1")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_default_does_not_construct_clients_or_open_network(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled registry constructed/opened a transport")
    monkeypatch.setattr("engineering_hub.registry_client.RegistryClient", forbidden)
    monkeypatch.setattr("engineering_hub.registry_stream.RegistryStream", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    settings = config(tmp_path)
    app = create_app(settings)
    assert app.state.registry_bridge is None
    with TestClient(app, base_url=settings.auth_config.origin) as client:
        response = client.get(BASE + "/peers", headers=bearer())
        assert response.status_code == 200
        assert all(peer["status"] == "unknown_stale" for peer in response.json()["items"])
        assert client.get(BASE + "/events", headers=bearer()).status_code == 503


def test_conflicting_explicit_bridge_fails_before_database_or_factories(tmp_path, factories):
    made, calls, _ = factories
    settings = config(tmp_path, registry_enabled=True, registry_origin=ORIGIN)
    explicit = CanonicalRegistryBridge(RegistryClient(ORIGIN))
    with pytest.raises(ValueError):
        create_app(settings, registry_bridge=explicit)
    assert not settings.database.exists()
    assert made == calls == []


def test_bootstrap_is_lazy_and_forwards_only_authenticated_agent_bearer(tmp_path, factories):
    made, calls, _ = factories
    settings = config(tmp_path, registry_enabled=True, registry_origin=ORIGIN)
    app = create_app(settings)
    assert type(app.state.registry_bridge) is CanonicalRegistryBridge
    assert app.state.registry_bridge.human_reader is None
    assert app.state.registry_bridge.stream is None
    assert made == [("client", ORIGIN)] and calls == []
    with TestClient(app, base_url=settings.auth_config.origin) as client:
        assert calls == []
        assert client.get(BASE + "/peers").status_code == 401
        assert client.get(BASE + "/peers", headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert calls == []
        session = app.state.sessions.create_session(42)
        client.cookies.set(settings.auth_config.cookie_name, session["cookie_token"])
        for agent in ("hermes", "codex", "claude"):
            result = client.get(BASE + "/peers", headers=bearer(agent) | {"X-Act-As": "human:42"})
            assert result.status_code == 200
            assert result.json()["advisory"] is True and result.json()["delivery_guaranteed"] is False
        assert [r.headers["authorization"] for r in calls] == [f"Bearer fixture-{a}" for a in ("hermes", "codex", "claude")]
        assert all("cookie" not in r.headers and "x-act-as" not in r.headers for r in calls)
        assert client.get(BASE + "/events", headers=bearer()).status_code == 503
        assert len(calls) == 3


def test_human_cookie_cannot_borrow_agent_credential(tmp_path, factories):
    _, calls, _ = factories
    settings = config(tmp_path, registry_enabled=True, registry_origin=ORIGIN, registry_events_enabled=True)
    app = create_app(settings)
    with TestClient(app, base_url=settings.auth_config.origin) as client:
        session = app.state.sessions.create_session(42)
        client.cookies.set(settings.auth_config.cookie_name, session["cookie_token"])
        for suffix in ("/peers", "/events", "/preflight?recipient=hermes"):
            assert client.get(BASE + suffix, headers={"X-Act-As": "codex"}).status_code == 503
        assert client.get(BASE + "/peers", headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert calls == []


@pytest.mark.parametrize("mode,status", [("unavailable", 503), ("malformed", 503), ("forbidden", 403), ("disconnect", 503)])
def test_enabled_upstream_failures_remain_sanitized(tmp_path, factories, mode, status):
    _, calls, response = factories
    def failure(request):
        if mode == "disconnect":
            raise httpx.ConnectError("PRIVATE_SOURCE", request=request)
        if mode == "malformed":
            return httpx.Response(200, json=wire_snapshot(private="PRIVATE_SOURCE"))
        return httpx.Response(status, text="PRIVATE_SOURCE")
    response[0] = failure
    settings = config(tmp_path, registry_enabled=True, registry_origin=ORIGIN)
    with TestClient(create_app(settings), base_url=settings.auth_config.origin) as client:
        result = client.get(BASE + "/peers", headers=bearer())
        assert result.status_code == status
        assert "PRIVATE" not in result.text and "reachable" not in result.text
    assert len(calls) == 1


def test_events_are_opt_in_lazy_and_use_original_agent(tmp_path, factories):
    made, calls, response = factories
    settings = config(tmp_path, registry_enabled=True, registry_origin=ORIGIN, registry_events_enabled=True)
    app = create_app(settings)
    assert app.state.registry_bridge.stream is not None
    assert made == [("client", ORIGIN), ("stream", ORIGIN)] and calls == []
    def remote(request):
        if request.url.path.endswith("/subscribe"):
            content = frame("registry.invalidated", {"topic": "engineering-hub", "registry_epoch": EPOCH,
                "snapshot_revision": REVISION, "cursor": 8}, f"{EPOCH}:engineering-hub:8")
            content += frame("registry.unavailable", {"topic": "engineering-hub", "registry_epoch": EPOCH,
                                                      "reason": "observer_unavailable"})
            return httpx.Response(200, text=content, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=wire_snapshot())
    response[0] = remote
    with TestClient(app, base_url=settings.auth_config.origin) as client:
        assert calls == []
        result = client.get(BASE + "/events", headers=bearer("claude") | {"X-Act-As": "codex"})
        assert result.status_code == 200 and "event: registry.unavailable" in result.text
        assert "event: registry.invalidated" in result.text
        assert f"id: {EPOCH}:engineering-hub:8" in result.text
        assert len(calls) == 2
        assert [r.url.path for r in calls] == [
            "/api/topic-registry/v1/topics/engineering-hub/snapshot",
            "/api/topic-registry/v1/topics/engineering-hub/subscribe"]
        assert all(r.headers["authorization"] == "Bearer fixture-claude" for r in calls)
        assert all("cookie" not in r.headers and "x-act-as" not in r.headers for r in calls)
    assert len(calls) == 2  # No reconnect/background subscription on shutdown.
