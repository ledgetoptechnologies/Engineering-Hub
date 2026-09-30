from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


def test_compose_uses_host_networking_for_real_ip_allowlist_and_persistent_data_mount():
    config = yaml.safe_load((ROOT / "compose.yaml").read_text())
    service = config["services"]["agent-message-bus"]
    assert service["restart"] == "unless-stopped"
    assert "./data:/var/lib/agent-message-bus" in service["volumes"]
    assert service["network_mode"] == "host"
    assert "ports" not in service
    assert service["user"]
    assert service["environment"]["AGENT_BUS_FILE_ROOT"] == "${AGENT_BUS_FILE_ROOT:-/var/lib/agent-message-bus/files}"


def test_env_example_has_separate_tokens_and_required_paths():
    text = (ROOT / ".env.example").read_text()
    for key in ("HERMES_TOKEN", "CODEX_TOKEN", "ADMIN_TOKEN", "AGENT_BUS_DB", "AGENT_BUS_AUDIT",
                "AGENT_BUS_FILE_ROOT", "CLAIM_LEASE_SECONDS"):
        assert f"{key}=" in text
    assert "HERMES_TOKEN=CODEX_TOKEN" not in text
