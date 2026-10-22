"""Authenticated, data-only label sets; agents never supply human answers."""
import base64
import binascii

from fastapi import Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.responses import Response

from .label_sets import MAX_IMAGE_BYTES
from .repository import HubError


class LabelAnswer(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    choice: str | None = Field(max_length=128)
    expected_revision: StrictInt = Field(ge=0)


def decode_image(value):
    # Bound encoded input before allocating its decoded copy. Never accept
    # data URLs, whitespace, implicit padding or non-canonical encodings.
    if type(value) is not str or not 0 < len(value) <= 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise HubError('invalid_label_image', 422)
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise HubError('invalid_label_image', 422) from None
    if len(result) > MAX_IMAGE_BYTES or base64.b64encode(result).decode('ascii') != value:
        raise HubError('invalid_label_image', 422)
    return result


def attach_label_set_routes(app, *, repository, actor, human):
    @app.get('/api/hub/v1/label-sets')
    def list_sets(request: Request, project: str = Query(min_length=1, max_length=64)):
        actor(request)
        return repository.list(project)

    @app.get('/api/hub/v1/label-sets/{set_id}')
    def read_set(set_id: str, request: Request):
        actor(request)
        return repository.get(set_id)

    @app.get('/api/hub/v1/label-sets/{set_id}/results')
    def export_results(set_id: str, request: Request):
        actor(request)
        return repository.results(set_id)

    @app.get('/api/hub/v1/label-sets/{set_id}/items/{item_id}/image')
    def read_image(set_id: str, item_id: str, request: Request):
        actor(request)
        content, media_type = repository.image(set_id, item_id)
        return Response(content, media_type=media_type, headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': 'inline', 'Content-Security-Policy': "default-src 'none'; sandbox",
        })

    @app.put('/api/hub/v1/label-sets/{set_id}/items/{item_id}/label')
    def label_item(set_id: str, item_id: str, payload: LabelAnswer, request: Request):
        identity = human(request, mutation=True)
        return repository.label(set_id, item_id, choice=payload.choice,
                                expected_revision=payload.expected_revision, actor=identity)


def register_label_set_tools(mcp, *, repository, identity, invoke):
    from mcp.server.fastmcp import Context

    @mcp.tool()
    def hub_label_set_create(set_id: str, project: str, title: str,
                             choices: list[dict], items: list[dict], ctx: Context,
                             instructions: str = '') -> dict:
        """Create a bounded data-only labeling set; same id/data is idempotent.

        Agents upload raster images separately. Only authenticated humans label;
        this record supplies no approval or execution capability.
        """
        principal = identity(ctx)
        return invoke(lambda: repository.create(set_id=set_id, project=project, title=title,
            choices=choices, items=items, instructions=instructions, actor=principal))

    @mcp.tool()
    def hub_label_set_put_image(set_id: str, item_id: str, content_base64: str,
                                media_type: str, ctx: Context) -> dict:
        """Upload one JPEG/PNG up to256KiB; verified and stripped of metadata.

        Immutable per item; identical retry is accepted. No paths, executable
        markup or image URLs are accepted. Per-set/global quotas apply.
        """
        principal = identity(ctx)
        return invoke(lambda: repository.upload_image(set_id, item_id,
            content=decode_image(content_base64), content_type=media_type, actor=principal))

    @mcp.tool()
    def hub_label_set_results(set_id: str, ctx: Context) -> dict:
        """Read human labels with attribution and revisions; never changes them."""
        identity(ctx)
        return invoke(lambda: repository.results(set_id))

    @mcp.tool()
    def hub_label_set_list(project: str, ctx: Context) -> dict:
        """List labeling sets for one project without any image bytes."""
        identity(ctx)
        return invoke(lambda: repository.list(project))
