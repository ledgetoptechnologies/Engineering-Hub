"""Explicit operator enrollment, not automatic build provenance discovery.

Only trusted local composition may construct this verifier, after independently
reviewing the build and exact record. An untrusted record's hashes, builder name,
labels or enrollment claims are never a source of approval. Nothing here loads
enrollment from requests, environment variables or the content store.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from types import MappingProxyType
from uuid import UUID

from engineering_hub.container_pilot_manifest import PilotManifest
from .build_record import RECORD_LIMIT, _canonical, _load_record


class BuildEnrollmentError(ValueError):
    """Safe enrollment error without build contents or configured identities."""


_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


@dataclass(frozen=True)
class BuildEnrollment:
    manifest_digest: str
    role: str
    record_digest: str
    builder_id: str
    invocation_id: str

    def __post_init__(self):
        try:
            if any(type(value) is not str or not _SHA.fullmatch(value)
                   for value in (self.manifest_digest, self.record_digest)):
                raise ValueError
            if type(self.role) is not str or self.role not in {"candidate", "rollback"}:
                raise ValueError
            if type(self.builder_id) is not str or not _ID.fullmatch(self.builder_id):
                raise ValueError
            if type(self.invocation_id) is not str or str(UUID(self.invocation_id)) != self.invocation_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise BuildEnrollmentError("Invalid explicit build enrollment") from None


class OperatorEnrolledBuildVerifier:
    """Immutable, exact manifest/role/record allowlist from trusted composition.

    This is a local operator trust anchor, not a signature or CI attestation
    verifier. Its constructor is a privileged enrollment action and must never
    be exposed as an agent/API tool. Supplying arbitrary generated enrollment
    objects in a test does not establish that a real build has been approved.
    The outer verify_image_record still validates the full OCI metadata chain.
    """

    def __init__(self, *, enrollments: tuple[BuildEnrollment, ...]):
        if type(enrollments) is not tuple or not 1 <= len(enrollments) <= 128:
            raise BuildEnrollmentError("Explicit bounded build enrollments required")
        approved = {}
        for item in enrollments:
            if type(item) is not BuildEnrollment:
                raise BuildEnrollmentError("Explicit build enrollment required")
            # Snapshot and revalidate even a dataclass constructed by bypass.
            try:
                copy = BuildEnrollment(item.manifest_digest, item.role, item.record_digest,
                                       item.builder_id, item.invocation_id)
            except (AttributeError, BuildEnrollmentError):
                raise BuildEnrollmentError("Invalid explicit build enrollment") from None
            key = (copy.manifest_digest, copy.role)
            if key in approved:
                raise BuildEnrollmentError("Duplicate manifest-role enrollment")
            approved[key] = copy
        self._approved = MappingProxyType(approved)

    def verify_build_record(self, record_bytes: bytes, record: dict,
                            manifest: PilotManifest, role: str) -> bool:
        try:
            if (type(manifest) is not PilotManifest or type(role) is not str
                    or role not in {"candidate", "rollback"}
                    or type(record_bytes) is not bytes or not 1 <= len(record_bytes) <= RECORD_LIMIT
                    or type(record) is not dict):
                return False
            approved = self._approved.get((manifest.digest(), role))
            if approved is None:
                return False
            record_digest = "sha256:" + hashlib.sha256(record_bytes).hexdigest()
            if (record_digest != approved.record_digest
                    or record_digest != getattr(manifest, role).provenance.attestation_digest
                    or _canonical(record) != record_bytes):
                return False
            parsed = _load_record(record_bytes)
            return (parsed["builder_id"] == approved.builder_id
                    and parsed["invocation_id"] == approved.invocation_id)
        except Exception:
            # Untrusted arguments may fail schema/canonicalization. Never echo.
            return False
