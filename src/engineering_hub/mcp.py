"""Hub tools, standalone or explicitly registered on the unified bus server."""
from fastapi import HTTPException
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse
from pydantic import ValidationError
from pydantic import StrictBool, StrictInt
from agent_message_bus.knowledge import (KnowledgeConflict, KnowledgeError,
    KnowledgeIntegrityError, KnowledgeNotFound, KnowledgeRepository, KnowledgeProjectArchived)
from .planning_api import PlanningWrite, Scope, Kind, Status
from . import planning

from .repository import HubError
from .work_queue import WorkWorker
from .work_queue_api import (WorkList, WorkGet, WorkSubmission, WorkClaim,
                             WorkJobRenew, WorkJobComplete, submit_reviewed_work)
import inspect
from agent_message_bus import session_hub_context
from agent_message_bus.agent_sessions import AgentSessionError


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


# Runs that still hold their task (repository's active-run set).
OVERVIEW_ACTIVE_RUNS = frozenset({'pending', 'running', 'held', 'failed'})


def build_hub_mcp(repo, auth, *, topic_peers=None, topic_preflight=None, autonomy_status=None,
                  work_queue=None, mcp=None, agent_status=None, image_versions=None,
                  planning_repository=None, label_sets=None, entity_images=None, project_registry=None,
                  staging_controller=None, component_registry=None, interactive_reviews=None,
                  infrastructure_service=None, infrastructure_collector=None, session_bridge=None, attention_delivery=None) -> FastMCP:
    """Call streamable_http_app once, mount its auth wrapper, run session_manager."""
    if mcp is None:
        mcp = FastMCP("Engineering Hub", stateless_http=True, streamable_http_path="/", json_response=True)
    elif type(mcp) is not FastMCP or getattr(mcp, '_hub_tools_registered', False):
        raise ValueError('Fresh explicit MCP registration owner required')
    mcp._hub_tools_registered = True
    if planning_repository is not None and type(planning_repository) is not KnowledgeRepository:
        raise ValueError('Explicit shared knowledge repository required')

    registration = mcp if session_bridge is None else session_bridge.registration(mcp)

    def identity(ctx):
        request = ctx.request_context.request
        if request is None:
            raise ToolError("unauthorized")
        operation = session_hub_context.current.get()
        if operation is not None and operation.bridge is session_bridge:
            return operation.principal.actor
        try:
            return auth.agent(request)
        except HTTPException:
            raise ToolError("unauthorized") from None

    def invoke(operation, *args):
        try:
            return operation(*args)
        except (HubError, AgentSessionError) as error:
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
        except (HubError, AgentSessionError) as error:
            raise ToolError(error.code) from None
        except (KnowledgeError, ValidationError):
            raise ToolError('invalid_planning_item') from None
        except Exception:
            raise ToolError('planning_unavailable') from None

    @registration.tool()
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

    @registration.tool()
    def hub_planning_get(scope: Scope, project: str, kind: Kind, item_id: str,
                         ctx: Context, topic: str | None = None) -> dict:
        """Read one complete advisory plan/idea, including its CAS version."""
        identity(ctx)
        return planning_invoke(lambda store: planning.get_item(store, scope=scope,
            project=project, topic=topic, kind=kind, item_id=item_id))

    @registration.tool()
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
        except (HubError, AgentSessionError) as error:
            raise ToolError(error.code) from None
        except Exception:
            raise ToolError('reachability_source_unavailable') from None

    @registration.tool()
    async def hub_overview(ctx: Context, active_only: StrictBool = False, project: str | None = None) -> dict:
        """Read shared tasks, decisions, runs and the emergency-pause state.

        With no arguments, everything is returned. active_only keeps tasks that
        aren't withdrawn, open decisions and runs that still hold their task
        (pending, running, held or failed); project keeps one project's tasks,
        decisions and runs. 'omitted' counts what a filter left out, so an agent
        knows to ask again.

        Presentation only, not access control: the caller can already read every
        project, and paused, agents, autonomy and image_versions stay global.
        """
        identity(ctx)
        if type(active_only) is not bool or (project is not None and (type(project) is not str or not project)):
            raise ToolError('invalid_overview_filter')
        tasks, decisions, runs = invoke(repo.list_task_views), invoke(repo.list_decisions), invoke(repo.list_runs)
        result = {"tasks": tasks, "decisions": decisions, "runs": runs, "paused": invoke(repo.get_paused)}
        if active_only or project is not None:
            active = {"tasks": lambda item: item.get('status') != 'withdrawn',
                      "decisions": lambda item: item.get('status') == 'open',
                      "runs": lambda item: item.get('status') in OVERVIEW_ACTIVE_RUNS}
            omitted = {}
            for key, keep in active.items():
                kept = [item for item in result[key]
                        if (not active_only or keep(item)) and (project is None or item.get('project') == project)]
                omitted[key] = len(result[key]) - len(kept)
                result[key] = kept
            result['omitted'] = omitted
        if autonomy_status is not None:
            result['autonomy'] = invoke(autonomy_status)
        if agent_status is not None:
            result['agents'] = await agent_status()
        if image_versions is not None:
            result['image_versions'] = invoke(image_versions)
        return result

    @registration.tool()
    def hub_staging_task_propose(project: str, title: str, client_request_id: str, ctx: Context,
                                 details: str | None = None, links: list[dict[str, str]] | None = None) -> dict:
        """Create an unclaimed staging-only task, not approval or execution.

        Reuse a request ID only for the same payload. An independent agent must
        review an exact candidate; registered execution admission is separate.
        """
        principal = identity(ctx)
        return invoke(lambda: repo.propose_staging_task(principal, project, title, client_request_id,
                                                       details=details, links=links))

    @registration.tool()
    def hub_project_tasks(project: str, ctx: Context, limit: StrictInt = 100,
                          after_task_id: str | None = None) -> dict:
        """Read a project's current task context after a conversation change.

        This explicit read does not inject context, wake a chat, claim work,
        grant execution authority, or prove task completion. Follow next_after
        to page by task ID; concurrent changes can alter subsequent pages.
        Pause and tasks share a read snapshot, not an execution fence.
        """
        identity(ctx)
        if not 1 <= limit <= 200:
            raise ToolError('invalid_limit')
        if after_task_id is not None:
            import uuid
            try:
                if str(uuid.UUID(after_task_id)) != after_task_id:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ToolError('invalid_cursor') from None
        def read():
            tasks, paused = repo.project_task_page(project, limit, after_task_id)
            more = len(tasks) > limit
            return {'project': project, 'tasks': tasks[:limit], 'truncated': more,
                    'next_after': tasks[limit-1]['id'] if more else None,
                    'paused': paused}
        return invoke(read)

    @registration.tool()
    def hub_orchestration_request_create(generation: StrictInt, project: str, title: str,
                                         client_request_id: str, ctx: Context) -> dict:
        """Record an inert request under the currently held orchestrator role; never starts work."""
        from .dispatch import DispatchRepository
        return invoke(DispatchRepository(repo).create_request, identity(ctx), generation, project, title, client_request_id)

    @registration.tool()
    def hub_orchestration_request_get(request_id: str, ctx: Context) -> dict:
        """Read a project-scoped orchestration request; no execution or wake authority."""
        from .dispatch import DispatchRepository
        return invoke(DispatchRepository(repo).get_request, identity(ctx), request_id)

    @registration.tool()
    def hub_orchestration_request_list(project: str, ctx: Context, limit: StrictInt = 50, after: str | None = None) -> dict:
        """Find project requests. UUID order is not chronological; restart to find new requests."""
        from .dispatch import DispatchRepository
        return invoke(DispatchRepository(repo).list_requests, identity(ctx), project, limit, after)

    @registration.tool()
    def hub_dispatch_create(generation: StrictInt, request_id: str, task_id: str, role: str,
                            window_seconds: StrictInt, client_request_id: str, ctx: Context) -> dict:
        """Record a role dispatch; it does not execute, notify, or wake the role."""
        from .dispatch import DispatchRepository
        return invoke(DispatchRepository(repo).create_dispatch, identity(ctx), generation, request_id, task_id, role, window_seconds, client_request_id)

    @registration.tool()
    def hub_dispatch_get(dispatch_id: str, ctx: Context) -> dict:
        """Read an authorized coordination dispatch; done is a report, not verified completion."""
        from .dispatch import DispatchRepository
        return invoke(DispatchRepository(repo).get_dispatch, identity(ctx), dispatch_id)

    @registration.tool()
    def hub_dispatch_list(request_id: str, ctx: Context, limit: StrictInt = 50, after: str | None = None) -> dict:
        """List visible dispatches; no wake or execution state is inferred."""
        from .dispatch import DispatchRepository
        return invoke(DispatchRepository(repo).list_dispatches, identity(ctx), request_id, limit, after)

    @registration.tool()
    def hub_dispatch_decisions(dispatch_id: str, ctx: Context, limit: StrictInt = 50, after_revision: StrictInt = 0) -> dict:
        """Read prior ask decisions with current dispatch and decision-read grants."""
        from .dispatch import DispatchRepository
        return invoke(DispatchRepository(repo).decisions, identity(ctx), dispatch_id, limit, after_revision)

    @registration.tool()
    def hub_dispatch_reports(dispatch_id: str, ctx: Context, limit: StrictInt = 50, after_revision: StrictInt = 0) -> dict:
        """Read bounded immutable reports in ascending dispatch revision order."""
        from .dispatch import DispatchRepository
        return invoke(DispatchRepository(repo).reports, identity(ctx), dispatch_id, limit, after_revision)

    @registration.tool()
    def hub_role_dispatch_inbox(role: str, generation: StrictInt, project: str, ctx: Context,
                                limit: StrictInt = 50, after: str | None = None) -> dict:
        """Discover open dispatches for this exact leased role session; no execution or wake is implied."""
        from .dispatch import DispatchRepository
        return invoke(DispatchRepository(repo).role_inbox, identity(ctx), role, generation, project, limit, after)

    @registration.tool()
    def hub_dispatch_report(dispatch_id: str, expected_revision: StrictInt, generation: StrictInt,
                            state: str, note: str, client_request_id: str, ctx: Context,
                            ask_title: str | None = None, ask_options: list[str] | None = None) -> dict:
        """Report under the exact current role lease. Ask records a decision, not approval or execution."""
        from .dispatch import DispatchRepository
        return invoke(DispatchRepository(repo).report, identity(ctx), dispatch_id, expected_revision, generation,
                      state, note, client_request_id, ask_title, ask_options)

    @registration.tool()
    def hub_dispatch_checkpoint(dispatch_id: str, expected_revision: StrictInt, generation: StrictInt,
                                client_request_id: str, ctx: Context) -> dict:
        """Manually check target lease and attributed progress/report timeout; stale is advisory, no wake is sent."""
        from .dispatch import DispatchRepository
        return invoke(DispatchRepository(repo).checkpoint, identity(ctx), dispatch_id, expected_revision, generation, client_request_id)

    @registration.tool()
    def hub_orchestrator_attention_list(generation: StrictInt, project: str, ctx: Context,
                                        limit: StrictInt = 50, after: str | None = None) -> dict:
        """Read pending role-addressed attention intents; routing and wake have not occurred."""
        from .dispatch import DispatchRepository
        return invoke(DispatchRepository(repo).attention, identity(ctx), generation, project, limit, after)

    @registration.tool()
    def hub_addressed_attention_list(generation: StrictInt, project: str, ctx: Context,
                                     limit: StrictInt = 50, after: str | None = None) -> dict:
        """Read pending attention for the exact owner-admitted role session."""
        if attention_delivery is None:
            raise ToolError('attention_delivery_unavailable')
        return invoke(attention_delivery.pending, identity(ctx), project, generation, limit, after)

    @registration.tool()
    def hub_addressed_attention_ack(generation: StrictInt, project: str, intent_id: str,
                                     expected_revision: StrictInt, ctx: Context) -> dict:
        """Record this exact role session's acknowledgment; it does not prove work or wake."""
        if attention_delivery is None:
            raise ToolError('attention_delivery_unavailable')
        return invoke(attention_delivery.acknowledge, identity(ctx), project, generation, intent_id, expected_revision)

    @registration.tool()
    def hub_orchestrator_report_inspect(generation: StrictInt, project: str, intent_id: str,
            expected_intent_revision: StrictInt, report_id: str, expected_report_revision: StrictInt, ctx: Context) -> dict:
        """Inspect one scoped worker report; its contents are untrusted and do not verify completion."""
        if attention_delivery is None:
            raise ToolError('attention_delivery_unavailable')
        return invoke(attention_delivery.inspect_report, identity(ctx), project, generation, intent_id,
                      expected_intent_revision, report_id, expected_report_revision)

    @registration.tool()
    def hub_worker_dispatch_pending(role: str, generation: StrictInt, project: str, ctx: Context,
                                    limit: StrictInt = 50, after: str | None = None) -> dict:
        """Read unacknowledged dispatches bound to this exact owner-admitted role session."""
        if attention_delivery is None:
            raise ToolError('attention_delivery_unavailable')
        return invoke(attention_delivery.worker_pending, identity(ctx), project, role, generation, limit, after)

    @registration.tool()
    def hub_worker_dispatch_ack(role: str, generation: StrictInt, project: str, dispatch_id: str,
                                expected_revision: StrictInt, ctx: Context, decision_id: str | None = None,
                                decision_revision: StrictInt | None = None) -> dict:
        """Record a scoped receipt; it does not authorize or prove execution or wake."""
        if attention_delivery is None:
            raise ToolError('attention_delivery_unavailable')
        return invoke(attention_delivery.worker_acknowledge, identity(ctx), project, role, generation,
                      dispatch_id, expected_revision, decision_id, decision_revision)

    @registration.tool()
    def hub_worker_decision_inspect(role: str, generation: StrictInt, project: str, dispatch_id: str,
                                    expected_dispatch_revision: StrictInt, decision_id: str,
                                    expected_decision_revision: StrictInt, ctx: Context) -> dict:
        """Inspect one current answered decision under the exact worker route; answer text is untrusted."""
        if attention_delivery is None:
            raise ToolError('attention_delivery_unavailable')
        return invoke(attention_delivery.worker_inspect_decision, identity(ctx), project, role, generation,
                      dispatch_id, expected_dispatch_revision, decision_id, expected_decision_revision)

    @registration.tool()
    def hub_orchestrator_attention_reconcile_closed(generation: StrictInt, intent_id: str,
            expected_revision: StrictInt, expected_dispatch_revision: StrictInt,
            client_request_id: str, ctx: Context) -> dict:
        """Resolve a closed-task intent without claiming delivery, wake, or execution."""
        from .dispatch import DispatchRepository
        return invoke(DispatchRepository(repo).reconcile_closed_attention, identity(ctx), generation, intent_id,
                      expected_revision, expected_dispatch_revision, client_request_id)

    @registration.tool()
    def hub_task_progress_get(task_id: str, ctx: Context) -> dict:
        """Read separately versioned task progress; it is not completion proof."""
        from .task_progress import TaskProgress
        return invoke(TaskProgress(repo).get, identity(ctx), task_id)

    @registration.tool()
    def hub_task_progress_update(task_id: str, expected_revision: StrictInt, client_request_id: str, checklist: list[dict],
                                 status_line: str, blocked_on: str, ctx: Context,
                                 agent_eta_guess: StrictInt | None = None, role: str | None = None,
                                 generation: StrictInt | None = None) -> dict:
        """Update separate CAS progress. Percentage is derived; completion still needs independent review."""
        from .task_progress import TaskProgress
        return invoke(TaskProgress(repo).update, identity(ctx), task_id, expected_revision, client_request_id, checklist,
                      status_line, blocked_on, agent_eta_guess, role, generation)

    @registration.tool()
    def hub_role_list(project: str, ctx: Context, limit: StrictInt = 50, after: str | None = None) -> dict:
        """List bounded roles fully visible to this session's current project grants."""
        from .roles import RoleRepository
        return invoke(RoleRepository(repo).list, identity(ctx), project, limit, after)

    @registration.tool()
    def hub_role_get(role: str, ctx: Context) -> dict:
        """Read one granted coordination role; no execution authority is conferred."""
        from .roles import RoleRepository
        return invoke(RoleRepository(repo).get, identity(ctx), role)

    @registration.tool()
    def hub_role_acquire(role: str, expected_revision: StrictInt, lease_seconds: StrictInt, ctx: Context) -> dict:
        """Acquire an expired or unheld coordination role for this authenticated session."""
        from .roles import RoleRepository
        return invoke(RoleRepository(repo).acquire, identity(ctx), role, expected_revision, lease_seconds)

    @registration.tool()
    def hub_role_renew(role: str, expected_revision: StrictInt, generation: StrictInt, lease_seconds: StrictInt, ctx: Context) -> dict:
        """Renew this exact session and generation's role lease."""
        from .roles import RoleRepository
        return invoke(RoleRepository(repo).renew, identity(ctx), role, expected_revision, generation, lease_seconds)

    @registration.tool()
    def hub_role_release(role: str, expected_revision: StrictInt, generation: StrictInt,
                         expected_handoff_revision: StrictInt, handoff: str, ctx: Context) -> dict:
        """Record a handoff and release the role lease atomically."""
        from .roles import RoleRepository
        return invoke(RoleRepository(repo).release, identity(ctx), role, expected_revision, generation, expected_handoff_revision, handoff)

    @registration.tool()
    def hub_role_claim(role: str, generation: StrictInt, task_id: str, client_request_id: str, ctx: Context) -> dict:
        """Attach an inert role-owned coordination claim; never claims executable work."""
        from .roles import RoleRepository
        return invoke(RoleRepository(repo).claim, identity(ctx), role, generation, task_id, client_request_id)

    @registration.tool()
    def hub_role_claims(role: str, ctx: Context, limit: StrictInt = 50, after: str | None = None) -> dict:
        """Read a bounded page of this role's outstanding coordination claims."""
        from .roles import RoleRepository
        return invoke(RoleRepository(repo).claims, identity(ctx), role, limit, after)

    @registration.tool()
    def hub_role_claim_release(role: str, generation: StrictInt, task_id: str, expected_revision: StrictInt, reason: str, ctx: Context) -> dict:
        """Release a coordination claim without asserting that work completed."""
        from .roles import RoleRepository
        return invoke(RoleRepository(repo).release_claim, identity(ctx), role, generation, task_id, expected_revision, reason)

    @registration.tool()
    def hub_tracking_task_propose(project: str, title: str, client_request_id: str, ctx: Context,
                                  details: str | None = None, links: list[dict[str, str]] | None = None) -> dict:
        """Propose an inert project tracking record; this never authorizes or dispatches work."""
        return invoke(lambda: repo.propose_tracking_task(identity(ctx), project, title, client_request_id,
                                                         details=details, links=links))

    @registration.tool()
    def hub_tracking_task_update(task_id: str, expected_revision: StrictInt, client_request_id: str,
                                 progress: str, evidence_links: list[dict[str, str]], ctx: Context,
                                 completion_claim: StrictBool = False) -> dict:
        """Append attributed evidence; completion is an unverified claim, not approval or proof.

        Returned updates and reviews are recent windows. If history_segments shows
        older entries, use hub_tracking_task_history with task_id and kind; omit
        before for the first page, then pass next_before as before for older pages.
        """
        return invoke(lambda: repo.update_tracking_task(identity(ctx), task_id, expected_revision,
                      client_request_id, progress=progress, evidence_links=evidence_links,
                      completion_claim=completion_claim))

    @registration.tool()
    def hub_tracking_task_review(task_id: str, expected_revision: StrictInt, client_request_id: str,
                                 verdict: str, note: str, evidence_links: list[dict[str, str]], ctx: Context) -> dict:
        """Independently verify a specific claim or reopen it; never grants execution authority.

        Returned updates and reviews are recent windows. If history_segments shows
        older entries, use hub_tracking_task_history with task_id and kind; omit
        before for the first page, then pass next_before as before for older pages.
        """
        return invoke(lambda: repo.review_tracking_task(identity(ctx), task_id, expected_revision,
                      client_request_id, verdict=verdict, note=note, evidence_links=evidence_links))

    @registration.tool()
    def hub_tracking_task_compact(task_id: str, expected_revision: StrictInt, client_request_id: str, ctx: Context) -> dict:
        """Explicitly compact old tracking history, preserving it in bounded pages.

        Returned updates and reviews are recent windows. If history_segments shows
        older entries, use hub_tracking_task_history with task_id and kind; omit
        before for the first page, then pass next_before as before for older pages.
        """
        return invoke(repo.compact_tracking_history, identity(ctx), task_id, expected_revision, client_request_id)

    @registration.tool()
    def hub_tracking_task_history(task_id: str, kind: str, ctx: Context,
                                  before: StrictInt | None = None) -> dict:
        """Read one bounded page of older tracking updates or reviews.

        Pass task_id and kind (updates or reviews). Omit before for the newest
        earlier page; pass next_before as before to continue toward older entries.
        A session needs tracking.read and current project access for each page.
        """
        identity(ctx)
        return invoke(repo.tracking_history, task_id, kind, before)

    @registration.tool()
    def hub_tracking_task_list(project: str, ctx: Context, limit: StrictInt = 100,
                               after_task_id: str | None = None, unfinished: StrictBool = False) -> dict:
        """Read inert tracking records; unfinished includes open work and unverified claims.

        Each task's updates and reviews are recent windows. If history_segments
        shows older entries, use hub_tracking_task_history with task_id and kind;
        omit before for the first page, then pass next_before as before for older
        pages. A session needs tracking.read and current project access.
        """
        identity(ctx)
        if not 1 <= limit <= 200:
            raise ToolError('invalid_limit')
        if after_task_id is not None:
            import uuid
            try:
                if str(uuid.UUID(after_task_id)) != after_task_id:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ToolError('invalid_cursor') from None
        rows = invoke(repo.tracking_tasks, project, limit, after_task_id, unfinished)
        more = len(rows) > limit
        return {'project': project, 'tasks': rows[:limit], 'truncated': more,
                'next_after': rows[limit - 1]['id'] if more else None}

    @registration.tool()
    def hub_task_get(task_id: str, ctx: Context) -> dict:
        """Read an assigned Hub task."""
        identity(ctx)
        return invoke(repo.get_task, task_id)

    @registration.tool()
    def hub_task_claim(task_id: str, expected_revision: int, ctx: Context) -> dict:
        """Claim an explicitly assigned task as the authenticated agent."""
        return invoke(repo.claim_task, identity(ctx), task_id, expected_revision)

    @registration.tool()
    def hub_task_candidate(task_id: str, expected_revision: int, commit: str, ctx: Context) -> dict:
        """Record an exact candidate commit for an owned task."""
        return invoke(repo.set_candidate, identity(ctx), task_id, expected_revision, commit)

    @registration.tool()
    def hub_task_review(task_id: str, expected_revision: int, commit: str, verdict: str, ctx: Context) -> dict:
        """Review another agent's exact candidate; self-review is forbidden."""
        return invoke(repo.review_task, identity(ctx), task_id, expected_revision, commit, verdict)

    @registration.tool()
    def hub_decision_request(project: str, title: str, options: list[str], ctx: Context, protected: bool = False,
                              details: str | None = None, links: list[dict[str, str]] | None = None,
                              allow_text: bool = False, client_request_id: str | None = None) -> dict:
        """Request a human decision. Reuse client_request_id for the original creation receipt.

        Without a request ID each call creates a new decision. This grants no approval.
        """
        principal = identity(ctx)
        return invoke(lambda: repo.create_decision(principal, project, title, options, protected,
                                                  details=details, links=links, allow_text=allow_text,
                                                  client_request_id=client_request_id))

    @registration.tool()
    def hub_decision_get(decision_id: str, ctx: Context) -> dict:
        """Read one shared decision and its answer; this grants no execution authority."""
        identity(ctx)
        return invoke(repo.get_decision, decision_id)

    @registration.tool()
    def hub_entity_withdraw(kind: str, entity_id: str, expected_revision: int, reason: str, ctx: Context,
                            superseded_by: str | None = None) -> dict:
        """Withdraw a task or decision you created that is no longer needed; nothing is deleted.

        kind is 'task' or 'decision'. Only unstarted tasks (no candidate, review or run) and
        unanswered, unprotected decisions qualify. Give a short reason and, if it was
        replaced, the full id of the replacement. This never answers, approves or reviews.
        """
        result = invoke(repo.withdraw, identity(ctx), kind, entity_id, expected_revision, reason, superseded_by)
        if kind == 'decision':
            from .decision_projection import public_decision
            return public_decision(result)
        return result

    @registration.tool()
    def hub_run_list(ctx: Context) -> dict:
        """Read execution records without dispatching work."""
        identity(ctx)
        return {"items": invoke(repo.list_runs)}

    @registration.tool()
    async def hub_topic_peers(topic: str, ctx: Context) -> dict:
        """Read exact-topic peer evidence; this is not a delivery guarantee."""
        identity(ctx)
        if topic_peers is None:
            raise ToolError('reachability_source_unavailable')
        return await invoke_topic(topic_peers, ctx, topic)

    @registration.tool()
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

    def hub_work_cancel_unclaimed(payload: dict, ctx: Context) -> dict:
        """Cancel only this agent's never-claimed job; never releases a running reservation."""
        principal = identity(ctx)
        data = work_input(WorkGet, payload)
        return invoke(lambda: configured_work().cancel_unclaimed(actor=principal, job_id=data.job_id))

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
                          hub_work_cancel_unclaimed, hub_work_claim, hub_work_renew, hub_work_complete):
            mcp.add_tool(operation)

    if label_sets is not None:
        from .label_sets_api import register_label_set_tools
        register_label_set_tools(mcp, repository=label_sets, identity=identity, invoke=invoke)
    if entity_images is not None:
        from .attachments_api import register_image_tools
        register_image_tools(mcp, repository=entity_images, identity=identity, invoke=invoke)
    if project_registry is not None:
        from .projects_api import register_project_tools
        register_project_tools(registration, repository=project_registry, identity=identity, invoke=invoke)

    if component_registry is not None:
        from .components_api import register_component_tools
        register_component_tools(registration, repository=component_registry, identity=identity, invoke=invoke)

    if staging_controller is not None:
        from .staging_tools import register_staging_tools
        register_staging_tools(mcp, controller=staging_controller, identity=identity)

    if infrastructure_service is not None:
        from .infrastructure_tools import register_infrastructure_tools
        register_infrastructure_tools(mcp, service=infrastructure_service, identity=identity, invoke=invoke,
                                      collector=infrastructure_collector)
    if interactive_reviews is not None:
        from .interactive_reviews_api import register_interactive_review_tools
        register_interactive_review_tools(mcp, repository=interactive_reviews, identity=identity, invoke=invoke)
    return mcp
