"""Fresh, bounded executor observations under an independently retained authority.

No transport, issuer, operator enrollment or launch is discovered here. This
protocol is intentionally distinct from the one-shot executor launch receipt.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, fields
import inspect
import json
import math
import os
import re
import secrets
import threading
import time
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .container_pilot_manifest import Platform
from .startup_reconciliation import ExecutorSessionIdentity

PROTOCOL = "engineering-hub.executor-current-observation.v1"
AUDIENCE = "engineering-hub.staging-session-observer"
SIGNATURE_DOMAIN = b"engineering-hub-current-observation-v1\x00"
MAX_OBSERVATION_BYTES = 8192
MAX_OBSERVATIONS = 4096
CLOCK_SKEW_MS = 5000
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SIGNATURE = re.compile(r"[A-Za-z0-9_-]{86}\Z")
_RELEASE_FIELDS = (
    "issuer_id", "implementation_commit", "runnable_digest",
    "platform_manifest_digest", "image_config_id", "platform", "launch_profile_digest",
)
_LAUNCH_FIELDS = (
    "launch_id", "boot_id", "authenticated_host_pid", "process_start_ticks",
    "container_id", "engine_id", "engine_pid", "engine_start_ticks", "approval_expires_at_ms",
)
_PAYLOAD_FIELDS = frozenset((*_RELEASE_FIELDS, *_LAUNCH_FIELDS,
                           "protocol", "audience", "nonce", "observation_id",
                           "observed_at_ms", "expires_at_ms"))


class CurrentExecutorObservationError(RuntimeError):
    """Sanitized observation denial; never includes evidence or transport errors."""


def _require(value):
    if not value:
        raise CurrentExecutorObservationError("Current executor identity unavailable")


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value and UUID(value).version == 4
    except (ValueError, TypeError, AttributeError):
        return False


def _integer(value, upper=2**63 - 1):
    return type(value) is int and 0 < value <= upper


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _strict(raw):
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_OBSERVATION_BYTES)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result

    def reject(_):
        raise ValueError

    value = json.loads(raw, object_pairs_hook=unique, parse_float=reject, parse_constant=reject)
    _require(_canonical(value) == raw)
    return value


def _clock():
    mono, wall = time.monotonic(), time.time()
    _require(type(mono) in (int, float) and math.isfinite(mono) and mono >= 0)
    _require(type(wall) in (int, float) and math.isfinite(wall) and wall > 0)
    return mono, int(wall * 1000)


def _sync(call):
    return (callable(call) and not inspect.iscoroutinefunction(call)
            and not inspect.isasyncgenfunction(call)
            and not inspect.iscoroutinefunction(getattr(call, "__call__", None))
            and not inspect.isasyncgenfunction(getattr(call, "__call__", None)))


@dataclass(frozen=True)
class CurrentExecutorReleasePins:
    """Same enrolled issuer/release pins, not a new or wire-selected authority."""
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
                     and re.fullmatch(r"[0-9a-f]{40}", self.implementation_commit))
            _require(all(type(v) is str and _SHA.fullmatch(v) for v in (
                self.runnable_digest, self.platform_manifest_digest,
                self.image_config_id, self.launch_profile_digest)))
            _require(type(self.platform) is Platform)
            object.__setattr__(self, "platform", Platform.model_validate(
                self.platform.model_dump(mode="json")))
        except Exception:
            raise CurrentExecutorObservationError("Invalid independent executor pins") from None


@dataclass(frozen=True)
class CurrentLaunchPins:
    """Independent exact live launch/Engine pins and ORIGINAL approval ceiling."""
    launch_id: str
    boot_id: str
    authenticated_host_pid: int
    process_start_ticks: int
    container_id: str
    engine_id: str
    engine_pid: int
    engine_start_ticks: int
    approval_expires_at_ms: int

    def __post_init__(self):
        try:
            _require(_uuid(self.launch_id) and _uuid(self.boot_id))
            _require(type(self.container_id) is str and _HEX.fullmatch(self.container_id))
            _require(type(self.engine_id) is str and _ID.fullmatch(self.engine_id))
            _require(all(_integer(v) for v in (
                self.authenticated_host_pid, self.process_start_ticks,
                self.engine_pid, self.engine_start_ticks, self.approval_expires_at_ms)))
        except Exception:
            raise CurrentExecutorObservationError("Invalid independent launch pins") from None


class CurrentExecutorObserver:
    """Callable fresh observer; sticky denial, no retry, cached result or renewal.

    The trusted synchronous transport implements observe_current_launch with
    nonce and timeout_ms keywords. It must independently bound its own I/O and
    use the SAME retained supervisor/Engine authority. The verifier checks the
    whole elapsed interval but cannot interrupt a blocked synchronous transport.
    A fixed LOCAL monotonic ceiling is derived once from the independently
    supplied original wall expiry. Remote monotonic timestamps are never used.
    """
    def __init__(self, *, release, launch, transport,
                 exchange_timeout_ms=2000, max_observation_lifetime_ms=5000):
        try:
            self._pid = os.getpid()
            self._lock = threading.RLock()
            self._closed = False
            self._checking = False
            _require(type(release) is CurrentExecutorReleasePins)
            self._release = CurrentExecutorReleasePins(**{
                f.name: getattr(release, f.name) for f in fields(CurrentExecutorReleasePins)})
            _require(type(launch) is CurrentLaunchPins)
            self._launch = CurrentLaunchPins(**{
                f.name: getattr(launch, f.name) for f in fields(CurrentLaunchPins)})
            self._exchange = getattr(transport, "observe_current_launch", None)
            _require(_sync(self._exchange))
            _require(type(exchange_timeout_ms) is int and 100 <= exchange_timeout_ms <= 10000)
            _require(type(max_observation_lifetime_ms) is int
                     and 100 <= max_observation_lifetime_ms <= 10000)
            mono, wall = _clock()
            _require(wall < self._launch.approval_expires_at_ms <= wall + 3_600_000)
            # Both ceilings are fixed at construction; wall reversal never extends either.
            self._deadline = mono + (self._launch.approval_expires_at_ms - wall) / 1000
            self._last_mono, self._last_wall = mono, wall
            self._timeout = exchange_timeout_ms
            self._ttl = max_observation_lifetime_ms
            self._nonces, self._observations = set(), set()
        except Exception:
            raise CurrentExecutorObservationError("Invalid current executor observer") from None

    def _guard(self):
        _require(os.getpid() == self._pid and not self._closed)
        mono, wall = _clock()
        _require(self._last_mono <= mono < self._deadline
                 and self._last_wall <= wall < self._launch.approval_expires_at_ms)
        self._last_mono, self._last_wall = mono, wall
        return mono, wall

    @contextmanager
    def _bounded_call(self):
        acquired = False
        try:
            started, _ = _clock()
            deadline = min(self._deadline, started + self._timeout / 1000)
            remaining = deadline - started
            _require(remaining > 0)
            acquired = self._lock.acquire(timeout=remaining)
            _require(acquired)
            yield deadline
        except BaseException as error:
            self._closed = True
            if not isinstance(error, Exception):
                raise
            raise CurrentExecutorObservationError("Current executor identity unavailable") from None
        finally:
            if acquired:
                self._lock.release()

    def __call__(self):
        # Never acquire an inherited mutex in a child process.
        if os.getpid() != self._pid:
            raise CurrentExecutorObservationError("Current executor identity unavailable")
        with self._bounded_call() as deadline:
            try:
                _require(not self._checking)
                start, wall_start = self._guard()
                self._checking = True
                _require(len(self._nonces) < MAX_OBSERVATIONS)
                nonce = secrets.token_hex(32)
                _require(type(nonce) is str and _HEX.fullmatch(nonce) and nonce not in self._nonces)
                self._nonces.add(nonce)
                _require(start < deadline)
                budget = min(self._timeout, int((deadline - start) * 1000))
                _require(budget > 0)
                raw = self._exchange(nonce=nonce, timeout_ms=budget)
                if inspect.isawaitable(raw) or inspect.isasyncgen(raw):
                    if inspect.iscoroutine(raw) and inspect.getcoroutinestate(raw) == inspect.CORO_CREATED:
                        raw.close()
                    raise ValueError
                after, wall_after = self._guard()
                _require(after < deadline)
                envelope = _strict(raw)
                _require(type(envelope) is dict and set(envelope) == {"payload", "signature"})
                payload, signature = envelope["payload"], envelope["signature"]
                _require(type(payload) is dict and set(payload) == _PAYLOAD_FIELDS)
                _require(type(signature) is str and _SIGNATURE.fullmatch(signature))
                decoded = base64.urlsafe_b64decode(signature + "==")
                _require(len(decoded) == 64
                         and base64.urlsafe_b64encode(decoded).decode().rstrip("=") == signature)
                Ed25519PublicKey.from_public_bytes(self._release.public_key).verify(
                    decoded, SIGNATURE_DOMAIN + _canonical(payload))
                _require(payload["protocol"] == PROTOCOL and payload["audience"] == AUDIENCE)
                _require(type(payload["nonce"]) is str and secrets.compare_digest(payload["nonce"], nonce))
                _require(_uuid(payload["observation_id"])
                         and payload["observation_id"] not in self._observations)
                for name in _RELEASE_FIELDS:
                    expected = getattr(self._release, name)
                    if name == "platform":
                        expected = expected.model_dump(mode="json")
                    _require(type(payload[name]) is type(expected) and payload[name] == expected)
                for name in _LAUNCH_FIELDS:
                    expected = getattr(self._launch, name)
                    _require(type(payload[name]) is type(expected) and payload[name] == expected)
                observed, expires = payload["observed_at_ms"], payload["expires_at_ms"]
                _require(_integer(observed) and _integer(expires))
                _require(wall_start - CLOCK_SKEW_MS <= observed <= wall_after + CLOCK_SKEW_MS)
                _require(observed < expires and wall_after < expires <= observed + self._ttl
                         and expires <= self._launch.approval_expires_at_ms)
                final, final_wall = self._guard()
                _require(final < deadline and final_wall < expires)
                self._observations.add(payload["observation_id"])
                return ExecutorSessionIdentity(self._launch.launch_id, self._release.image_config_id)
            except BaseException as error:
                self._closed = True
                if not isinstance(error, Exception):
                    raise
                raise CurrentExecutorObservationError("Current executor identity unavailable") from None
            finally:
                self._checking = False

    def close(self):
        """Revoke local observations only; never close or renew caller authority."""
        if os.getpid() != self._pid:
            raise CurrentExecutorObservationError("Current executor identity unavailable")
        with self._lock:
            self._closed = True
