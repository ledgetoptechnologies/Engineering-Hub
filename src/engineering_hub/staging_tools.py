"""Agent-facing staging operations: fixed profiles, project grants, no host shell."""
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from .repository import HubError
from .staging_api import (StagingSubmission, StagingKeep,
                          StagingCleanupPlan, StagingCleanupApply)


def register_staging_tools(mcp, *, controller, identity):
    def parse(model, payload):
        try:
            return model.model_validate(payload).model_dump(exclude_none=True)
        except ValidationError:
            raise ToolError('invalid_staging_request') from None

    async def call(operation):
        try:
            return await operation()
        except HubError as error:
            raise ToolError(error.code) from None
        except Exception:
            raise ToolError('staging_unavailable') from None

    def read(operation):
        try:
            return operation()
        except HubError as error:
            raise ToolError(error.code) from None
        except Exception:
            raise ToolError('staging_unavailable') from None

    @mcp.tool()
    async def hub_staging_service_status(ctx: Context) -> dict:
        """Read authorized profiles and actual worker reachability; not readiness proof."""
        actor = identity(ctx)
        return await call(lambda: controller.status(actor))

    @mcp.tool()
    async def hub_staging_build_submit(payload: dict, ctx: Context) -> dict:
        """Queue one bounded source ZIP for an operator-reviewed build/test recipe.

        A UUID request_id deduplicates the exact archive, declared source commit
        and recipe. Source digest is the ZIP SHA256. No shell, image, resource
        identity or deployment target is accepted. This cannot deploy production.
        """
        actor = identity(ctx)
        data = parse(StagingSubmission, payload)
        return await call(lambda: controller.submit(actor, data))

    @mcp.tool()
    def hub_staging_build_get(job_id: str, ctx: Context) -> dict:
        """Read an authorized canonical job and its resource identities."""
        actor = identity(ctx)
        return read(lambda: controller.get_job(actor, job_id))

    @mcp.tool()
    def hub_staging_resources(project: str, ctx: Context, kind: str | None = None,
                              state: str | None = None, after: str | None = None,
                              limit: int = 50) -> dict:
        """List retained staging resources in a project with explicit read grants."""
        actor = identity(ctx)
        return read(lambda: controller.resources.list_resources(
            actor, project, kind=kind, state=state, after=after, limit=limit))

    @mcp.tool()
    def hub_staging_resource_keep(resource_id: str, payload: dict, ctx: Context) -> dict:
        """Keep this agent's owned resource temporarily using its current revision.

        Human owner pins and all active-job/rollback protections remain enforced.
        """
        actor = identity(ctx)
        data = parse(StagingKeep, payload)
        mode = data.pop('mode')
        return read(lambda: controller.resources.set_keep(actor, resource_id, mode, **data))

    @mcp.tool()
    def hub_staging_cleanup_plan(payload: dict, ctx: Context) -> dict:
        """Preview expired owned resources and reasons to keep everything else."""
        actor = identity(ctx)
        data = parse(StagingCleanupPlan, payload)
        return read(lambda: controller.resources.cleanup_plan(actor, **data))

    @mcp.tool()
    async def hub_staging_cleanup_apply(payload: dict, ctx: Context) -> dict:
        """Apply the exact unexpired plan; unknown outcomes require reconciliation.

        Only registered owned resources can be deleted. This never performs a
        Docker prune, host-path deletion, task reassignment or held-job replay.
        """
        actor = identity(ctx)
        data = parse(StagingCleanupApply, payload)
        return await call(lambda: controller.cleanup_apply(actor, data['plan_id'], data['digest']))
