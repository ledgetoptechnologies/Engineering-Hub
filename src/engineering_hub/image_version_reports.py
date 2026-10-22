"""Advisory image comparisons from an existing, attributed knowledge report.

This reader opens no network connection and grants no deploy authority. Native
collectors use the existing authenticated knowledge CAS API to publish metadata;
their observations are not independent host attestations.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_message_bus.knowledge import KnowledgeNotFound
from .image_versions import (
    ImageObservation, ImageVersionStatus, ImageVersionTarget, compare_image_versions,
)

REPORT_PROJECT = 'engineering-hub'
REPORT_PATH = 'observations/image-versions-v1.json'
MAX_REPORT_BYTES = 64 * 1024


class ImageReportItem(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    target: ImageVersionTarget
    running: ImageObservation | None = None
    published: ImageObservation | None = None


class ImageVersionReport(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    schema_version: Literal[1]
    items: list[ImageReportItem] = Field(min_length=1, max_length=32)

    @model_validator(mode='after')
    def unique_targets(self):
        keys = [(row.target.project, row.target.host, row.target.service,
                 row.target.environment) for row in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError('Unique image targets required')
        return self


class ImageVersionsSummary(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    configured: bool
    source: Literal['agent_observation_report'] = 'agent_observation_report'
    reason: Literal['available', 'not_configured', 'invalid_report', 'source_unavailable']
    reported_by: str | None = Field(default=None, max_length=128)
    report_updated_at: str | None = Field(default=None, max_length=64)
    advisory_only: Literal[True] = True
    items: list[ImageVersionStatus] = Field(default_factory=list, max_length=32)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate report field')
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError('Nonfinite report value')


def image_versions_for(repository, *, now: datetime | None = None) -> dict:
    """Recompute age on every read; never re-date a stale or missing observation."""
    def summary(reason, configured=False, **kwargs):
        return ImageVersionsSummary(reason=reason, configured=configured,
                                    **kwargs).model_dump()

    if repository is None:
        return summary('not_configured')
    try:
        entry = repository.get(REPORT_PROJECT, REPORT_PATH)
    except KnowledgeNotFound:
        return summary('not_configured')
    except Exception:
        return summary('source_unavailable', configured=True)
    try:
        raw = entry.content.encode('utf-8')
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError('Report too large')
        # Pydantic JSON mode parses aware datetime strings under strict models;
        # reject duplicate keys and non-finite literals before that parse.
        document = json.loads(raw, object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
        if type(document) is not dict or type(document.get('schema_version')) is not int:
            raise ValueError('Explicit report schema required')
        report = ImageVersionReport.model_validate_json(raw)
        checked = now if now is not None else datetime.now(timezone.utc)
        items = [compare_image_versions(item.target, item.running, item.published,
                                        now=checked) for item in report.items]
        # Attribution is from the authenticated knowledge record, not a claimed
        # author inside its user-editable JSON content.
        return summary('available', configured=True, items=items,
                       reported_by=entry.updated_by, report_updated_at=entry.updated_at)
    except Exception:
        # No parser fragments, report content, credentials or exception text.
        return summary('invalid_report', configured=True)
