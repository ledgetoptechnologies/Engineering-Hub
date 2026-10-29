"""Production connection-mode contract; local synthetic identities only."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.transport_security import TransportSecuritySettings
import pytest

from agent_message_bus.agent_sessions import AgentSessionRepository
from agent_message_bus.agent_sessions_api import OWNER_CONNECTION_PREFIX, attach_owner_routes
from agent_message_bus.agent_sessions_config import AgentSessionsConfig
from agent_message_bus.app import create_app as create_bus
from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app as create_hub
from engineering_hub.auth import AuthConfig


ORIGIN = "https://hub.example.test"
OWNER = "human:cf:" + "a" * 64
AUTH = AuthSettings("legacy-hermes", "legacy-codex", claude_token="legacy-claude")
PRODUCTION_AUTH = AuthSettings("legacy-hermes", "legacy-codex", allowed_ips=frozenset({"127.0.0.1"}),
                               claude_token="legacy-claude")


def test_disabled_is_still_default_and_staging_local_origin_remains_valid(tmp_path, monkeypatch):
    for name in ("HUB_AGENT_SESSIONS_MODE", "HUB_AGENT_SESSIONS_DB",
                 "HUB_AGENT_SESSIONS_OWNER", "HUB_AGENT_SESSIONS_ORIGIN",
                 "HUB_AGENT_SESSIONS_HUB_BRIDGE"):
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


def test_production_mode_requires_all_explicit_environment_fields(monkeypatch):
    monkeypatch.setenv("HUB_AGENT_SESSIONS_MODE", "production")
    monkeypatch.setenv("HUB_AGENT_SESSIONS_HUB_BRIDGE", "disabled")
    for name in ("HUB_AGENT_SESSIONS_DB", "HUB_AGENT_SESSIONS_OWNER", "HUB_AGENT_SESSIONS_ORIGIN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="explicit_production_agent_sessions_configuration_required"):
        AgentSessionsConfig.from_env()


def test_production_bridge_requires_production_session_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_AGENT_SESSIONS_MODE", "disabled")
    monkeypatch.setenv("HUB_AGENT_SESSIONS_HUB_BRIDGE", "enabled")
    with pytest.raises(ValueError, match="hub_bridge_requires_staging_sessions"):
        AgentSessionsConfig.from_env()


def _production_bus(tmp_path, *, bridge=False, auth=PRODUCTION_AUTH):
    config = AgentSessionsConfig(tmp_path / "agent-sessions.sqlite3", OWNER, ORIGIN,
                                 environment="production", hub_bridge=bridge)
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
