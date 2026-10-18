"""Authenticated admission/lease API; no execution or deployment authority.

The operator supplies the queue/action registry explicitly. A completed work
record is a worker report, not deployment evidence, approval or promotion.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .repository import AGENTS, HubError
from .work_queue import WorkTarget, WorkWorker


Key = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$')]
JobID = Annotated[str, Path(min_length=36, max_length=36, pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')]


class WorkInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class WorkTargetInput(WorkInput):
    task_id: str = Field(min_length=36, max_length=36)
    source_commit: str = Field(pattern=r'^[a-f0-9]{40}$')
    artifact_digest: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')

    @field_validator('task_id')
    @classmethod
    def canonical_task(cls, value):
        if str(UUID(value)) != value:
            raise ValueError('canonical UUID required')
        return value


class WorkSubmission(WorkInput):
    request_id: Key
    action_key: Key
    environment: Literal['production', 'staging']
    target: WorkTargetInput
    approval_id: Key | None = None


class WorkInstance(WorkInput):
    instance_id: str = Field(min_length=36, max_length=36)

    @field_validator('instance_id')
    @classmethod
    def canonical_instance(cls, value):
        if str(UUID(value)) != value:
            raise ValueError('canonical UUID required')
        return value


class WorkClaim(WorkInstance):
    lease_seconds: int = Field(default=60, ge=1, le=600)


class WorkRenew(WorkClaim):
    fence: int = Field(ge=1, le=2**63 - 1)


class WorkComplete(WorkInstance):
    fence: int = Field(ge=1, le=2**63 - 1)
    outcome: Literal['succeeded', 'failed', 'held']
    evidence_id: Key | None = None


class WorkPause(WorkInput):
    paused: bool


def _authorization(request):
    # Never let duplicate/malformed bearer input silently select a cookie or a
    # different identity. The configured authentication callable still checks
    # the credential and real source IP; forwarded identity is not consulted.
    values = request.headers.getlist('authorization')
    if len(values) > 1 or (values and re.fullmatch(r'(?i:Bearer) [^\s,]+', values[0]) is None):
        raise HTTPException(401, detail={'code': 'invalid_work_authorization'})


def attach_work_queue_routes(app, *, repo, queue, actor, agent, human):
    """Attach explicit queue operations using the app's existing auth functions.

    Callers must also install the application's HubError/validation handlers.
    Neither this module nor the claim route creates an executor or a worker.
    """
    def configured():
        if queue is None:
            raise HTTPException(423, detail={'code': 'work_queue_not_configured'})
        return queue

    def identity(request, authenticate, *, mutation=False):
        _authorization(request)
        return authenticate(request, mutation=True) if mutation else authenticate(request)

    @app.get('/api/hub/v1/work/status')
    def status(request: Request, limit: int = Query(default=50, ge=1, le=100)):
        identity(request, actor)
        return configured().status(limit=limit)

    @app.get('/api/hub/v1/work/jobs')
    def jobs(request: Request, limit: int = Query(default=50, ge=1, le=100)):
        identity(request, actor)
        return {'items': configured().status(limit=limit)['jobs']}

    @app.post('/api/hub/v1/work/jobs', status_code=201)
    def submit(payload: WorkSubmission, request: Request):
        principal = identity(request, agent)
        work = configured()
        task = repo.get_task(payload.target.task_id)
        review = task.get('review')
        if task.get('owner') != principal:
            raise HubError('forbidden', 403)
        if work.action_project(payload.action_key) != task.get('project'):
            raise HubError('work_project_mismatch', 403)
        if (task.get('status') != 'verified' or task.get('candidate_commit') != payload.target.source_commit
                or type(review) is not dict or review.get('verdict') != 'approve'
                or review.get('actor') not in AGENTS or review.get('actor') == principal
                or review.get('commit') != payload.target.source_commit):
            raise HubError('review_required', 409)
        return work.submit(actor=principal, request_id=payload.request_id,
                           action_key=payload.action_key, environment=payload.environment,
                           target=WorkTarget(**payload.target.model_dump()), approval_id=payload.approval_id)

    @app.post('/api/hub/v1/work/claim')
    def claim(payload: WorkClaim, request: Request):
        principal = identity(request, agent)
        return {'job': configured().claim(worker=WorkWorker(principal, payload.instance_id),
                                          lease_seconds=payload.lease_seconds)}

    @app.post('/api/hub/v1/work/jobs/{job_id}/renew')
    def renew(job_id: JobID, payload: WorkRenew, request: Request):
        principal = identity(request, agent)
        return configured().renew(job_id=job_id, worker=WorkWorker(principal, payload.instance_id),
                                  fence=payload.fence, lease_seconds=payload.lease_seconds)

    @app.post('/api/hub/v1/work/jobs/{job_id}/complete')
    def complete(job_id: JobID, payload: WorkComplete, request: Request):
        principal = identity(request, agent)
        return configured().complete(job_id=job_id, worker=WorkWorker(principal, payload.instance_id),
                                     fence=payload.fence, outcome=payload.outcome, evidence_id=payload.evidence_id)

    @app.post('/api/hub/v1/work/pause')
    def pause(payload: WorkPause, request: Request):
        identity(request, human, mutation=True)
        work = configured()
        work.set_paused(payload.paused)
        return {'paused': payload.paused}
