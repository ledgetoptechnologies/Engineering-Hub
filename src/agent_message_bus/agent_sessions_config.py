"""Explicit environment-bound connection configuration; inert when disabled."""
from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


_RFC1918_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def _validate_private_origin(value):
    if type(value) is not str or not value or "%" in value:
        raise ValueError("production_agent_sessions_private_origin_required")
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        raise ValueError("production_agent_sessions_private_origin_required") from None
    if (parsed.scheme != "http" or type(address) is not ipaddress.IPv4Address
            or port is None or not 1 <= port <= 65535
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or not any(address in network for network in _RFC1918_NETWORKS)
            or value != f"http://{address}:{port}"):
        raise ValueError("production_agent_sessions_private_origin_required")
    return value


@dataclass(frozen=True)
class AgentSessionsConfig:
    database: Path
    owner: str
    origin: str
    environment: str = "staging"
    hub_bridge: bool = False
    private_origin: str | None = None

    def __post_init__(self):
        if type(self.hub_bridge) is not bool:
            raise ValueError("explicit_hub_bridge_mode_required")
        if (type(self.environment) is not str
                or self.environment not in {"staging", "production"}):
            raise ValueError("explicit_staging_agent_sessions_required")
        if self.private_origin is not None:
            if self.environment != "production":
                raise ValueError("private_origin_requires_production_agent_sessions")
            _validate_private_origin(self.private_origin)
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

    @property
    def transport_origin(self):
        """Exact listener origin for installation/session and MCP client traffic."""
        return self.private_origin or self.origin

    @classmethod
    def from_env(cls):
        mode = os.environ.get("HUB_AGENT_SESSIONS_MODE", "disabled")
        bridge = os.environ.get("HUB_AGENT_SESSIONS_HUB_BRIDGE", "disabled")
        private_origin = os.environ.get("HUB_AGENT_SESSIONS_PRIVATE_ORIGIN")
        if bridge not in {"disabled", "enabled"}:
            raise ValueError("invalid_hub_bridge_mode")
        if mode == "disabled":
            if private_origin is not None:
                raise ValueError("private_origin_requires_production_agent_sessions")
            if bridge != "disabled":
                raise ValueError("hub_bridge_requires_staging_sessions")
            return None
        if mode not in {"staging", "production"}:
            raise ValueError("HUB_AGENT_SESSIONS_MODE must be disabled, staging, or production")
        if private_origin is not None and mode != "production":
            raise ValueError("private_origin_requires_production_agent_sessions")
        required = [os.environ.get(name, "") for name in (
            "HUB_AGENT_SESSIONS_DB", "HUB_AGENT_SESSIONS_OWNER", "HUB_AGENT_SESSIONS_ORIGIN")]
        if not all(required):
            error = ("explicit_staging_agent_sessions_configuration_required"
                     if mode == "staging"
                     else "explicit_production_agent_sessions_configuration_required")
            raise ValueError(error)
        return cls(Path(required[0]), required[1], required[2], environment=mode,
                   hub_bridge=bridge == "enabled", private_origin=private_origin)
