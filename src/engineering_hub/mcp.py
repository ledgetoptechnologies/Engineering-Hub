"""Independent Hub MCP interface; never extends or replaces the existing bus."""
from fastapi import HTTPException
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse

from .repository import HubError
import inspect


class HubMCPAuthMiddleware:
    """Wrap only the mounted MCP ASGI app; authenticate discovery and calls alike."""

    def __init__(self, app, auth):
        self.app, self.auth = app, auth

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            try:
                self.auth.agent(Request(scope))
            except HTTPException as error:
                status = 403 if error.status_code == 403 else 401
                response = JSONResponse({"detail": {"code": "forbidden" if status == 403 else "unauthorized"}},
                                        status_code=status,
                                        headers={"WWW-Authenticate": "Bearer"} if status == 401 else {})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_hub_mcp(repo, auth, *, topic_peers=None, topic_preflight=None, autonomy_status=None) -> FastMCP:
    """Call streamable_http_app once, mount its auth wrapper, run session_manager."""
    mcp = FastMCP("Engineering Hub", stateless_http=True, streamable_http_path="/", json_response=True)

    def identity(ctx):
        request = ctx.request_context.request
        if request is None:
            raise ToolError("unauthorized")
        try:
            return auth.agent(request)
        except HTTPException:
            raise ToolError("unauthorized") from None

    def invoke(operation, *args):
        try:
            return operation(*args)
        except HubError as error:
            # Repository codes are fixed internal identifiers, never peer titles.
            raise ToolError(error.code) from None
        except Exception:
            raise ToolError("internal_error") from None

    async def invoke_topic(operation, ctx, *args):
        principal = identity(ctx)
        request = ctx.request_context.request
        scheme, _, bearer = request.headers.get('authorization', '').partition(' ')
        try:
            result = operation(*args, principal=principal,
                               bearer=bearer if scheme.lower() == 'bearer' else None)
            return await result if inspect.isawaitable(result) else result
        except HubError as error:
            raise ToolError(error.code) from None
        except Exception:
            raise ToolError('reachability_source_unavailable') from None

    @mcp.tool()
    def hub_overview(ctx: Context) -> dict:
        """Read shared tasks, decisions, runs and the emergency-pause state."""
        identity(ctx)
        result = {"tasks": invoke(repo.list_tasks), "decisions": invoke(repo.list_decisions),
                  "runs": invoke(repo.list_runs), "paused": invoke(repo.get_paused)}
        if autonomy_status is not None:
            result['autonomy'] = invoke(autonomy_status)
        return result

    @mcp.tool()
    def hub_task_get(task_id: str, ctx: Context) -> dict:
        """Read an assigned Hub task."""
        identity(ctx)
        return invoke(repo.get_task, task_id)

    @mcp.tool()
    def hub_task_claim(task_id: str, expected_revision: int, ctx: Context) -> dict:
        """Claim an explicitly assigned task as the authenticated agent."""
        return invoke(repo.claim_task, identity(ctx), task_id, expected_revision)

    @mcp.tool()
    def hub_task_candidate(task_id: str, expected_revision: int, commit: str, ctx: Context) -> dict:
        """Record an exact candidate commit for an owned task."""
        return invoke(repo.set_candidate, identity(ctx), task_id, expected_revision, commit)

    @mcp.tool()
    def hub_task_review(task_id: str, expected_revision: int, commit: str, verdict: str, ctx: Context) -> dict:
        """Review another agent's exact candidate; self-review is forbidden."""
        return invoke(repo.review_task, identity(ctx), task_id, expected_revision, commit, verdict)

    @mcp.tool()
    def hub_decision_request(project: str, title: str, options: list[str], ctx: Context, protected: bool = False) -> dict:
        """Request a human decision; this tool cannot answer or grant approval."""
        return invoke(repo.create_decision, identity(ctx), project, title, options, protected)

    @mcp.tool()
    def hub_run_list(ctx: Context) -> dict:
        """Read execution records without dispatching work."""
        identity(ctx)
        return {"items": invoke(repo.list_runs)}

    @mcp.tool()
    async def hub_topic_peers(topic: str, ctx: Context) -> dict:
        """Read exact-topic peer evidence; this is not a delivery guarantee."""
        identity(ctx)
        if topic_peers is None:
            raise ToolError('reachability_source_unavailable')
        return await invoke_topic(topic_peers, ctx, topic)

    @mcp.tool()
    async def hub_topic_preflight(topic: str, recipient: str, ctx: Context) -> dict:
        """Read advisory reachability before sending; never discards offline mail."""
        identity(ctx)
        if topic_preflight is None:
            raise ToolError('reachability_source_unavailable')
        return await invoke_topic(topic_preflight, ctx, topic, recipient)

    return mcp
