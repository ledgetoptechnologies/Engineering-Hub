"""Listener separation is tested without live credentials, Cloudflare or hosts."""
import asyncio
import time
from dataclasses import replace

import pytest
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.cloudflare_access import (
    AccessDenied, AccessUnavailable, CloudflareAccessVerifier, CloudflareIdentity,
)
from engineering_hub.ingress import HubIngressMiddleware

BASE = "/api/hub/v1"
ORIGIN = "https://hub.example"


def settings(tmp_path, mode="agents", *, ips=frozenset(), configured=True):
    ui = tmp_path / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "index.html").write_text("private-dashboard")
    (ui / "bundle.js").write_text("private-script")
    auth = AuthConfig(ORIGIN, beau_github_id=42, human_auth_mode="cloudflare_access",
                      cloudflare_issuer="https://hub-test.cloudflareaccess.com" if configured else "",
                      cloudflare_audience="hub-only" if configured else "",
                      cloudflare_allowed_emails=("beau@example.com",) if configured else ())
    return Settings(tmp_path / "hub.db", AuthSettings("test-hermes", "test-codex",
                    claude_token="test-claude", allowed_ips=ips), auth, ui, mode)


@pytest.fixture
def checker(monkeypatch):
    class Checker:
        calls = []

        def __init__(self, config):
            assert config.audience == "hub-only"

        async def verify(self, token):
            self.calls.append(token)
            if token == "unavailable":
                raise AccessUnavailable()
            if token == "crash":
                raise RuntimeError("private internal error")
            if token == "cancel":
                raise asyncio.CancelledError()
            if token != "signed-test-fixture":
                raise AccessDenied()
            return CloudflareIdentity("subject", "beau@example.com", "human:cf:" + "a" * 64,
                                      int(time.time()) + 600)

    monkeypatch.setattr("engineering_hub.ingress.CloudflareAccessVerifier", Checker)
    return Checker


def test_ingress_validation_before_app_start(tmp_path):
    with pytest.raises(ValueError, match="Unknown Hub ingress"):
        settings(tmp_path, "typo")
    with pytest.raises(ValueError, match="configured Cloudflare"):
        settings(tmp_path, "browser", configured=False)
    with pytest.raises(ValueError, match="configured Cloudflare"):
        Settings(tmp_path / "x.db", AuthSettings("h", "c"), AuthConfig(ORIGIN),
                 ingress_mode="browser")
    assert not (tmp_path / "hub.db").exists()


def test_ingress_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_DB", str(tmp_path / "env.db"))
    monkeypatch.setenv("HERMES_TOKEN", "test-h")
    monkeypatch.setenv("CODEX_TOKEN", "test-c")
    monkeypatch.delenv("HUB_INGRESS_MODE", raising=False)
    assert Settings.from_env().ingress_mode == "combined"
    monkeypatch.setenv("HUB_INGRESS_MODE", "agents")
    assert Settings.from_env().ingress_mode == "agents"
    monkeypatch.setenv("HUB_INGRESS_MODE", "AGENTS")
    with pytest.raises(ValueError, match="Unknown Hub ingress"):
        Settings.from_env()


@pytest.mark.parametrize("component", ["fixture_workflow", "container_pilot_workflow",
                                     "staging_dispatcher", "owned_pilot_service"])
def test_browser_never_owns_staging_execution(tmp_path, component):
    with pytest.raises(ValueError, match="must not own staging"):
        create_app(settings(tmp_path, "browser"), **{component: object()})
    assert not (tmp_path / "hub.db").exists()


@pytest.mark.parametrize("identity", ["hermes", "codex", "claude"])
def test_agent_bearer_api_and_identity(tmp_path, identity):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post(BASE + "/decisions", headers={"Authorization": f"Bearer test-{identity}"},
                               json={"project": "treedar", "title": "test", "options": ["a", "b"]})
        assert response.status_code == 201
        task = app.state.repository.create_task("human:42", "treedar", "test", "task")
        claimed = client.post(BASE + "/tasks/" + task["id"] + "/claim",
                              headers={"Authorization": f"Bearer test-{identity}"},
                              json={"expected_revision": task["revision"]})
        assert claimed.status_code == 200 and claimed.json()["owner"] == identity
        assert client.get(BASE + "/overview", headers={"Authorization": f"Bearer test-{identity}"}).status_code == 200


@pytest.mark.parametrize("path", ["/", "/index.html", "/bundle.js", "/docs", "/openapi.json",
                                  BASE + "/auth/login", BASE + "/auth/me", BASE + "/auth/",
                                  "/mcpevil", "/api/hub/v10/overview", "/health/", "/healthz"])
