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

    def __post_init__(self):
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
        if mode == "disabled":
            return None
        if mode != "staging":
            raise ValueError("HUB_AGENT_SESSIONS_MODE must be disabled or staging")
        required = [os.environ.get(name, "") for name in (
            "HUB_AGENT_SESSIONS_DB", "HUB_AGENT_SESSIONS_OWNER", "HUB_AGENT_SESSIONS_ORIGIN")]
        if not all(required):
            raise ValueError("explicit_staging_agent_sessions_configuration_required")
        return cls(Path(required[0]), required[1], required[2])
