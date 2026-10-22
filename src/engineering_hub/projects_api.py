"""Human-owned project records; listing never enrolls an executor or an agent."""
from typing import Literal

from fastapi import Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .repository import PROJECTS


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    project_id: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,63}$')
    name: str = Field(min_length=1, max_length=100)
    client_request_id: str = Field(min_length=1, max_length=128)
    description: str = Field(default='', max_length=4096)
    repository_url: str | None = Field(default=None, max_length=2048)
    lead_agent: Literal['codex', 'hermes', 'claude'] | None = None
    status: Literal['on_track', 'at_risk', 'off_track', 'paused'] = 'on_track'
    parent_project: str | None = Field(default=None, max_length=64)


class ProjectEdit(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: StrictInt = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=4096)
    repository_url: str | None = Field(default=None, max_length=2048)
    lead_agent: Literal['codex', 'hermes', 'claude'] | None = None
    status: Literal['on_track', 'at_risk', 'off_track', 'paused'] | None = None
    parent_project: str | None = Field(default=None, max_length=64)


class ProjectRevision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: StrictInt = Field(ge=1)


def project_view(item):
    # Metadata is not host inventory, execution policy or observed health.
    return {**item, 'environment': 'staging' if item['id'] in PROJECTS else 'notes_only',
            'executor_enabled': False, 'health': 'unknown',
            'reason': 'inventory_and_acceptance_required' if item['id'] in PROJECTS else 'metadata_only'}


def attach_project_routes(app, *, repository, actor, human):
    @app.get('/api/hub/v1/projects/{project_id}')
    def get_project(project_id: str, request: Request):
        actor(request)
        return project_view(repository.get_project(project_id))

    @app.post('/api/hub/v1/projects', status_code=201)
    def create_project(payload: ProjectInput, request: Request):
        identity = human(request, mutation=True)
        return project_view(repository.create_project(identity, **payload.model_dump()))

    @app.patch('/api/hub/v1/projects/{project_id}')
    def update_project(project_id: str, payload: ProjectEdit, request: Request):
        identity = human(request, mutation=True)
        fields = payload.model_dump(exclude_unset=True)
        revision = fields.pop('expected_revision')
        return project_view(repository.update_project(identity, project_id, revision, **fields))

    @app.post('/api/hub/v1/projects/{project_id}/archive')
    def archive_project(project_id: str, payload: ProjectRevision, request: Request):
        identity = human(request, mutation=True)
        return project_view(repository.archive_project(identity, project_id, payload.expected_revision))


def register_project_tools(mcp, *, repository, identity, invoke):
    from mcp.server.fastmcp import Context

    @mcp.tool()
    def hub_project_list(ctx: Context, include_archived: bool = False) -> dict:
        """Read human-created projects. No project creation, deletion or enrollment."""
        identity(ctx)
        return invoke(lambda: {'items': [project_view(item) for item in repository.list_projects(include_archived)]})

    @mcp.tool()
    def hub_project_get(project_id: str, ctx: Context) -> dict:
        """Read one project's metadata; repository links may be absent."""
        identity(ctx)
        return invoke(lambda: project_view(repository.get_project(project_id)))
