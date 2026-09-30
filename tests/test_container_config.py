from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


def test_compose_has_localhost_binding_and_persistent_data_mount():
    config = yaml.safe_load((ROOT / "compose.yaml").read_text())
    service = config["services"]["agent-message-bus"]
    assert service["restart"] == "unless-stopped"
    assert "./data:/var/lib/agent-message-bus" in service["volumes"]
    assert service["ports"][0].startswith("127.0.0.1:")
    assert service["user"]


def test_env_example_has_separate_tokens_and_required_paths():
    text = (ROOT / ".env.example").read_text()
    for key in ("HERMES_TOKEN", "CODEX_TOKEN", "ADMIN_TOKEN", "AGENT_BUS_DB", "AGENT_BUS_AUDIT", "CLAIM_LEASE_SECONDS"):
        assert f"{key}=" in text
    assert "HERMES_TOKEN=CODEX_TOKEN" not in text
