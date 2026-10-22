"""Same-origin, authenticated raster context. No agent HTML/file hosting."""
from fastapi import Request
from starlette.responses import Response

from .label_sets_api import decode_image


def attach_image_routes(app, *, repository, actor):
    @app.get('/api/hub/v1/tasks/{entity_id}/attachments')
    def task_images(entity_id: str, request: Request):
        return repository.list(kind='task', entity_id=entity_id, actor=actor(request))

    @app.get('/api/hub/v1/decisions/{entity_id}/attachments')
    def decision_images(entity_id: str, request: Request):
        return repository.list(kind='decision', entity_id=entity_id, actor=actor(request))

    @app.get('/api/hub/v1/attachments/{image_id}/image')
    def image(image_id: str, request: Request):
        content, media_type = repository.image(image_id=image_id, actor=actor(request))
        return Response(content, media_type=media_type, headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': 'inline', 'Content-Security-Policy': "default-src 'none'; sandbox",
        })


def register_image_tools(mcp, *, repository, identity, invoke):
    from mcp.server.fastmcp import Context

    @mcp.tool()
    def hub_entity_put_image(image_id: str, kind: str, entity_id: str, expected_revision: int,
                              content_base64: str, media_type: str, ctx: Context,
                              title: str = '') -> dict:
        """Append a bounded sanitized JPEG/PNG to an open decision or unreviewed task.

        image_id is a caller-generated UUID for safe identical retries. CAS and
        ownership/review guards apply; an image never supplies authorization.
        """
        principal = identity(ctx)
        return invoke(lambda: repository.put(image_id=image_id, kind=kind, entity_id=entity_id,
            expected_revision=expected_revision, content=decode_image(content_base64),
            media_type=media_type, title=title, actor=principal))

    @mcp.tool()
    def hub_entity_images(kind: str, entity_id: str, ctx: Context) -> dict:
        """List authenticated decision/task image metadata, without binary bytes."""
        principal = identity(ctx)
        return invoke(lambda: repository.list(kind=kind, entity_id=entity_id, actor=principal))
