"""Real signatures with synthetic identities; no live policy or credentials."""
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig, SessionStore
from engineering_hub import cloudflare_access as access
from engineering_hub.dto import DecisionDTO


BASE = "/api/hub/v1"
ISSUER = "https://fixture-team.cloudflareaccess.com"
AUDIENCE = "fixture-eh-only-audience"
EMAILS = ("operator@first.example", "operator@second.example")


@pytest.fixture
def hub(tmp_path, monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="fixture-key", alg="RS256", use="sig")
    requests = []

    def keys(request):
        requests.append(str(request.url))
        assert str(request.url) == ISSUER + "/cdn-cgi/access/certs"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"keys": [jwk]})

    config = AuthConfig("https://hub.example", beau_github_id=42,
                        human_auth_mode="cloudflare_access", cloudflare_issuer=ISSUER,
                        cloudflare_audience=AUDIENCE, cloudflare_allowed_emails=EMAILS)
    verifier = access.CloudflareAccessVerifier(config.cloudflare_config, transport=httpx.MockTransport(keys))
    monkeypatch.setattr(access, "CloudflareAccessVerifier", lambda _: verifier)
    settings = Settings(tmp_path / "hub.sqlite", AuthSettings("test-hermes", "test-codex", claude_token="test-claude"), config)
    app = create_app(settings)

    def assertion(**overrides):
        now = int(time.time())
        claims = dict(iss=ISSUER, aud=[AUDIENCE], sub="synthetic-operator", email=EMAILS[0],
                      type="app", iat=now - 1, exp=now + 300)
        claims.update(overrides)
        return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "fixture-key"})

    with TestClient(app, base_url=config.origin, follow_redirects=False) as client:
        yield client, app, config, assertion, requests


def sign_in(client, token):
    headers = {"Cf-Access-Jwt-Assertion": token}
    response = client.get(BASE + "/auth/login", headers=headers)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/"
    assert "Secure" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    data = client.get(BASE + "/auth/me", headers=headers).json()
    assert data["authenticated"] and data["github_id"] is None
    return {**headers, "Origin": "https://hub.example", "X-Hub-CSRF": data["csrf_token"]}, data


@pytest.mark.parametrize("email", EMAILS)
def test_exact_allowed_identities_can_create_tasks_answer_and_pause(hub, email):
    client, app, _, assertion, requests = hub
    headers, data = sign_in(client, assertion(email=email))
    assert data["email"] == email and data["auth_provider"] == "cloudflare_access"
    assert data["actor"].startswith("human:cf:")
    task = client.post(BASE + "/tasks", headers=headers, json={
        "project": "engineering-hub", "title": "Synthetic task", "client_request_id": "cf-task"})
    assert task.status_code == 201, task.text
    decision = app.state.repository.create_decision("codex", "engineering-hub", "Choose fixture", ["A", "B"])
    answered = client.post(BASE + f"/decisions/{decision['id']}/answer", headers=headers,
                           json={"expected_revision": decision["revision"], "answer": "A"})
    assert answered.status_code == 200, answered.text
    assert DecisionDTO.model_validate(answered.json()).answered_by == data["actor"]
    assert client.post(BASE + "/pause", headers=headers, json={"paused": True}).status_code == 200
    assert app.state.repository.list_audit()[0]["actor"] == data["actor"]
    assert len(requests) == 1


@pytest.mark.parametrize("override", [
    {"email": "other@first.example"}, {"email": "operator@first.example.evil"},
    {"aud": ["ops-audience"]}, {"iss": "https://other.cloudflareaccess.com"},
    {"exp": 1}, {"type": "service"}, {"email": None},
])
def test_valid_signature_is_not_sufficient_for_human_access(hub, override):
    client, _, _, assertion, _ = hub
    response = client.get(BASE + "/auth/login", headers={"Cf-Access-Jwt-Assertion": assertion(**override)})
    assert response.status_code == 401
    assert "hub_session=" not in response.headers.get("set-cookie", "")
    assert client.get(BASE + "/auth/me").json()["authenticated"] is False


def test_session_alone_or_forged_email_never_bypasses_assertion(hub):
    client, _, _, assertion, _ = hub
    headers, _ = sign_in(client, assertion())
    for forged in ({}, {"Cf-Access-Authenticated-User-Email": EMAILS[0]},
                   {"Cf-Access-Jwt-Assertion": "forged", "Cf-Access-Authenticated-User-Email": EMAILS[0]}):
        assert client.get(BASE + "/overview", headers=forged).status_code == 401
        assert client.get(BASE + "/auth/me", headers=forged).json()["authenticated"] is False
    assert client.get(BASE + "/overview", headers=headers).status_code == 200


def test_binding_logout_csrf_and_github_fallback_are_closed(hub):
    client, app, config, assertion, _ = hub
    headers, _ = sign_in(client, assertion())
    other = {"Cf-Access-Jwt-Assertion": assertion(sub="other-operator")}
    assert client.get(BASE + "/overview", headers=other).status_code == 401
    prior_cookie = client.cookies.get(config.cookie_name)
    assert client.get(BASE + "/auth/login", headers=other).status_code == 403
    assert client.cookies.get(config.cookie_name) == prior_cookie
    assert client.get(BASE + "/overview", headers=headers).status_code == 200
    changed_email = {"Cf-Access-Jwt-Assertion": assertion(email=EMAILS[1])}
    assert client.get(BASE + "/overview", headers=changed_email).status_code == 401
    assert client.post(BASE + "/pause", headers={**headers, "Origin": "https://evil.example"}, json={"paused": True}).status_code == 403
    assert client.post(BASE + "/pause", headers={**headers, "X-Hub-CSRF": "wrong"}, json={"paused": True}).status_code == 403
    assert client.post(BASE + "/auth/logout", headers=headers).status_code == 200
    assert client.get(BASE + "/auth/me", headers=headers).json()["authenticated"] is False
    assert client.get(BASE + "/auth/login", headers=other).status_code == 403
    sign_in(client, assertion())  # Original identity can explicitly sign in again.
    session = app.state.sessions.create_session(42)
    client.cookies.set(config.cookie_name, session["cookie_token"])
    assert client.get(BASE + "/overview", headers=headers).status_code == 401
    assert client.get(BASE + "/auth/callback?code=ignored&state=ignored").status_code == 404


