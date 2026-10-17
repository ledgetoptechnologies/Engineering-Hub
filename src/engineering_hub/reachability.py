"""Pure, advisory projection of exact-topic listener evidence.

These are INTERNAL observer inputs, not agent assertions or HTTP request models.
The integration must construct them only after authenticating an authoritative
connector/runtime observer. Pydantic validation is not evidence authentication.
No registry, network client, timers, session registration or delivery grant is
provided here. Evaluate on an event or an explicitly requested read; do not poll.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Agent = Literal["codex", "hermes", "claude"]
Status = Literal[
    "unlinked", "linked_unverified", "reachable", "busy_queued", "offline",
    "unknown_stale",
]


class _EvidenceModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    @field_validator("*", mode="after", check_fields=False)
    @classmethod
    def bounded_clean_strings(cls, value):
        if isinstance(value, str) and (
            not value or len(value) > 256 or value != value.strip()
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("invalid evidence label")
        if isinstance(value, datetime) and value.utcoffset() is None:
            raise ValueError("evidence time must be timezone-aware")
        return value

    @field_validator("binding_revision", "snapshot_revision", check_fields=False)
    @classmethod
    def public_revision_is_opaque(cls, value):
        if value is not None:
            try:
                if str(UUID(value)) != value:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise ValueError("public revision must be a canonical UUID") from None
        return value


class TopicBinding(_EvidenceModel):
    """Operator/control-plane binding; never inferred from message body.topic."""

    topic: str
    agent: Agent
    session_id: str = Field(repr=False)
    session_generation: int = Field(gt=0)
    connector_generation: int = Field(gt=0)
    observer_id: str = Field(repr=False)
    # Random public revision allocated by the ONE authoritative registry, never
    # a session identifier or its hash. This module creates no competing registry.
    binding_revision: str | None = None


class ObserverContext(_EvidenceModel):
    """Current trusted observer incarnation, supplied separately from its cache."""

    observer_id: str = Field(repr=False)
    epoch: str = Field(repr=False)
    now: datetime
    max_age_seconds: int = Field(default=120, gt=0, le=3600)
    snapshot_revision: str | None = None


class RouteObservation(_EvidenceModel):
    """Authenticated observer evidence, ordered by observer-local sequence.

    A bus stream proves only a connection, never chat readiness. A connector or
    runtime observer must verify the selected session and its input/queue path
    before emitting ready/busy_queued. Freshness alone is not such verification.
    """

    topic: str
    agent: Agent
    session_id: str = Field(repr=False)
    session_generation: int = Field(gt=0)
    connector_generation: int = Field(gt=0)
    observer_id: str = Field(repr=False)
    observer_epoch: str = Field(repr=False)
    sequence: int = Field(gt=0)
    observed_at: datetime
    provenance: Literal["connector_observer", "runtime_control", "bus_stream"]
    event: Literal["ready", "busy_queued", "disconnected", "offline", "stream_connected"]
    native_attachment: Literal["verified", "unverified"] = "unverified"
    runtime_acceptance: Literal["idle", "queue", "unverified"] = "unverified"

    @model_validator(mode="after")
    def provenance_supports_event(self):
        if self.provenance == "bus_stream" and self.event != "stream_connected":
            raise ValueError("bus stream does not prove session readiness")
        if self.provenance != "bus_stream" and self.event == "stream_connected":
            raise ValueError("stream evidence requires bus stream provenance")
        return self


class PublicReachability(_EvidenceModel):
    """Only this safe projection is intended for authenticated REST/MCP/UI reads."""

    topic: str
    agent: Agent
    status: Status
    reason: Literal[
        "no_exact_binding", "no_observer_evidence", "observer_unavailable",
        "observer_restarted", "binding_generation_mismatch", "evidence_expired",
        "evidence_in_future", "conflicting_evidence", "stream_only",
        "session_ready", "session_busy_queue_verified", "session_disconnected",
        "session_offline",
        "runtime_readiness_unverified",
    ]
    observed_at: datetime | None = None
    # Transition time is unknown without a complete transition log. Never turn
    # a last-seen timestamp into a claim about when this state began.
    state_since: datetime | None = None
    last_evidence_at: datetime | None = None
    # A client may locally expire green readiness at this deadline without an
    # HTTP request, observer refresh or model call. A countdown is not polling;
    # extending readiness still requires new authoritative evidence.
    valid_until: datetime | None = None
    advisory_only: Literal[True] = True
    link_state: Literal["linked", "unlinked", "unknown"] = "unknown"
    transport_state: Literal["attached", "disconnected", "unverified", "unknown"] = "unknown"
    availability_state: Literal["available", "busy", "offline", "unverified", "unknown"] = "unknown"
    evidence_source: Literal["connector_observer", "runtime_control", "bus_stream"] | None = None
    evidence_level: Literal["runtime_verified", "observed", "unverified"] = "unverified"
    snapshot_revision: str | None = None
    binding_revision: str | None = None


def project_reachability(
    *, topic: str, agent: Agent, binding: TopicBinding | None,
    observer: ObserverContext | None,
    observations: tuple[RouteObservation, ...] = (),
    binding_snapshot_complete: bool = False,
) -> PublicReachability:
    """Project a single requested topic/participant without performing any I/O.

    Current observer identity/epoch MUST come from the live observer authority,
    not the cached observations themselves. Missing authority fails closed. A
    newer observation for this topic/agent with an old session or connector
    generation invalidates older readiness rather than falling back to it.
    Different topics never establish readiness or clear this topic's disconnect.

    No output guarantees delivery: a disconnect can occur immediately afterward.
    Durable acceptance, selected-session delivery and completion are separate.
    """
    # Validate even the unlinked path without accepting coerced labels/principals.
    public = PublicReachability(topic=topic, agent=agent, status="unlinked",
                                reason="no_exact_binding", link_state="unlinked",
                                snapshot_revision=observer.snapshot_revision if observer else None)
    if type(binding_snapshot_complete) is not bool:
        raise TypeError("binding snapshot completeness requires a boolean")
    if binding is None or binding.topic != topic or binding.agent != agent:
        return (public if binding_snapshot_complete else PublicReachability(
            topic=topic, agent=agent, status="unknown_stale",
            reason="observer_unavailable"))

    def result(status: Status, reason: str, event: RouteObservation | None = None):
        verified = status in {"reachable", "busy_queued"}
        transport = ("attached" if verified else "disconnected" if status == "offline"
                     else "unverified" if status == "linked_unverified" else "unknown")
        availability = {"reachable": "available", "busy_queued": "busy",
                        "offline": "offline", "linked_unverified": "unverified"}.get(status, "unknown")
        return PublicReachability(topic=topic, agent=agent, status=status,
                                  reason=reason,
                                  observed_at=event.observed_at if event else None,
                                  last_evidence_at=event.observed_at if event else None,
                                  valid_until=(event.observed_at + timedelta(seconds=observer.max_age_seconds)
                                               if verified and event and observer else None),
                                  link_state="linked", transport_state=transport,
                                  availability_state=availability,
                                  evidence_source=event.provenance if event else None,
                                  evidence_level="runtime_verified" if verified else "observed" if event else "unverified",
                                  snapshot_revision=observer.snapshot_revision if observer else None,
                                  binding_revision=binding.binding_revision)

    if observer is None or observer.observer_id != binding.observer_id:
        return result("unknown_stale", "observer_unavailable")
    related = tuple(event for event in observations if
                    event.topic == topic and event.agent == agent
                    and event.observer_id == binding.observer_id)
    current = tuple(event for event in related
                    if event.observer_epoch == observer.epoch)
    if not current:
        if related:
            return result("unknown_stale", "observer_restarted")
        return result("linked_unverified", "no_observer_evidence")
    latest_sequence = max(event.sequence for event in current)
    latest = tuple(event for event in current if event.sequence == latest_sequence)
    event = latest[0]
    if any(item != event for item in latest):
        return result("unknown_stale", "conflicting_evidence")
    if (event.session_id != binding.session_id
            or event.session_generation != binding.session_generation
            or event.connector_generation != binding.connector_generation):
        return result("unknown_stale", "binding_generation_mismatch")
    if event.observed_at > observer.now:
        return result("unknown_stale", "evidence_in_future")
    if observer.now - event.observed_at > timedelta(seconds=observer.max_age_seconds):
        return result("unknown_stale", "evidence_expired", event)
    if event.event == "stream_connected":
        return result("linked_unverified", "stream_only", event)
    expected_acceptance = "queue" if event.event == "busy_queued" else "idle"
    if event.event in {"ready", "busy_queued"} and (
        event.native_attachment != "verified"
        or event.runtime_acceptance != expected_acceptance
    ):
        return result("linked_unverified", "runtime_readiness_unverified", event)
    if event.event == "ready":
        return result("reachable", "session_ready", event)
    if event.event == "busy_queued":
        return result("busy_queued", "session_busy_queue_verified", event)
    if event.event == "disconnected":
        return result("offline", "session_disconnected", event)
    return result("offline", "session_offline", event)


def project_topic_snapshot(
    topic: str, *, bindings: tuple[TopicBinding, ...] = (),
    observer: ObserverContext | None = None,
    observations: tuple[RouteObservation, ...] = (),
    binding_snapshot_complete: bool = False,
) -> dict:
    """Public read projection for all configured peer identities on one topic.

    Default construction is deliberately unconfigured/unknown. An integration
    injects a coherent snapshot from the existing authoritative registry and
    observer, never agent message bodies. No provider or registry is created.
    """
    items = []
    for agent in ("codex", "hermes", "claude"):
        matching = tuple(binding for binding in bindings
                         if binding.topic == topic and binding.agent == agent)
        if len(matching) > 1:
            item = PublicReachability(topic=topic, agent=agent, status="unknown_stale",
                                      reason="conflicting_evidence")
        else:
            item = project_reachability(
                topic=topic, agent=agent, binding=matching[0] if matching else None,
                observer=observer, observations=observations,
                binding_snapshot_complete=binding_snapshot_complete,
            )
        items.append(item.model_dump(mode="json"))
    return {"topic": topic, "items": items, "advisory": True,
            "delivery_guaranteed": False,
            "snapshot_revision": observer.snapshot_revision if observer else None}