def test_agents_cannot_serve_browser_paths_even_with_valid_token(tmp_path, path):
    with TestClient(create_app(settings(tmp_path)), base_url=ORIGIN, follow_redirects=False) as client:
        response = client.get(path, headers={"Authorization": "Bearer test-codex", "Host": "hub.example",
                                             "CF-Access-Jwt-Assertion": "forged", "X-Forwarded-Host": "hub.example"})
        assert response.status_code == 403
        assert "location" not in response.headers
        assert "private-dashboard" not in response.text


@pytest.mark.parametrize("headers", [[], [("Authorization", "")], [("Authorization", "Basic test-codex")],
                                    [("Authorization", "Bearer wrong")],
                                    [("Authorization", "Bearer test-codex"), ("authorization", "Bearer test-hermes")],
                                    [("Authorization", "Bearer test-codex, Bearer test-hermes")]])
def test_agents_reject_missing_malformed_duplicate_authorization(tmp_path, headers):
    with TestClient(create_app(settings(tmp_path)), base_url=ORIGIN) as client:
        client.cookies.set("hub_session", "pretend-human-session")
        response = client.get(BASE + "/overview", headers=headers)
        assert response.status_code == 401


def test_agent_source_ip_cannot_be_forwarded_or_access_header(tmp_path):
    app = create_app(settings(tmp_path, ips=frozenset({"192.0.2.5"})))
    with TestClient(app, base_url=ORIGIN, client=("192.0.2.6", 123)) as client:
        response = client.get(BASE + "/overview", headers={"Authorization": "Bearer test-codex",
                             "X-Forwarded-For": "192.0.2.5", "CF-Connecting-IP": "192.0.2.5",
                             "CF-Access-Authenticated-User-Email": "beau@example.com"})
        assert response.status_code == 403
    with TestClient(create_app(settings(tmp_path, ips=frozenset({"192.0.2.5"}))),
                    base_url=ORIGIN, client=("192.0.2.5", 123)) as client:
        assert client.get(BASE + "/overview", headers={"Authorization": "Bearer test-codex"}).status_code == 200


def test_cookie_and_access_headers_never_sufficient_on_agent_api(tmp_path):
    config = settings(tmp_path)
    app = create_app(config)
    session = app.state.sessions.create_session(42)
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set("hub_session", session["cookie_token"])
        assert client.get(BASE + "/overview", headers={"CF-Access-Jwt-Assertion": "anything",
                          "CF-Access-Authenticated-User-Email": "beau@example.com"}).status_code == 401


@pytest.mark.parametrize("mode", ["agents", "browser"])
@pytest.mark.parametrize("path", ["/%6dcp", "/api/hub/v1%2foverview", "/api/hub/v1/%2e%2e/auth",
                                  "/health%2f", "/%68ealth", "/api//hub/v1/overview"])
def test_separated_encoded_and_ambiguous_paths_denied(tmp_path, checker, mode, path):
    with TestClient(create_app(settings(tmp_path, mode)), base_url=ORIGIN, follow_redirects=False) as client:
        response = client.get(path, headers={"Authorization": "Bearer test-codex"} if mode == "agents"
                              else {"CF-Access-Jwt-Assertion": "signed-test-fixture"})
        assert response.status_code == 403


@pytest.mark.parametrize("path", ["/", "/index.html", "/bundle.js", "/docs", "/openapi.json",
                                  BASE + "/auth/login", BASE + "/overview", "/health/"])
def test_browser_all_assets_and_auth_require_access_assertion(tmp_path, checker, path):
    with TestClient(create_app(settings(tmp_path, "browser")), base_url=ORIGIN) as client:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"CF-Access-Jwt-Assertion": "forged"}).status_code == 401
        assert client.get(path, headers={"CF-Access-Authenticated-User-Email": "beau@example.com",
                                        "X-Forwarded-Host": "hub.example"}).status_code == 401


@pytest.mark.parametrize("headers", [[("Authorization", "")], [("Authorization", "Bearer test-codex")],
                                     [("AUTHORIZATION", "malformed")],
                                     [("Authorization", "a"), ("authorization", "b")]])
def test_browser_rejects_every_authorization_before_verifier(tmp_path, checker, headers):
    with TestClient(create_app(settings(tmp_path, "browser")), base_url=ORIGIN) as client:
        response = client.get("/", headers=headers + [("CF-Access-Jwt-Assertion", "signed-test-fixture")])
        assert response.status_code in {401, 403}
        assert checker.calls == []


