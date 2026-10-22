"""Explicit pre-enrolled queued pilot on the existing unified Hub front doors.

Import starts nothing. This source API neither migrates/enrolls an existing Hub
database nor discovers an executor, credentials, listener or processing worker.
Independent manifest/host enrollment and exclusion of all other writers remain
operator activation prerequisites; a target callback is only an additional veto.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import inspect
import ipaddress

from .app import Settings, create_app
from .auth import AuthConfig
from .container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from .container_pilot_client import ContainerPilotClient
from .events import NativeHubEventFeed
from .frontends import HubFrontends
from .lifecycle import finish_owned
from .pilot_ownership import PilotOwner
from .pilot_service import (REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS,
                            _TRANSFER_LOCK, _TRANSFERRED_OWNERS)
from .queued_pilot import QueuedContainerPilot
from .queued_pilot_runtime import QueuedPilotRuntime, _canonical_uuid
from .queued_pilot_service import QueuedPilotServiceError, _synchronous
from .repository import HubRepository
from .server import _bind, _signal_stop
from .split_server import _options, _owned_service, _serve_listeners
from .unified_app import _create_unified_bus
from .work_queue import WorkAction, WorkQueue, _key


async def _reject_async_generation_result(result):
    """Reject the sync callback's result without leaking returned local work.

    A returned local Future/Task transfers cleanup of that object, not authority
    over arbitrary work on another loop. Never schedule custom awaitables or
    close a coroutine that somebody else has already started. Cancellation of a
    local Task is cooperative: retain the lifecycle until its cleanup finishes.
    """
    if not inspect.isawaitable(result):
        return
    if (inspect.iscoroutine(result)
            and inspect.getcoroutinestate(result) == inspect.CORO_CREATED):
        result.close()
    elif (asyncio.isfuture(result)
          and result.get_loop() is asyncio.get_running_loop()):
        if result is asyncio.current_task():
            # The readiness observer is already lifecycle-owned. Cancelling and
            # gathering ourselves would deadlock its startup-failure cleanup.
            raise QueuedPilotServiceError()
        result.cancel()
        await finish_owned(asyncio.gather(result, return_exceptions=True))
    raise QueuedPilotServiceError()


async def serve_owned_unified_queued_pilot(settings, manifest, client_factory, *,
                                         owner, bus_paths, action_key, machine_id,
                                         target_verifier, supervisor_stop_grace_seconds,
                                         agent_host='0.0.0.0', agent_port=8770,
                                         browser_host='0.0.0.0', browser_port=8771,
                                         stop_event=None, on_ready=None,
                                         install_signals=True, generation_id=None,
                                         on_generation_ready=None):
    """Bind first, then transfer one pre-enrolled owner and dedicated client.

    Failed listener binding leaves the caller's owner untouched. Once the pair
    factory accepts that exact capability it is single-use, including failures;
    cleanup closes the accepted client then owner only after core/bus drain.
    A factory returning the wrong type or raising owns its unaccepted resources.
    It must supply a fresh, dedicated, unclosed client; exact-type validation
    does not prove freshness or exclusivity and cannot detect borrowed clients.
    The external supervisor grace is asserted, not configured or verified here.
    Default unified startup never selects this source-only composition.
    """
    try:
        if (type(settings) is not Settings or settings.ingress_mode != 'combined'
                or type(settings.auth_config) is not AuthConfig
                or settings.auth_config.human_auth_mode != 'cloudflare_access'
                or settings.auth_config.cloudflare_config is None
                or type(owner) is not PilotOwner
                or type(machine_id) is not str or machine_id != settings.staging_machine_id
                or not _synchronous(client_factory) or not _synchronous(target_verifier)
                or type(supervisor_stop_grace_seconds) is not int
                or supervisor_stop_grace_seconds < REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS
                or stop_event is not None and type(stop_event) is not asyncio.Event
                or type(install_signals) is not bool
                or on_ready is not None and not callable(on_ready)
                or (generation_id is None) != (on_generation_ready is None)
                or generation_id is not None and not _canonical_uuid(generation_id)
                or on_generation_ready is not None and not _synchronous(on_generation_ready)):
            raise ValueError()
        owner.assert_active(settings.database)
        _key(action_key)
        target = ContainerPilotTarget.from_manifest(manifest)
        if target.target_id != 'engineering-hub:staging':
            raise ValueError()
        action = WorkAction(machine_id, frozenset({'staging'}),
                            project='engineering-hub', requires_execution_proof=True)
    except Exception:
        raise ValueError('Explicit pre-enrolled unified queued pilot configuration required') from None

    client = None
    generation_drain = None
    transferred = constructed = False
    stop = stop_event if stop_event is not None else asyncio.Event()

    def additional_veto(*args):
        result = target_verifier(*args)
        if inspect.isawaitable(result):
            if (inspect.iscoroutine(result)
                    and inspect.getcoroutinestate(result) == inspect.CORO_CREATED):
                result.close()
            return False
        return result is True

    def pair_factory():
        nonlocal client, transferred, constructed, generation_drain
        if constructed:
            raise QueuedPilotServiceError()
        constructed = True
        owner.assert_active(settings.database)
        with _TRANSFER_LOCK:
            if owner in _TRANSFERRED_OWNERS:
                raise QueuedPilotServiceError()
            _TRANSFERRED_OWNERS.add(owner)
            transferred = True
        accepted = client_factory()
        if type(accepted) is not ContainerPilotClient:
            raise QueuedPilotServiceError()
        client = accepted
        repo = HubRepository(settings.database,
            execution_principal='executor:container-pilot', pilot_owner=owner)
        workflow = ContainerPilotWorkflow(repo, client, [target], pilot_owner=owner)
        queue = WorkQueue(repo, {action_key: action}, target_verifier=additional_veto)
        bridge = QueuedContainerPilot(queue, workflow, action_key=action_key, machine_id=machine_id)
        feed = NativeHubEventFeed(settings.database)
        runtime = QueuedPilotRuntime(bridge, feed)
        if generation_id is not None:
            generation_drain = runtime.bind_generation_drain(owner, generation_id)
        bus = _create_unified_bus(settings, bus_paths=bus_paths)
        core = create_app(settings, repository=repo, event_feed=feed,
                          work_queue=queue, queued_pilot_runtime=runtime, bus_app=bus)
        return HubFrontends(core)

    async def cleanup():
        failed = False
        try:
            if generation_drain is not None:
                try:
                    await generation_drain.revoke()
                except BaseException:
                    failed = True
            if client is not None:
                try:
                    await client.aclose()
                except BaseException:
                    failed = True
        finally:
            if transferred:
                try:
                    owner.close()
                except BaseException:
                    failed = True
        if failed:
            raise QueuedPilotServiceError()

    async def operation():
        try:
            if stop.is_set():
                return
            _options(pair_factory, (agent_port, browser_port), stop, on_ready, install_signals)
            ipaddress.ip_address(agent_host)
            if not isinstance(browser_host, str) or '%' in browser_host:
                raise ValueError()
            ipaddress.ip_address(browser_host)
            if agent_port and agent_port == browser_port:
                raise ValueError()
            with ExitStack() as stack:
                agent_socket = stack.enter_context(_bind(agent_host, agent_port))
                browser_socket = stack.enter_context(_bind(browser_host, browser_port))
                if install_signals:
                    stack.enter_context(_signal_stop(stop))
                pair = pair_factory()
                async with pair.lifespan():
                    def close_admission():
                        # Sticky veto before listener drain, never force-cancel
                        # an accepted remote mutation or close its bus beneath it.
                        try:
                            pair.core.state.queued_pilot_runtime.close_admission()
                        finally:
                            pair.close_admission()
                    async def generation_ready(*addresses):
                        if generation_drain is not None:
                            result = on_generation_ready(generation_drain)
                            await _reject_async_generation_result(result)
                        if on_ready is not None:
                            result = on_ready(*addresses)
                            if inspect.isawaitable(result):
                                await result
                    await _serve_listeners((pair.agents, pair.browser),
                        (agent_socket, browser_socket), stop, generation_ready,
                        (agent_socket.getsockname(), browser_socket.getsockname()),
                        on_stopping=close_admission)
        finally:
            if transferred:
                await finish_owned(cleanup())

    try:
        await _owned_service(operation, stop)
    except Exception:
        raise QueuedPilotServiceError() from None
