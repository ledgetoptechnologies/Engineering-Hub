"""Explicit unified Hub factory; never select or migrate legacy state implicitly."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecuritySettings

from .app import Settings, create_app


def create_unified_app(settings=None):
    """Open an operator-selected, already quiesced legacy state directory.

    Existing writer exclusion and a coherent backup are operator preconditions;
    a path check is not a cross-process audit lock. No migration/copy, credentials,
    listener, job, watcher fallback or second bus is discovered here.
    """
    settings = settings or Settings.from_env()
    cleanup_flag = os.environ.get('HUB_BUS_STARTUP_CLEANUP', '0')
    if cleanup_flag not in {'0', '1'}:
        raise ValueError('HUB_BUS_STARTUP_CLEANUP must be 0 or 1')
    if (settings.ingress_mode != 'combined'
            or settings.auth_config.human_auth_mode != 'cloudflare_access'
            or settings.auth_config.cloudflare_config is None):
        raise ValueError('Unified Hub requires one shared split-frontends core')
    paths = {}
    for name in ('AGENT_BUS_DB', 'AGENT_BUS_AUDIT', 'AGENT_BUS_FILE_ROOT'):
        value = os.environ.get(name)
        if not value or not Path(value).is_absolute():
            raise ValueError('Explicit existing absolute bus paths required')
        paths[name] = Path(value)
    if (not paths['AGENT_BUS_DB'].is_file()
            or not paths['AGENT_BUS_AUDIT'].is_file()
            or not paths['AGENT_BUS_FILE_ROOT'].is_dir()
            or settings.database.resolve() == paths['AGENT_BUS_DB'].resolve()
            or (settings.database.exists()
                and settings.database.samefile(paths['AGENT_BUS_DB']))):
        raise ValueError('Existing separate bus and Hub state required')
    origin = settings.auth_config.origin
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys([urlsplit(origin).netloc,
                                         *settings.agent_allowed_hosts])),
        allowed_origins=[origin],
    )
    # Factory import is inert; only this explicit operation may construct it.
    from agent_message_bus.app import create_app as create_bus_app
    bus = create_bus_app(db_path=paths['AGENT_BUS_DB'],
                         audit_path=paths['AGENT_BUS_AUDIT'],
                         file_root=paths['AGENT_BUS_FILE_ROOT'],
                         auth=settings.agent_auth,
                         push_events_operational=True,
                         # Preserve every row during the initial parity check.
                         # After verification the operator may restore the old
                         # startup-retention behavior explicitly with value 1.
                         startup_cleanup=cleanup_flag == '1',
                         transport_security_settings=security)
    return create_app(settings, bus_app=bus)
