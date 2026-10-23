"""In-memory admission boundary for an explicitly reconciled manual session.

This component starts nothing and proves no restore, inventory or writer-exclusion
facts. Only trusted operator composition may accept its one fresh challenge.
There is deliberately no CLI, persisted approval, environment or HTTP opener.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import inspect
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from uuid import UUID

from .pilot_ownership import PilotOwner
from .lifecycle import finish_owned


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


@dataclass(frozen=True)
class AdmissionSubject:
    """Closed operation metadata; possession of this value grants nothing."""
    kind: str
    target_id: str
    manifest_digest: str
    subject_digest: str
    job_id: str | None = None
    run_id: str | None = None
    phase: str | None = None
    action: str | None = None

    def __post_init__(self):
        _require(type(self.kind) is str
                 and self.kind in {'operator_accept', 'claim', 'command', 'reconcile'}
                 and self.target_id == 'engineering-hub:staging'
                 and _digest(self.manifest_digest) and _digest(self.subject_digest))
        _require(all(value is None or type(value) is str and 0 < len(value) <= 128
                     and value.isascii() and all(33 <= ord(c) <= 126 for c in value)
                     for value in (self.phase, self.action)))
        if self.kind == 'operator_accept':
            _require(self.job_id is None and self.run_id is None
                     and self.phase == 'operator_accept' and self.action == 'accept')
        else:
            _require(_uuid(self.job_id))
            if self.kind == 'claim':
                _require(self.run_id is None)
            else:
                _require(_uuid(self.run_id))
            _require(self.phase is not None and self.action is not None)


@dataclass(frozen=True, eq=False)
class _PreparedAdmission:
    gate: object
    owner: object
    pid: int
    loop: object
    task: object
    epoch: str
    generation: int
    subject: AdmissionSubject
    deadline: float
    verified_wall_ms: int
    expires_wall_ms: int


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
                 lifetime_seconds, current_executor_identity, async_admission=False):
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._closed = False
        self._accepted = False
        self._attempted = False
        self._checking = False
        self._challenge = secrets.token_hex(32)
        self._created = time.monotonic()
        self._async_admission = async_admission
        self._loop = None
        self._generation = 0
        self._last_scope_wall_ms = 0
        self._preparing = self._prepared = self._active_scope = None
        self._scope_transitioned = None
        self._proof_nonces, self._proof_observations = set(), set()
        try:
            _require(type(async_admission) is bool)
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
            if self._async_admission:
                self._validate_local()
            else:
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
        if self._async_admission:
            self._assert_scope()
            return
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

    @property
    def async_admission(self):
        return self._async_admission

    @property
    def async_admission_required(self):
        return self._async_admission

    def _validate_local(self):
        try:
            _require(not self._closed)
            self._time()
            self._owner.assert_active(self._database)
            _require(self._owner.epoch == self._epoch)
        except BaseException as error:
            self._closed = True
            if isinstance(error, Exception):
                raise StartupReconciliationError() from None
            raise

    def assert_local_binding(self, *, owner, target_id, manifest_digest):
        """Local metadata only: never fresh identity or effect admission."""
        self._same_process()
        with self._lock:
            _require(owner is self._owner and target_id == self._target
                     and manifest_digest == self._manifest)
            self._validate_local()

    def assert_local_permitted(self, *, owner, target_id, manifest_digest):
        """Local lifecycle veto only; not a replacement for consumed admission."""
        self.assert_local_binding(owner=owner, target_id=target_id, manifest_digest=manifest_digest)
        _require(self._accepted)

    def _async_context(self):
        self._same_process()
        try:
            loop, task = asyncio.get_running_loop(), asyncio.current_task()
        except RuntimeError:
            raise StartupReconciliationError() from None
        _require(task is not None and (self._loop is None or loop is self._loop))
        if self._loop is None:
            self._loop = loop
        return loop, task

    def _assert_scope(self, *, owner=None, subject=None):
        loop, task = self._async_context()
        scope = self._active_scope
        _require(self._async_admission and scope is not None and scope.gate is self
                 and scope.pid == os.getpid() and scope.loop is loop and scope.task is task
                 and scope.owner is self._owner and scope.epoch == self._epoch
                 and (scope.generation == self._generation
                      or self._scope_transitioned is scope and scope.generation == 0
                      and self._generation == 1 and self._accepted
                      and scope.subject.kind == 'operator_accept')
                 and (owner is None or owner is self._owner)
                 and (subject is None or type(subject) is AdmissionSubject and scope.subject == subject))
        self._validate_local()
        self._scope_time(scope)
        return scope

    def _scope_time(self, scope):
        # Sample AFTER owner/proof/local reads. A monotonic deadline alone does
        # not retain an original absolute report/launch/signed-expiry veto when
        # wall time jumps forward during a slow audit or local proof read.
        try:
            wall = time.time()
            _require(type(wall) in (int, float) and math.isfinite(wall) and wall > 0)
            wall_ms = int(wall * 1000)
            _require(max(scope.verified_wall_ms, self._last_scope_wall_ms) <= wall_ms < scope.expires_wall_ms
                     and time.monotonic() < scope.deadline)
            self._last_scope_wall_ms = wall_ms
        except BaseException as error:
            self.close()
            if isinstance(error, Exception):
                raise StartupReconciliationError() from None
            raise

    def assert_active_subject(self, *, owner, subject):
        """Check the exact consumed scope; metadata alone never satisfies this."""
        self._same_process()
        with self._lock:
            self._assert_scope(owner=owner, subject=subject)

    assert_scope = assert_active_subject

    async def prepare_admission(self, *, owner, subject, deadline):
        """Await one owned fresh exchange without holding gate or SQL locks."""
        from .current_executor_observation import VerifiedExecutorObservation
        loop, task = self._async_context()
        with self._lock:
            _require(self._async_admission and type(subject) is AdmissionSubject
                     and owner is self._owner and subject.target_id == self._target
                     and subject.manifest_digest == self._manifest
                     and type(deadline) in (int, float) and math.isfinite(deadline)
                     and self._preparing is None and self._prepared is None and self._active_scope is None)
            self._validate_local()
            _require((subject.kind == 'operator_accept' and not self._accepted and not self._attempted)
                     or (subject.kind != 'operator_accept' and self._accepted))
            started = time.monotonic()
            ceiling = min(float(deadline), self._deadline)
            _require(started < ceiling)
            token, generation = object(), self._generation
            self._preparing = token
        try:
            proof = await finish_owned(loop.run_in_executor(None, self._observer))
            if inspect.iscoroutine(proof) and inspect.getcoroutinestate(proof) == inspect.CORO_CREATED:
                proof.close()
            with self._lock:
                _require(self._preparing is token and self._generation == generation)
                self._validate_local()
                _require(type(proof) is VerifiedExecutorObservation
                         and type(proof.identity) is ExecutorSessionIdentity and proof.identity == self._executor
                         and type(proof.nonce) is str and re.fullmatch('[0-9a-f]{64}', proof.nonce)
                         and _uuid(proof.observation_id)
                         and all(type(t) in (int, float) and math.isfinite(t)
                                 for t in (proof.started_monotonic, proof.expires_monotonic))
                         and started <= proof.started_monotonic <= time.monotonic()
                         and proof.started_monotonic < proof.expires_monotonic
                         and type(proof.verified_wall_ms) is int
                         and type(proof.expires_wall_ms) is int
                         and 0 < proof.verified_wall_ms < proof.expires_wall_ms)
                _require(len(self._proof_nonces) < 4096 and proof.nonce not in self._proof_nonces
                         and proof.observation_id not in self._proof_observations)
                self._proof_nonces.add(proof.nonce)
                self._proof_observations.add(proof.observation_id)
                ceiling = min(ceiling, proof.expires_monotonic)
                _require(time.monotonic() < ceiling and self._async_context() == (loop, task))
                prepared = _PreparedAdmission(self, owner, self._pid, loop, task, self._epoch,
                    generation, subject, ceiling, proof.verified_wall_ms, proof.expires_wall_ms)
                self._scope_time(prepared)
                self._prepared = prepared
                def unconsumed_after_yield():
                    if self._prepared is prepared:
                        self.close()
                loop.call_soon(unconsumed_after_yield)
                return prepared
        except BaseException as error:
            self.close()
            if isinstance(error, Exception):
                raise StartupReconciliationError() from None
            raise
        finally:
            with self._lock:
                if self._preparing is token:
                    self._preparing = None

    @contextmanager
    def admission_scope(self, prepared, *, owner, subject):
        """Consume once, on this task/loop, without yielding until local completion."""
        self._same_process()
        entered = False
        try:
            with self._lock:
                loop, task = self._async_context()
                _require(self._async_admission and type(prepared) is _PreparedAdmission
                         and self._prepared is prepared and self._active_scope is None
                         and prepared.gate is self and prepared.owner is owner is self._owner
                         and prepared.pid == os.getpid() and prepared.loop is loop and prepared.task is task
                         and prepared.epoch == self._epoch and prepared.generation == self._generation
                         and type(subject) is AdmissionSubject and prepared.subject == subject)
                self._prepared = None
                self._active_scope = prepared
                self._assert_scope(owner=owner, subject=subject)
                entered = True
                def yielded():
                    if self._active_scope is prepared:
                        self.close()
                tripwire = loop.call_soon(yielded)
            try:
                yield
            finally:
                tripwire.cancel()
        except BaseException:
            self.close()
            raise
        finally:
            if entered:
                with self._lock:
                    if self._active_scope is prepared:
                        self._active_scope = None
                    if self._scope_transitioned is prepared:
                        self._scope_transitioned = None

    def accept_operator_reconciliation(self, *, owner, report):
        """Trusted local composition only. One attempt; any failure seals closed.

        A valid report records operator acceptance, not automated fact checking.
        Possession of this Python object is not remote or human authentication.
        """
        self._same_process()
        with self._lock:
            try:
                _require(not self._attempted and not self._accepted)
                if self._async_admission:
                    scope = self._assert_scope(owner=owner)
                    _require(scope.subject.kind == 'operator_accept' and self._generation == 0)
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
                if self._async_admission:
                    self._assert_scope(owner=owner)
                self._accepted = True
                if self._async_admission:
                    # Sole permitted generation transition within one consumed
                    # operator scope; result-audit checks retain this exact scope.
                    self._generation = 1
                    self._scope_transitioned = scope
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
                if self._async_admission:
                    _require(self._assert_scope(owner=owner).subject.kind != 'operator_accept')
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

    def assert_binding(self, *, owner, target_id, manifest_digest):
        """Validate a pending/accepted composition binding without opening it."""
        self._same_process()
        with self._lock:
            try:
                _require(owner is self._owner and type(target_id) is str and target_id == self._target
                         and type(manifest_digest) is str and manifest_digest == self._manifest)
                self._validate()
            except BaseException as error:
                self._closed = True
                if isinstance(error, Exception):
                    raise StartupReconciliationError() from None
                raise

    def assert_current_permitted(self, *, owner, target_id, manifest_digest):
        """Recheck the independently observed executor; do not accept an echo from a caller."""
        self.assert_permitted(owner=owner, target_id=target_id, manifest_digest=manifest_digest,
                              executor=self._executor)

    @property
    def admission_state(self):
        """Sanitized local lifecycle only, not readiness or current-host proof."""
        self._same_process()
        with self._lock:
            if not self._closed:
                try:
                    self._time()  # Local monotonic deadline only; no observer/owner I/O.
                except StartupReconciliationError:
                    self._closed = True
            return 'closed' if self._closed else 'accepted' if self._accepted else 'pending'

    def close(self):
        # Closing a child's inherited object neither unlocks nor closes its owner.
        if os.getpid() != self._pid:
            self._closed = True
            return
        with self._lock:
            self._closed = True
            self._generation += 1
            self._prepared = self._active_scope = None
            self._scope_transitioned = None
