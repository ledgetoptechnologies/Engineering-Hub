"""Explicit normal Hub service over an already owner-enrolled ledger.

No executor profile, credential, client, work queue or pilot runtime is selected.
This is not enrollment, recovery, reconciliation or an automatic startup mode.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import ipaddress
import os
from pathlib import Path

from .app import Settings, create_app
from .auth import AuthConfig
from .deployment_config import load_deployment_config
from .frontends import HubFrontends
from .pilot_ownership import PilotOwner
from .private_browser_socket import _private_parent
from .repository import HubRepository
from .server import _bind, _signal_stop
from .split_server import _options, _owned_service, _serve_listeners
from .unified_app import _create_unified_bus


class OwnedInactiveHubError(RuntimeError):
    def __init__(self):
        super().__init__('Owned inactive Hub unavailable; preserve existing state')


async def serve_owned_inactive_hub(settings, *, bus_paths,
                                   agent_host='0.0.0.0', agent_port=8770,
                                   browser_host='0.0.0.0', browser_port=8771,
                                   stop_event=None, on_ready=None, install_signals=True):
    """Bind before acquiring one existing owner; release only after full drain.

    Ordinary tasks, decisions, authentication and shared bus notes remain usable.
    All execution and queue routes remain unconfigured. Existing queue/run rows
    and admission markers are preserved, not resumed, repaired or discarded.
    """
    if (type(settings) is not Settings or settings.work_queue_enabled
            or settings.ingress_mode != 'combined'
            or type(settings.auth_config) is not AuthConfig
            or settings.auth_config.human_auth_mode != 'cloudflare_access'
            or settings.auth_config.cloudflare_config is None
            or os.environ.get('HUB_BUS_STARTUP_CLEANUP', '0') != '0'):
        raise OwnedInactiveHubError()
    _options(lambda: None, (agent_port, browser_port), stop_event, on_ready, install_signals)
    stop = stop_event if stop_event is not None else asyncio.Event()
    owner = None
    constructed = False

    def pair_factory():
        nonlocal owner, constructed
        if constructed:
            raise OwnedInactiveHubError()
        constructed = True
        if settings.require_owned_ledger:
            from .protected_layout import validate_existing_boundary
            validate_existing_boundary(settings.database, shared_paths=tuple(bus_paths.values()))
        # Full ancestor walk before ownership, without creating or repairing it.
        parent_fd, _ = _private_parent(Path(settings.database))
        os.close(parent_fd)
        owner = PilotOwner(settings.database, require_existing=True)
        repo = HubRepository(settings.database, pilot_owner=owner)
        bus = _create_unified_bus(settings, bus_paths=bus_paths)
        return HubFrontends(create_app(settings, repository=repo, bus_app=bus))

    async def operation():
        try:
            if stop.is_set():
                return
            ipaddress.ip_address(agent_host)
            ipaddress.ip_address(browser_host)
            if agent_port and agent_port == browser_port:
                raise OwnedInactiveHubError()
            with ExitStack() as stack:
                agent_socket = stack.enter_context(_bind(agent_host, agent_port))
                browser_socket = stack.enter_context(_bind(browser_host, browser_port))
                if install_signals:
                    stack.enter_context(_signal_stop(stop))
                pair = pair_factory()
                async with pair.lifespan():
                    # Existing ordinary serve_frontends has a 30s graceful limit.
                    # This existing helper instead closes admission/disconnects
                    # idle read streams and drains accepted mutations without
                    # force cancellation before releasing the shared lifecycle.
                    await _serve_listeners((pair.agents, pair.browser),
                        (agent_socket, browser_socket), stop, on_ready,
                        (agent_socket.getsockname(), browser_socket.getsockname()),
                        on_stopping=pair.close_admission)
        finally:
            # The operation exits its one shared core only after both listeners
            # drain. _owned_service shields this operation from caller cancellation.
            if owner is not None:
                owner.close()

    try:
        await _owned_service(operation, stop)
    except Exception:
        raise OwnedInactiveHubError() from None


async def serve_configuration(config_path):
    deployment = load_deployment_config(config_path)
    deployment.validate_ledger_boundary()
    settings = Settings.from_env(deployment)
    await serve_owned_inactive_hub(settings, bus_paths=deployment.bus_paths)


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            # Unknown switches may contain an accidentally pasted credential.
            # Never echo argv, rejected values, parser fragments or paths.
            self.exit(2, str(OwnedInactiveHubError()) + '\n')

    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--config', required=True,
                        help='Existing non-secret Hub deployment configuration')
    args = parser.parse_args(argv)
    try:
        asyncio.run(serve_configuration(args.config))
    except (Exception, KeyboardInterrupt):
        parser.exit(2, str(OwnedInactiveHubError()) + '\n')


if __name__ == '__main__':
    main()
