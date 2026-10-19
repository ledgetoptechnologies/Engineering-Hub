"""Explicit owned loopback executor listener, not an installed host service.

No configuration, credentials, driver identity, providers or runtime endpoints
are discovered. A trusted operator factory must freshly dedicate a complete
resource bundle; non-null dependencies are not authenticity or enrollment proof.
This seam does not authorize a real Docker pilot or establish reboot recovery.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import inspect
import os
import sys

from engineering_hub.lifecycle import finish_owned
from engineering_hub.pilot_service import REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS
from engineering_hub.server import _bind, _signal_stop
from engineering_hub.split_server import _owned_service, _serve_listeners

from .adapter import HostAdapter
from .asgi import PilotASGI


class HostExecutorServiceError(RuntimeError):
    """Sanitized service failure, without credentials, paths or callback text."""

    def __init__(self):
        super().__init__('Hub owned host executor unavailable')


def _synchronous(value):
    return (callable(value) and not inspect.iscoroutinefunction(value)
            and not inspect.isasyncgenfunction(value)
            and not inspect.iscoroutinefunction(getattr(value, '__call__', None))
            and not inspect.isasyncgenfunction(getattr(value, '__call__', None)))


def _reject_awaitable(value):
    if inspect.isawaitable(value) or inspect.isasyncgen(value):
        if (inspect.iscoroutine(value)
                and inspect.getcoroutinestate(value) == inspect.CORO_CREATED):
            value.close()
        # A started coroutine, Future or Task still belongs to its creator.
        raise HostExecutorServiceError()


class _AdmissionGate:
    """Sticky new-request veto; accepted HTTP retains its original lifetime."""

    def __init__(self, app, stop):
        self.app, self.stop = app, stop
        self.closed = False

    def close_admission(self):
        self.closed = True

    async def __call__(self, scope, receive, send):
        if scope.get('type') == 'http' and (self.closed or self.stop.is_set()):
            await PilotASGI._error(send, 503, 'executor_unavailable')
            return
        await self.app(scope, receive, send)


def _exit_context(manager, exception):
    result = manager.__exit__(*exception)
    _reject_awaitable(result)
    # Cleanup-only ownership: a context cannot suppress a service failure or
    # cancellation and turn an incomplete listener lifecycle into success.


async def serve_owned_host_executor(resource_factory, *, bearer_token,
                                    supervisor_stop_grace_seconds, port,
                                    host='127.0.0.1', stop_event=None,
                                    on_ready=None, install_signals=True):
    """Bind explicit loopback, then own a dedicated adapter/provider bundle.

    The synchronous factory returns a synchronous context manager yielding an
    exact HostAdapter. It owns partial construction/failed entry and must never
    return borrowed or reused resources. Successful entry transfers adapter
    cleanup to this service: after all accepted HTTP/raw executor work drains,
    adapter.close runs once before the context exits. The context owns provider
    cleanup, must attempt all providers even if one close fails, and must support
    exit on an executor thread. It may idempotently close the adapter too.
    Freshness, provider authenticity and correct arbitrary-context cleanup are
    trusted factory preconditions, not guarantees established by type checks.
    Known closed, inherited, closing or active adapters are rejected before
    transfer; their context retains cleanup responsibility. These snapshots do
    not prove fresh construction, exclusive use, or protection against races.

    Supervisor grace is an operator assertion, not an installed timeout check.
    No force-cancel deadline is applied to accepted work. on_ready receives one
    actual bound socket address after listener startup; no host/port is inferred.
    """
    if (not _synchronous(resource_factory) or type(bearer_token) is not str
            or not bearer_token or any(not 33 <= ord(char) <= 126 for char in bearer_token)
            or type(supervisor_stop_grace_seconds) is not int
            or supervisor_stop_grace_seconds < REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS
            or type(host) is not str or host not in {'127.0.0.1', '::1'}
            or type(port) is not int or not 0 <= port <= 65535
            or stop_event is not None and type(stop_event) is not asyncio.Event
            or on_ready is not None and not callable(on_ready)
            or type(install_signals) is not bool):
        raise ValueError('explicit owned loopback executor configuration required')
    stop = stop_event if stop_event is not None else asyncio.Event()

    async def operation():
        if stop.is_set():
            return
        with ExitStack() as stack:
            listener = stack.enter_context(_bind(host, port))
            if install_signals:
                stack.enter_context(_signal_stop(stop))
            manager = resource_factory()
            _reject_awaitable(manager)
            if (not _synchronous(getattr(manager, '__enter__', None))
                    or not _synchronous(getattr(manager, '__exit__', None))):
                raise HostExecutorServiceError()
            value = manager.__enter__()
            adapter = None
            try:
                _reject_awaitable(value)
                if type(value) is not HostAdapter:
                    raise HostExecutorServiceError()
                owner_pid = getattr(value, '_owner_pid', None)
                active = getattr(value, '_active_operations', None)
                active_threads = getattr(value, '_active_threads', None)
                if (type(owner_pid) is not int or owner_pid != os.getpid()
                        or type(active) is not int or active != 0
                        or type(active_threads) is not dict or active_threads
                        or getattr(value, '_closed', None) is not False
                        or getattr(value, '_closing', None) is not False
                        or getattr(value, '_finalizing_close', None) is not False):
                    raise HostExecutorServiceError()
                adapter = value
                if any(getattr(adapter, name, None) is None for name in (
                    'record_store', 'metadata_store', 'provenance_verifier', 'backend',
                    'baseline_provider', 'state_provisioner', 'secret_resolver',
                    'driver_identity_verifier')):
                    raise HostExecutorServiceError()
                gate = _AdmissionGate(PilotASGI(adapter, bearer_token), stop)
                try:
                    await _serve_listeners((gate,), (listener,), stop, on_ready,
                                           (listener.getsockname(),),
                                           on_stopping=gate.close_admission)
                finally:
                    gate.close_admission()
            finally:
                try:
                    if adapter is not None:
                        await finish_owned(asyncio.to_thread(adapter.close))
                finally:
                    await finish_owned(asyncio.to_thread(_exit_context, manager, sys.exc_info()))

    try:
        # Initial scheduling precedes resource construction. This retained task
        # spans listener draining, adapter close and provider-context exit, so
        # repeated caller cancellation cannot abandon accepted thread effects.
        await _owned_service(operation, stop)
    except Exception:
        raise HostExecutorServiceError() from None
