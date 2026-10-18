from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engineering_hub.auth import AuthConfig, SessionStore, build_auth_router, require_human


class FakeGitHub:
    def __init__(self, user=123):
        self.user = user
        self.calls = []

    async def exchange(self, code, verifier):
        self.calls.append((code, verifier))
        if isinstance(self.user, Exception):
            raise self.user
        return self.user


def setup(tmp_path, user=123, configured=True, clock=None):
    config = AuthConfig("https://hub.example", "client" if configured else "", "secret", 123)
    store = SessionStore(tmp_path / "hub.db", **({"clock": clock} if clock else {}))
    provider = FakeGitHub(user)
    app = FastAPI()
    app.include_router(build_auth_router(config, store, provider))
    return TestClient(app, base_url=config.base_url, follow_redirects=False), config, store, provider


def pending(client):
    response = client.get("/api/hub/v1/auth/login")
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert "secret" not in response.headers["location"]
    return query["state"][0], response


def test_login_success_flags_and_persisted_session(tmp_path):
    client, config, store, provider = setup(tmp_path)
    state, response = pending(client)
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    callback = client.get("/api/hub/v1/auth/callback", params={"state": state, "code": "code"})
    assert callback.status_code == 303
    data = client.get("/api/hub/v1/auth/me").json()
    assert data["authenticated"] is True and data["actor"] == "human:123"
    token = client.cookies.get(config.cookie_name)
    assert SessionStore(tmp_path / "hub.db").session_for(token)["github_id"] == 123
    assert token.encode() not in (tmp_path / "hub.db").read_bytes()
    assert len(provider.calls) == 1
    assert client.get("/api/hub/v1/auth/callback", params={"state": state, "code": "code"}).status_code == 400


def test_state_mismatch_expiry_and_missing_browser_cookie(tmp_path):
    now = [1000]
    client, config, store, provider = setup(tmp_path, clock=lambda: now[0])
    state, _ = pending(client)
    assert client.get("/api/hub/v1/auth/callback", params={"state": "wrong", "code": "code"}).status_code == 400
    client.cookies.clear()
    assert client.get("/api/hub/v1/auth/callback", params={"state": state, "code": "code"}).status_code == 400
    state, _ = pending(client)
    now[0] += 601
    assert client.get("/api/hub/v1/auth/callback", params={"state": state, "code": "code"}).status_code == 400
    assert not provider.calls


@pytest.mark.parametrize("user,status", [(999, 403), (True, 403), (RuntimeError("secret-provider-error"), 502)])
def test_denied_identity_and_provider_failure(tmp_path, user, status):
    client, _, _, _ = setup(tmp_path, user)
    state, _ = pending(client)
    response = client.get("/api/hub/v1/auth/callback", params={"state": state, "code": "private-code"})
    assert response.status_code == status
    assert "secret" not in response.text and "private-code" not in response.text
    assert client.get("/api/hub/v1/auth/me").json()["authenticated"] is False


def test_agent_bearer_cannot_be_human_and_unconfigured_login(tmp_path):
    client, _, _, _ = setup(tmp_path, configured=False)
    assert client.get("/api/hub/v1/auth/login").status_code == 503
    assert client.get("/api/hub/v1/auth/me", headers={"Authorization": "Bearer agent-token"}).json() == {
        "authenticated": False, "actor": None, "csrf_token": None, "github_id": None}
    assert client.post("/api/hub/v1/auth/logout", headers={"Authorization": "Bearer agent-token"}).status_code == 401


def test_csrf_origin_logout_and_session_expiry(tmp_path):
    now = [1000]
    client, config, store, _ = setup(tmp_path, clock=lambda: now[0])
    session = store.create_session(123)
    client.cookies.set(config.cookie_name, session["cookie_token"])
    path = "/api/hub/v1/auth/logout"
    assert client.post(path).status_code == 403
    assert client.post(path, headers={"origin": "https://evil.example", "X-Hub-CSRF": session["csrf_token"]}).status_code == 403
    assert client.post(path, headers={"origin": config.origin, "X-Hub-CSRF": "wrong"}).status_code == 403
    response = client.post(path, headers={"origin": config.origin, "X-Hub-CSRF": session["csrf_token"]})
    assert response.status_code == 200
    assert store.session_for(session["cookie_token"]) is None
    session = store.create_session(123)
    now[0] += 28801
    assert store.session_for(session["cookie_token"]) is None


def test_state_survives_reopen_and_is_one_use(tmp_path):
    store = SessionStore(tmp_path / "hub.db")
    state = store.create_state()
    reopened = SessionStore(tmp_path / "hub.db")
    assert reopened.consume_state(state["state"]) == state["verifier"]
    assert store.consume_state(state["state"]) is None


def test_base_url_policy():
    with pytest.raises(ValueError):
        AuthConfig("http://192.168.1.1")
    with pytest.raises(ValueError):
        AuthConfig("https://hub.example/path")
    assert not AuthConfig("http://127.0.0.1:8000").secure
