"""Read-only, identity-preserving canonical topic registry bridge.

Nothing here enrolls readers, activates a watcher, selects a session or caches
readiness. Each call uses its own authenticated reader; service-reader snapshots
must never be substituted for agent-scoped reads by the caller.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import ipaddress
import json
import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

MAX_BODY_BYTES = 65536
DEADLINE_SECONDS = 5.0


def _uuid(value: Any) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("invalid public revision")
    return value


def validate_epoch(value: Any) -> str:
    """Canonical public UUID, never an opaque provider/session reference."""
    return _uuid(value)


def validate_topic(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", value):
        raise ValueError("invalid registry topic")
    return value


def validate_origin(value: Any) -> str:
    try:
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError()
        parsed = urlsplit(value)
        _ = parsed.port
        if (not isinstance(value, str) or parsed.scheme not in {"https", "http"}
                or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(ord(c) <= 32 or ord(c) >= 127 for c in value) or "\\" in value):
            raise ValueError()
        if parsed.scheme == "http" and not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid registry origin; HTTPS required except literal loopback") from None
    return value.rstrip("/")


def _utc(value: Any) -> datetime:
    # Reject naive time, offsets other than actual UTC, epoch numbers and lax
    # coercions. JSON Schema's draft double-escaped offset pattern is not used.
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value
    ):
        raise ValueError("expected UTC timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


PublicUUID = Annotated[str, BeforeValidator(_uuid)]
UTCTime = Annotated[datetime, BeforeValidator(_utc)]
Topic = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")]
Cursor = Annotated[int, Field(ge=0, le=2**63 - 1)]
Reason = Literal[
    "no_exact_binding", "inventory_incomplete", "owner_selection_unverified",
    "no_observer_evidence", "observer_unavailable", "observer_restarted",
    "binding_generation_mismatch", "evidence_expired", "evidence_in_future",
    "conflicting_evidence", "stream_only", "session_ready",
    "session_busy_queue_verified", "session_disconnected", "session_offline",
    "runtime_readiness_unverified",
]
RecoveryReason = Literal[
    "initial_snapshot", "epoch_changed", "retention_gap", "catchup_limit",
    "subscriber_overflow",
]


class _Wire(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class RegistryPeer(_Wire):
    agent: Literal["codex", "hermes", "claude"]
    status: Literal["unlinked", "linked_unverified", "reachable", "busy_queued", "offline", "unknown_stale"]
    reason: Reason
    link_state: Literal["linked", "unlinked", "unknown"]
    transport_state: Literal["attached", "disconnected", "unverified", "unknown"]
    availability_state: Literal["available", "busy", "offline", "unverified", "unknown"]
    evidence_level: Literal["runtime_verified", "observed", "unverified"]
    binding_revision: PublicUUID | None
    attachment_revision: PublicUUID | None
    last_evidence_at: UTCTime | None
    valid_until: UTCTime | None

    @model_validator(mode="after")
    def coherent(self) -> RegistryPeer:
        green = self.status in {"reachable", "busy_queued"}
        if green:
            expected = ("session_ready", "available") if self.status == "reachable" else ("session_busy_queue_verified", "busy")
            if ((self.reason, self.availability_state) != expected
                    or self.link_state != "linked" or self.transport_state != "attached"
                    or self.evidence_level != "runtime_verified"
                    or any(x is None for x in (self.binding_revision, self.attachment_revision, self.last_evidence_at, self.valid_until))):
                raise ValueError("incoherent runtime readiness")
        elif self.valid_until is not None or self.evidence_level == "runtime_verified":
            raise ValueError("unverified state cannot carry readiness")
        if self.status == "unlinked" and (
            self.reason != "no_exact_binding" or self.link_state != "unlinked"
            or self.attachment_revision is not None or self.last_evidence_at is not None
            or self.evidence_level != "unverified"
        ):
            raise ValueError("incoherent unlinked state")
        if self.status == "offline" and (
            self.reason not in {"session_disconnected", "session_offline"}
            or self.link_state != "linked" or self.binding_revision is None
            or self.transport_state != "disconnected" or self.availability_state != "offline"
        ):
            raise ValueError("incoherent offline state")
        if not green and (self.reason in {"session_ready", "session_busy_queue_verified"}
                          or self.availability_state in {"available", "busy"}):
            raise ValueError("readiness conflicts with status")
        if self.status == "linked_unverified" and self.link_state != "linked":
            raise ValueError("unverified link must be linked")
        if self.status == "linked_unverified" and self.reason not in {
            "owner_selection_unverified", "no_observer_evidence", "stream_only", "runtime_readiness_unverified"
        }:
            raise ValueError("incoherent unverified reason")
        if self.status == "unknown_stale" and self.reason not in {
            "inventory_incomplete", "observer_unavailable", "observer_restarted",
            "binding_generation_mismatch", "evidence_expired", "evidence_in_future", "conflicting_evidence"
        }:
            raise ValueError("incoherent unknown reason")
        return self


def _peers(value: Any) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise ValueError("expected peer array")
    return tuple(value)


class RegistrySnapshot(_Wire):
    topic: Topic
    registry_epoch: PublicUUID | None
    snapshot_revision: PublicUUID | None
    as_of: UTCTime
    complete: bool
    source_state: Literal["active", "degraded", "unconfigured", "stopped"]
    peers: Annotated[tuple[RegistryPeer, ...], BeforeValidator(_peers), Field(max_length=3)]

    @model_validator(mode="after")
    def coherent(self) -> RegistrySnapshot:
        if len({p.agent for p in self.peers}) != len(self.peers):
            raise ValueError("duplicate peer")
        if self.source_state == "active" and (self.registry_epoch is None or self.snapshot_revision is None):
            raise ValueError("active source requires public revisions")
        for peer in self.peers:
            if not self.complete and peer.status == "unlinked":
                raise ValueError("incomplete inventory cannot assert absence")
            if self.source_state != "active" and (
                peer.status != "unknown_stale" or peer.reason != "observer_unavailable"
                or peer.transport_state != "unknown" or peer.availability_state != "unknown"
                or peer.evidence_level != "unverified" or peer.valid_until is not None
            ):
                raise ValueError("unavailable observer cannot assert live state")
            if peer.last_evidence_at is not None and peer.last_evidence_at > self.as_of:
                raise ValueError("evidence after snapshot")
            if peer.valid_until is not None and (
                peer.valid_until <= self.as_of
                or peer.valid_until > peer.last_evidence_at + timedelta(seconds=120)
            ):
                raise ValueError("invalid evidence deadline")
        return self


class RegistryPointer(_Wire):
    topic: Topic
    registry_epoch: PublicUUID
    snapshot_revision: PublicUUID
    cursor: Cursor


class RegistrySnapshotRequired(_Wire):
    topic: Topic
    registry_epoch: PublicUUID
    head: Cursor
    reason: RecoveryReason


class RegistryUnavailable(_Wire):
    topic: Topic
    registry_epoch: PublicUUID | None
    reason: Literal["observer_unavailable"]


class RegistryClientError(Exception):
    """Safe public errors never contain source URLs, payloads or credentials."""


class RegistrySourceUnavailable(RegistryClientError):
    def __init__(self):
        super().__init__("topic registry source unavailable")


class RegistryAccessDenied(RegistryClientError):
    def __init__(self, status_code: Literal[401, 403]):
        self.status_code = status_code
        super().__init__("topic registry reader not authorized")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _decode(payload: bytes | str) -> Any:
    if not isinstance(payload, (bytes, str)) or len(payload.encode("utf-8") if isinstance(payload, str) else payload) > MAX_BODY_BYTES:
        raise ValueError("invalid registry body")
    return json.loads(payload, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON constant")))


def parse_event(event_type: str, payload: bytes | str) -> RegistryPointer | RegistrySnapshotRequired | RegistryUnavailable:
    """Parse SSE data only; transport must enforce requested topic/epoch/id."""
    models = {"registry.invalidated": RegistryPointer,
              "registry.snapshot_required": RegistrySnapshotRequired,
              "registry.unavailable": RegistryUnavailable}
    try:
        return models[event_type].model_validate(_decode(payload))
    except (KeyError, ValueError, TypeError, RecursionError):
        raise RegistrySourceUnavailable() from None


@dataclass(frozen=True, slots=True)
class RegistryClient:
    base_url: str
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False, kw_only=True)

    def __post_init__(self):
        object.__setattr__(self, "base_url", validate_origin(self.base_url))

    @property
    def source_identity(self) -> str:
        """Pinned public origin only; contains no authentication material."""
        return self.base_url

    async def aclose(self) -> None:
        """No persistent HTTP client or credential state is retained to close."""

    async def fetch_snapshot(self, topic: str, bearer: str) -> RegistrySnapshot:
        # Inputs are never interpolated until validation; no X-Act-As or caller
        # role fields, cookies, persistent client, credential cache or redirects.
        validate_topic(topic)
        if not isinstance(bearer, str) or not bearer or len(bearer) > 8192 or any(not 33 <= ord(c) <= 126 for c in bearer):
            raise RegistryAccessDenied(401)
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                async with httpx.AsyncClient(transport=self.transport, trust_env=False,
                                             follow_redirects=False, timeout=DEADLINE_SECONDS) as client:
                    async with client.stream("GET", self.base_url + "/api/topic-registry/v1/topics/" + topic + "/snapshot",
                                             headers={"Authorization": "Bearer " + bearer, "Accept": "application/json", "Accept-Encoding": "identity"}) as response:
                        if response.status_code in {401, 403}:
                            raise RegistryAccessDenied(response.status_code)
                        if response.status_code != 200:
                            raise RegistrySourceUnavailable()
                        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                            raise RegistrySourceUnavailable()
                        if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                            raise RegistrySourceUnavailable()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_BODY_BYTES:
                                raise RegistrySourceUnavailable()
                            body.extend(chunk)
                        result = RegistrySnapshot.model_validate(_decode(bytes(body)))
                        if result.topic != topic:
                            raise RegistrySourceUnavailable()
                        return result
        except RegistryClientError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, UnicodeError, RecursionError):
            raise RegistrySourceUnavailable() from None
