"""Public REST response contracts derived from persisted Hub records.

Unknown fields fail response validation instead of silently leaking private
state or silently dropping public fields. Optional fields absent in the source
remain absent via response_model_exclude_unset on the REST routes.
"""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


AgentId = Literal['hermes', 'codex', 'claude']
ProjectId = Literal['engineering-hub', '3d-viewer', 'project-alpha', 'ledge-top-ops', 'treedar']
Commit = Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')]
Digest = Annotated[str, Field(pattern=r'^sha256:[0-9a-f]{64}$')]
ExecutorId = Annotated[str, Field(pattern=r'^executor:[a-z][a-z0-9-]{0,63}$')]
HumanId = Annotated[str, Field(pattern=r'^human:(?:[1-9][0-9]{0,19}|cf:[0-9a-f]{64})$')]
RunState = Literal['pending', 'running', 'succeeded', 'failed', 'rolled_back', 'held']
RunEventState = Literal['running', 'succeeded', 'failed', 'rolled_back', 'held']


class ResponseDTO(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class EntityDTO(ResponseDTO):
    id: str
    revision: int = Field(ge=1)
    updated_at: str


class ReviewDTO(ResponseDTO):
    actor: AgentId
    commit: Commit
    verdict: Literal['approve']


class TaskDTO(EntityDTO):
    project: ProjectId
    title: str
    status: Literal['assigned', 'claimed', 'in_review', 'verified']
    owner: AgentId | None
    candidate_commit: Commit | None
    review: ReviewDTO | None
    repair_count: int = Field(ge=0, le=3)
    scope: Literal['staging'] | None = None
    proposed_by: AgentId | None = None


class DecisionDTO(EntityDTO):
    project: ProjectId
    title: str
    options: list[str]
    protected: bool
    status: Literal['open', 'answered']
    answer: str | None
    execution_authorization: Literal[False]
    answered_by: HumanId | None = None


class RunEventDTO(ResponseDTO):
    event: RunEventState
    actor: ExecutorId
    command_id: str
    fencing_generation: int = Field(ge=1)
    event_id: str | None = None
    reason: Literal['candidate_outcome_uncertain', 'staging_paused', 'rollback_failed',
                    'rollback_outcome_uncertain', 'rollback_smoke_failed',
                    'rollback_smoke_outcome_uncertain'] | None = None

    @model_validator(mode='after')
    def held_reason_only(self):
        if self.reason is not None and self.event != 'held':
            raise ValueError('only held events carry a hold reason')
        return self

    @field_validator('event_id')
    @classmethod
    def canonical_event_id(cls, value):
        if value is not None and str(UUID(value)) != value:
            raise ValueError('canonical event identity required')
        return value


class RunDTO(EntityDTO):
    task_id: str
    project: ProjectId
    target: str
    commit: Commit
    artifact_digest: Digest
    policy_revision: int = Field(ge=1)
    review: ReviewDTO
    status: RunState
    command_id: str | None
    fencing_generation: int = Field(ge=1)
    created_by: AgentId
    execution_actor: ExecutorId | None
    execution_manifest_digest: Digest | None
    events: list[RunEventDTO]


class RecoveryDTO(ResponseDTO):
    run: RunDTO
    recovery: Literal['recovered', 'held', 'paused']
    occurrence: str

    @field_validator('occurrence')
    @classmethod
    def canonical_occurrence(cls, value):
        if str(UUID(value)) != value:
            raise ValueError('canonical recovery occurrence required')
        return value


class ProjectDTO(ResponseDTO):
    id: ProjectId
    name: str
    environment: Literal['staging']
    executor_enabled: bool
    health: str
    reason: str


class AgentDTO(ResponseDTO):
    id: AgentId
    transport: str
    chat_readiness: str
    reason: str


class DispatchResultDTO(ResponseDTO):
    examined: int = Field(ge=0)
    dispatched: int = Field(ge=0)
    cursor: int = Field(ge=0)
    paused: bool
    more: bool


class DispatchReceiptDTO(ResponseDTO):
    sequence: int = Field(ge=1)
    entity_id: str
    revision: int = Field(ge=1)
    state: Literal['reserved', 'running', 'needs_reconciliation', 'succeeded',
                   'rolled_back', 'deferred', 'skipped']
    run_id: str | None
    reason: str | None
    attempt_head: int = Field(ge=0)
    needs_reconciliation: bool


class PilotTasksDTO(ResponseDTO):
    state: Literal['not_started', 'active', 'stopping', 'stopped']
    inflight: int = Field(ge=0, le=64)
    max_inflight: int = Field(ge=1, le=64)


class PilotLifecycleDTO(ResponseDTO):
    state: Literal['not_started', 'active', 'stopping', 'stopped']
    required_supervisor_stop_grace_seconds: int = Field(ge=1)
    configured_supervisor_stop_grace_seconds: int = Field(ge=1)
    supervisor_configuration_verified: Literal[False]
    configuration_source: Literal['operator_assertion']
    tasks: PilotTasksDTO


class QueuedPilotJobDTO(ResponseDTO):
    id: str
    state: Literal['succeeded', 'failed', 'held']


class QueuedPilotRuntimeDTO(ResponseDTO):
    state: Literal['not_started', 'native_source_unavailable', 'active', 'stopping', 'stopped', 'failed']
    running: bool
    active: bool
    last_job: QueuedPilotJobDTO | None


class AutonomyDTO(ResponseDTO):
    configured: bool
    state: Literal['disabled', 'not_started', 'native_source_unavailable', 'active', 'stopping', 'stopped', 'failed']
    running: bool
    receipts: list[DispatchReceiptDTO]
    last_result: DispatchResultDTO | None = None
    pilot_lifecycle: PilotLifecycleDTO | None = None
    queued_pilot: QueuedPilotRuntimeDTO | None = None


class CapabilitiesDTO(ResponseDTO):
    production: bool
    host_dispatch: bool
    github_merge: bool
    processing: bool
    fixture_dispatch: bool
    container_pilot_dispatch: bool
    merge_gate_strength: Literal['not_enforced']


class OverviewDTO(ResponseDTO):
    paused: bool
    projects: list[ProjectDTO]
    tasks: list[TaskDTO]
    decisions: list[DecisionDTO]
    runs: list[RunDTO]
    agents: list[AgentDTO]
    autonomy: AutonomyDTO
    capabilities: CapabilitiesDTO


class TaskListDTO(ResponseDTO):
    items: list[TaskDTO]


class DecisionListDTO(ResponseDTO):
    items: list[DecisionDTO]


class RunListDTO(ResponseDTO):
    items: list[RunDTO]


class ProjectListDTO(ResponseDTO):
    items: list[ProjectDTO]


class AgentListDTO(ResponseDTO):
    items: list[AgentDTO]


class PauseDTO(EntityDTO):
    id: Literal['pause']
    paused: bool


class EventStatusDTO(ResponseDTO):
    available: bool
    reason: str
    observer_epoch: str
    cursor: int = Field(ge=0)


class HealthDTO(ResponseDTO):
    ok: Literal[True]
    service: Literal['engineering-hub-control']
    build_commit: Commit | None