def test_agent_bearers_remain_separate_and_bad_bearer_never_falls_back(hub):
    client, _, _, assertion, requests = hub
    headers, _ = sign_in(client, assertion())
    assert client.get(BASE + "/overview", headers={"Authorization": "Bearer test-codex"}).status_code == 200
    assert client.get(BASE + "/overview", headers={**headers, "Authorization": "Bearer bad"}).status_code == 401
    assert client.get(BASE + "/auth/me", headers={**headers, "Authorization": "Bearer test-codex"}).json()["authenticated"] is False
    assert client.get(BASE + "/auth/login", headers={**headers, "Authorization": "Bearer test-codex"}).status_code == 401
    assert len(requests) == 1


def test_access_session_is_hashed_durable_and_not_extended_by_new_jwt(hub):
    client, app, config, assertion, _ = hub
    headers, data = sign_in(client, assertion(exp=int(time.time()) + 15))
    token = client.cookies.get(config.cookie_name)
    # Reopen the real store, rather than treating an in-memory session as durable.
    reopened = SessionStore(app.state.sessions.path)
    with reopened._read_db() as db:
        row = db.execute("SELECT token_hash, expires FROM auth_access_sessions").fetchone()
        assert row[0] != token
        assert token not in str(row)
        expiry = row[1]
    now = [expiry + 1]
    app.state.sessions.clock = lambda: now[0]
    assert client.get(BASE + "/auth/me", headers={"Cf-Access-Jwt-Assertion": assertion()}).json()["authenticated"] is False
    assert data["csrf_token"] and headers["X-Hub-CSRF"]


def test_unconfigured_cloudflare_mode_denies_and_does_not_fall_back(tmp_path):
    config = AuthConfig("https://hub.example", "client", "secret", 42, human_auth_mode="cloudflare_access")
    settings = Settings(tmp_path / "hub.sqlite", AuthSettings("h", "c", claude_token="cl"), config)
    app = create_app(settings)
    with TestClient(app, base_url=config.origin) as client:
        assert client.get(BASE + "/auth/login").status_code == 503
        session = app.state.sessions.create_session(42)
        client.cookies.set(config.cookie_name, session["cookie_token"])
        assert client.get(BASE + "/auth/me").json()["authenticated"] is False
        assert client.get(BASE + "/overview").status_code in {401, 503}
        assert client.get("/health").status_code == 200


def test_environment_selects_exact_provider_and_allowlist(monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_DB", str(tmp_path / "hub.sqlite"))
    monkeypatch.setenv("HUB_BASE_URL", "https://hub.example")
    monkeypatch.setenv("HUB_HUMAN_AUTH_MODE", "cloudflare_access")
    monkeypatch.setenv("HUB_CF_ACCESS_ISSUER", ISSUER)
    monkeypatch.setenv("HUB_CF_ACCESS_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("HUB_CF_ACCESS_ALLOWED_EMAILS", ", ".join(EMAILS))
    for name, value in (("HERMES_TOKEN", "h"), ("CODEX_TOKEN", "c"), ("CLAUDE_TOKEN", "cl")):
        monkeypatch.setenv(name, value)
    config = Settings.from_env().auth_config
    assert config.human_auth_mode == "cloudflare_access"
    assert config.cloudflare_config.allowed_emails == EMAILS


def test_access_requires_secure_origin_and_explicit_mode():
    with pytest.raises(ValueError):
        AuthConfig("http://localhost", human_auth_mode="cloudflare_access")
    with pytest.raises(ValueError):
        AuthConfig("https://hub.example", human_auth_mode="other")


def test_alternate_hostname_redirects_navigation_only_without_query_secrets(tmp_path):
    config = AuthConfig("https://hub.example", alternate_origins=("https://alternate.example",))
    settings = Settings(tmp_path / "hub.sqlite", AuthSettings("h", "c", claude_token="cl"), config)
    app = create_app(settings)
    with TestClient(app, base_url="https://alternate.example", follow_redirects=False) as client:
        navigation = client.get("/?code=private-code&state=private-state")
        assert navigation.status_code == 303
        assert navigation.headers["location"] == "https://hub.example/"
        assert navigation.headers["cache-control"] == "no-store"
        for method, path in (("GET", BASE + "/auth/login"), ("POST", BASE + "/tasks"),
                             ("POST", "/mcp/")):
            response = client.request(method, path, headers={"Authorization": "Bearer c"})
            assert response.status_code == 421
            assert "location" not in response.headers
        assert client.get("https://hub.example/health").status_code == 200


@pytest.mark.parametrize("alternate", ["http://alternate.example", "https://hub.example",
    "https://alternate.example/path", "https://u:p@alternate.example", "https://alternate.example?x=1"])
def test_invalid_alternate_origin_is_rejected(alternate):
    with pytest.raises(ValueError):
        AuthConfig("https://hub.example", alternate_origins=(alternate,))
