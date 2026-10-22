"""Transport-only disconnect for idle read-only subscriptions during drain.

No database, native event source, task executor or lifecycle owner is stopped.
"""
import asyncio
import re

from .lifecycle import finish_owned


def _event_path(path):
    return path == '/api/hub/v1/events' or re.fullmatch(
        r'/api/hub/v1/topics/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}/events', path) is not None


class StreamDisconnect:
    """End exact event/MCP GET streams, never cancel accepted POST handlers.

    Only idle receive observers are cancelled. The containing runner retains
    the shared owner and its existing request-drain policy.
    """

    def __init__(self, app, stopping):
        self.app, self.stopping = app, stopping

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        streaming = (scope.get('type') == 'http' and scope.get('method') == 'GET'
                     and (_event_path(path) or path in {'/mcp', '/mcp/'}))
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