def test_browser_duplicate_assertion_and_verifier_failures(tmp_path, checker):
    with TestClient(create_app(settings(tmp_path, "browser")), base_url=ORIGIN) as client:
        assert client.get("/", headers=[("CF-Access-Jwt-Assertion", "signed-test-fixture")] * 2).status_code == 401
        for token in ("unavailable", "crash"):
            response = client.get("/bundle.js", headers={"CF-Access-Jwt-Assertion": token})
            assert response.status_code == 503
            assert "private" not in response.text


def test_browser_current_human_session_csrf_and_single_verification(tmp_path, checker):
    with TestClient(create_app(settings(tmp_path, "browser")), base_url=ORIGIN,
                    follow_redirects=False) as client:
        signed = {"CF-Access-Jwt-Assertion": "signed-test-fixture"}
        assert client.get("/", headers=signed).text == "private-dashboard"
        assert client.get("/bundle.js", headers=signed).text == "private-script"
        assert client.get(BASE + "/overview", headers=signed).status_code == 401  # Still needs local session.
        assert client.get(BASE + "/auth/login", headers=signed).status_code == 303
        me = client.get(BASE + "/auth/me", headers=signed).json()
        assert me["authenticated"] and me["email"] == "beau@example.com"
        assert client.get(BASE + "/overview", headers=signed).status_code == 200
        assert len(checker.calls) == 6  # One assertion check per request; no legacy duplicate.
        payload = {"project": "treedar", "title": "human task", "client_request_id": "test"}
        assert client.post(BASE + "/tasks", headers=signed, json=payload).status_code == 403
        csrf = {**signed, "Origin": ORIGIN, "X-Hub-CSRF": me["csrf_token"]}
        assert client.post(BASE + "/tasks", headers=csrf, json=payload).status_code == 201
        assert client.get(BASE + "/overview").status_code == 401  # Cookie alone never enough.
        assert client.get("/mcp/", headers=signed).status_code == 403


@pytest.mark.parametrize("mode", ["agents", "browser"])
def test_exact_health_is_public_but_not_other_methods(tmp_path, checker, mode):
    with TestClient(create_app(settings(tmp_path, mode)), base_url=ORIGIN) as client:
        assert client.get("/health").status_code == 200
        assert client.head("/health").status_code == 200
        assert client.post("/health").status_code in {401, 403}
        assert not checker.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agents", "browser"])
async def test_pure_asgi_websocket_rejection_and_lifespan_forwarded(checker, mode):
    received = []
    sent = []

    async def downstream(scope, receive, send):
        received.append(scope["type"])

    async def send(message):
        sent.append(message)

    config = AuthConfig(ORIGIN, human_auth_mode="cloudflare_access",
                        cloudflare_issuer="https://hub-test.cloudflareaccess.com",
                        cloudflare_audience="hub-only", cloudflare_allowed_emails=("beau@example.com",))
    middleware = HubIngressMiddleware(downstream, mode=mode, agent_auth=AuthSettings("h", "c"),
                                       cloudflare_config=config.cloudflare_config)
    await middleware({"type": "websocket"}, None, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]
    await middleware({"type": "lifespan"}, None, send)
    assert received == ["lifespan"]


def test_combined_default_still_serves_assets_and_agent_api(tmp_path):
    with TestClient(create_app(settings(tmp_path, "combined")), base_url=ORIGIN) as client:
        assert client.get("/").text == "private-dashboard"
        assert client.get(BASE + "/overview", headers={"Authorization": "Bearer test-codex"}).status_code == 200


@pytest.mark.parametrize("host", ["192.168.50.80:8771", "hub-control-agents:8770", "[::1]:8770", "localhost"])
def test_explicit_mcp_agent_hosts(tmp_path, host):
    config = replace(settings(tmp_path), agent_allowed_hosts=(host,))
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        headers = {"Authorization": "Bearer test-codex", "Accept": "application/json, text/event-stream"}
        payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
        for allowed in (host, "hub.example"):
            assert client.post("/mcp/", headers={**headers, "Host": allowed}, json=payload).status_code == 200
        assert client.post("/mcp/", headers={**headers, "Host": "attacker.example"}, json=payload).status_code == 421
        assert client.get("/", headers={**headers, "Host": host}).status_code == 403


