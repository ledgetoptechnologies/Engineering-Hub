import pytest
from starlette.requests import Request

from agent_message_bus.auth import AuthSettings


def test_separate_tokens_map_to_separate_identities():
    auth = AuthSettings("hermes-secret", "codex-secret", "admin-secret")
    assert auth.identity_for("hermes-secret") == "hermes"
    assert auth.identity_for("codex-secret") == "codex"
    assert auth.identity_for("admin-secret") == "admin"
    assert auth.identity_for("wrong") is None


def test_claude_token_is_optional_and_keeps_existing_positional_arguments(monkeypatch):
    allowed_ips = frozenset({"192.0.2.1"})
    legacy = AuthSettings("hermes-secret", "codex-secret", "admin-secret", allowed_ips)
    assert legacy.allowed_ips == allowed_ips
    assert legacy.claude_token is None

    monkeypatch.setenv("HERMES_TOKEN", "hermes-secret")
    monkeypatch.setenv("CODEX_TOKEN", "codex-secret")
    monkeypatch.setenv("CLAUDE_TOKEN", "claude-secret")
    configured = AuthSettings.from_env()
    assert configured.identity_for("claude-secret") == "claude"


def test_claude_token_authenticates_as_agent():
    auth = AuthSettings("hermes-secret", "codex-secret", claude_token="claude-secret")
    request = Request({
        "type": "http", "method": "GET", "path": "/api/messages",
        "headers": [(b"authorization", b"Bearer claude-secret")],
        "client": ("127.0.0.1", 12345), "server": ("test", 80),
        "scheme": "http", "query_string": b"", "root_path": "",
    })
    assert auth.agent(request) == "claude"


def test_startup_rejects_missing_or_reused_agent_tokens(monkeypatch):
    with pytest.raises(ValueError, match="HERMES_TOKEN"):
        AuthSettings("", "codex-secret")
    with pytest.raises(ValueError, match="distinct"):
        AuthSettings("same-secret", "same-secret")

    monkeypatch.delenv("HERMES_TOKEN", raising=False)
    monkeypatch.setenv("CODEX_TOKEN", "codex-secret")
    with pytest.raises(ValueError, match="HERMES_TOKEN"):
        AuthSettings.from_env()
