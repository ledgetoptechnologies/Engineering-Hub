"""Actual stateful MCP and preserved REST contracts for the unified owner.

These tests do not represent installed-host, registry publication or wake proof.
"""
import json
import asyncio
import sys
import importlib.util
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from agent_message_bus.app import create_app as create_bus
from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.frontends import HubFrontends


AUTH = AuthSettings("unified-hermes", "unified-codex", "unified-admin",
                    claude_token="unified-claude")
BASE = "http://127.0.0.1:8000"
BUS_TOOLS = {
    "send_message", "get_send_receipt", "presence_status", "receive_messages",
    "wait_for_messages", "claim_message", "ack_message", "reject_message", "get_message",
    "knowledge_put", "knowledge_get", "knowledge_list", "knowledge_search",
    "semantic_search", "semantic_status", "file_put", "file_get", "file_list", "file_history",
    "feedback_submit", "feedback_get", "feedback_list", "feedback_update", "bus_info",
}
HUB_TOOLS = {
    "hub_overview", "hub_task_get", "hub_task_claim", "hub_task_candidate", "hub_task_review",
    "hub_decision_request", "hub_run_list", "hub_topic_peers", "hub_topic_preflight",
}


def security():
    return TransportSecuritySettings(allowed_hosts=["hub.test", "127.0.0.1:8000"],
                                     allowed_origins=["https://hub.test"])


@pytest.fixture
def composed(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")
    bus = create_bus(db_path=tmp_path / "messages.sqlite3", audit_path=tmp_path / "audit.jsonl",
                     file_root=tmp_path / "files", auth=AUTH, push_events_operational=False,
                     transport_security_settings=security())
    config = Settings(tmp_path / "hub.sqlite3", AUTH, AuthConfig(
        "https://hub.test", human_auth_mode="cloudflare_access",
        cloudflare_issuer="https://hub-test.cloudflareaccess.com", cloudflare_audience="hub-only",
        cloudflare_allowed_emails=("beau@example.com",)),
        agent_allowed_hosts=("127.0.0.1:8000",))
    return create_app(config, bus_app=bus), bus, config


def headers(token=AUTH.codex_token, session=None):
    values = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    if token is not None:
        values["Authorization"] = f"Bearer {token}"
    if session is not None:
        values["Mcp-Session-Id"] = session
    return values


def rpc(client, method, params=None, *, token=AUTH.codex_token, session=None, path="/mcp"):
    return client.post(path, headers=headers(token, session), json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def result(response):
    assert response.status_code == 200, response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        assert len(data) == 1
        envelope = json.loads(data[0])
    else:
        envelope = response.json()
    assert "error" not in envelope, envelope
    return envelope["result"]


def initialize(client, token=AUTH.codex_token, path="/mcp"):
    response = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "unified-contract", "version": "1"}}, token=token, path=path)
    result(response)
    session = response.headers.get("Mcp-Session-Id")
    assert session, "The unified transport must retain stateful bus sessions"
    notified = client.post(path, headers=headers(token, session), json={
        "jsonrpc": "2.0", "method": "notifications/initialized"})
    assert notified.status_code == 202
    return session


def call(client, session, name, arguments=None, token=AUTH.codex_token):
    value = result(rpc(client, "tools/call", {"name": name, "arguments": arguments or {}},
                       token=token, session=session))
    if not value.get("isError") and "structuredContent" not in value:
        value["structuredContent"] = json.loads(value["content"][0]["text"])
    return value


def content(*args, **kwargs):
    value = call(*args, **kwargs)
    assert not value.get("isError"), value
    return value["structuredContent"]


def test_one_stateful_session_lists_exactly_bus_and_hub_tools(composed):
    core, bus, _ = composed
    assert core.state.settings.agent_auth is bus.state.service.auth
    with TestClient(core, base_url=BASE) as client:
        session = initialize(client)
        listed = result(rpc(client, "tools/list", session=session))["tools"]
        assert len(listed) == 33
        assert {tool["name"] for tool in listed} == BUS_TOOLS | HUB_TOOLS
        assert content(client, session, "hub_overview")["tasks"] == []
        assert bus.state.service.repository is bus.state.repository


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_no_auth_invalid_auth_and_foreign_sessions_denied(composed, path):
    core, _, _ = composed
    with TestClient(core, base_url=BASE) as client:
        session = initialize(client, path=path)
        for token in (None, "not-a-token", AUTH.admin_token):
            assert rpc(client, "tools/list", token=token, session=session, path=path).status_code == 401
        for method in ("tools/list", "tools/call"):
            assert rpc(client, method, token=AUTH.hermes_token, session=session, path=path).status_code == 404
        assert rpc(client, "tools/list", session="unknown-session", path=path).status_code == 404
        assert client.delete(path, headers=headers(AUTH.hermes_token, session)).status_code == 404
        assert result(rpc(client, "tools/list", session=session, path=path))["tools"]


