"""Agent-initiated staging tasks; creating a record does not dispatch work."""
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from .dto import TaskDTO


class StagingTaskProposal(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    project: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    client_request_id: str = Field(min_length=1, max_length=128)


def attach_staging_task_routes(app, *, repo, agent):
    @app.post('/api/hub/v1/staging/tasks', status_code=201, response_model=TaskDTO,
              response_model_exclude_unset=True, tags=['staging-tasks'],
              summary='Propose an unclaimed staging-only task as an authenticated agent',
              description='Persists scope and proposer; does not approve, execute, enroll a worker or authorize production.')
    def propose(payload: StagingTaskProposal, request: Request):
        return repo.propose_staging_task(agent(request), payload.project,
                                        payload.title, payload.client_request_id)
