"""One-shot consumer of independently issued, signed executor launch evidence.

This is NOT a launcher, IPC authentication mechanism or memory attestation.
Trusted composition supplies the independent release enrollment and transport.
The issuer must authenticate its peer and correlate that live process to the
actual immutable container, boot, process start and effective launch profile;
caller-supplied victim identities cannot establish that authority. No issuer or
key is discovered here. Acceptance covers only a bounded attested launch interval,
not continuing process integrity or cancellation of already admitted effects.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import inspect
import json
import math
import os
import re
import secrets
import threading
import time
from types import MappingProxyType
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from engineering_hub.container_pilot_manifest import Platform


PROTOCOL = "engineering-hub.executor-launch.v1"
AUDIENCE = "engineering-hub.container-executor"
MAX_RECEIPT_BYTES = 8192
ISSUANCE_SKEW_MS = 5000
SIGNATURE_DOMAIN = b"engineering-hub-launch-receipt-v1\x00"
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SIGNATURE = re.compile(r"[A-Za-z0-9_-]{86}\Z")
_FIELDS = frozenset({
    "protocol", "audience", "nonce", "receipt_id", "launch_id", "issuer_id",
    "boot_id", "authenticated_host_pid", "process_start_ticks", "container_id",
    "runnable_digest", "platform_manifest_digest", "image_config_id", "platform",
    "implementation_commit", "launch_profile_digest", "issued_at_ms", "expires_at_ms",
})


class RuntimeIdentityError(RuntimeError):
    """Sanitized denial; contains no receipt, key, transport or process details."""


def _require(value):
    if not value:
        raise RuntimeIdentityError("Trusted executor launch identity unavailable")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _wall_ms():
    value = time.time()
    _require(type(value) in (float, int) and math.isfinite(value) and value > 0)
    return int(value * 1000)


def _strict(raw):
    _require(type(raw) is bytes and 1 <= len(raw) <= MAX_RECEIPT_BYTES)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result

    def reject(_):
        raise ValueError

    result = json.loads(raw, object_pairs_hook=unique, parse_float=reject,
                        parse_constant=reject)
    _require(_canonical(result) == raw)
    return result


@dataclass(frozen=True)
class ExecutorReleaseEnrollment:
    """Privileged independent pins, never derived from an untrusted receipt.

    Construction validates shape, not operator approval or the launcher's ability
    to establish these claims. The platform is copied, including when composing
    the verifier, so a mutable/bypassed source object cannot change authority.
    """

    issuer_id: str
    public_key: bytes
    implementation_commit: str
    runnable_digest: str
    platform_manifest_digest: str
    image_config_id: str
    platform: Platform
    launch_profile_digest: str

    def __post_init__(self):
        try:
            _require(type(self.issuer_id) is str and _ID.fullmatch(self.issuer_id))
            _require(type(self.public_key) is bytes and len(self.public_key) == 32)
            _require(type(self.implementation_commit) is str
                     and _COMMIT.fullmatch(self.implementation_commit))
            _require(all(type(value) is str and _SHA.fullmatch(value) for value in (
                self.runnable_digest, self.platform_manifest_digest,
                self.image_config_id, self.launch_profile_digest)))
            _require(type(self.platform) is Platform)
            platform = Platform.model_validate(self.platform.model_dump(mode="json"))
            object.__setattr__(self, "platform", platform)
        except Exception:
            raise RuntimeIdentityError("Invalid independent executor release enrollment") from None


class LaunchReceiptIdentityVerifier:
    """Accept exactly one nonce-bound Ed25519 receipt, without later issuer I/O.

    Transport ``issue_receipt(nonce=..., timeout_ms=...)`` is caller-owned and
    must return bounded canonical bytes. The timeout is cooperative: checks around
    a blocking exchange reject overruns but cannot forcibly cancel that call.
    The signed host PID is in the issuer's namespace; this consumer cannot infer
    peer mapping by comparing it to its own potentially namespaced PID. Instead
    the trusted issuer supplies that mapping, bound to the internal challenge.
    The transport must exchange directly as THIS consumer process, not relay the
    nonce through another permitted process. Signature and nonce checks alone do
    not prove that precondition; real kernel peer/container mapping is external.
    A separate local PID fence denies inherited verifier use after fork.
    """

    def __init__(self, *, enrollment, issuer_transport,
                 max_receipt_lifetime_seconds, exchange_timeout_ms):
        try:
            _require(type(enrollment) is ExecutorReleaseEnrollment)
            enrolled = ExecutorReleaseEnrollment(**{
                name: getattr(enrollment, name)
                for name in ExecutorReleaseEnrollment.__dataclass_fields__})
            _require(type(max_receipt_lifetime_seconds) is int
                     and 1 <= max_receipt_lifetime_seconds <= 3600)
            _require(type(exchange_timeout_ms) is int and 100 <= exchange_timeout_ms <= 10000)
            issue = getattr(issuer_transport, "issue_receipt", None)
            _require(callable(issue) and not inspect.iscoroutinefunction(issue)
                     and not inspect.iscoroutinefunction(getattr(issue, "__call__", None)))
            self._pid = os.getpid()
            self._lock = threading.Lock()
            self._closed = True
            start = time.monotonic()
            wall_start = _wall_ms()
            _require(math.isfinite(start) and type(wall_start) is int and wall_start > 0)
            nonce = secrets.token_hex(32)
            _require(type(nonce) is str and _HEX.fullmatch(nonce))
            _require(os.getpid() == self._pid and time.monotonic() < start + exchange_timeout_ms / 1000)
            raw = issue(nonce=nonce, timeout_ms=exchange_timeout_ms)
            if inspect.isawaitable(raw):
                # Reject an incorrectly declared synchronous transport, without
                # scheduling or cancelling caller-owned async work.
                if inspect.iscoroutine(raw) and inspect.getcoroutinestate(raw) == inspect.CORO_CREATED:
                    raw.close()
                raise RuntimeIdentityError("Synchronous launch issuer exchange required")
            after = time.monotonic()
            wall_after = _wall_ms()
            _require(os.getpid() == self._pid and math.isfinite(after)
                     and start <= after < start + exchange_timeout_ms / 1000)
            envelope = _strict(raw)
            _require(type(envelope) is dict and set(envelope) == {"payload", "signature"})
            payload, signature = envelope["payload"], envelope["signature"]
            _require(type(payload) is dict and set(payload) == _FIELDS)
            _require(type(signature) is str and _SIGNATURE.fullmatch(signature))
            decoded = base64.b64decode(signature + "==", altchars=b"-_", validate=True)
            _require(len(decoded) == 64 and base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") == signature)
            Ed25519PublicKey.from_public_bytes(enrolled.public_key).verify(
                decoded, SIGNATURE_DOMAIN + _canonical(payload))
            _require(payload["protocol"] == PROTOCOL and payload["audience"] == AUDIENCE
                     and payload["nonce"] == nonce)
            for name in ("receipt_id", "launch_id", "boot_id"):
                value = payload[name]
                _require(type(value) is str and str(UUID(value)) == value)
            for name in ("authenticated_host_pid", "process_start_ticks", "issued_at_ms", "expires_at_ms"):
                _require(type(payload[name]) is int and 0 < payload[name] <= (2**63 - 1))
            _require(type(payload["container_id"]) is str and _HEX.fullmatch(payload["container_id"]))
            for name in ("issuer_id", "implementation_commit", "runnable_digest",
                         "platform_manifest_digest", "image_config_id", "launch_profile_digest"):
                _require(type(payload[name]) is str and payload[name] == getattr(enrolled, name))
            _require(type(payload["platform"]) is dict
                     and payload["platform"] == enrolled.platform.model_dump(mode="json"))
            issued, expires = payload["issued_at_ms"], payload["expires_at_ms"]
            lifetime_ms = expires - issued
            _require(0 < lifetime_ms <= max_receipt_lifetime_seconds * 1000
                     and wall_start - ISSUANCE_SKEW_MS <= issued <= wall_after + ISSUANCE_SKEW_MS
                     and issued <= wall_start + ISSUANCE_SKEW_MS
                     and expires > wall_after)
            # Never renew TTL from response arrival or subsequent wall-clock reads.
            deadline = min(start + lifetime_ms / 1000,
                           after + (expires - wall_after) / 1000)
            now = time.monotonic()
            _require(os.getpid() == self._pid and math.isfinite(now) and after <= now < deadline
                     and now < start + exchange_timeout_ms / 1000)
            self._commit = enrolled.implementation_commit
            self._deadline = deadline
            self._not_before = start
            # Store no mutable receipt object or transport capability for effects.
            self._receipt = MappingProxyType({**payload,
                "platform": MappingProxyType(dict(payload["platform"]))})
            self._closed = False
        except Exception:
            raise RuntimeIdentityError("Trusted executor launch identity unavailable") from None

    def implementation_commit(self) -> str:
        # Check before acquiring a potentially inherited locked threading.Lock.
        try:
            _require(os.getpid() == self._pid)
            with self._lock:
                now = time.monotonic()
                _require(os.getpid() == self._pid and not self._closed
                         and math.isfinite(now) and self._not_before <= now < self._deadline)
                return self._commit
        except Exception:
            raise RuntimeIdentityError("Trusted executor launch identity unavailable") from None

    def close(self):
        """Revoke this verifier only; never close or call the caller's transport."""
        _require(os.getpid() == self._pid)
        with self._lock:
            self._closed = True
