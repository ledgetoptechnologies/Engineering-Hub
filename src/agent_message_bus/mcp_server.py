from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .api import Service
from .database import InvalidTransition, MessageError, NotFound, message_dict
from .knowledge import KnowledgeRepository, entry_dict, search_result_dict


def build_mcp(service: Service) -> FastMCP:
    import os

    allowed_hosts = [
        value.strip()
        for value in os.environ.get(
            "AGENT_BUS_ALLOWED_HOSTS",
            "127.0.0.1:8765,127.0.0.1,localhost:8765,localhost,192.168.60.92:8765,192.168.60.92",
        ).split(",")
        if value.strip()
    ]
    mcp = FastMCP(
        "agent-message-bus",
        stateless_http=False,
        json_response=True,
        host=os.environ.get("AGENT_BUS_CONTAINER_HOST", "0.0.0.0"),
        port=int(os.environ.get("AGENT_BUS_PORT", "8765")),
        transport_security=TransportSecuritySettings(allowed_hosts=allowed_hosts),
    )

    def authenticated(ctx: Context) -> str:
        request = ctx.request_context.request
        return service.auth.agent(request)

    knowledge = KnowledgeRepository(service.repository.path)

    @mcp.tool()
    def send_message(recipient: str, message_type: str, body: Any, priority: str = "normal",
                     correlation_id: str | None = None, in_reply_to: str | None = None,
                     requires_user_approval: bool = False, ctx: Context | None = None) -> dict[str, Any]:
        """Send one addressed message to the other agent."""
        if ctx is None: raise ValueError("request context required")
        sender = authenticated(ctx)
        message = service.repository.insert(sender=sender, recipient=recipient, message_type=message_type,
                                            body=body, priority=priority, correlation_id=correlation_id,
                                            in_reply_to=in_reply_to,
                                            requires_user_approval=requires_user_approval)
        service.audit_message("send", sender, message, request=ctx.request_context.request)
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
        service.audit_message("claim", identity, message, request=ctx.request_context.request)
        return message_dict(message)

    @mcp.tool()
    def ack_message(message_id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Acknowledge a message without deleting it."""
        if ctx is None: raise ValueError("request context required")
        identity = authenticated(ctx)
        message = service.repository.acknowledge(message_id, identity)
        service.audit_message("acknowledge", identity, message, request=ctx.request_context.request)
        return message_dict(message)

    @mcp.tool()
    def reject_message(message_id: str, reason: str, ctx: Context | None = None) -> dict[str, Any]:
        """Reject a claimed message with a redacted audit record."""
        if ctx is None: raise ValueError("request context required")
        identity = authenticated(ctx)
        message = service.repository.reject(message_id, identity, reason)
        service.audit_message("reject", identity, message, request=ctx.request_context.request)
        return message_dict(message)

    @mcp.tool()
    def get_message(message_id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Read a message addressed to the authenticated agent."""
        if ctx is None: raise ValueError("request context required")
        identity = authenticated(ctx)
        message = service.repository.get(message_id)
        if message.recipient != identity: raise NotFound("message not found")
        return message_dict(message)

    @mcp.tool(name="knowledge_put")
    def knowledge_put(project: str, path: str, title: str, content: str, expected_version: int,
                      ctx: Context | None = None) -> dict[str, Any]:
        """Create or compare-and-swap update a shared project/path document.

        Use expected_version=0 to create a new document; updates must provide
        the version returned by knowledge_get/list/search. Content is limited
        to 64 KiB of UTF-8 text.
        """
        if ctx is None: raise ValueError("request context required")
        identity = authenticated(ctx)
        entry = knowledge.put(project=project, path=path, title=title, content=content,
                              author=identity, expected_version=expected_version)
        request = ctx.request_context.request
        service.audit.append(operation="knowledge_put", agent=identity, result="ok",
                             correlation_id=getattr(request.state, "correlation_id", None),
                             request_id=getattr(request.state, "request_id", None))
        return entry_dict(entry)

    @mcp.tool(name="knowledge_get")
    def knowledge_get(project: str, path: str, ctx: Context | None = None) -> dict[str, Any]:
        """Read one shared knowledge document and verify its SHA-256 digest."""
        if ctx is None: raise ValueError("request context required")
        authenticated(ctx)
        return entry_dict(knowledge.get(project, path))

    @mcp.tool(name="knowledge_list")
    def knowledge_list(project: str, path_prefix: str | None = None, limit: int = 50,
                       ctx: Context | None = None) -> dict[str, Any]:
        """List shared knowledge metadata in a project, optionally by path prefix."""
        if ctx is None: raise ValueError("request context required")
        authenticated(ctx)
        entries = knowledge.list(project=project, path_prefix=path_prefix, limit=limit)
        return {"documents": [entry_dict(entry, include_content=False) for entry in entries]}

    @mcp.tool(name="knowledge_search")
    def knowledge_search(query: str, project: str | None = None, limit: int = 20,
                         ctx: Context | None = None) -> dict[str, Any]:
        """Find shared knowledge matching every case-insensitive query term."""
        if ctx is None: raise ValueError("request context required")
        authenticated(ctx)
        entries = knowledge.search(query=query, project=project, limit=limit)
        return {"results": [search_result_dict(entry, query) for entry in entries]}

    return mcp
