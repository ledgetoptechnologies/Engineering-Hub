"""Production connection-mode contract; local synthetic identities only."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.transport_security import TransportSecuritySettings
import pytest

from agent_message_bus.agent_sessions import AgentSessionRepository
from agent_message_bus.agent_sessions_api import OWNER_CONNECTION_PREFIX, attach_owner_routes
from agent_message_bus.agent_sessions_config import AgentSessionsConfig
from agent_message_bus.agent_sessions_transport import AgentSessionsBoundaryMiddleware
from agent_message_bus.app import create_app as create_bus
from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app as create_hub
from engineering_hub.auth import AuthConfig
from engineering_hub.ingress import HubIngressMiddleware


ORIGIN = "https://hub.example.test"
PRIVATE_ORIGIN = "http://192.168.50.80:8770"
PRIVATE_HOST = "192.168.50.80:8770"
OWNER = "human:cf:" + "a" * 64
AUTH = AuthSettings("legacy-hermes", "legacy-codex", claude_token="legacy-claude")
PRODUCTION_AUTH = AuthSettings("legacy-hermes", "legacy-codex", allowed_ips=frozenset({"127.0.0.1"}),
                               claude_token="legacy-claude")


def test_disabled_is_still_default_and_staging_local_origin_remains_valid(tmp_path, monkeypatch):
    for name in ("HUB_AGENT_SESSIONS_MODE", "HUB_AGENT_SESSIONS_DB",
                 "HUB_AGENT_SESSIONS_OWNER", "HUB_AGENT_SESSIONS_ORIGIN",
                 "HUB_AGENT_SESSIONS_HUB_BRIDGE", "HUB_AGENT_SESSIONS_PRIVATE_ORIGIN"):
        monkeypatch.delenv(name, raising=False)
    assert AgentSessionsConfig.from_env() is None
    staging = AgentSessionsConfig(tmp_path / "staging.sqlite3", "human:42", "http://127.0.0.1")
    assert staging.environment == "staging" and not staging.hub_bridge


def test_production_mode_requires_explicit_cloudflare_owner_and_https(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_AGENT_SESSIONS_MODE", "production")
    monkeypatch.setenv("HUB_AGENT_SESSIONS_DB", str(tmp_path / "production.sqlite3"))
    monkeypatch.setenv("HUB_AGENT_SESSIONS_OWNER", OWNER)
    monkeypatch.setenv("HUB_AGENT_SESSIONS_ORIGIN", ORIGIN)
    monkeypatch.setenv("HUB_AGENT_SESSIONS_HUB_BRIDGE", "disabled")
    config = AgentSessionsConfig.from_env()
    assert config is not None
    assert config.environment == "production" and config.origin == ORIGIN
    assert not config.hub_bridge

    with pytest.raises(ValueError, match="production_agent_sessions_https_origin_required"):
        AgentSessionsConfig(tmp_path / "insecure.sqlite3", OWNER, "http://127.0.0.1",
                            environment="production")
    with pytest.raises(ValueError, match="production_agent_sessions_dns_origin_required"):
        AgentSessionsConfig(tmp_path / "loopback.sqlite3", OWNER, "https://127.0.0.1",
                            environment="production")
    with pytest.raises(ValueError, match="production_agent_sessions_cloudflare_owner_required"):
        AgentSessionsConfig(tmp_path / "github-owner.sqlite3", "human:42", ORIGIN,
                            environment="production")


def test_production_private_origin_is_explicit_transport_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_AGENT_SESSIONS_MODE", "production")
    monkeypatch.setenv("HUB_AGENT_SESSIONS_DB", str(tmp_path / "production.sqlite3"))
    monkeypatch.setenv("HUB_AGENT_SESSIONS_OWNER", OWNER)
    monkeypatch.setenv("HUB_AGENT_SESSIONS_ORIGIN", ORIGIN)
    monkeypatch.setenv("HUB_AGENT_SESSIONS_PRIVATE_ORIGIN", PRIVATE_ORIGIN)
    monkeypatch.setenv("HUB_AGENT_SESSIONS_HUB_BRIDGE", "disabled")
    config = AgentSessionsConfig.from_env()
    assert config is not None
    assert config.origin == ORIGIN
    assert config.private_origin == PRIVATE_ORIGIN
    assert config.transport_origin == PRIVATE_ORIGIN

    default = AgentSessionsConfig(tmp_path / "default.sqlite3", OWNER, ORIGIN,
                                  environment="production")
    assert default.transport_origin == ORIGIN


@pytest.mark.parametrize("private_origin", [
    "http://10.1.2.3:8770", "http://172.16.0.1:8770", "http://172.31.255.254:8770",
    "http://192.168.50.80:8770",
])
def test_production_accepts_only_the_three_rfc1918_ranges(tmp_path, private_origin):
    config = AgentSessionsConfig(tmp_path / "sessions.sqlite3", OWNER, ORIGIN,
                                 environment="production", private_origin=private_origin)
    assert config.transport_origin == private_origin


@pytest.mark.parametrize("private_origin", [
    "https://192.168.50.80:8770", "http://8.8.8.8:8770", "http://example.test:8770",
    "http://[fd00::80]:8770", "http://127.0.0.1:8770", "http://169.254.1.10:8770",
    "http://100.64.0.1:8770", "http://192.0.2.1:8770", "http://198.18.0.1:8770",
    "http://224.0.0.1:8770", "http://192.168.50.80", "http://192.168.50.80:0",
    "http://192.168.50.80:65536", "http://192.168.50.80:08770",
    "http://192.168.050.80:8770", "http://user@192.168.50.80:8770",
    "http://192.168.50.80:8770/path", "http://192.168.50.80:8770?x=1",
    "http://192.168.50.80:8770#frag", "http://192.168.50.80:8770%2Fignored",
])
def test_production_private_origin_rejects_noncanonical_or_non_rfc1918_values(tmp_path, private_origin):
    with pytest.raises(ValueError, match="production_agent_sessions_private_origin_required"):
        AgentSessionsConfig(tmp_path / "sessions.sqlite3", OWNER, ORIGIN,
                            environment="production", private_origin=private_origin)


def test_private_origin_is_production_only_in_configuration(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="private_origin_requires_production_agent_sessions"):
        AgentSessionsConfig(tmp_path / "staging.sqlite3", "human:42", "http://127.0.0.1",
                            environment="staging", private_origin=PRIVATE_ORIGIN)
    monkeypatch.setenv("HUB_AGENT_SESSIONS_MODE", "staging")
    monkeypatch.setenv("HUB_AGENT_SESSIONS_DB", str(tmp_path / "staging.sqlite3"))
    monkeypatch.setenv("HUB_AGENT_SESSIONS_OWNER", "human:42")
    monkeypatch.setenv("HUB_AGENT_SESSIONS_ORIGIN", "http://127.0.0.1")
    monkeypatch.setenv("HUB_AGENT_SESSIONS_HUB_BRIDGE", "disabled")
    monkeypatch.setenv("HUB_AGENT_SESSIONS_PRIVATE_ORIGIN", PRIVATE_ORIGIN)
    with pytest.raises(ValueError, match="private_origin_requires_production_agent_sessions"):
        AgentSessionsConfig.from_env()

    monkeypatch.setenv("HUB_AGENT_SESSIONS_MODE", "disabled")
    with pytest.raises(ValueError, match="private_origin_requires_production_agent_sessions"):
        AgentSessionsConfig.from_env()


def test_production_mode_requires_all_explicit_environment_fields(monkeypatch):
    monkeypatch.setenv("HUB_AGENT_SESSIONS_MODE", "production")
    monkeypatch.setenv("HUB_AGENT_SESSIONS_HUB_BRIDGE", "disabled")
    monkeypatch.delenv("HUB_AGENT_SESSIONS_PRIVATE_ORIGIN", raising=False)
    for name in ("HUB_AGENT_SESSIONS_DB", "HUB_AGENT_SESSIONS_OWNER", "HUB_AGENT_SESSIONS_ORIGIN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="explicit_production_agent_sessions_configuration_required"):
        AgentSessionsConfig.from_env()


def test_production_bridge_requires_production_session_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_AGENT_SESSIONS_MODE", "disabled")
    monkeypatch.setenv("HUB_AGENT_SESSIONS_HUB_BRIDGE", "enabled")
    with pytest.raises(ValueError, match="hub_bridge_requires_staging_sessions"):
        AgentSessionsConfig.from_env()


def _production_bus(tmp_path, *, bridge=False, auth=PRODUCTION_AUTH, private_origin=None):
    config = AgentSessionsConfig(tmp_path / "agent-sessions.sqlite3", OWNER, ORIGIN,
                                 environment="production", hub_bridge=bridge,
                                 private_origin=private_origin)
    transport = TransportSecuritySettings(allowed_hosts=["hub.example.test"],
                                          allowed_origins=[ORIGIN])
    return create_bus(db_path=tmp_path / "legacy.sqlite3", audit_path=tmp_path / "audit.jsonl",
        file_root=tmp_path / "files", auth=auth, startup_cleanup=False,
        push_events_operational=False, agent_sessions_config=config,
        transport_security_settings=transport), config


def test_hub_accepts_production_sessions_only_with_cloudflare_and_exact_origin(tmp_path):
    bus, config = _production_bus(tmp_path)
    auth = AuthConfig(ORIGIN, human_auth_mode="cloudflare_access",
        cloudflare_issuer="https://fixture.cloudflareaccess.com", cloudflare_audience="fixture-app",
        cloudflare_allowed_emails=("owner@example.test",))
    app = create_hub(Settings(tmp_path / "hub.sqlite3", PRODUCTION_AUTH, auth), bus_app=bus)
    assert app.state.embedded_bus.state.agent_sessions_config is config
    assert app.state.session_hub_bridge is None


def test_private_listener_preserves_https_owner_origin_and_exact_mcp_host(tmp_path):
    bus, config = _production_bus(tmp_path, private_origin=PRIVATE_ORIGIN)
    auth = AuthConfig(ORIGIN, human_auth_mode="cloudflare_access",
        cloudflare_issuer="https://fixture.cloudflareaccess.com", cloudflare_audience="fixture-app",
        cloudflare_allowed_emails=("owner@example.test",))
    app = create_hub(Settings(tmp_path / "hub.sqlite3", PRODUCTION_AUTH, auth), bus_app=bus)
    security = app.state.mcp.settings.transport_security
    assert config.origin == ORIGIN
    assert config.transport_origin == PRIVATE_ORIGIN
    assert PRIVATE_HOST in security.allowed_hosts
    assert "hub.example.test" in security.allowed_hosts
    assert security.allowed_origins == [ORIGIN]
    assert app.state.embedded_bus.state.agent_sessions_config is config


def test_unified_factory_adds_only_configured_private_host_to_mcp_allowlist(tmp_path, monkeypatch):
    from engineering_hub.unified_app import _create_unified_bus

    bus_path = tmp_path / "messages.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    files_path = tmp_path / "files"
    bus_path.touch()
    audit_path.touch()
    files_path.mkdir()
    monkeypatch.setenv("HUB_AGENT_SESSIONS_MODE", "production")
    monkeypatch.setenv("HUB_AGENT_SESSIONS_DB", str(tmp_path / "sessions.sqlite3"))
    monkeypatch.setenv("HUB_AGENT_SESSIONS_OWNER", OWNER)
    monkeypatch.setenv("HUB_AGENT_SESSIONS_ORIGIN", ORIGIN)
    monkeypatch.setenv("HUB_AGENT_SESSIONS_PRIVATE_ORIGIN", PRIVATE_ORIGIN)
    monkeypatch.setenv("HUB_AGENT_SESSIONS_HUB_BRIDGE", "disabled")
    monkeypatch.setenv("HUB_BUS_STARTUP_CLEANUP", "0")
    auth = AuthConfig(ORIGIN, human_auth_mode="cloudflare_access",
        cloudflare_issuer="https://fixture.cloudflareaccess.com", cloudflare_audience="fixture-app",
        cloudflare_allowed_emails=("owner@example.test",))
    settings = Settings(tmp_path / "hub.sqlite3", PRODUCTION_AUTH, auth)
    bus = _create_unified_bus(settings, bus_paths={
        "AGENT_BUS_DB": bus_path, "AGENT_BUS_AUDIT": audit_path, "AGENT_BUS_FILE_ROOT": files_path})
    security = bus.state.mcp.settings.transport_security
    assert bus.state.agent_sessions_config.origin == ORIGIN
    assert bus.state.agent_sessions_config.transport_origin == PRIVATE_ORIGIN
    assert security.allowed_hosts == ["hub.example.test", PRIVATE_HOST]
    assert security.allowed_origins == [ORIGIN]


@pytest.mark.asyncio
async def test_private_listener_host_and_source_ip_are_exact(tmp_path):
    import httpx

    auth = AuthSettings("legacy-hermes", "legacy-codex", allowed_ips=frozenset({"10.8.0.5"}),
                        claude_token="legacy-claude")
    bus, config = _production_bus(tmp_path, auth=auth, private_origin=PRIVATE_ORIGIN)
    body = {"family": "codex", "name": "Synthetic laptop", "host_label": "vpn-laptop",
            "credential_sha256": "0" * 64}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bus, client=("10.8.0.5", 45678)),
                                 base_url=config.transport_origin) as allowed:
        accepted = await allowed.post("/api/agent-sessions/v1/enrollments", json=body)
        assert accepted.status_code == 200
        wrong_host = await allowed.post("/api/agent-sessions/v1/enrollments", headers={
            "Host": "hub.example.test"}, json=body)
        assert wrong_host.status_code == 403
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bus, client=("10.8.0.6", 45678)),
                                 base_url=config.transport_origin) as denied:
        wrong_ip = await denied.post("/api/agent-sessions/v1/enrollments", json=body)
        assert wrong_ip.status_code == 403


@pytest.mark.asyncio
async def test_private_session_mcp_host_admission_requires_private_host(tmp_path):
    import httpx
    from starlette.responses import Response

    auth = AuthSettings("legacy-hermes", "legacy-codex", allowed_ips=frozenset({"10.8.0.5"}),
                        claude_token="legacy-claude")
    config = AgentSessionsConfig(tmp_path / "sessions.sqlite3", OWNER, ORIGIN,
                                 environment="production", private_origin=PRIVATE_ORIGIN)
    reached = []

    async def downstream(scope, receive, send):
        reached.append(scope["path"])
        await Response(status_code=204)(scope, receive, send)

    boundary = AgentSessionsBoundaryMiddleware(downstream, auth=auth,
        repository=object(), config=config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boundary, client=("10.8.0.5", 45678)),
                                 base_url=config.transport_origin) as client:
        headers = {"Authorization": "Bearer ehs_synthetic", "Host": PRIVATE_HOST}
        allowed = await client.post("/mcp", headers=headers)
        assert allowed.status_code == 204
        blocked = await client.post("/mcp", headers={**headers, "Host": "hub.example.test"})
        assert blocked.status_code == 403
    assert reached == ["/mcp"]

    ingress_reached = []

    async def hub_downstream(scope, receive, send):
        ingress_reached.append(scope["path"])
        await Response(status_code=204)(scope, receive, send)

    ingress = HubIngressMiddleware(hub_downstream, mode="agents", agent_auth=auth,
        legacy_bus_enabled=False, agent_sessions=object(), agent_sessions_config=config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ingress, client=("10.8.0.5", 45678)),
                                 base_url=config.transport_origin) as client:
        denied = await client.post("/mcp", headers={"Host": "hub.example.test",
            "Authorization": "Bearer ehs_synthetic"})
        assert denied.status_code == 403
    assert ingress_reached == []


def test_production_hub_bridge_is_separate_and_has_no_implicit_grants(tmp_path):
    bus, _ = _production_bus(tmp_path, bridge=True)
    auth = AuthConfig(ORIGIN, human_auth_mode="cloudflare_access",
        cloudflare_issuer="https://fixture.cloudflareaccess.com", cloudflare_audience="fixture-app",
        cloudflare_allowed_emails=("owner@example.test",))
    app = create_hub(Settings(tmp_path / "hub.sqlite3", PRODUCTION_AUTH, auth), bus_app=bus)
    assert app.state.session_hub_bridge is not None
    with bus.state.agent_sessions._transaction() as db:
        assert db.execute("SELECT count(*) FROM session_hub_grants").fetchone()[0] == 0


def test_production_sessions_refuse_non_cloudflare_human_auth(tmp_path):
    bus, _ = _production_bus(tmp_path)
    auth = AuthConfig(ORIGIN, beau_github_id=42)
    with pytest.raises(ValueError, match="production_agent_connections_origin_and_storage_binding_required"):
        create_hub(Settings(tmp_path / "hub.sqlite3", PRODUCTION_AUTH, auth), bus_app=bus)


def test_production_sessions_refuse_an_unrestricted_source_ip_policy(tmp_path):
    config = AgentSessionsConfig(tmp_path / "agent-sessions.sqlite3", OWNER, ORIGIN,
                                 environment="production")
    with pytest.raises(ValueError, match="production_agent_sessions_source_ip_allowlist_required"):
        create_bus(db_path=tmp_path / "legacy.sqlite3", audit_path=tmp_path / "audit.jsonl",
            file_root=tmp_path / "files", auth=AUTH, startup_cleanup=False,
            push_events_operational=False, agent_sessions_config=config)


def test_production_session_database_must_live_under_hub_state_root(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    config = AgentSessionsConfig(outside / "agent-sessions.sqlite3", OWNER, ORIGIN,
                                 environment="production")
    bus = create_bus(db_path=tmp_path / "legacy.sqlite3", audit_path=tmp_path / "audit.jsonl",
        file_root=tmp_path / "files", auth=PRODUCTION_AUTH, startup_cleanup=False,
        push_events_operational=False, agent_sessions_config=config)
    auth = AuthConfig(ORIGIN, human_auth_mode="cloudflare_access",
        cloudflare_issuer="https://fixture.cloudflareaccess.com", cloudflare_audience="fixture-app",
        cloudflare_allowed_emails=("owner@example.test",))
    with pytest.raises(ValueError, match="production_agent_connections_origin_and_storage_binding_required"):
        create_hub(Settings(tmp_path / "hub.sqlite3", PRODUCTION_AUTH, auth), bus_app=bus)


def test_owner_capabilities_report_selected_environment(tmp_path):
    app = FastAPI()
    repository = AgentSessionRepository(tmp_path / "sessions.sqlite3", OWNER)
    attach_owner_routes(app, repository, lambda request, mutation=False: OWNER,
                        environment="production")
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get(OWNER_CONNECTION_PREFIX + "/capabilities").json() == {
            "enabled": True, "environment": "production", "owner": True}
