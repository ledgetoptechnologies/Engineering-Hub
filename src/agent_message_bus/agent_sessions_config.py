"""Explicit environment-bound connection configuration; inert when disabled."""
from dataclasses import dataclass
import ipaddress
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
        if (type(self.environment) is not str
                or self.environment not in {"staging", "production"}):
            raise ValueError("explicit_staging_agent_sessions_required")
        if not isinstance(self.database, Path):
            raise ValueError("explicit_staging_agent_sessions_required")
        if not self.database.is_absolute() or not re.fullmatch(r"human:(?:[1-9][0-9]{0,19}|cf:[a-f0-9]{64})", self.owner):
            error = ("staging_agent_sessions_owner_and_path_required"
                     if self.environment == "staging"
                     else "production_agent_sessions_owner_and_path_required")
            raise ValueError(error)
        if self.environment == "production":
            if not self.owner.startswith("human:cf:") or len(self.owner) != len("human:cf:") + 64:
                raise ValueError("production_agent_sessions_cloudflare_owner_required")
            if self.origin != self.origin.lower():
                raise ValueError("production_agent_sessions_canonical_origin_required")
        parsed = urlsplit(self.origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
            error = ("staging_agent_sessions_origin_required"
                     if self.environment == "staging"
                     else "production_agent_sessions_origin_required")
            raise ValueError(error)
        if self.environment == "production" and parsed.scheme != "https":
            raise ValueError("production_agent_sessions_https_origin_required")
        if self.environment == "production":
            try:
                ipaddress.ip_address(parsed.hostname)
            except ValueError:
                pass
            else:
                raise ValueError("production_agent_sessions_dns_origin_required")
            if parsed.hostname == "localhost" or "." not in parsed.hostname:
                raise ValueError("production_agent_sessions_dns_origin_required")
        try:
            parsed.port
        except ValueError:
            error = ("staging_agent_sessions_origin_required"
                     if self.environment == "staging"
                     else "production_agent_sessions_origin_required")
            raise ValueError(error) from None

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
        if mode not in {"staging", "production"}:
            raise ValueError("HUB_AGENT_SESSIONS_MODE must be disabled, staging, or production")
        required = [os.environ.get(name, "") for name in (
            "HUB_AGENT_SESSIONS_DB", "HUB_AGENT_SESSIONS_OWNER", "HUB_AGENT_SESSIONS_ORIGIN")]
        if not all(required):
            error = ("explicit_staging_agent_sessions_configuration_required"
                     if mode == "staging"
                     else "explicit_production_agent_sessions_configuration_required")
            raise ValueError(error)
        return cls(Path(required[0]), required[1], required[2], environment=mode,
                   hub_bridge=bridge == "enabled")
