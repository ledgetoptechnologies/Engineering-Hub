"""Explicit read-only REST projection of independently approved legacy history.

No configuration discovery, import, mutation or activation. The composition
owns the reader and its shutdown. Actor is the existing trusted server auth
function; no principal/recipient selector is accepted from query or body.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import re
from typing import Annotated, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from .archive_messages import ArchiveMessageReader, ArchiveReadError


_UUID = r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
ArchiveId = Annotated[str, Field(min_length=36, max_length=36, pattern=_UUID)]
Agent = Literal['hermes', 'codex', 'claude']


class ArchiveDTO(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    archive_inert: Literal[True]

    @field_validator('archive_inert', mode='before')
    @classmethod
    def explicit_inert_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError('explicit inert boolean required')
        return value


class ArchiveMessageDTO(ArchiveDTO):
    id: ArchiveId
    sender: Agent
    recipient: Agent
    message_type: str = Field(min_length=1, max_length=128)
    priority: Literal['low', 'normal', 'high', 'urgent']
    body: JsonValue
    status: Literal['pending', 'claimed', 'acknowledged', 'rejected']
    correlation_id: str | None = Field(min_length=1, max_length=128)
    in_reply_to: ArchiveId | None
    created_at: str = Field(min_length=1, max_length=64)

    @field_validator('message_type', 'correlation_id', 'created_at')
    @classmethod
    def safe_metadata(cls, value):
        if value is not None:
            if any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError('invalid metadata')
            value.encode('utf-8', errors='strict')
        return value

    @field_validator('created_at')
    @classmethod
    def aware_timestamp(cls, value):
        if datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('timezone required')
        return value


class ArchivePageDTO(ArchiveDTO):
    items: list[ArchiveMessageDTO] = Field(max_length=50)
    next_after_id: ArchiveId | None


class ArchiveQuery(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    limit: int = Field(default=50, ge=1, le=50)
    after_id: ArchiveId | None = None


def _error(status, code):
    raise HTTPException(status, detail={'code': code}) from None


async def _closed_request(request, allowed):
    if any(key not in allowed or len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        _error(422, 'invalid_archive_input')
    # Read at most the first ASGI body chunk, never accumulate a supplied body.
    # GET with a body is not an alternative actor/SQL/import channel.
    stream = request.stream()
    try:
        chunk = await asyncio.wait_for(anext(stream), timeout=1)
        if chunk:
            _error(422, 'invalid_archive_input')
    except (TimeoutError, StopAsyncIteration):
        _error(422, 'invalid_archive_input')
    finally:
        await stream.aclose()


def attach_archive_routes(app, *, reader, actor):
    """Register two authenticated GET routes; caller retains reader ownership."""
    if (reader is not None and type(reader) is not ArchiveMessageReader) or not callable(actor):
        raise ValueError('Explicit approved archive reader and server auth required')

    def principal(request):
        values = request.headers.getlist('authorization')
        if len(values) > 1 or (values and re.fullmatch(r'(?i:Bearer) [^\s,]+', values[0]) is None):
            _error(401, 'unauthorized')
        return actor(request)

    def configured():
        if reader is None:
            _error(503, 'archive_unavailable')
        return reader

    @app.get('/api/hub/v1/archive/messages', response_model=ArchivePageDTO)
    async def messages(request: Request):
        identity = principal(request)
        await _closed_request(request, {'limit', 'after_id'})
        try:
            values = dict(request.query_params)
            if 'limit' in values:
                if re.fullmatch(r'[1-9][0-9]?', values['limit']) is None:
                    _error(422, 'invalid_archive_input')
                values['limit'] = int(values['limit'])
            query = ArchiveQuery.model_validate(values)
        except ValidationError:
            _error(422, 'invalid_archive_input')
        active = configured()
        try:
            result = await run_in_threadpool(active.list_messages, identity, limit=query.limit, after_id=query.after_id)
            return ArchivePageDTO.model_validate(result)
        except (ArchiveReadError, ValidationError):
            _error(503, 'archive_unavailable')

    @app.get('/api/hub/v1/archive/messages/{message_id}', response_model=ArchiveMessageDTO)
    async def message(message_id: str, request: Request):
        identity = principal(request)
        await _closed_request(request, set())
        if len(message_id) != 36 or re.fullmatch(_UUID, message_id) is None:
            _error(422, 'invalid_archive_input')
        active = configured()
        try:
            result = await run_in_threadpool(active.get_message, identity, message_id)
            if result is None:
                _error(404, 'archive_message_not_found')
            return ArchiveMessageDTO.model_validate(result)
        except (ArchiveReadError, ValidationError):
            _error(503, 'archive_unavailable')
