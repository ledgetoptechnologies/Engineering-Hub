"""Authenticated advisory planning routes over the existing shared store."""
from typing import Literal

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from agent_message_bus.knowledge import (KnowledgeRepository, KnowledgeConflict,
    KnowledgeError, KnowledgeIntegrityError, KnowledgeNotFound, KnowledgeProjectArchived)
from .planning import get_item, list_items, put_item


Scope = Literal['topic', 'hub_improvements']
Kind = Literal['plan', 'feature', 'idea', 'improvement']
Status = Literal['proposed', 'planned', 'in_progress', 'done', 'declined']


class PlanningWrite(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    scope: Scope
    project: str = Field(min_length=1, max_length=64)
    topic: str | None = Field(default=None, max_length=128)
    kind: Kind
    item_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=65536)
    status: Status = 'proposed'
    expected_version: StrictInt = Field(ge=0)


def attach_planning_routes(app, *, repository, actor):
    if repository is not None and type(repository) is not KnowledgeRepository:
        raise ValueError('Explicit shared knowledge repository required')

    def invoke(operation):
        if repository is None:
            raise HTTPException(503, detail={'code': 'planning_unavailable'})
        try:
            return operation(repository)
        except KnowledgeProjectArchived:
            raise HTTPException(409, detail={'code': 'project_archived'}) from None
        except KnowledgeConflict as error:
            raise HTTPException(409, detail={'code': 'planning_version_conflict',
                                             'current_version': error.current_version}) from None
        except KnowledgeNotFound:
            raise HTTPException(404, detail={'code': 'planning_item_not_found'}) from None
        except KnowledgeIntegrityError:
            raise HTTPException(503, detail={'code': 'planning_integrity_failure'}) from None
        except KnowledgeError:
            raise HTTPException(422, detail={'code': 'invalid_planning_item'}) from None
        except Exception:
            raise HTTPException(503, detail={'code': 'planning_unavailable'}) from None

    @app.get('/api/hub/v1/planning')
    def planning_list(request: Request, scope: Scope, project: str = Query(min_length=1, max_length=64),
                      topic: str | None = Query(default=None, max_length=128), kind: Kind | None = None,
                      limit: int = Query(default=50, ge=1, le=99)):
        actor(request)
        return invoke(lambda repo: list_items(repo, scope=scope, project=project,
                                             topic=topic, kind=kind, limit=limit))

    @app.get('/api/hub/v1/planning/document')
    def planning_get(request: Request, scope: Scope, kind: Kind,
                     project: str = Query(min_length=1, max_length=64),
                     item_id: str = Query(min_length=1, max_length=64),
                     topic: str | None = Query(default=None, max_length=128)):
        actor(request)
        return invoke(lambda repo: get_item(repo, scope=scope, project=project,
                                            topic=topic, kind=kind, item_id=item_id))

    @app.post('/api/hub/v1/planning')
    def planning_put(payload: PlanningWrite, request: Request):
        identity = actor(request, mutation=True)
        return invoke(lambda repo: put_item(repo, author=identity, **payload.model_dump()))
