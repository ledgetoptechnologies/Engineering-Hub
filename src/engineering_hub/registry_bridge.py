"""Read-only, request-scoped canonical registry projection, not another registry.

Original authenticated agents use their own bearer to preserve remote membership.
Human readers need an independently configured fixed-topic read-only resolver;
no shared service cache, role header, provider key or owner grant is created.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re

from .reachability import PublicReachability
import json

from .registry_client import RegistryAccessDenied, RegistrySourceUnavailable, RegistrySnapshot


@dataclass(frozen=True)
class HumanRegistryAccess:
    bearer: str = field(repr=False)
    allowed_topics: frozenset[str]

    def __post_init__(self):
        if (not isinstance(self.bearer, str) or not self.bearer
                or any(not 33 <= ord(char) <= 126 for char in self.bearer)
                or type(self.allowed_topics) is not frozenset
                or any(not isinstance(topic, str) or re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,127}', topic) is None
                       for topic in self.allowed_topics)):
            raise ValueError('invalid_registry_reader')


def project_registry_snapshot(snapshot, *, topic, now=None, stream_configured=False):
    """Project validated public evidence without fabricating private subjects.

    Evaluates expiry on every explicit read. Clock/freshness checks never renew
    the source timestamp or extend a deadline. Missing peer rows stay unknown.
    """
    now = now or datetime.now(timezone.utc)
    if type(snapshot) is not RegistrySnapshot:
        raise RegistrySourceUnavailable()
    snapshot = RegistrySnapshot.model_validate_json(json.dumps(snapshot.model_dump(mode='json')))
    if now.utcoffset() is None or snapshot.topic != topic:
        raise RegistrySourceUnavailable()
    peers = {item.agent: item for item in snapshot.peers}
    items = []
    for agent in ('codex', 'hermes', 'claude'):
        peer = peers.get(agent)
        if peer is None:
            item = PublicReachability(topic=topic, agent=agent, status='unknown_stale',
                                      reason='no_observer_evidence')
        else:
            values = dict(topic=topic, agent=agent, status=peer.status, reason=peer.reason,
                          observed_at=peer.last_evidence_at, last_evidence_at=peer.last_evidence_at,
                          valid_until=peer.valid_until, link_state=peer.link_state,
                          transport_state=peer.transport_state, availability_state=peer.availability_state,
                          evidence_level=peer.evidence_level, binding_revision=peer.binding_revision,
                          attachment_revision=peer.attachment_revision,
                          snapshot_revision=snapshot.snapshot_revision)
            if snapshot.as_of > now or (peer.last_evidence_at is not None and peer.last_evidence_at > now):
                values.update(status='unknown_stale', reason='evidence_in_future', valid_until=None,
                              transport_state='unknown', availability_state='unknown', evidence_level='unverified')
            elif peer.valid_until is not None and peer.valid_until <= now:
                values.update(status='unknown_stale', reason='evidence_expired', valid_until=None,
                              transport_state='unknown', availability_state='unknown', evidence_level='observed')
            item = PublicReachability(**values)
        items.append(item.model_dump(mode='json'))
    return dict(topic=topic, items=items, advisory=True, delivery_guaranteed=False,
                snapshot_revision=snapshot.snapshot_revision, registry_epoch=snapshot.registry_epoch,
                event_source='canonical_registry' if stream_configured else 'none')


class CanonicalRegistryBridge:
    """Explicit injected clients only; no environment activation or background I/O."""

    def __init__(self, client, *, stream=None, human_reader=None):
        if stream is not None and client.source_identity != stream.source_identity:
            raise ValueError('registry_source_identity_mismatch')
        self.client, self.stream, self.human_reader = client, stream, human_reader

    def access(self, principal, topic, agent_bearer=None):
        if principal in {'codex', 'hermes', 'claude'}:
            if not isinstance(agent_bearer, str) or not agent_bearer:
                raise RegistryAccessDenied(403)
            return agent_bearer
        if isinstance(principal, str) and re.fullmatch(r'human:[0-9]+', principal):
            if self.human_reader is None:
                raise RegistrySourceUnavailable()
            access = self.human_reader(principal, topic)
            if type(access) is not HumanRegistryAccess or topic not in access.allowed_topics:
                raise RegistryAccessDenied(403)
            return access.bearer
        raise RegistryAccessDenied(403)

    async def snapshot(self, principal, topic, *, agent_bearer=None):
        bearer = self.access(principal, topic, agent_bearer)
        snapshot = await self.client.fetch_snapshot(topic, bearer)
        return project_registry_snapshot(snapshot, topic=topic, stream_configured=self.stream is not None)

    async def events(self, principal, topic, *, agent_bearer=None, after=None, epoch=None, check_access=None):
        bearer = self.access(principal, topic, agent_bearer)
        if self.stream is None:
            raise RegistrySourceUnavailable()
        options = dict(after=after, epoch=epoch)
        if check_access is not None:
            options['check_access'] = check_access
        events = self.stream.events(topic, bearer, **options)
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()
