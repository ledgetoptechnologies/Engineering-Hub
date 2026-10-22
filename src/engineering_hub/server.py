"""One lifecycle owner, a LAN agent socket, and an authenticated browser socket.

This runner neither configures the host nor creates a tunnel. The proxy
connector must be configured separately to use the browser socket.
"""
import asyncio
from contextlib import ExitStack, contextmanager
import inspect
import ipaddress
import signal
import socket
import sys
import threading

import uvicorn

from .lifecycle import finish_owned
from .stream_shutdown import StreamDisconnect


class _Listener(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The shared runner, not either frontend, owns process shutdown.
        yield


@contextmanager
def _signal_stop(stop):
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Hub signal owner must run in the main thread")
    loop = asyncio.get_running_loop()
    saved = {}
    try:
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name)
            saved[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def _bind(host, port):
    address = ipaddress.ip_address(host)  # No DNS/implicit host selection.
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.setblocking(False)
        sock.bind((host, port))
        return sock
    except BaseException:
        sock.close()
        raise


def _listener(app):
    return _Listener(uvicorn.Config(
        app, lifespan="off", interface="asgi3", loop="asyncio", http="h11", ws="none",
        access_log=False, proxy_headers=False, server_header=False, log_config=None,
        workers=1,
        limit_concurrency=128, timeout_graceful_shutdown=30,
    ))


async def serve_frontends(pair_factory, *, agent_host="0.0.0.0", agent_port=8770,
                          browser_host="127.0.0.1", browser_port=8771, stop_event=None, on_ready=None,
                          install_signals=True, agent_alias_port=None):
    """Serve both front doors, starting/stopping the shared core exactly once.

    Port zero is supported for isolated tests; operators use explicit fixed
    ports. Browser bind defaults to loopback; a proxy-reachable numeric address
    requires the configured Cloudflare Access frontend, never a bare ASGI pair.
    The factory must construct its core/owner only when invoked. A failed bind
    happens before construction or ownership startup. Loss of either listener
    drains the other, then closes the one shared core. No replacement is spawned.
    An optional compatibility agent socket shares the same agent server and core;
    it does not create a second MCP session manager or lifecycle owner.
    """
    if not callable(pair_factory):
        raise ValueError("Hub frontend factory required before owner construction")
    ipaddress.ip_address(agent_host)
    if not isinstance(browser_host, str) or "%" in browser_host:
        raise ValueError("Hub browser host must be a numeric IP address without a scope ID")
    browser_address = ipaddress.ip_address(browser_host)
    for port in (agent_port, browser_port):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Hub listener ports must be integers between 0 and 65535")
    if agent_port and agent_port == browser_port:
        raise ValueError("Hub agent and browser ports must be distinct")
    if agent_alias_port is not None:
        if type(agent_alias_port) is not int or not 0 <= agent_alias_port <= 65535:
            raise ValueError("Hub alias port must be an integer between 0 and 65535")
        if agent_alias_port and agent_alias_port in (agent_port, browser_port):
            raise ValueError("Hub alias, agent and browser ports must be distinct")
    stop = stop_event if stop_event is not None else asyncio.Event()
    with ExitStack() as stack:
        agent_socket = stack.enter_context(_bind(agent_host, agent_port))
        browser_socket = stack.enter_context(_bind(browser_host, browser_port))
        agent_sockets = [agent_socket]
        if agent_alias_port is not None:
            agent_sockets.append(stack.enter_context(_bind(agent_host, agent_alias_port)))
        if install_signals:
            stack.enter_context(_signal_stop(stop))
        pair = pair_factory()
        if not browser_address.is_loopback:
            from .frontends import HubFrontends
            if not isinstance(pair, HubFrontends):
                raise ValueError("Non-loopback browser bind requires authenticated HubFrontends")
        async with pair.lifespan():
            # Configuration failure must unwind the core owner too.
            stopping = asyncio.Event()
            servers = [_listener(StreamDisconnect(app, stopping))
                       for app in (pair.agents, pair.browser)]
            workers = [asyncio.create_task(server.serve(sockets=sockets))
                       for server, sockets in zip(servers, (agent_sockets, [browser_socket]))]
            stopped = asyncio.create_task(stop.wait())

            async def ready():
                while not all(server.started for server in servers):
                    await asyncio.sleep(0.01)  # Local socket startup, not inbox polling.

            readiness = asyncio.create_task(ready())
            try:
                done, _ = await asyncio.wait([*workers, stopped, readiness],
                                             return_when=asyncio.FIRST_COMPLETED)
                if readiness in done and stopped not in done and not any(w in done for w in workers):
                    if on_ready is not None:
                        result = on_ready(agent_socket.getsockname(), browser_socket.getsockname())
                        if inspect.isawaitable(result):
                            await result
                    done, _ = await asyncio.wait([*workers, stopped],
                                                 return_when=asyncio.FIRST_COMPLETED)
                if stopped not in done:
                    raise RuntimeError("Hub listener stopped unexpectedly")
            finally:
                # Sticky admission veto and read-only disconnect precede the
                # first cleanup await. Keep the core alive for accepted work.
                admission_error = None
                try:
                    if callable(getattr(pair, 'close_admission', None)):
                        pair.close_admission()
                except BaseException as error:
                    admission_error = error
                finally:
                    stopping.set()
                    for server in servers:
                        server.should_exit = True
                    stopped.cancel()
                    readiness.cancel()

                async def drain():
                    await asyncio.gather(stopped, readiness, return_exceptions=True)
                    results = await asyncio.gather(*workers, return_exceptions=True)
                    if any(isinstance(result, BaseException) for result in results):
                        raise RuntimeError("Hub listener cleanup failed") from None

                # Keep the shared owner alive until both frontends drain, even
                # if the supervisor cancels the runner again during cleanup.
                try:
                    await finish_owned(drain())
                except BaseException as cleanup_error:
                    if admission_error is not None:
                        raise admission_error from cleanup_error
                    raise
                if admission_error is not None:
                    raise admission_error