def test_message_lifecycle_crosses_mcp_and_original_rest_paths(composed):
    core, _, _ = composed
    with TestClient(core, base_url=BASE) as client:
        codex = initialize(client)
        hermes = initialize(client, AUTH.hermes_token)
        sent = content(client, hermes, "send_message", {"recipient": "codex", "message_type": "info",
                       "body": {"topic": "unified-test"}, "client_request_id": "unified-send-1"},
                       token=AUTH.hermes_token)
        mid = sent["id"]
        assert content(client, codex, "receive_messages")["messages"][0]["id"] == mid
        assert client.get(f"/api/messages/{mid}", headers=headers(AUTH.hermes_token)).status_code == 404
        assert call(client, hermes, "get_message", {"message_id": mid}, token=AUTH.hermes_token)["isError"]
        claimed = client.post(f"/api/messages/{mid}/claim", headers=headers())
        assert claimed.status_code == 200 and claimed.json()["status"] == "claimed"
        assert content(client, codex, "ack_message", {"message_id": mid})["status"] == "acknowledged"
        assert client.get(f"/api/messages/{mid}", headers=headers()).json()["status"] == "acknowledged"
        receipt = client.get("/api/message-receipts", params={"client_request_id": "unified-send-1"},
                             headers=headers(AUTH.hermes_token))
        assert receipt.status_code == 200 and receipt.json()["message_id"] == mid


def test_shared_knowledge_files_and_hub_task_use_same_session(composed):
    core, _, _ = composed
    with TestClient(core, base_url=BASE) as client:
        codex = initialize(client)
        claude = initialize(client, AUTH.claude_token)
        document = {"project": "engineering-hub", "path": "cutover.md", "title": "Cutover",
                    "content": "Durable unified knowledge", "expected_version": 0}
        assert content(client, codex, "knowledge_put", document)["updated_by"] == "codex"
        read = content(client, claude, "knowledge_get", {"project": document["project"],
                        "path": document["path"]}, token=AUTH.claude_token)
        assert read["content"] == document["content"]
        assert call(client, claude, "knowledge_put", document, token=AUTH.claude_token)["isError"]
        metadata = content(client, codex, "file_put", {"project": "engineering-hub", "path": "plan.txt",
                           "content": "versioned file", "expected_version": 0})
        assert metadata["version"] == 1
        assert content(client, claude, "file_get", {"project": "engineering-hub", "path": "plan.txt"},
                       token=AUTH.claude_token)["content"] == "versioned file"
        task = core.state.repository.create_task("human:123", "engineering-hub", "Cutover fixture", "unified-1")
        assert content(client, codex, "hub_task_claim", {"task_id": task["id"], "expected_revision": 1})["owner"] == "codex"
        assert content(client, claude, "hub_overview", token=AUTH.claude_token)["tasks"][0]["owner"] == "codex"


def test_legacy_rest_routes_and_admin_boundary_preserved(composed):
    core, _, _ = composed
    with TestClient(core, base_url=BASE) as client:
        for path in ("/api/messages", "/api/presence", "/api/files", "/api/feedback"):
            assert client.get(path).status_code == 401
            assert client.get(path, headers=headers()).status_code == 200
        assert client.post("/api/admin/cleanup", headers=headers()).status_code == 401
        assert client.post("/api/admin/cleanup", headers=headers(AUTH.admin_token)).status_code == 200


@pytest.mark.parametrize("invalid", ["copied-auth", "same-database"])
def test_shared_auth_object_and_distinct_database_required(tmp_path, monkeypatch, invalid):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")
    bus = create_bus(db_path=tmp_path / "bus.db", audit_path=tmp_path / "audit.jsonl",
                     file_root=tmp_path / "files", auth=AUTH, push_events_operational=False,
                     transport_security_settings=security())
    config = Settings(tmp_path / "hub.db", AUTH, AuthConfig("https://hub.test"),
                      agent_allowed_hosts=("127.0.0.1:8000",))
    if invalid == "copied-auth":
        config = replace(config, agent_auth=replace(AUTH))
    else:
        config = replace(config, database=bus.state.repository.path)
    with pytest.raises(ValueError):
        create_app(config, bus_app=bus)


