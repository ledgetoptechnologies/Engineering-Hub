from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .api import Service
from .database import InvalidTransition, MessageError, NotFound, message_dict


def build_mcp(service: Service) -> FastMCP:
    mcp = FastMCP("agent-message-bus", stateless_http=False, json_response=True)

    def authenticated(ctx: Context) -> str:
        request = ctx.request_context.request
        return service.auth.agent(request)

    @mcp.tool()
    def send_message(recipient: str, message_type: str, body: Any, priority: str = "normal",
                     correlation_id: str | None = None, requires_user_approval: bool = False,
                     ctx: Context | None = None) -> dict[str, Any]:
        """Send one addressed message to the other agent."""
        if ctx is None: raise ValueError("request context required")
        sender = authenticated(ctx)
        message = service.repository.insert(sender=sender, recipient=recipient, message_type=message_type,
                                            body=body, priority=priority, correlation_id=correlation_id,
                                            requires_user_approval=requires_user_approval)
        service.audit_message("send", sender, message)
        return message_dict(message)

    @mcp.tool()
    def receive_messages(limit: int = 50, ctx: Context | None = None) -> dict[str, Any]:
        """Return pending messages addressed to the authenticated agent."""
        if ctx is None: raise ValueError("request context required")
        identity = authenticated(ctx)
        messages = service.repository.list_for_recipient(identity, status="pending", limit=limit)
        return {"messages": [message_dict(message) for message in messages]}

    @mcp.tool()
    def claim_message(message_id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Claim a pending or expired message for the authenticated agent."""
        if ctx is None: raise ValueError("request context required")
        identity = authenticated(ctx)
        message = service.repository.claim(message_id, identity)
        service.audit_message("claim", identity, message)
        return message_dict(message)

    @mcp.tool()
    def ack_message(message_id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Acknowledge a message without deleting it."""
        if ctx is None: raise ValueError("request context required")
        identity = authenticated(ctx)
        message = service.repository.acknowledge(message_id, identity)
        service.audit_message("acknowledge", identity, message)
        return message_dict(message)

    @mcp.tool()
    def reject_message(message_id: str, reason: str, ctx: Context | None = None) -> dict[str, Any]:
        """Reject a claimed message with a redacted audit record."""
        if ctx is None: raise ValueError("request context required")
        identity = authenticated(ctx)
        message = service.repository.reject(message_id, identity, reason)
        service.audit_message("reject", identity, message)
        return message_dict(message)

    @mcp.tool()
    def get_message(message_id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Read a message addressed to the authenticated agent."""
        if ctx is None: raise ValueError("request context required")
        identity = authenticated(ctx)
        message = service.repository.get(message_id)
        if message.recipient != identity: raise NotFound("message not found")
        return message_dict(message)

    return mcp
