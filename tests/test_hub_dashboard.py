"""The built browser bundle is public; private Hub records never are."""
from pathlib import Path

from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig


def test_static_dashboard_does_not_shadow_auth_or_private_api(tmp_path):
    ui = tmp_path / "dashboard"
    ui.mkdir()
    (ui / "index.html").write_text('<html><title>Engineering Hub</title></html>', encoding="utf-8")
    (ui / "app.js").write_text('console.log("bundle fixture")', encoding="utf-8")
    settings = Settings(
        tmp_path / "hub.sqlite3", AuthSettings("fixture-hermes", "fixture-codex"),
        AuthConfig("http://127.0.0.1:18770"), ui,
    )
    with TestClient(create_app(settings), base_url=settings.auth_config.origin) as client:
        for path in ("/", "/?demo=1", "/app.js"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert "script-src 'self'" in response.headers["content-security-policy"]
        assert client.get("/api/hub/v1/auth/me").json()["authenticated"] is False
        assert client.get("/api/hub/v1/auth/login", follow_redirects=False).status_code == 503
        assert client.get("/api/hub/v1/overview").status_code == 401
        assert client.get("/api/hub/v1/topics/topic/peers").status_code == 401
        assert client.get("/api/hub/v1/events").status_code == 401
        assert client.post("/api/hub/v1/tasks", json={
            "project": "engineering-hub", "title": "No enrollment", "client_request_id": "fixture",
        }).status_code == 401
        assert client.get("/../hub.sqlite3").status_code == 404
        assert client.get("/not-an-app-route").status_code == 404