def test_health_reports_embedded_failure_without_hiding_hub(composed):
    core, bus, _ = composed
    with TestClient(core, base_url=BASE) as client:
        disabled = client.get("/health").json()
        assert disabled["ok"] is False and disabled["bus"]["schema_version"] == 4
        assert disabled["bus"]["push"]["state"] == "disabled_configuration"
        bus.state.push_status.update(state="failed", reason="SyntheticFailure")
        failed = client.get("/health").json()
        assert failed["ok"] is False
        assert failed["bus"]["push"]["state"] == "failed"
        assert failed["service"] == "engineering-hub-control"


@pytest.mark.parametrize("change,expected", [
    ({}, True),
    ({"ok": False}, False),
    ({"database": "error"}, False),
    ({"audit": "invalid"}, False),
    ({"audit": "error"}, False),
    ({"push": {"state": "disabled_configuration", "subscriptions_enabled": False}}, False),
    ({"push": {"state": "disabled_unsupported", "subscriptions_enabled": False}}, False),
    ({"push": {"state": "healthy", "subscriptions_enabled": False}}, False),
    ({"push": {"state": "failed", "subscriptions_enabled": True}}, False),
])
def test_unified_health_does_not_trust_legacy_ok_flag_alone(tmp_path, monkeypatch, change, expected):
    """Synthetic health summaries exercise readiness projection, not watcher readiness."""
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")
    bus = create_bus(db_path=tmp_path / "bus.db", audit_path=tmp_path / "audit.jsonl",
                     file_root=tmp_path / "files", auth=AUTH, push_events_operational=False,
                     transport_security_settings=security())
    summary = {"ok": True, "database": "ok", "audit": "ok", "schema_version": 4,
               "push": {"state": "healthy", "subscriptions_enabled": True}, **change}

    async def synthetic_bus_health(request):
        return JSONResponse(summary)

    route = next(route for route in bus.routes if getattr(route, "path", None) == "/health")
    route.endpoint = synthetic_bus_health
    core = create_app(Settings(tmp_path / "hub.db", AUTH, AuthConfig("https://hub.test"),
                               agent_allowed_hosts=("127.0.0.1:8000",)), bus_app=bus)
    with TestClient(core, base_url=BASE) as client:
        projected = client.get("/health").json()
        assert projected["ok"] is expected
        assert projected["bus"] == summary


@pytest.mark.asyncio
async def test_browser_denies_all_bus_paths_before_access_verification(composed, monkeypatch):
    core, _, _ = composed
    def unexpected_verifier(*args, **kwargs):
        raise AssertionError("Bus paths must be denied before human Access verification")
    monkeypatch.setattr("engineering_hub.ingress.CloudflareAccessVerifier.verify", unexpected_verifier)
    pair = HubFrontends(core)
    async with pair.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.browser), base_url=BASE) as browser:
            for path in ("/mcp", "/mcp/", "/api/messages", "/api/messages/id/claim",
                         "/api/message-receipts/id", "/api/presence", "/api/files/file",
                         "/api/feedback", "/api/admin/cleanup"):
                response = await browser.get(path, headers={"CF-Access-Jwt-Assertion": "anything"})
                assert response.status_code == 403, (path, response.text)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.agents), base_url=BASE) as agent:
            assert (await agent.get("/api/messages", headers=headers())).status_code == 200
            assert (await agent.get("/api/hub/v1/overview", headers=headers())).status_code == 200


def test_source_ip_allowlist_applies_to_both_bus_and_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")
    auth = replace(AUTH, allowed_ips=frozenset({"192.0.2.1"}))
    bus = create_bus(db_path=tmp_path / "bus.db", audit_path=tmp_path / "audit.jsonl",
                     file_root=tmp_path / "files", auth=auth, push_events_operational=False,
                     transport_security_settings=security())
    core = create_app(Settings(tmp_path / "hub.db", auth, AuthConfig("https://hub.test"),
                               agent_allowed_hosts=("127.0.0.1:8000",)), bus_app=bus)
    with TestClient(core, base_url=BASE) as client:
        for path in ("/mcp", "/api/messages", "/api/hub/v1/overview"):
            response = rpc(client, "initialize", path=path) if path == "/mcp" else client.get(path, headers=headers())
            assert response.status_code == 403, (path, response.text)


