"""Human-scoped topic discovery; no inferred message membership or readiness."""
from typing import Annotated

from fastapi import Request, Response
from pydantic import Field

from .dto import ResponseDTO
from .registry_client import RegistryAccessDenied, RegistrySourceUnavailable
from .repository import HubError


class TopicCatalogItem(ResponseDTO):
    topic: Annotated[str, Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,127}$')]
    # A membership grant does not establish activity, peers or online status.
    last_activity_at: None = None
    peer_count: None = None


class TopicCatalogDTO(ResponseDTO):
    items: list[TopicCatalogItem] = Field(max_length=500)


def attach_topic_catalog_route(app, *, bridge, human):
    @app.get('/api/hub/v1/topics', response_model=TopicCatalogDTO)
    def topics(request: Request, response: Response):
        principal = human(request)
        if bridge is None:
            raise HubError('topic_catalog_unavailable', 503)
        try:
            identifiers = bridge.topics_for_human(principal)
        except RegistryAccessDenied:
            raise HubError('topic_catalog_forbidden', 403) from None
        except RegistrySourceUnavailable:
            raise HubError('topic_catalog_unavailable', 503) from None
        response.headers['Cache-Control'] = 'no-store'
        return TopicCatalogDTO(items=[TopicCatalogItem(topic=value) for value in identifiers])
