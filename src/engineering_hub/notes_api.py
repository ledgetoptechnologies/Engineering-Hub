"""Authenticated browser access to the existing MCP knowledge store.

Documents are data, never agent instructions or task/approval authority. This
module creates no database and never falls back to a second notes store.
"""
from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from agent_message_bus.knowledge import (
    KnowledgeRepository, KnowledgeConflict, KnowledgeError, KnowledgeIntegrityError,
    KnowledgeNotFound, KnowledgeProjectArchived, entry_dict,
)


class NoteWrite(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    project: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=65536)
    expected_version: StrictInt = Field(ge=0)


def attach_notes_routes(app, *, repository, actor):
    if repository is not None and type(repository) is not KnowledgeRepository:
        raise ValueError('Explicit shared knowledge repository required')

    def invoke(operation):
        if repository is None:
            raise HTTPException(503, detail={'code': 'notes_unavailable'})
        try:
            return operation(repository)
        except KnowledgeProjectArchived:
            raise HTTPException(409, detail={'code': 'project_archived'}) from None
        except KnowledgeConflict as error:
            raise HTTPException(409, detail={'code': 'notes_version_conflict',
                                             'current_version': error.current_version}) from None
        except KnowledgeNotFound:
            raise HTTPException(404, detail={'code': 'note_not_found'}) from None
        except KnowledgeIntegrityError:
            raise HTTPException(503, detail={'code': 'notes_integrity_failure'}) from None
        except KnowledgeError:
            raise HTTPException(422, detail={'code': 'invalid_note'}) from None
        except Exception:
            raise HTTPException(503, detail={'code': 'notes_unavailable'}) from None

    @app.get('/api/hub/v1/notes')
    def list_notes(request: Request, project: str = Query(min_length=1, max_length=64),
                   query: str | None = Query(default=None, min_length=1, max_length=200),
                   path_prefix: str | None = Query(default=None, min_length=1, max_length=512),
                   limit: int = Query(default=50, ge=1, le=99)):
        actor(request)
        if query is not None and path_prefix is not None:
            raise HTTPException(422, detail={'code': 'choose_note_search_or_prefix'})
        def read(repo):
            entries = (repo.search(query=query, project=project, limit=limit + 1)
                       if query is not None else
                       repo.list(project=project, path_prefix=path_prefix, limit=limit + 1))
            return {'items': [entry_dict(entry, include_content=False) for entry in entries[:limit]],
                    'truncated': len(entries) > limit}
        return invoke(read)

    @app.get('/api/hub/v1/notes/document')
    def get_note(request: Request, project: str = Query(min_length=1, max_length=64),
                 path: str = Query(min_length=1, max_length=512)):
        actor(request)
        return invoke(lambda repo: entry_dict(repo.get(project, path)))

    @app.post('/api/hub/v1/notes')
    def save_note(payload: NoteWrite, request: Request):
        identity = actor(request, mutation=True)
        return invoke(lambda repo: entry_dict(repo.put(
            project=payload.project, path=payload.path, title=payload.title,
            content=payload.content, author=identity, expected_version=payload.expected_version)))