@pytest.mark.parametrize("invalid", ["no-access", "wrong-ingress", "missing-paths", "relative-path"])
def test_factory_rejects_configuration_before_constructing_bus(tmp_path, monkeypatch, invalid):
    from engineering_hub.unified_app import create_unified_app

    config = Settings(tmp_path / "hub.db", AUTH, AuthConfig(
        "https://hub.test", human_auth_mode="cloudflare_access",
        cloudflare_issuer="https://hub-test.cloudflareaccess.com", cloudflare_audience="hub-only",
        cloudflare_allowed_emails=("beau@example.com",)))
    for name in ("AGENT_BUS_DB", "AGENT_BUS_AUDIT", "AGENT_BUS_FILE_ROOT"):
        monkeypatch.delenv(name, raising=False)
    if invalid == "no-access":
        config = replace(config, auth_config=AuthConfig("https://hub.test"))
    elif invalid == "wrong-ingress":
        config = replace(config, ingress_mode="agents")
    elif invalid == "relative-path":
        monkeypatch.setenv("AGENT_BUS_DB", "relative/bus.db")

    def forbidden_construction(*args, **kwargs):
        raise AssertionError("Invalid factory configuration must not construct/migrate bus state")

    monkeypatch.setattr("agent_message_bus.app.create_app", forbidden_construction)
    with pytest.raises(ValueError):
        create_unified_app(config)


@asynccontextmanager
async def native_client(url, token):
    notifications = asyncio.Queue()

    async def on_message(message):
        if isinstance(message, types.ServerNotification):
            await notifications.put(message)

    # The test server uses a reserved ephemeral socket; pin an approved Host
    # rather than permitting arbitrary ephemeral hosts in production settings.
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}",
                                          "Host": "127.0.0.1:8000"},
                                  timeout=httpx.Timeout(10, read=None)) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, session_id):
            async with ClientSession(read, write, message_handler=on_message) as session:
                initialized = await session.initialize()
                yield session, notifications, session_id, initialized


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Requires the actual Linux inotify SQLite event source")
async def test_native_bus_subscription_survives_hub_composition(tmp_path, monkeypatch):
    # pytest's importlib mode deliberately does not put tests/ on sys.path.
    # Load the existing, side-effect-free loopback helpers by their exact file.
    spec = importlib.util.spec_from_file_location(
        "_unified_native_helpers", Path(__file__).with_name("test_mcp_push.py"))
    assert spec is not None and spec.loader is not None
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    loopback, no_notification, resource_json = helpers.loopback, helpers.no_notification, helpers.resource_json

    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")
    bus = create_bus(db_path=tmp_path / "bus.db", audit_path=tmp_path / "audit.jsonl",
                     file_root=tmp_path / "files", auth=AUTH, push_events_operational=True,
                     transport_security_settings=security())
    core = create_app(Settings(tmp_path / "hub.db", AUTH, AuthConfig("https://hub.test"),
                               agent_allowed_hosts=("127.0.0.1:8000",)), bus_app=bus)
    async with loopback(core) as url:
        async with httpx.AsyncClient() as http:
            health = (await http.get(url.removesuffix("/mcp") + "/health")).json()
            assert health["ok"] is True and health["bus"]["push"]["state"] == "healthy"
        async with native_client(url, AUTH.codex_token) as (codex, events, sid, initialized):
            assert initialized.capabilities.resources.subscribe is True
            assert sid()
            assert {tool.name for tool in (await codex.list_tools()).tools} == BUS_TOOLS | HUB_TOOLS
            assert [str(item.uri) for item in (await codex.list_resources()).resources] == ["agentbus://inbox/codex"]
            await codex.subscribe_resource(AnyUrl("agentbus://inbox/codex"))
            async with native_client(url, AUTH.hermes_token) as (hermes, other_events, _, _):
                assert not (await hermes.call_tool("hub_overview")).isError
                sent = await hermes.call_tool("send_message", {
                    "recipient": "codex", "message_type": "info", "body": {"topic": "unified-native"}})
                assert not sent.isError
                event = await asyncio.wait_for(events.get(), 3)
                assert str(event.root.params.uri) == "agentbus://inbox/codex"
                assert set(event.root.params.model_dump(by_alias=True, exclude_none=True)) == {"uri"}
                assert await no_notification(other_events)
                page = resource_json(await codex.read_resource(AnyUrl("agentbus://inbox/codex?after=0")))
                assert page["messages"][0]["body"] == {"topic": "unified-native"}
                with pytest.raises(McpError):
                    await hermes.read_resource(AnyUrl("agentbus://inbox/codex?after=0"))
                with pytest.raises(McpError):
                    await hermes.subscribe_resource(AnyUrl("agentbus://inbox/codex"))
            await codex.unsubscribe_resource(AnyUrl("agentbus://inbox/codex"))
            assert bus.state.push_publisher.subscription_count == 0
