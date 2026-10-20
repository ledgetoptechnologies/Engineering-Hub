"""Proxy-reachable browser ingress still requires real Access signatures.

Only the JWKS HTTP transport is replaced.  No fixture supplies an already
verified identity, so signature, claims and session checks remain production
code. These are application-boundary checks, not tunnel/network acceptance.
"""
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.cloudflare_access import CloudflareAccessVerifier


BASE = "/api/hub/v1"
ORIGIN = "https://hub.example"
PROTECTED = ("/", "/index.html", "/bundle.js", BASE + "/overview")


@pytest.fixture
def signed_browser(tmp_path, monkeypatch):
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("private-dashboard")
    (ui / "bundle.js").write_text("private-script")
    auth = AuthConfig(
        ORIGIN, human_auth_mode="cloudflare_access",
        cloudflare_issuer="https://hub-test.cloudflareaccess.com",
        cloudflare_audience="hub-only", cloudflare_allowed_emails=("beau@example.com",),
    )
    config = Settings(tmp_path / "hub.db", AuthSettings("test-hermes", "test-codex"),
                      auth, ui, "browser")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = {**jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
              "kid": "proxy-browser-test", "alg": "RS256", "use": "sig"}
    endpoint = {"available": True, "requests": []}

    def keys(request):
        endpoint["requests"].append(str(request.url))
        assert str(request.url) == auth.cloudflare_issuer + "/cdn-cgi/access/certs"
        if not endpoint["available"]:
            return httpx.Response(503, text="key service unavailable")
        return httpx.Response(200, json={"keys": [public]})

    verifier = CloudflareAccessVerifier(auth.cloudflare_config, transport=httpx.MockTransport(keys))
    monkeypatch.setattr("engineering_hub.ingress.CloudflareAccessVerifier", lambda config: verifier)
    now = int(time.time())
    claims = {"iss": auth.cloudflare_issuer, "aud": ["hub-only"], "iat": now - 1,
              "exp": now + 600, "sub": "proxy-browser-subject", "email": "beau@example.com", "type": "app"}

    def sign(**overrides):
        return jwt.encode({**claims, **overrides}, key, algorithm="RS256", headers={"kid": public["kid"]})

    app = create_app(config)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False,
                    client=("192.0.2.45", 12345)) as client:
        yield client, sign, claims, endpoint


def test_proxy_signed_browser_login_session_and_csrf(signed_browser):
    client, sign, _, endpoint = signed_browser
    headers = {"CF-Access-Jwt-Assertion": sign()}
    assert client.get("/", headers=headers).text == "private-dashboard"
    assert client.get("/bundle.js", headers=headers).text == "private-script"
    assert client.get(BASE + "/overview", headers=headers).status_code == 401
    assert client.get(BASE + "/auth/login", headers=headers).status_code == 303
    me = client.get(BASE + "/auth/me", headers=headers).json()
    assert me["authenticated"] is True
    assert me["email"] == "beau@example.com"
    assert client.get(BASE + "/overview", headers=headers).status_code == 200
    body = {"project": "treedar", "title": "signed human task", "client_request_id": "proxy-test"}
    assert client.post(BASE + "/tasks", headers=headers, json=body).status_code == 403
    assert client.post(BASE + "/tasks", headers={**headers, "Origin": ORIGIN,
                       "X-Hub-CSRF": me["csrf_token"]}, json=body).status_code == 201
    assert len(endpoint["requests"]) == 1


@pytest.mark.parametrize("invalid", ["issuer", "audience", "expired", "email", "unsigned", "signature"])
def test_proxy_invalid_real_assertions_deny_assets_and_api_even_with_session(signed_browser, invalid):
    client, sign, claims, _ = signed_browser
    assert client.get(BASE + "/auth/login", headers={"CF-Access-Jwt-Assertion": sign()}).status_code == 303
    if invalid == "issuer":
        token = sign(iss="https://other.cloudflareaccess.com")
    elif invalid == "audience":
        token = sign(aud=["ops-only"])
    elif invalid == "expired":
        token = sign(iat=int(time.time()) - 1000, exp=int(time.time()) - 1)
    elif invalid == "email":
        token = sign(email="other@example.com")
    elif invalid == "unsigned":
        token = jwt.encode(claims, "", algorithm="none", headers={"kid": "proxy-browser-test"})
    else:
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode(claims, other_key, algorithm="RS256", headers={"kid": "proxy-browser-test"})
    for path in PROTECTED:
        response = client.get(path, headers={"CF-Access-Jwt-Assertion": token})
        assert response.status_code == 401
        assert "private-dashboard" not in response.text and "private-script" not in response.text


@pytest.mark.parametrize("host", ["hub.example", "192.168.50.80:8771", "attacker.example"])
def test_proxy_headers_host_and_stolen_local_cookie_do_not_grant_access(signed_browser, host):
    client, sign, _, _ = signed_browser
    assert client.get(BASE + "/auth/login", headers={"CF-Access-Jwt-Assertion": sign()}).status_code == 303
    # Keep the genuine issued session cookie: it must not bypass fresh Access.
    for path in PROTECTED:
        response = client.get(path, headers={
            "Host": host, "X-Forwarded-Host": "hub.example", "X-Forwarded-Proto": "https",
            "Forwarded": "for=127.0.0.1;host=hub.example;proto=https",
            "CF-Connecting-IP": "127.0.0.1", "CF-Access-Authenticated-User-Email": "beau@example.com",
        })
        assert response.status_code == 401


@pytest.mark.parametrize("path", PROTECTED)
def test_proxy_agent_bearer_never_authenticates_browser(signed_browser, path):
    client, sign, _, _ = signed_browser
    response = client.get(path, headers={"Authorization": "Bearer test-codex",
                                        "CF-Access-Jwt-Assertion": sign()})
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/api/messages", "/api/presence", "/api/admin/cleanup"])
def test_proxy_browser_cannot_reach_machine_routes(signed_browser, path):
    client, sign, _, _ = signed_browser
    assert client.get(path, headers={"CF-Access-Jwt-Assertion": sign()}).status_code == 403


def test_proxy_jwks_unavailable_does_not_serve_assets_or_api(signed_browser):
    client, sign, _, endpoint = signed_browser
    endpoint["available"] = False
    for path in PROTECTED:
        response = client.get(path, headers={"CF-Access-Jwt-Assertion": sign()})
        assert response.status_code == 503
        assert "private-dashboard" not in response.text and "private-script" not in response.text
    assert len(endpoint["requests"]) == 1  # Retry cooldown prevents one outage becoming a fetch storm.
