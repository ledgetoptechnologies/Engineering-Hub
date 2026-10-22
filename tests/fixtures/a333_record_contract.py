"""Frozen DTO excerpt, not a runtime recovery reader.

Source commit: a333704dcf8c14ef071d4af434928e657e5baed0
Source path: src/engineering_hub/dto.py
Source Git blob: d45fce484f6a83ac6a5a545e314954b8035e44be
Only the imports and definitions needed by TaskDTO/DecisionDTO are retained.
Definitions below are copied verbatim; no dependency on local Git history.
"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


AgentId = Literal['hermes', 'codex', 'claude']
ProjectId = Literal['engineering-hub', '3d-viewer', 'project-alpha', 'ledge-top-ops', 'treedar']
Commit = Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')]
HumanId = Annotated[str, Field(pattern=r'^human:(?:[1-9][0-9]{0,19}|cf:[0-9a-f]{64})$')]


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
    # Audited time the task entered its current status; derived, never stored.
    state_since: str | None = None


class DecisionDTO(EntityDTO):
    project: ProjectId
    title: str
    options: list[str]
    protected: bool
    status: Literal['open', 'answered']
    answer: str | None
    execution_authorization: Literal[False]
    answered_by: HumanId | None = None
