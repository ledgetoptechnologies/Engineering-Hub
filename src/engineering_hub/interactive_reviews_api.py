"""Typed declarative review APIs; current project grants remain in repository."""
import json
from typing import Annotated, Literal

from fastapi import Path, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .openapi_body import request_body
from .repository import HubError
from .work_queue_api import _authorization


Key = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$')]
Identity = Annotated[str, Path(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')]


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ReviewChoice(Closed):
    id: Key
    label: str = Field(min_length=1, max_length=100)


class ReviewItem(Closed):
    id: Key
    title: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=4096)
    choices: list[ReviewChoice] | None = Field(default=None, min_length=2, max_length=20)
    asset_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')


class ReviewCreate(Closed):
    project: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,63}$')
    component_id: str = Field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
    component_revision: int = Field(ge=1)
    topic: str = Field(min_length=1, max_length=200)
    dataset_digest: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')
    schema_name: Literal['choice-text.v1'] = Field(default='choice-text.v1', alias='schema')
    title: str = Field(min_length=1, max_length=100)
    items: list[ReviewItem] = Field(min_length=1, max_length=1000)
    client_request_id: str = Field(min_length=1, max_length=128)


class ReviewAnswer(Closed):
    item_id: Key
    value: str = Field(min_length=1, max_length=2000)


class ReviewAnswers(Closed):
    expected_revision: int = Field(ge=1)
    client_request_id: str = Field(min_length=1, max_length=128)
    answers: list[ReviewAnswer] = Field(max_length=100)
    complete: bool = False


def _object(pairs):
    value = {}
    for key, entry in pairs:
        if key in value:
            raise HubError('invalid_review_json')
        value[key] = entry
    return value


async def _payload(request, model, maximum):
    lengths = request.headers.getlist('content-length')
    if len(lengths) > 1 or lengths and (not lengths[0].isdigit() or int(lengths[0]) > maximum):
        raise HubError('review_request_too_large', 413)
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
        raise HubError('review_json_required', 415)
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > maximum:
            raise HubError('review_request_too_large', 413)
        data.extend(chunk)
    try:
        return model.model_validate(json.loads(data, object_pairs_hook=_object))
    except ValidationError as error:
        raise RequestValidationError([{k: v for k, v in entry.items() if k not in ('input', 'ctx')}
                                      for entry in error.errors()]) from None
    except (ValueError, UnicodeError, RecursionError):
        raise HubError('invalid_review_json') from None


def attach_interactive_review_routes(app, repository, actor, human, *, human_admission=None):
    def identity(request, *, mutation=False):
        _authorization(request)
        return human(request, mutation=True) if mutation else actor(request)

    @app.get('/api/hub/v1/interactive-reviews')
    def list_reviews(request: Request, project: str, component_id: str | None = None,
                     state: Literal['open', 'completed'] | None = None, after: str | None = None,
                     limit: int = Query(default=100, ge=1, le=100)):
        return repository.list_reviews(identity(request), project, component_id=component_id,
                                       state=state, after=after, limit=limit)

    @app.get('/api/hub/v1/interactive-reviews/{review_id}')
    def get_review(review_id: Identity, request: Request):
        return repository.get_review(identity(request), review_id)

    @app.get('/api/hub/v1/interactive-reviews/{review_id}/state')
    def get_state(review_id: Identity, request: Request):
        return repository.get_state(identity(request), review_id)

    @app.post('/api/hub/v1/interactive-reviews', status_code=201, openapi_extra=request_body(ReviewCreate))
    async def create(request: Request):
        _authorization(request)
        principal = actor(request, mutation=True)
        payload = await _payload(request, ReviewCreate, 1024 * 1024 + 16384)
        if actor(request, mutation=True) != principal:
            raise HubError('review_identity_changed', 403)
        return repository.create_review(principal, **payload.model_dump(by_alias=True, exclude_none=True))

    @app.post('/api/hub/v1/interactive-reviews/{review_id}/answers', openapi_extra=request_body(ReviewAnswers))
    async def answer(review_id: Identity, request: Request):
        principal = identity(request, mutation=True)
        payload = await _payload(request, ReviewAnswers, 256 * 1024)
        if identity(request, mutation=True) != principal:
            raise HubError('review_identity_changed', 403)
        if human_admission is None:
            raise HubError('interactive_review_admission_unavailable', 503)
        guard = human_admission(request, principal)
        if not callable(guard):
            raise HubError('interactive_review_admission_unavailable', 503)
        return repository.answer_review(principal, review_id, admission_guard=guard, **payload.model_dump())


def register_interactive_review_tools(mcp, *, repository, identity, invoke):
    from mcp.server.fastmcp import Context

    @mcp.tool()
    def hub_interactive_review_create(payload: ReviewCreate, ctx: Context) -> dict:
        """Create a bounded choice/text review under explicit project grants; no uploaded code or URLs."""
        principal = identity(ctx)
        return invoke(lambda: repository.create_review(principal, **payload.model_dump(by_alias=True, exclude_none=True)))

    @mcp.tool()
    def hub_interactive_review_get(review_id: str, ctx: Context) -> dict:
        """Read an authorized current review, including version/dataset attribution and answers."""
        return invoke(lambda: repository.get_review(identity(ctx), review_id))

    @mcp.tool()
    def hub_interactive_review_list(project: str, ctx: Context, component_id: str | None = None,
                                    state: str | None = None, after: str | None = None, limit: int = 100) -> dict:
        """Read bounded review summaries. Humans answer through the authenticated Hub UI."""
        return invoke(lambda: repository.list_reviews(identity(ctx), project,
            component_id=component_id, state=state, after=after, limit=limit))
