"""Independent Hub MCP interface; never extends or replaces the existing bus."""
from fastapi import HTTPException
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse
from pydantic import ValidationError

from .repository import HubError
from .work_queue import WorkWorker
from .work_queue_api import (WorkList, WorkGet, WorkSubmission, WorkClaim,
                             WorkJobRenew, WorkJobComplete, submit_reviewed_work)
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


def build_hub_mcp(repo, auth, *, topic_peers=None, topic_preflight=None, autonomy_status=None,
                  work_queue=None) -> FastMCP:
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
        except ToolError:
            raise
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

    def work_input(model, payload):
        try:
            return model.model_validate(payload)
        except ValidationError:
            raise ToolError('invalid_input') from None

    def configured_work():
        if work_queue is None:
            raise ToolError('work_queue_not_configured')
        return work_queue

    def hub_work_status(payload: dict, ctx: Context) -> dict:
        """Read queue policy/registry/jobs; no worker or execution is started."""
        identity(ctx)
        query = work_input(WorkList, payload)
        return invoke(lambda: configured_work().status(limit=query.limit))

    def hub_work_jobs(payload: dict, ctx: Context) -> dict:
        """Read a bounded shared job list, including reasons work is blocked."""
        identity(ctx)
        query = work_input(WorkList, payload)
        return {'items': invoke(lambda: configured_work().status(limit=query.limit))['jobs']}

    def hub_work_get(payload: dict, ctx: Context) -> dict:
        """Read one durable job without renewing, dispatching or reconciling it."""
        identity(ctx)
        query = work_input(WorkGet, payload)
        return invoke(lambda: configured_work().get_job(query.job_id))

    def hub_work_submit(payload: dict, ctx: Context) -> dict:
        """Admit registered work for an owned, independently reviewed exact task."""
        principal = identity(ctx)
        data = work_input(WorkSubmission, payload)
        return invoke(lambda: submit_reviewed_work(repo, configured_work(), principal, data))

    def hub_work_claim(payload: dict, ctx: Context) -> dict:
        """Reserve one eligible job for this agent and process-instance UUID."""
        principal = identity(ctx)
        data = work_input(WorkClaim, payload)
        return {'job': invoke(lambda: configured_work().claim(
            worker=WorkWorker(principal, data.instance_id), lease_seconds=data.lease_seconds))}

    def hub_work_renew(payload: dict, ctx: Context) -> dict:
        """Renew only this authenticated agent's matching live instance/fence."""
        principal = identity(ctx)
        data = work_input(WorkJobRenew, payload)
        return invoke(lambda: configured_work().renew(
            job_id=data.job_id, worker=WorkWorker(principal, data.instance_id),
            fence=data.fence, lease_seconds=data.lease_seconds))

    def hub_work_complete(payload: dict, ctx: Context) -> dict:
        """Record a bound outcome; execution proof is required, not fabricated here."""
        principal = identity(ctx)
        data = work_input(WorkJobComplete, payload)
        return invoke(lambda: configured_work().complete(
            job_id=data.job_id, worker=WorkWorker(principal, data.instance_id),
            fence=data.fence, outcome=data.outcome, evidence_id=data.evidence_id))

    # Default/off deployments preserve the established nine-tool contract and
    # must not advertise an unconfigured worker/admission capability.
    if work_queue is not None:
        for operation in (hub_work_status, hub_work_jobs, hub_work_get, hub_work_submit,
                          hub_work_claim, hub_work_renew, hub_work_complete):
            mcp.add_tool(operation)

    return mcp
