from __future__ import annotations

import asyncio
from typing import Any

from .push import PushPublisher


class MCPStreamPresenceMiddleware:
    """Observe only authorized, successful, persistent MCP SSE GET responses."""

    def __init__(self, app, publisher: PushPublisher):
        self.app = app
        self.publisher = publisher

    async def __call__(self, scope: dict[str, Any], receive, send) -> None:
        if (scope.get("type") != "http" or scope.get("method") != "GET"
                or scope.get("path", "").rstrip("/") != "/mcp"):
            await self.app(scope, receive, send)
            return

        stream_token: str | None = None
        state = scope.setdefault("state", {})

        async def observe_send(message: dict[str, Any]) -> None:
            nonlocal stream_token
            if message["type"] == "http.response.start":
                headers = {key.lower(): value for key, value in message.get("headers", [])}
                content_type = headers.get(b"content-type", b"").decode("latin-1").lower()
                await send(message)
                agent = state.get("mcp_presence_agent")
                session_id = state.get("mcp_presence_session_id")
                if (message.get("status") == 200
                        and content_type.split(";", 1)[0].strip() == "text/event-stream"
                        and isinstance(agent, str) and isinstance(session_id, str) and session_id):
                    stream_token = await self.publisher.open_presence_stream(session_id, agent)
                return

            await send(message)
            if (stream_token is not None and message["type"] == "http.response.body"
                    and message.get("body")):
                await self.publisher.record_presence_stream_write(stream_token)

        try:
            await self.app(scope, receive, observe_send)
        finally:
            if stream_token is not None:
                cleanup = asyncio.create_task(self.publisher.close_presence_stream(stream_token))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Keep cleanup alive and give it a chance to finish before propagating cancellation.
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        pass
                    raise
