"""Narrow opt-in tools for installation mailboxes, never legacy Hub authority."""
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import StrictInt

from .agent_sessions import AgentSessionError
from .agent_sessions_api import bearer, MessageSend, Claim, ClaimFinish, ClaimRenew


def install_session_tools(mcp, repository):
    def operation(ctx, name, *args, **kwargs):
        try:
            return getattr(repository, name)(bearer(ctx.request_context.request), *args, **kwargs)
        except AgentSessionError as exc:
            raise ToolError(exc.code) from None

    @mcp.tool()
    def session_identity(ctx: Context) -> dict:
        """Read this staging installation/session identity; no legacy actor grant."""
        principal = operation(ctx, "authenticate_session")
        return {"installation_id": principal.installation_id, "session_id": principal.session_id,
                "family": principal.family, "expires_at": principal.expires_at}

    @mcp.tool()
    def session_peers(ctx: Context) -> dict:
        """List approved staging peers, not project membership or execution grants."""
        return operation(ctx, "peers")

    @mcp.tool()
    def session_message_send(payload: MessageSend, ctx: Context) -> dict:
        """Send bounded text to an explicit installation; sender comes from auth."""
        return operation(ctx, "send", **payload.model_dump())

    @mcp.tool()
    def session_messages(ctx: Context, after: StrictInt = 0, limit: StrictInt = 50) -> dict:
        """Read only this installation's mailbox; no family-inbox fallback."""
        return operation(ctx, "messages", after=after, limit=limit)

    @mcp.tool()
    def session_message_get(message_id: str, ctx: Context) -> dict:
        return operation(ctx, "get", message_id)

    @mcp.tool()
    def session_message_claim(message_id: str, payload: Claim, ctx: Context) -> dict:
        """Claim as this exact session; returned handle is not execution authority."""
        return operation(ctx, "claim", message_id, **payload.model_dump())

    @mcp.tool()
    def session_message_renew(message_id: str, payload: ClaimRenew, ctx: Context) -> dict:
        return operation(ctx, "renew", message_id, **payload.model_dump())

    @mcp.tool()
    def session_message_acknowledge(message_id: str, payload: ClaimFinish, ctx: Context) -> dict:
        return operation(ctx, "acknowledge", message_id, **payload.model_dump())
