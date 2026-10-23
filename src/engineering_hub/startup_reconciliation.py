"""In-memory admission boundary for an explicitly reconciled manual session.

This component starts nothing and proves no restore, inventory or writer-exclusion
facts. Only trusted operator composition may accept its one fresh challenge.
There is deliberately no CLI, persisted approval, environment or HTTP opener.
"""
from __future__ import annotations

from dataclasses import dataclass
import inspect
import os
from pathlib import Path
import re
import secrets
import threading
import time
from uuid import UUID

from .pilot_ownership import PilotOwner


class StartupReconciliationError(RuntimeError):
    def __init__(self):
        super().__init__('manual staging session reconciliation required')


def _require(value):
    if not value:
        raise StartupReconciliationError()


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value and UUID(value).version == 4
    except (ValueError, TypeError, AttributeError):
        return False


def _digest(value):
    return type(value) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', value) is not None


@dataclass(frozen=True)
class ExecutorSessionIdentity:
    """Expected independently enrolled identity, not an executor self-attestation."""
    generation_id: str
    artifact_digest: str


@dataclass(frozen=True)
class OperatorReconciliation:
    """An operator's exact report binding; boolean attestations do not prove facts."""
    challenge: str
    owner_epoch: str
    target_id: str
    manifest_digest: str
    executor: ExecutorSessionIdentity
    report_digest: str
    inventory_reconciled: bool
    writers_excluded: bool
    unresolved_effects_absent: bool


class StartupReconciliation:
    """Single-use, PID/owner/identity/deadline-bound manual admission capability.

    ``current_executor_identity`` is a mandatory trusted synchronous observer. It
    must verify current identity against independent enrollment, not echo a
    caller-selected expectation. Its timeout is cooperative; this class checks
    elapsed time but cannot interrupt blocking I/O. It is not a trust factory.

    The lifetime begins at construction, including operator review time. Every
    caller must invoke assert_permitted immediately before its next admission or
    phase. This does not cancel accepted effects or synchronize a remote writer.
    """
    def __init__(self, *, owner, database, target_id, manifest_digest, executor,
                 lifetime_seconds, current_executor_identity):
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._closed = False
        self._accepted = False
        self._attempted = False
        self._checking = False
        self._challenge = secrets.token_hex(32)
        self._created = time.monotonic()
        try:
            _require(type(owner) is PilotOwner and isinstance(database, (str, Path)))
            self._database = Path(database)
            _require(self._database.is_absolute())
            _require(type(target_id) is str and target_id == 'engineering-hub:staging')
            _require(_digest(manifest_digest) and type(executor) is ExecutorSessionIdentity
                     and _uuid(executor.generation_id) and _digest(executor.artifact_digest))
            _require(type(lifetime_seconds) is int and 1 <= lifetime_seconds <= 3600)
            _require(callable(current_executor_identity)
                     and not inspect.iscoroutinefunction(current_executor_identity)
                     and not inspect.isasyncgenfunction(current_executor_identity)
                     and not inspect.iscoroutinefunction(getattr(current_executor_identity, '__call__', None))
                     and not inspect.isasyncgenfunction(getattr(current_executor_identity, '__call__', None)))
            self._owner = owner
            self._epoch = owner.epoch
            _require(_uuid(self._epoch))
            self._target = target_id
            self._manifest = manifest_digest
            self._executor = executor
            self._observer = current_executor_identity
            self._deadline = self._created + lifetime_seconds
            self._last_time = self._created
            self._validate()
        except BaseException as error:
            self._closed = True
            if isinstance(error, Exception):
                raise StartupReconciliationError() from None
            raise

    @property
    def challenge(self):
        self._same_process()
        with self._lock:
            _require(not self._closed and not self._attempted)
            return self._challenge

    def _same_process(self):
        # Check before touching an inherited potentially locked mutex.
        if os.getpid() != self._pid:
            self._closed = True
            raise StartupReconciliationError()

    def _time(self):
        now = time.monotonic()
        _require(type(now) in (int, float) and self._last_time <= now < self._deadline)
        self._last_time = now

    def _validate(self):
        _require(not self._closed and not self._checking)
        self._time()
        self._owner.assert_active(self._database)
        _require(self._owner.epoch == self._epoch)
        self._checking = True
        try:
            value = self._observer()
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value) and inspect.getcoroutinestate(value) == inspect.CORO_CREATED:
                    value.close()
                raise StartupReconciliationError()
        finally:
            self._checking = False
        _require(type(value) is ExecutorSessionIdentity and _uuid(value.generation_id)
                 and _digest(value.artifact_digest) and value == self._executor)
        # Observer may block, revoke/re-enter this object, or replace local state.
        _require(not self._closed)
        self._time()
        self._owner.assert_active(self._database)
        _require(self._owner.epoch == self._epoch)

    def accept_operator_reconciliation(self, *, owner, report):
        """Trusted local composition only. One attempt; any failure seals closed.

        A valid report records operator acceptance, not automated fact checking.
        Possession of this Python object is not remote or human authentication.
        """
        self._same_process()
        with self._lock:
            try:
                _require(not self._attempted and not self._accepted)
                self._attempted = True
                self._validate()
                _require(owner is self._owner and type(report) is OperatorReconciliation)
                _require(type(report.challenge) is str
                         and secrets.compare_digest(report.challenge, self._challenge))
                _require(type(report.owner_epoch) is str and report.owner_epoch == self._epoch
                         and type(report.target_id) is str and report.target_id == self._target
                         and type(report.manifest_digest) is str and report.manifest_digest == self._manifest
                         and type(report.executor) is ExecutorSessionIdentity
                         and _uuid(report.executor.generation_id) and _digest(report.executor.artifact_digest)
                         and report.executor == self._executor
                         and _digest(report.report_digest)
                         and report.inventory_reconciled is True
                         and report.writers_excluded is True
                         and report.unresolved_effects_absent is True)
                self._accepted = True
            except BaseException as error:
                self._closed = True
                if isinstance(error, Exception):
                    raise StartupReconciliationError() from None
                raise

    def assert_permitted(self, *, owner, target_id, manifest_digest, executor):
        """Check each next admission/phase; never returns a reusable permit token."""
        self._same_process()
        with self._lock:
            # Normal readiness probes while the operator is reviewing do not
            # consume the single acceptance attempt. Identity/expiry failures do.
            if not self._accepted:
                try:
                    self._validate()
                except BaseException as error:
                    self._closed = True
                    if isinstance(error, Exception):
                        raise StartupReconciliationError() from None
                    raise
                raise StartupReconciliationError()
            try:
                _require(owner is self._owner
                         and type(target_id) is str and target_id == self._target
                         and type(manifest_digest) is str and manifest_digest == self._manifest
                         and type(executor) is ExecutorSessionIdentity
                         and _uuid(executor.generation_id) and _digest(executor.artifact_digest)
                         and executor == self._executor)
                self._validate()
            except BaseException as error:
                self._closed = True
                if isinstance(error, Exception):
                    raise StartupReconciliationError() from None
                raise

    def close(self):
        # Closing a child's inherited object neither unlocks nor closes its owner.
        if os.getpid() != self._pid:
            self._closed = True
            return
        with self._lock:
            self._closed = True
