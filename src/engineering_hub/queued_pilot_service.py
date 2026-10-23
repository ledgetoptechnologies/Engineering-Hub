"""Explicit private queued-pilot composition, not an installed service or CLI.

The operator supplies a complete manifest, a dedicated client factory and a
synchronous additional target veto. That veto is not evidence of machine-wide
database-restore safety: independent enrollment/current-host reconciliation and
exclusion of other writers remain activation gates. No held work is adopted,
recovered or replayed here. Existing listeners and default factories are intact.
"""
from __future__ import annotations

import asyncio
import inspect

from .app import Settings, create_app
from .auth import AuthConfig
from .container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from .container_pilot_client import ContainerPilotClient
from .events import NativeHubEventFeed
from .owned_tasks import finish_owned
from .frontends import HubFrontends
from .pilot_ownership import PilotOwner
from .pilot_service import REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS
from .queued_pilot import QueuedContainerPilot
from .queued_pilot_runtime import QueuedPilotRuntime
from .repository import HubRepository
from .split_server import _owned_service, serve_private_coordinator
from .startup_reconciliation import StartupReconciliation
from .work_queue import WorkAction, WorkQueue, _key


class QueuedPilotServiceError(RuntimeError):
    """Sanitized composition or lifecycle failure without private details."""

    def __init__(self):
        super().__init__('Hub owned queued pilot unavailable')


def _synchronous(value):
    return (callable(value) and not inspect.iscoroutinefunction(value)
            and not inspect.isasyncgenfunction(value)
            and not inspect.iscoroutinefunction(getattr(value, '__call__', None))
            and not inspect.isasyncgenfunction(getattr(value, '__call__', None)))


async def serve_owned_queued_pilot(settings, manifest, client_factory, *,
                                 action_key, machine_id, target_verifier, reconciliation_factory, socket_path,
                                 supervisor_stop_grace_seconds,
                                 agent_host='0.0.0.0', agent_port=8770,
                                 stop_event=None, on_ready=None,
                                 install_signals=True, recover_socket=False):
    """Bind first, then own exactly one persistent private queue composition.

    ``client_factory`` must synchronously return a new, dedicated, exact
    ContainerPilotClient. Ownership transfers only after that exact-type check;
    an invalid return or a factory that raises owns its own unaccepted resources.
    Freshness/exclusivity is the trusted factory's precondition, not something
    the exact-type check proves; never return a borrowed, reused or closed client.
    This factory exposes no injected owner or ephemeral-storage override.

    The supervisor grace is an operator assertion, not a configured or verified
    installed timeout. Shutdown uses the existing listener/core drain before
    closing the accepted client and finally its newly constructed PilotOwner.
    """
    try:
        if (type(settings) is not Settings or settings.ingress_mode != 'combined'
                or type(settings.auth_config) is not AuthConfig
                or settings.auth_config.human_auth_mode != 'cloudflare_access'
                or settings.auth_config.cloudflare_config is None
                or type(machine_id) is not str or machine_id != settings.staging_machine_id
                or not _synchronous(client_factory) or not _synchronous(target_verifier)
                or not _synchronous(reconciliation_factory)
                or stop_event is not None and type(stop_event) is not asyncio.Event
                or type(supervisor_stop_grace_seconds) is not int
                or supervisor_stop_grace_seconds < REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS):
            raise ValueError()
        _key(action_key)
        target = ContainerPilotTarget.from_manifest(manifest)
        if target.target_id != 'engineering-hub:staging':
            raise ValueError()
        action = WorkAction(machine_id, frozenset({'staging'}),
                            project='engineering-hub', requires_execution_proof=True)
    except Exception:
        raise ValueError('explicit owned queued pilot configuration required') from None

    owner = client = reconciliation = None
    lifecycle_task = asyncio.current_task()
    rejected_reconciliation_tasks = []
    constructed = False
    stop = stop_event if stop_event is not None else asyncio.Event()

    def additional_veto(*args):
        result = target_verifier(*args)
        if inspect.isawaitable(result):
            # A misdeclared synchronous function cannot smuggle async approval.
            # Close only its unstarted coroutine, never another owner's Future.
            if (inspect.iscoroutine(result)
                    and inspect.getcoroutinestate(result) == inspect.CORO_CREATED):
                result.close()
            return False
        return result is True

    def pair_factory():
        nonlocal owner, client, reconciliation, constructed
        if constructed:
            raise QueuedPilotServiceError()
        constructed = True
        owner = PilotOwner(settings.database)
        reconciliation = _reconciliation(reconciliation_factory, owner, target,
                                         rejected_reconciliation_tasks, lifecycle_task)
        accepted = client_factory()
        if type(accepted) is not ContainerPilotClient:
            raise QueuedPilotServiceError()
        client = accepted
        repo = HubRepository(settings.database,
            execution_principal='executor:container-pilot', pilot_owner=owner)
        workflow = ContainerPilotWorkflow(repo, client, [target], pilot_owner=owner)
        queue = WorkQueue(repo, {action_key: action}, target_verifier=additional_veto)
        bridge = QueuedContainerPilot(queue, workflow,
                                      action_key=action_key, machine_id=machine_id,
                                      reconciliation=reconciliation)
        feed = NativeHubEventFeed(settings.database)
        runtime = QueuedPilotRuntime(bridge, feed)
        core = create_app(settings, repository=repo, event_feed=feed,
                          work_queue=queue, queued_pilot_runtime=runtime)
        return HubFrontends(core)

    async def cleanup():
        failed = False
        try:
            if rejected_reconciliation_tasks:
                try:
                    await finish_owned(asyncio.gather(*rejected_reconciliation_tasks, return_exceptions=True))
                except BaseException:
                    failed = True
            if reconciliation is not None:
                try:
                    reconciliation.close()
                except BaseException:
                    failed = True
            if client is not None:
                try:
                    await client.aclose()
                except BaseException:
                    failed = True
        finally:
            if owner is not None:
                try:
                    owner.close()
                except BaseException:
                    failed = True
        if failed:
            raise QueuedPilotServiceError()

    async def operation():
        try:
            await serve_private_coordinator(pair_factory, socket_path=socket_path,
                agent_host=agent_host, agent_port=agent_port, stop_event=stop,
                on_ready=on_ready, install_signals=install_signals,
                recover_socket=recover_socket)
        finally:
            if owner is not None or client is not None:
                await cleanup()

    try:
        # Retain the whole ownership operation, not a newly scheduled cleanup
        # after cancellation. The existing helper requests stop and drains this
        # same task through repeated caller cancellation, including client close.
        await _owned_service(operation, stop)
    except Exception:
        raise QueuedPilotServiceError() from None


def _reconciliation(factory, owner, target, rejected_local_tasks, lifecycle_task):
    """Trusted composition dependency, not verification of operator facts.

    The factory owns invalid returns; a valid exact bound gate transfers to the
    service. Never schedule custom awaitables. Retain a returned local Task/Future
    for cancellation/drain before owner cleanup; unreturned work is forbidden by
    the trusted factory contract and cannot be discovered by this component.
    """
    value = factory(owner, target)
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value) and inspect.getcoroutinestate(value) == inspect.CORO_CREATED:
            value.close()
        elif (asyncio.isfuture(value) and value.get_loop() is asyncio.get_running_loop()
              and value is not asyncio.current_task() and value is not lifecycle_task):
            value.cancel()
            rejected_local_tasks.append(value)
        raise QueuedPilotServiceError()
    if type(value) is not StartupReconciliation:
        raise QueuedPilotServiceError()
    value.assert_binding(owner=owner, target_id=target.target_id, manifest_digest=target.manifest_digest)
    return value
