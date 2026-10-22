"""Hub tools, standalone or explicitly registered on the unified bus server."""
from fastapi import HTTPException
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse
from pydantic import ValidationError
from pydantic import StrictInt
from agent_message_bus.knowledge import (KnowledgeConflict, KnowledgeError,
    KnowledgeIntegrityError, KnowledgeNotFound, KnowledgeRepository, KnowledgeProjectArchived)
from .planning_api import PlanningWrite, Scope, Kind, Status
from . import planning

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
                  work_queue=None, mcp=None, agent_status=None, image_versions=None,
                  planning_repository=None, label_sets=None, entity_images=None, project_registry=None) -> FastMCP:
    """Call streamable_http_app once, mount its auth wrapper, run session_manager."""
    if mcp is None:
        mcp = FastMCP("Engineering Hub", stateless_http=True, streamable_http_path="/", json_response=True)
    elif type(mcp) is not FastMCP or getattr(mcp, '_hub_tools_registered', False):
        raise ValueError('Fresh explicit MCP registration owner required')
    mcp._hub_tools_registered = True
    if planning_repository is not None and type(planning_repository) is not KnowledgeRepository:
        raise ValueError('Explicit shared knowledge repository required')

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

    def planning_invoke(operation):
        if planning_repository is None:
            raise ToolError('planning_unavailable')
        try:
            return operation(planning_repository)
        except KnowledgeProjectArchived:
            raise ToolError('project_archived') from None
        except KnowledgeConflict as error:
            raise ToolError('planning_version_conflict: current_version=' + str(error.current_version)) from None
        except KnowledgeNotFound:
            raise ToolError('planning_item_not_found') from None
        except KnowledgeIntegrityError:
            raise ToolError('planning_integrity_failure') from None
        except (KnowledgeError, ValidationError):
            raise ToolError('invalid_planning_item') from None
        except Exception:
            raise ToolError('planning_unavailable') from None

    @mcp.tool()
    def hub_planning_list(scope: Scope, project: str, ctx: Context,
                          topic: str | None = None, kind: Kind | None = None,
                          limit: StrictInt = 50) -> dict:
        """List advisory topic plans/ideas or the separate EH improvements backlog.

        hub_improvements requires project engineering-hub and no topic. Records
        are shared context, not task assignment, approval or execution rights.
        A truncated list requires a narrower topic/kind; it is not a full backlog.
        """
        identity(ctx)
        return planning_invoke(lambda store: planning.list_items(store, scope=scope,
            project=project, topic=topic, kind=kind, limit=limit))

    @mcp.tool()
    def hub_planning_get(scope: Scope, project: str, kind: Kind, item_id: str,
                         ctx: Context, topic: str | None = None) -> dict:
        """Read one complete advisory plan/idea, including its CAS version."""
        identity(ctx)
        return planning_invoke(lambda store: planning.get_item(store, scope=scope,
            project=project, topic=topic, kind=kind, item_id=item_id))

    @mcp.tool()
    def hub_planning_put(payload: PlanningWrite, ctx: Context) -> dict:
        """Create/update an advisory plan, feature, idea or EH improvement.

        New item expected_version=0; edits require a fresh read and reconcile on
        conflict. Stable item_id deduplicates the same record. Status is advisory:
        planned/done never approve, schedule, deploy or prove work completed.
        Agent author is authenticated. Keep credentials/private data out.
        """
        author = identity(ctx)
        return planning_invoke(lambda store: planning.put_item(store, author=author,
            **PlanningWrite.model_validate(payload).model_dump()))

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
    async def hub_overview(ctx: Context) -> dict:
        """Read shared tasks, decisions, runs and the emergency-pause state."""
        identity(ctx)
        result = {"tasks": invoke(repo.list_task_views), "decisions": invoke(repo.list_decisions),
                  "runs": invoke(repo.list_runs), "paused": invoke(repo.get_paused)}
        if autonomy_status is not None:
            result['autonomy'] = invoke(autonomy_status)
        if agent_status is not None:
            result['agents'] = await agent_status()
        if image_versions is not None:
            result['image_versions'] = invoke(image_versions)
        return result

    @mcp.tool()
    def hub_staging_task_propose(project: str, title: str, client_request_id: str, ctx: Context,
                                 details: str | None = None, links: list[dict[str, str]] | None = None) -> dict:
        """Create an unclaimed staging-only task, not approval or execution.

        Reuse a request ID only for the same payload. An independent agent must
        review an exact candidate; registered execution admission is separate.
        """
        principal = identity(ctx)
        return invoke(lambda: repo.propose_staging_task(principal, project, title, client_request_id,
                                                       details=details, links=links))

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
    def hub_decision_request(project: str, title: str, options: list[str], ctx: Context, protected: bool = False,
                              details: str | None = None, links: list[dict[str, str]] | None = None,
                              allow_text: bool = False) -> dict:
        """Request a human decision; this tool cannot answer or grant approval."""
        principal = identity(ctx)
        return invoke(lambda: repo.create_decision(principal, project, title, options, protected,
                                                  details=details, links=links, allow_text=allow_text))

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
        """Check exact-topic presence/send guidance; queue offline mail durably.

        Online is advisory, not receipt or completion. Busy may delay replies;
        unknown/offline must not imply an immediate response or trigger retries.
        """
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
            worker=WorkWorker(principal, data.instance_id), lease_seconds=data.lease_seconds,
            action_keys=None if data.action_keys is None else tuple(data.action_keys)))}

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

    # Default/off deployments expose task proposals but
    # must not advertise an unconfigured worker/admission capability.
    if work_queue is not None:
        for operation in (hub_work_status, hub_work_jobs, hub_work_get, hub_work_submit,
                          hub_work_claim, hub_work_renew, hub_work_complete):
            mcp.add_tool(operation)

    if label_sets is not None:
        from .label_sets_api import register_label_set_tools
        register_label_set_tools(mcp, repository=label_sets, identity=identity, invoke=invoke)
    if entity_images is not None:
        from .attachments_api import register_image_tools
        register_image_tools(mcp, repository=entity_images, identity=identity, invoke=invoke)
    if project_registry is not None:
        from .projects_api import register_project_tools
        register_project_tools(mcp, repository=project_registry, identity=identity, invoke=invoke)

    return mcp
