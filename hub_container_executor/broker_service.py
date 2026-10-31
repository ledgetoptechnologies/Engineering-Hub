"""Explicit owned seqpacket adapter service; not wired to an installed launcher.

Caller provides a pre-bound exclusive listener, pinned peer and a fresh adapter
context manager. Successful validation transfers their cleanup to this service.
It does not create identities, sockets, credentials, storage or isolation.
"""
import asyncio
import concurrent.futures
import inspect
import os
import socket
import time

from engineering_hub.lifecycle import finish_owned
from .adapter import HostAdapter
from .broker_seqpacket import PeerPin, PilotCommandBroker, BrokerTransportError, BrokerAcceptTimeout, BrokerAdmissionClosed, BrokerReadTimeout


async def _readable_or_stop(listener, stop):
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    def mark_ready():
        if not ready.done():
            ready.set_result(None)
    loop.add_reader(listener.fileno(), mark_ready)
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait((ready, stopping), return_when=asyncio.FIRST_COMPLETED)
        return not stop.is_set()
    finally:
        loop.remove_reader(listener.fileno())
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)
        if not ready.done():
            ready.cancel()


async def serve_owned_broker(resource_factory, *, listener, peer, stop_event, on_ready=None,
                             command_timeout_seconds=125):
    """Serve one admitted packet at a time and drain it before closing resources.

    Inputs must be newly dedicated; listener has no other acceptor. The factory
    returns a synchronous context manager yielding an exact HostAdapter. The
    listener and peer pin transfer ownership after validation. Any malformed or
    uncertain exchange stops this service, preserving evidence; it never retries.
    Stop/cancellation closes admission but cannot cancel accepted adapter work.
    """
    if (not callable(resource_factory) or inspect.iscoroutinefunction(resource_factory)
            or type(listener) is not socket.socket or listener.family != socket.AF_UNIX
            or listener.type & socket.SOCK_SEQPACKET != socket.SOCK_SEQPACKET
            or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1
            or type(peer) is not PeerPin or type(stop_event) is not asyncio.Event
            or (on_ready is not None and not callable(on_ready))
            or type(command_timeout_seconds) not in (int, float)
            or not 0 < command_timeout_seconds <= 125):
        raise ValueError('explicit owned broker configuration required')
    owner = os.getpid()
    loop = asyncio.get_running_loop()
    gate = {'closed': False}
    def admit(deadline):
        # The decision is serialized on the same event-loop thread as Event.set
        # and cancellation. Once granted, this is accepted work and must drain.
        decision = concurrent.futures.Future()
        def decide():
            if not decision.done():
                decision.set_result(not gate['closed'] and not stop_event.is_set()
                                    and time.monotonic() < deadline)
        try:
            loop.call_soon_threadsafe(decide)
            remaining = deadline - time.monotonic()
            return remaining > 0 and decision.result(timeout=remaining) is True
        except Exception:
            return False
    async def operation():
        manager = adapter = broker = None
        entered = False
        try:
            if stop_event.is_set():
                return
            manager = resource_factory()
            if (inspect.isawaitable(manager) or not callable(getattr(manager, '__enter__', None))
                    or not callable(getattr(manager, '__exit__', None))):
                raise BrokerTransportError()
            candidate = await asyncio.to_thread(manager.__enter__)
            entered = True
            if (type(candidate) is not HostAdapter or getattr(candidate, '_owner_pid', None) != owner
                    or getattr(candidate, '_closed', None) is not False
                    or getattr(candidate, '_closing', None) is not False
                    or getattr(candidate, '_finalizing_close', None) is not False
                    or getattr(candidate, '_active_operations', None) != 0
                    or getattr(candidate, '_active_threads', None) != {}):
                raise BrokerTransportError()
            adapter = candidate
            broker = PilotCommandBroker(listener, adapter, peer, admission=admit)
            if on_ready is not None:
                result = on_ready()
                if inspect.isawaitable(result):
                    await result
            while not stop_event.is_set() and await _readable_or_stop(listener, stop_event):
                if stop_event.is_set():
                    break
                # No cancellation or stop may abandon a thread that may be inside
                # an accepted adapter effect. Unknown outcomes stay in its ledger.
                try:
                    await finish_owned(asyncio.to_thread(broker.serve_once, command_timeout_seconds,
                        accept_timeout_seconds=min(.25, command_timeout_seconds),
                        read_timeout_seconds=min(1.0, command_timeout_seconds)))
                except (BrokerAcceptTimeout, BrokerReadTimeout):
                    # No complete packet reached admission or the adapter.
                    if stop_event.is_set() or gate['closed']:
                        break
                    continue
                except BrokerAdmissionClosed:
                    if stop_event.is_set() or gate['closed']:
                        break
                    raise
        finally:
            gate['closed'] = True
            if os.getpid() == owner:
                try:
                    if broker is not None:
                        broker.close()
                finally:
                    try:
                        if adapter is not None and type(adapter) is HostAdapter:
                            await finish_owned(asyncio.to_thread(adapter.close))
                    finally:
                        try:
                            if manager is not None and entered:
                                await finish_owned(asyncio.to_thread(manager.__exit__, None, None, None))
                        finally:
                            listener.close()
                            peer.close()
    task = asyncio.create_task(operation())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        gate['closed'] = True
        stop_event.set()
        try:
            await finish_owned(task)
        finally:
            raise asyncio.CancelledError from None
