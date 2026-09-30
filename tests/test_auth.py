from agent_message_bus.auth import AuthSettings


def test_separate_tokens_map_to_separate_identities():
    auth = AuthSettings("hermes-secret", "codex-secret", "admin-secret")
    assert auth.identity_for("hermes-secret") == "hermes"
    assert auth.identity_for("codex-secret") == "codex"
    assert auth.identity_for("admin-secret") == "admin"
    assert auth.identity_for("wrong") is None
