"""Explicit, staging-only connection configuration; inert when disabled."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class AgentSessionsConfig:
    database: Path
    owner: str
    origin: str
    environment: str = "staging"
    hub_bridge: bool = False

    def __post_init__(self):
        if type(self.hub_bridge) is not bool:
            raise ValueError("explicit_hub_bridge_mode_required")
        if self.environment != "staging" or not isinstance(self.database, Path):
            raise ValueError("explicit_staging_agent_sessions_required")
        if not self.database.is_absolute() or not re.fullmatch(r"human:(?:[1-9][0-9]{0,19}|cf:[a-f0-9]{64})", self.owner):
            raise ValueError("staging_agent_sessions_owner_and_path_required")
        parsed = urlsplit(self.origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("staging_agent_sessions_origin_required")
        try:
            parsed.port
        except ValueError:
            raise ValueError("staging_agent_sessions_origin_required") from None

    @classmethod
    def from_env(cls):
        mode = os.environ.get("HUB_AGENT_SESSIONS_MODE", "disabled")
        bridge = os.environ.get("HUB_AGENT_SESSIONS_HUB_BRIDGE", "disabled")
        if bridge not in {"disabled", "enabled"}:
            raise ValueError("invalid_hub_bridge_mode")
        if mode == "disabled":
            if bridge != "disabled":
                raise ValueError("hub_bridge_requires_staging_sessions")
            return None
        if mode != "staging":
            raise ValueError("HUB_AGENT_SESSIONS_MODE must be disabled or staging")
        required = [os.environ.get(name, "") for name in (
            "HUB_AGENT_SESSIONS_DB", "HUB_AGENT_SESSIONS_OWNER", "HUB_AGENT_SESSIONS_ORIGIN")]
        if not all(required):
            raise ValueError("explicit_staging_agent_sessions_configuration_required")
        return cls(Path(required[0]), required[1], required[2], hub_bridge=bridge == "enabled")