@pytest.mark.parametrize("host", ["*", "192.168.50.80:*", "https://hub.example", "user@hub.example",
                                  "hub.example/path", "hub.example?x", "hub.example#x", "hub.example:",
                                  "hub.example:0", "hub.example:65536", "hub.example:abc", "hub.example:08770",
                                  "[not-ipv6]:8770", "::1", "host\nname", "host name", "host\\name",
                                  "host%2fname", "", "Hub.example", "host.", "a..b", "a:80:90"])
def test_agent_host_rejects_nonexact_or_malformed_values(tmp_path, host):
    with pytest.raises(ValueError, match="Agent allowed hosts"):
        replace(settings(tmp_path), agent_allowed_hosts=(host,))


def test_agent_hosts_env_and_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_TOKEN", "h")
    monkeypatch.setenv("CODEX_TOKEN", "c")
    monkeypatch.setenv("HUB_AGENT_ALLOWED_HOSTS", "192.168.50.80:8771, [::1]:8771")
    assert Settings.from_env().agent_allowed_hosts == ("192.168.50.80:8771", "[::1]:8771")
    with pytest.raises(ValueError, match="Duplicate"):
        replace(settings(tmp_path), agent_allowed_hosts=("localhost", "localhost"))


def test_browser_actual_signed_assertion_and_forgery_with_local_key_transport(tmp_path, monkeypatch):
    config = settings(tmp_path, "browser")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = {**jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
              "kid": "ingress-test", "alg": "RS256", "use": "sig"}
    fetches = []

    def keys(request):
        fetches.append(str(request.url))
        return httpx.Response(200, json={"keys": [public]})

    verifier = CloudflareAccessVerifier(config.auth_config.cloudflare_config,
                                       transport=httpx.MockTransport(keys))
    monkeypatch.setattr("engineering_hub.ingress.CloudflareAccessVerifier", lambda config: verifier)
    now = int(time.time())
    claims = {"iss": config.auth_config.cloudflare_issuer, "aud": ["hub-only"], "iat": now - 1,
              "exp": now + 600, "sub": "real-signed-subject", "email": "beau@example.com", "type": "app"}
    signed = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "ingress-test"})
    forged = jwt.encode(claims, "forged-test-key-" * 3, algorithm="HS256", headers={"kid": "ingress-test"})
    wrong_audience = jwt.encode({**claims, "aud": ["ops-only"]}, key, algorithm="RS256",
                               headers={"kid": "ingress-test"})
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong_signature = jwt.encode(claims, other_key, algorithm="RS256", headers={"kid": "ingress-test"})
    with TestClient(create_app(config), base_url=ORIGIN, follow_redirects=False) as client:
        assert client.get("/", headers={"CF-Access-Jwt-Assertion": forged}).status_code == 401
        assert not fetches
        header = {"CF-Access-Jwt-Assertion": signed}
        assert client.get("/", headers=header).text == "private-dashboard"
        assert client.get(BASE + "/auth/login", headers=header).status_code == 303
        assert client.get(BASE + "/overview", headers=header).status_code == 200
        for bad in (wrong_audience, wrong_signature):
            for path in ("/", "/bundle.js", BASE + "/overview"):
                assert client.get(path, headers={"CF-Access-Jwt-Assertion": bad}).status_code == 401
        assert len(fetches) == 1
        assert client.get("/", headers={"CF-Access-Jwt-Assertion": forged}).status_code == 401


@pytest.mark.asyncio
async def test_ingress_preserves_response_stream_and_propagates_cancellation(checker):
    messages = []
    body_reads = []

    async def downstream(scope, receive, send):
        assert scope["state"]["cloudflare_identity"].email == "beau@example.com"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await send({"type": "http.response.body", "body": b"second", "more_body": False})

    async def receive():
        body_reads.append(True)
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    config = AuthConfig(ORIGIN, human_auth_mode="cloudflare_access",
                        cloudflare_issuer="https://hub-test.cloudflareaccess.com",
                        cloudflare_audience="hub-only", cloudflare_allowed_emails=("beau@example.com",))
    middleware = HubIngressMiddleware(downstream, mode="browser", agent_auth=AuthSettings("h", "c"),
                                       cloudflare_config=config.cloudflare_config)
    scope = {"type": "http", "path": "/api/hub/v1/events", "method": "GET",
             "headers": [(b"cf-access-jwt-assertion", b"signed-test-fixture")]}
    await middleware(scope, receive, send)
    assert [message.get("body") for message in messages[1:]] == [b"first", b"second"]
    assert not body_reads
    scope["headers"] = [(b"cf-access-jwt-assertion", b"cancel")]
    with pytest.raises(asyncio.CancelledError):
        await middleware(scope, receive, send)
