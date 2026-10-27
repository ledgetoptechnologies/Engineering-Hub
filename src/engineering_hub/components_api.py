"""Authenticated component catalog routes and tools; inert metadata only."""
from typing import Annotated, Literal

from fastapi import Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .work_queue_api import _authorization


ComponentKind = Literal['web_app', 'desktop_app', 'mobile_app', 'service', 'package', 'library', 'tool', 'other']
ComponentStatus = Literal['active', 'planned', 'paused', 'retired']
ComponentId = Annotated[str, Path(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')]


class ComponentInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    slug: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,63}$')
    name: str = Field(min_length=1, max_length=100)
    client_request_id: str = Field(min_length=1, max_length=128)
    description: str = Field(default='', max_length=4096)
    kind: ComponentKind = 'other'
    status: ComponentStatus = 'planned'
    repository_url: str | None = Field(default=None, max_length=2048)
    package_url: str | None = Field(default=None, max_length=2048)
    legacy_project_id: str | None = Field(default=None, pattern=r'^[a-z0-9][a-z0-9-]{0,63}$')


class ComponentEdit(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=4096)
    kind: ComponentKind | None = None
    status: ComponentStatus | None = None
    repository_url: str | None = Field(default=None, max_length=2048)
    package_url: str | None = Field(default=None, max_length=2048)
    legacy_project_id: str | None = Field(default=None, pattern=r'^[a-z0-9][a-z0-9-]{0,63}$')


class ComponentRevision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: int = Field(ge=1)


class ComponentCreate(ComponentInput):
    project: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,63}$')


class ComponentUpdate(ComponentEdit):
    component_id: str = Field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')


class ComponentRetirement(ComponentRevision):
    component_id: str = Field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')


def attach_component_routes(app, repository, actor, human):
    def identity(request, *, mutation=False):
        _authorization(request)
        return actor(request, mutation=mutation)

    @app.get('/api/hub/v1/projects/{project}/components')
    def list_components(project: str, request: Request, include_retired: bool = False,
                        kind: ComponentKind | None = None, status: ComponentStatus | None = None,
                        after: Annotated[str | None, Query(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')] = None,
                        limit: int = Query(default=100, ge=1, le=100)):
        identity(request)
        return repository.list_components(project, include_retired=include_retired,
                                           kind=kind, status=status, after=after, limit=limit)

    @app.get('/api/hub/v1/components/{component_id}')
    def get_component(component_id: ComponentId, request: Request):
        identity(request)
        return repository.get_component(component_id)

    @app.post('/api/hub/v1/projects/{project}/components', status_code=201)
    def create_component(project: str, payload: ComponentInput, request: Request):
        return repository.create_component(identity(request, mutation=True), project, **payload.model_dump())

    @app.patch('/api/hub/v1/components/{component_id}')
    def update_component(component_id: ComponentId, payload: ComponentEdit, request: Request):
        principal = identity(request, mutation=True)
        fields = payload.model_dump(exclude_unset=True)
        revision = fields.pop('expected_revision')
        return repository.update_component(principal, component_id, revision, **fields)

    @app.post('/api/hub/v1/components/{component_id}/retire')
    def retire_component(component_id: ComponentId, payload: ComponentRevision, request: Request):
        return repository.retire_component(identity(request, mutation=True), component_id, payload.expected_revision)


def register_component_tools(mcp, *, repository, identity, invoke):
    from mcp.server.fastmcp import Context

    @mcp.tool()
    def hub_component_list(project: str, ctx: Context, include_retired: bool = False,
                           kind: ComponentKind | None = None, status: ComponentStatus | None = None,
                           after: str | None = None, limit: int = 100) -> dict:
        """Read a project's component catalog; metadata confers no execution or access grant."""
        identity(ctx)
        return invoke(lambda: repository.list_components(project, include_retired=include_retired,
            kind=kind, status=status, after=after, limit=limit))

    @mcp.tool()
    def hub_component_get(component_id: str, ctx: Context) -> dict:
        """Read one component by its stable UUID; metadata confers no execution or access grant."""
        identity(ctx)
        return invoke(lambda: repository.get_component(component_id))

    def mutation_actor(ctx):
        principal = identity(ctx)
        # Apply the same duplicate/malformed bearer guard as the HTTP routes.
        _authorization(ctx.request_context.request)
        return principal

    @mcp.tool()
    def hub_component_create(payload: ComponentCreate, ctx: Context) -> dict:
        """Create component metadata within an active existing project; no execution or access grant.

        Actor comes from authentication. Reuse a request key only for the same
        actor and payload. This cannot create, archive or delete a project.
        """
        principal = mutation_actor(ctx)
        return invoke(lambda: repository.create_component(principal, **payload.model_dump()))

    @mcp.tool()
    def hub_component_update(payload: ComponentUpdate, ctx: Context) -> dict:
        """Edit component metadata using its current revision; status edits can restore retired rows.

        Project, slug, UUID and provenance stay immutable. No execution or access grant.
        """
        principal = mutation_actor(ctx)
        fields = payload.model_dump(exclude_unset=True)
        component_id = fields.pop('component_id')
        revision = fields.pop('expected_revision')
        return invoke(lambda: repository.update_component(principal, component_id, revision, **fields))

    @mcp.tool()
    def hub_component_retire(payload: ComponentRetirement, ctx: Context) -> dict:
        """Retire component metadata using its current revision; preserve the row, slug and history."""
        principal = mutation_actor(ctx)
        return invoke(lambda: repository.retire_component(principal, payload.component_id, payload.expected_revision))
