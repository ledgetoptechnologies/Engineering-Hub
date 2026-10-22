"""Pure, read-only comparison of an observed runtime and a published image.

Collectors, not this module, must establish the provenance and linkage of the
reported OCI index/manifest/config digests. A tag, revision label, or desired
deployment record is not proof of what a production container is running.
There are no network requests, database writes, deployment actions or clocks
started by importing or calling this module.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MAX_OBSERVATION_LIFETIME_SECONDS = 1800
MAX_FUTURE_SKEW_SECONDS = 30
Digest = Annotated[str, Field(pattern=r'^sha256:[0-9a-f]{64}$')]
Repository = Annotated[str, Field(
    min_length=12, max_length=255,
    pattern=r'^ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*/[a-z0-9]+(?:[._/-][a-z0-9]+)*$')]
Reference = Annotated[str, Field(
    min_length=1, max_length=128,
    pattern=r'^(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|sha256:[0-9a-f]{64})$')]
Identifier = Annotated[str, Field(
    min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')]
PlatformPart = Annotated[str, Field(
    min_length=1, max_length=32, pattern=r'^[a-z0-9][a-z0-9_.-]{0,31}$')]
DigestKind = Literal['config', 'manifest', 'index']
State = Literal['up_to_date', 'update_available', 'unknown']
Reason = Literal[
    'same_image', 'different_image', 'running_not_observed', 'published_not_observed',
    'observation_source_mismatch', 'reference_mismatch', 'repository_mismatch',
    'platform_unverified', 'platform_mismatch', 'running_observation_in_future',
    'published_observation_in_future', 'running_observation_expired',
    'published_observation_expired', 'identity_unverified', 'identity_conflict',
]


class _Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class ImagePlatform(_Model):
    os: PlatformPart
    architecture: PlatformPart
    variant: PlatformPart | None = None


class ImageIdentity(_Model):
    repository: Repository
    platform: ImagePlatform | None = None
    index_digest: Digest | None = None
    manifest_digest: Digest | None = None
    config_digest: Digest | None = None


class ImageObservation(_Model):
    identity: ImageIdentity
    source: Literal['runtime', 'registry']
    reference: Reference | None = None
    observed_at: datetime
    valid_until: datetime

    @model_validator(mode='after')
    def bounded_interval(self):
        for value in (self.observed_at, self.valid_until):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError('Timezone-aware observation times required')
        lifetime = (self.valid_until - self.observed_at).total_seconds()
        if not 0 < lifetime <= MAX_OBSERVATION_LIFETIME_SECONDS:
            raise ValueError('Observation lifetime must be positive and bounded')
        return self


class ImageVersionTarget(_Model):
    project: Identifier
    host: Identifier
    service: Identifier
    environment: Literal['production', 'staging'] = 'production'
    repository: Repository
    reference: Reference


class ImageVersionStatus(_Model):
    project: Identifier
    host: Identifier
    service: Identifier
    environment: Literal['production', 'staging']
    repository: Repository
    reference: Reference
    state: State
    reason: Reason
    checked_at: datetime
    running_observed_at: datetime | None = None
    published_observed_at: datetime | None = None
    valid_until: datetime | None = None
    digest_kind: DigestKind | None = None
    current_digest: Digest | None = None
    available_digest: Digest | None = None


def compare_image_versions(target: ImageVersionTarget,
                           running: ImageObservation | None,
                           published: ImageObservation | None, *,
                           now: datetime) -> ImageVersionStatus:
    """Compare validated observations for one configured release track.

    ``update_available`` means the already-published configured target differs;
    it does not assert semantic-version ordering or authorize an update. The
    provider must bind each observation to this target's actual host/service.
    Compare like digest kinds, preferring the runnable config then its platform
    manifest. A changed multi-platform index alone need not change this machine's
    platform image. Contradictory linked identities fail unknown.
    """
    if type(target) is not ImageVersionTarget:
        raise TypeError('Validated image target required')
    if (not isinstance(now, datetime) or now.tzinfo is None
            or now.utcoffset() is None):
        raise ValueError('Timezone-aware comparison time required')
    if any(value is not None and type(value) is not ImageObservation
           for value in (running, published)):
        raise TypeError('Validated image observations required')
    now = now.astimezone(timezone.utc)
    base = target.model_dump()
    base.update(checked_at=now,
                running_observed_at=running.observed_at if running else None,
                published_observed_at=published.observed_at if published else None)

    def unknown(reason: Reason):
        return ImageVersionStatus(**base, state='unknown', reason=reason)

    if running is None:
        return unknown('running_not_observed')
    if published is None:
        return unknown('published_not_observed')
    if running.source != 'runtime' or published.source != 'registry':
        return unknown('observation_source_mismatch')
    if published.reference != target.reference:
        return unknown('reference_mismatch')
    if (running.identity.repository != target.repository
            or published.identity.repository != target.repository):
        return unknown('repository_mismatch')
    if (target.reference.startswith('sha256:')
            and target.reference not in (published.identity.index_digest,
                                        published.identity.manifest_digest)):
        # Registry manifest references identify an index/manifest, not a Docker
        # config ID. Do not trust a report whose requested immutable reference
        # is absent from its claimed response identity chain.
        return unknown('identity_conflict')
    for name, observation in (('running', running), ('published', published)):
        if observation.observed_at > now + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
            return unknown(f'{name}_observation_in_future')
        if observation.valid_until <= now:
            return unknown(f'{name}_observation_expired')
    current, available = running.identity, published.identity
    if current.platform is None or available.platform is None:
        return unknown('platform_unverified')
    if current.platform != available.platform:
        return unknown('platform_mismatch')

    common = {
        kind: (getattr(current, f'{kind}_digest'), getattr(available, f'{kind}_digest'))
        for kind in ('config', 'manifest', 'index')
        if (getattr(current, f'{kind}_digest') is not None
            and getattr(available, f'{kind}_digest') is not None)
    }
    # Equal parent digests cannot name unequal children on the same platform.
    for parent, children in (('index', ('manifest', 'config')), ('manifest', ('config',))):
        if parent in common and common[parent][0] == common[parent][1]:
            if any(child in common and common[child][0] != common[child][1] for child in children):
                return unknown('identity_conflict')
    if not common:
        return unknown('identity_unverified')
    kind, (current_digest, available_digest) = next(iter(common.items()))
    same = current_digest == available_digest
    if kind == 'index' and not same:
        # An index can change solely because another architecture or an
        # attestation changed. Without this machine's manifest/config identity,
        # different parent indexes do not establish an available runtime update.
        return unknown('identity_unverified')
    return ImageVersionStatus(
        **base, state='up_to_date' if same else 'update_available',
        reason='same_image' if same else 'different_image', digest_kind=kind,
        current_digest=current_digest, available_digest=available_digest,
        valid_until=min(running.valid_until, published.valid_until))
