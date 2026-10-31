"""Bounded, non-secret deployment settings selected explicitly by an operator."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path

from .auth import AuthConfig
from .ingress import validate_agent_hosts


_MAX_BYTES = 64 * 1024
_ERROR = "Invalid Hub deployment configuration"


@dataclass(frozen=True)
class DeploymentConfig:
    state_root: Path
    auth_config: AuthConfig
    allowed_hosts: tuple[str, ...]
    allowed_ips: frozenset[str]
    protected_ledger: bool = False

    def __post_init__(self):
        if type(self.protected_ledger) is not bool:
            raise ValueError(_ERROR)

    @property
    def hub_database(self) -> Path:
        if self.protected_ledger:
            from .protected_layout import database_path
            return database_path()
        return self.state_root / "hub.sqlite3"

    def validate_ledger_boundary(self):
        """Opt-in metadata gate only; never opens, creates or migrates a DB."""
        if self.protected_ledger:
            from .protected_layout import validate_existing_boundary
            validate_existing_boundary(self.hub_database, shared_root=self.state_root,
                                       shared_paths=tuple(self.bus_paths.values()))

    @property
    def bus_paths(self) -> dict[str, Path]:
        root = self.state_root / "agent-message-bus"
        return {"AGENT_BUS_DB": root / "messages.sqlite3",
                "AGENT_BUS_AUDIT": root / "audit.jsonl",
                "AGENT_BUS_FILE_ROOT": root / "files"}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_ERROR)
        result[key] = value
    return result


def _constant(_value):
    raise ValueError(_ERROR)


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError(_ERROR)


def _strings(value):
    if type(value) is not list or not value or any(type(v) is not str for v in value):
        raise ValueError(_ERROR)
    return tuple(value)


def load_deployment_config(path: str | Path) -> DeploymentConfig:
    """Read without writes, environment mutation, migration, or fallback defaults.

    Configuration cannot contain credentials, executable flags or arbitrary paths.
    The configuration's parent retains settings and bus state. Version two
    explicitly selects the fixed, separately mounted existing private ledger;
    neither version creates or migrates that ledger.
    """
    try:
        if not isinstance(path, (str, Path)):
            raise ValueError(_ERROR)
        selected = Path(path)
        if not selected.is_absolute():
            raise ValueError(_ERROR)
        with selected.open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError(_ERROR)
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                              parse_constant=_constant)
        if type(document) is not dict or type(document.get('schema_version')) is not int:
            raise ValueError(_ERROR)
        version = document['schema_version']
        common = ('schema_version', 'base_url', 'github' if version == 3 else 'cloudflare_access',
                  'agent_allowed_hosts', 'agent_allowed_ips')
        if version == 1:
            _keys(document, common)
        elif version in (2, 3):
            _keys(document, (*common, 'storage'))
            _keys(document['storage'], ('layout',))
            if document['storage']['layout'] != 'protected-ledger-v1':
                raise ValueError(_ERROR)
            from .protected_layout import database_path
            private_root = database_path().parent
            if selected.parent == private_root or private_root in selected.parent.parents:
                raise ValueError(_ERROR)
        else:
            raise ValueError(_ERROR)
        if version == 3:
            github = document['github']
            _keys(github, ('client_id', 'allowed_user_id'))
            client_id = github['client_id']
            if (type(client_id) is not str or not 1 <= len(client_id) <= 128
                    or not client_id.isascii() or not all(c.isalnum() or c in '_-' for c in client_id)
                    or type(github['allowed_user_id']) is not int
                    or not 0 < github['allowed_user_id'] < 2**63):
                raise ValueError(_ERROR)
            auth = AuthConfig(document['base_url'], human_auth_mode='github',
                              github_client_id=client_id, beau_github_id=github['allowed_user_id'])
            if not auth.secure:
                raise ValueError(_ERROR)
        else:
            access = document["cloudflare_access"]
            _keys(access, ("issuer", "audience", "allowed_emails"))
            for value in (document["base_url"], access["issuer"], access["audience"]):
                if type(value) is not str or not value:
                    raise ValueError(_ERROR)
            emails = _strings(access["allowed_emails"])
            auth = AuthConfig(document["base_url"], human_auth_mode="cloudflare_access",
                              cloudflare_issuer=access["issuer"],
                              cloudflare_audience=access["audience"],
                              cloudflare_allowed_emails=emails)
            if auth.cloudflare_config is None:
                raise ValueError(_ERROR)
        hosts = _strings(document["agent_allowed_hosts"])
        validate_agent_hosts(hosts)
        ips = _strings(document["agent_allowed_ips"])
        for value in ips:
            if "%" in value or str(ipaddress.ip_address(value)) != value:
                raise ValueError(_ERROR)
        if len(set(ips)) != len(ips):
            raise ValueError(_ERROR)
        return DeploymentConfig(selected.parent, auth, hosts, frozenset(ips), version in (2, 3))
    except (OSError, ValueError, TypeError, RecursionError):
        # Do not echo paths, parser fragments, rejected keys, or file contents.
        raise ValueError(_ERROR) from None
