"""Explicit split-process listener ownership, without host activation.

Factories must be synchronous and inert until called. The private factory owns
any resources it constructs but does not return successfully; the public runner
owns only an exact AsyncClient successfully returned by its factory. Dedicated
UDS transport construction remains the public caller's responsibility. Neither
entry point creates directories, reads environment configuration, installs a
service, or provides a TCP fallback for the private browser connection.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import inspect
import ipaddress

import httpx

from .lifecycle import finish_owned
from .private_browser_socket import private_browser_socket
from .private_gateway import PrivateBrowserGateway, PublicBrowserGateway, _is_events
from .server import _bind, _listener, _signal_stop


class SplitServerError(RuntimeError):
    """Sanitized listener/lifecycle failure; no paths, tokens or callback text."""

    def __init__(self):
        super().__init__('Hub split listener unavailable')


def _options(factory, ports, stop_event, on_ready, install_signals):
    if not callable(factory) or inspect.iscoroutinefunction(factory):
        raise ValueError('Explicit synchronous Hub factory required')
    if any(type(port) is not int or not 0 <= port <= 65535 for port in ports):
        raise ValueError('Hub listener ports must be integers between 0 and 65535')
    if stop_event is not None and type(stop_event) is not asyncio.Event:
        raise ValueError('Explicit asyncio stop event required')
    if on_ready is not None and not callable(on_ready):
        raise ValueError('Hub readiness callback must be callable')
    if type(install_signals) is not bool:
        raise ValueError('Hub signal option must be boolean')


class _StreamDisconnect:
    """End read-only event subscriptions when listener admission is stopping.

    Only an idle receive observer is cancelled. Ordinary requests, including
    every POST, retain their original receive and must drain to completion.
    """

    def __init__(self, app, stopping):
        self.app, self.stopping = app, stopping

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        streaming = (scope.get('type') == 'http' and scope.get('method') == 'GET'
                     and (_is_events(path) or path in {'/mcp', '/mcp/'}))
        if not streaming:
            return await self.app(scope, receive, send)

        async def disconnectable_receive():
            if self.stopping.is_set():
                return {'type': 'http.disconnect'}
            incoming = asyncio.create_task(receive())
            stopped = asyncio.create_task(self.stopping.wait())
            try:
                done, _ = await asyncio.wait((incoming, stopped),
                                             return_when=asyncio.FIRST_COMPLETED)
                if stopped in done:
                    return {'type': 'http.disconnect'}
                return incoming.result()
            finally:
                for observer in (incoming, stopped):
                    if not observer.done():
                        observer.cancel()
                await finish_owned(asyncio.gather(incoming, stopped, return_exceptions=True))

        return await self.app(scope, disconnectable_receive, send)


def _task(coroutine):
    try:
        return asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise


async def _serve_listeners(apps, sockets, stop, on_ready, ready_args,
                           on_stopping=None):
    if stop.is_set():
        return
    stopping = asyncio.Event()
    servers = [_listener(_StreamDisconnect(app, stopping)) for app in apps]
    # Never force-cancel an accepted mutation after a wall-clock grace period.
    # The external supervisor must also allow the actual drain to finish.
    for server in servers:
        server.config.timeout_graceful_shutdown = None
    workers, observers = [], []

    async def ready():
        while not all(server.started for server in servers):
            await asyncio.sleep(0.01)  # Local listener startup, not inbox polling.
        if any(worker.done() for worker in workers) or stop.is_set():
            return
        if on_ready is not None:
            result = on_ready(*ready_args)
            if inspect.isawaitable(result):
                await result

    try:
        for server, sock in zip(servers, sockets):
            workers.append(_task(server.serve(sockets=[sock])))
        stopped = _task(stop.wait())
        observers.append(stopped)
        readiness = _task(ready())
        observers.append(readiness)
        done, _ = await asyncio.wait((*workers, stopped, readiness),
                                     return_when=asyncio.FIRST_COMPLETED)
        if readiness in done:
            readiness.result()
            if stopped not in done and not any(worker in done for worker in workers):
                done, _ = await asyncio.wait((*workers, stopped),
                                             return_when=asyncio.FIRST_COMPLETED)
        if stopped not in done:
            raise SplitServerError()
    finally:
        try:
            if on_stopping is not None:
                on_stopping()  # Sticky admission veto before the first await.
        finally:
            stopping.set()
            for server in servers:
                server.should_exit = True
            for observer in observers:
                observer.cancel()

            async def drain():
                await asyncio.gather(*observers, return_exceptions=True)
                results = await asyncio.gather(*workers, return_exceptions=True)
                if any(isinstance(result, BaseException) for result in results):
                    raise SplitServerError()

            await finish_owned(drain())


async def _owned_service(operation, stop):
    task = _task(operation())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        try:
            await finish_owned(task)
        finally:
            raise asyncio.CancelledError from None


async def serve_private_coordinator(pair_factory, *, socket_path,
                                    agent_host='0.0.0.0', agent_port=8770,
                                    stop_event=None, on_ready=None,
                                    install_signals=True, recover_socket=False):
    """Bind agent TCP and protected browser UDS before constructing one owner.

    ``on_ready(agent_sockname, private_sockname)`` is an optional notification,
    not an owner of work; it may be cancelled during shutdown. Agent requests
    retain their actual TCP source address. No browser TCP listener is opened.
    Loss of either listener stops the other before the single pair lifespan
    exits. Cancellation requests a stop, then waits through repeated cancellation
    until listener requests and accepted core work are terminal.

    Recovery is a separate, explicit endpoint protocol. The default preserves
    rejection of every existing socket; no environment flag enables recovery.
    """
    _options(pair_factory, (agent_port,), stop_event, on_ready, install_signals)
    # The public-only process must not import the private application factory
    # or its database/executor dependencies merely to own a browser listener.
    from .frontends import HubFrontends
    if type(recover_socket) is not bool:
        raise ValueError('Explicit boolean endpoint recovery option required')
    try:
        if type(agent_host) is not str:
            raise ValueError()
        ipaddress.ip_address(agent_host)
    except (ValueError, TypeError):
        raise ValueError('Literal Hub agent address required') from None
    stop = stop_event if stop_event is not None else asyncio.Event()

    async def operation():
        if stop.is_set():
            return
        with ExitStack() as stack:
            agent = stack.enter_context(_bind(agent_host, agent_port))
            socket_context = private_browser_socket
            if recover_socket:
                from .recoverable_browser_socket import recoverable_browser_socket
                socket_context = recoverable_browser_socket
            browser = stack.enter_context(socket_context(socket_path))
            if install_signals:
                stack.enter_context(_signal_stop(stop))
            pair = pair_factory()
            if (type(pair) is not HubFrontends or pair.state != 'new'
                    or pair.settings.ingress_mode != 'combined'
                    or pair.settings.auth_config.human_auth_mode != 'cloudflare_access'
                    or pair.settings.auth_config.cloudflare_config is None):
                raise SplitServerError()
            if (getattr(pair.core.state, 'staging_runtime', None) is not None
                    or getattr(pair.core.state, 'owned_pilot_service', None) is not None):
                raise SplitServerError()
            runtime = getattr(pair.core.state, 'queued_pilot_runtime', None)
            if runtime is not None:
                from .queued_pilot_runtime import QueuedPilotRuntime
                if type(runtime) is not QueuedPilotRuntime:
                    raise SplitServerError()

            def close_admission():
                try:
                    pair.close_admission()
                finally:
                    if runtime is not None:
                        runtime.close_admission()

            receiver = PrivateBrowserGateway(
                pair.browser, agent_auth=pair.settings.agent_auth,
                cloudflare_config=pair.settings.auth_config.cloudflare_config)
            async with pair.lifespan():
                try:
                    await _serve_listeners((pair.agents, receiver), (agent, browser),
                                           stop, on_ready,
                                           (agent.getsockname(), browser.getsockname()),
                                           on_stopping=close_admission)
                finally:
                    # Includes errors before any server worker was created.
                    close_admission()

    try:
        await _owned_service(operation, stop)
    except Exception:
        raise SplitServerError() from None


async def serve_public_browser(client_factory, *, browser_port=8771,
                               stop_event=None, on_ready=None,
                               install_signals=True):
    """Bind only literal loopback, then accept one dedicated private UDS client.

    ``on_ready(browser_sockname)`` runs only after listener startup. The factory
    must return a dedicated real AsyncClient, normally constructed by
    create_private_browser_client; there is no database, agent settings, private
    credential, or core argument. A constructor/factory that fails before
    returning remains responsible for its unaccepted resources. The accepted
    client closes only after requests drain, even on repeated cancellation.
    """
    _options(client_factory, (browser_port,), stop_event, on_ready, install_signals)
    stop = stop_event if stop_event is not None else asyncio.Event()

    async def operation():
        if stop.is_set():
            return
        with ExitStack() as stack:
            browser = stack.enter_context(_bind('127.0.0.1', browser_port))
            if install_signals:
                stack.enter_context(_signal_stop(stop))
            client = client_factory()
            if type(client) is not httpx.AsyncClient:
                raise SplitServerError()
            try:
                gateway = PublicBrowserGateway(client)
                await _serve_listeners((gateway,), (browser,), stop, on_ready,
                                       (browser.getsockname(),))
            finally:
                await finish_owned(client.aclose())

    try:
        await _owned_service(operation, stop)
    except Exception:
        raise SplitServerError() from None
