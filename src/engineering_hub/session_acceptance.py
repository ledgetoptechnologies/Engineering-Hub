"""Opt-in, process-local human channel for a manually reconciled staging session.

No routes are installed at import, and no worker, launcher or service is started.
Trusted composition supplies operator authority, retained evidence and durable
audit storage. This module neither establishes those facts nor reloads approval.
"""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import hashlib
import inspect
import json
import os
import re
import secrets
import threading
import time
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import AuthConfig, SessionStore, require_human
from .pilot_ownership import PilotOwner
from .startup_reconciliation import (ExecutorSessionIdentity, OperatorReconciliation,
                                     StartupReconciliation)

_BODY_READ_TIMEOUT_SECONDS = 2.0


class SessionAcceptanceError(RuntimeError):
    def __init__(self, status=409):
        self.status = status
        super().__init__('staging session acceptance unavailable')


def _require(value, status=409):
    if not value:
        raise SessionAcceptanceError(status)


def _sync_callable(value):
    return (callable(value) and not any(check(candidate) for candidate in
            (value, getattr(value, '__call__', None)) for check in
            (inspect.iscoroutinefunction, inspect.isasyncgenfunction)))


@dataclass(frozen=True)
class SessionAcceptanceAudit:
    """Intent/result metadata; no challenge, credential, or claim of fact proof."""
    event: Literal['intent', 'result']
    outcome: Literal['accept', 'decline', 'accepted', 'declined', 'failed']
    request_id: str
    revision: int
    actor: str
    owner_epoch: str
    target_id: str
    manifest_digest: str
    report_digest: str
    executor: ExecutorSessionIdentity
    execution_authorization: bool = False


class SessionAcceptanceBroker:
    """One request for one existing gate, created only by trusted composition.

    ``operator_actor`` must come from independent owner/operator enrollment, NOT
    the global human allowlist or the first browser to arrive. ``audit`` must
    synchronously commit the supplied record durably and return exactly True.
    Its correctness/durability is a trusted integration obligation, not something
    this class can infer from a boolean acknowledgement.

    The broker lock serializes this request only; it does not serialize direct
    gate permits. Intent is committed BEFORE opening. Result audit follows gate
    acceptance. If it fails the gate closes, but a concurrently accepted effect
    is not undone. Integration must serialize acceptance with admissions on the
    owned Hub event loop or provide a real handover barrier. No rollback or
    transaction across audit storage and the in-memory gate is claimed.
    """
    def __init__(self, *, gate, owner, target_id, manifest_digest, executor,
                 report, evidence, operator_actor, audit, lifetime_seconds=600):
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._closed = False
        self._processing = False
        self._preparing = None
        self._state = 'pending'
        self._revision = 1
        self._gate = gate
        try:
            _require(type(gate) is StartupReconciliation and type(owner) is PilotOwner)
            _require(type(operator_actor) is str and re.fullmatch(
                r'human:(?:[1-9][0-9]{0,19}|cf:[0-9a-f]{64})', operator_actor))
            _require(_sync_callable(audit))
            _require(type(lifetime_seconds) is int and 1 <= lifetime_seconds <= 3600)
            _require(type(evidence) is str and 0 < len(evidence.encode('utf-8')) <= 8192
                     and '\x00' not in evidence)
            _require(type(report) is OperatorReconciliation
                     and type(executor) is ExecutorSessionIdentity
                     and report.executor == executor
                     and report.owner_epoch == owner.epoch
                     and report.target_id == target_id
                     and report.manifest_digest == manifest_digest
                     and report.inventory_reconciled is True
                     and report.writers_excluded is True
                     and report.unresolved_effects_absent is True
                     and report.report_digest == 'sha256:' + hashlib.sha256(evidence.encode()).hexdigest()
                     and report.challenge == gate.challenge)
            binding = gate.assert_local_binding if gate.async_admission_required else gate.assert_binding
            binding(owner=owner, target_id=target_id, manifest_digest=manifest_digest)
            self._owner, self._target, self._manifest = owner, target_id, manifest_digest
            self._executor, self._report = executor, report
            self._evidence, self._operator, self._audit = evidence, operator_actor, audit
            self._id = secrets.token_hex(32)
            self._last_time = time.monotonic()
            _require(type(self._last_time) in (int, float) and 0 <= self._last_time < float('inf'))
            self._deadline = self._last_time + lifetime_seconds
        except BaseException as error:
            self._seal()
            if isinstance(error, Exception):
                raise SessionAcceptanceError() from None
            raise

    def _seal(self):
        self._closed = True
        self._state = 'closed'
        if type(self._gate) is StartupReconciliation:
            self._gate.close()

    def _process(self):
        # Never acquire an inherited mutex after fork.
        if os.getpid() != self._pid:
            self._seal()
            raise SessionAcceptanceError(410)

    def _authorize(self, actor):
        _require(type(actor) is str and actor.isascii()
                 and secrets.compare_digest(actor, self._operator), 403)

    def _current(self):
        _require(not self._closed, 410)
        now = time.monotonic()
        _require(type(now) in (int, float) and self._last_time <= now < self._deadline, 410)
        self._last_time = now
        binding = (self._gate.assert_local_binding if self._gate.async_admission_required
                   else self._gate.assert_binding)
        binding(owner=self._owner, target_id=self._target, manifest_digest=self._manifest)

    def _accept_subject(self, actor):
        from .startup_reconciliation import AdmissionSubject
        canonical = json.dumps(dict(actor=actor, request_id=self._id,
            revision=self._revision, report_digest=self._report.report_digest),
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        return AdmissionSubject(kind='operator_accept', target_id=self._target,
            manifest_digest=self._manifest, subject_digest='sha256:' + hashlib.sha256(canonical).hexdigest(),
            job_id=None, run_id=None, phase='operator_accept', action='accept')

    def _request(self, request_id, expected_revision, action):
        _require(type(request_id) is str and request_id.isascii()
                 and secrets.compare_digest(request_id, self._id)
                 and type(expected_revision) is int and expected_revision == self._revision
                 and action in ('accept', 'decline') and type(action) is str
                 and self._state == 'pending')

    async def decide_async(self, *, actor, request_id, expected_revision, action):
        """Prepare remote identity without broker locks, then decide without yield.

        A reservation is not acceptance. Gate-owned observation I/O must drain
        before cancellation returns; any failed preparation seals this broker.
        Decline and explicit synchronous mode retain their original local path.
        """
        self._process()
        self._authorize(actor)
        if not self._gate.async_admission_required or action == 'decline':
            return self.decide(actor=actor, request_id=request_id,
                               expected_revision=expected_revision, action=action)
        with self._lock:
            self._request(request_id, expected_revision, action)
            _require(self._preparing is None and not self._processing)
            try:
                self._current()
                subject = self._accept_subject(actor)
                retained = (self._owner, self._report, self._executor, self._evidence,
                            self._operator, self._deadline, self._id, self._revision)
                token = object()
                self._preparing = token
            except BaseException as error:
                self._seal()
                if isinstance(error, Exception):
                    raise SessionAcceptanceError(503) from None
                raise
        try:
            prepared = await self._gate.prepare_admission(
                owner=self._owner, subject=subject, deadline=self._deadline)
            with self._lock:
                self._process()
                self._authorize(actor)
                self._request(request_id, expected_revision, action)
                _require(self._preparing is token and not self._processing
                         and self._owner is retained[0] and self._report is retained[1]
                         and (self._executor, self._evidence, self._operator, self._deadline,
                              self._id, self._revision) == retained[2:])
                self._current()
                _require(self._accept_subject(actor) == subject)
                # Exactly one synchronous segment: scope, intent, opening,
                # result. No observation or await occurs while holding this lock.
                with self._gate.admission_scope(prepared, owner=self._owner, subject=subject):
                    return self.decide(actor=actor, request_id=request_id,
                                       expected_revision=expected_revision, action=action)
        except BaseException as error:
            with self._lock:
                self._seal()
            if isinstance(error, Exception):
                raise SessionAcceptanceError(503) from None
            raise
        finally:
            with self._lock:
                if self._preparing is token:
                    self._preparing = None

    def _write_audit(self, event, outcome, actor):
        record = SessionAcceptanceAudit(event, outcome, self._id, self._revision,
            actor, self._report.owner_epoch, self._target, self._manifest,
            self._report.report_digest, self._executor)
        value = self._audit(record)
        if inspect.iscoroutine(value) and inspect.getcoroutinestate(value) == inspect.CORO_CREATED:
            value.close()
        _require(value is True, 503)
        self._current()  # Callback can block, revoke, or re-enter.

    def snapshot(self, *, actor):
        self._process()
        self._authorize(actor)
        with self._lock:
            try:
                _require(not self._processing)
                self._current()
                return dict(request_id=self._id, revision=self._revision,
                    state=self._state, evidence=self._evidence,
                    report_digest=self._report.report_digest,
                    expires_in_seconds=max(0, int(self._deadline - self._last_time)),
                    execution_authorization=False)
            except BaseException as error:
                self._seal()
                if isinstance(error, Exception):
                    raise SessionAcceptanceError(410) from None
                raise

    def decide(self, *, actor, request_id, expected_revision, action):
        self._process()
        self._authorize(actor)
        with self._lock:
            # Only same-thread callback reentry can acquire this RLock while
            # processing. Other threads wait and subsequently take the normal
            # stale/replay path below, without revoking an accepted session.
            if self._processing:
                self._seal()
                raise SessionAcceptanceError(503)
            # Stale/foreign requests do not revoke an already accepted session.
            self._request(request_id, expected_revision, action)
            self._processing = True
            intent_recorded = result_attempted = False
            try:
                self._current()
                if action == 'accept' and self._gate.async_admission_required:
                    self._gate.assert_active_subject(owner=self._owner,
                                                     subject=self._accept_subject(actor))
                self._write_audit('intent', action, actor)
                intent_recorded = True
                if action == 'accept':
                    # Audit callbacks may consume freshness without yielding.
                    if self._gate.async_admission_required:
                        self._authorize(actor)
                        self._request(request_id, expected_revision, action)
                        self._gate.assert_active_subject(owner=self._owner,
                                                         subject=self._accept_subject(actor))
                    self._gate.accept_operator_reconciliation(owner=self._owner, report=self._report)
                    self._state = 'accepted'
                    result_attempted = True
                    self._write_audit('result', 'accepted', actor)
                    if self._gate.async_admission_required:
                        # A durable result may itself consume the remaining
                        # proof window. Retain it as uncertainty and seal rather
                        # than returning a late accepted response.
                        self._gate.assert_active_subject(owner=self._owner,
                                                         subject=self._accept_subject(actor))
                else:
                    self._gate.close()
                    self._state = 'declined'
                    # _current must not require an active gate after deliberate close.
                    record = SessionAcceptanceAudit('result', 'declined', self._id,
                        self._revision, actor, self._report.owner_epoch, self._target,
                        self._manifest, self._report.report_digest, self._executor)
                    result_attempted = True
                    value = self._audit(record)
                    if inspect.iscoroutine(value) and inspect.getcoroutinestate(value) == inspect.CORO_CREATED:
                        value.close()
                    _require(value is True and not self._closed, 503)
                self._revision += 1
                return dict(request_id=self._id, revision=self._revision,
                            state=self._state, execution_authorization=False)
            except BaseException as error:
                self._seal()
                # If the opening failed after a confirmed intent, record the
                # first result as failed. Never retry an uncertain audit call.
                if intent_recorded and not result_attempted:
                    try:
                        value = self._audit(SessionAcceptanceAudit('result', 'failed', self._id,
                            self._revision, actor, self._report.owner_epoch, self._target,
                            self._manifest, self._report.report_digest, self._executor))
                        if inspect.iscoroutine(value) and inspect.getcoroutinestate(value) == inspect.CORO_CREATED:
                            value.close()
                    except BaseException:
                        pass
                if isinstance(error, Exception):
                    raise SessionAcceptanceError(503) from None
                raise
            finally:
                self._processing = False

    def close(self):
        if os.getpid() != self._pid:
            self._seal()
            return
        with self._lock:
            self._seal()

    def assert_binding(self, *, gate, owner, target_id, manifest_digest):
        """Check a trusted composition's exact gate/owner without granting it.

        Not caller-selected remote authority. A failed handover seals this
        broker's retained gate, never closes another caller-supplied gate/owner.
        """
        self._process()
        with self._lock:
            try:
                _require(not self._processing and gate is self._gate and owner is self._owner
                         and type(target_id) is str and target_id == self._target
                         and type(manifest_digest) is str and manifest_digest == self._manifest)
                self._current()
            except BaseException as error:
                self._seal()
                if isinstance(error, Exception):
                    raise SessionAcceptanceError() from None
                raise


class _Answer(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    request_id: str = Field(pattern=r'^[0-9a-f]{64}$', min_length=64, max_length=64)
    expected_revision: int = Field(ge=1, le=2**31 - 1)
    action: Literal['accept', 'decline']


def attach_session_acceptance_routes(app, *, broker, config, sessions):
    """Explicit opt-in attachment; uses the real existing human/CSRF boundary.

    Do not attach to an agent ingress, select an operator from request data, or
    expose report creation. This is source-only until reviewed composition wires
    the same gate and an independently enrolled operator/durable audit sink.
    """
    _require(type(broker) is SessionAcceptanceBroker and type(config) is AuthConfig
             and type(sessions) is SessionStore)
    path = '/api/hub/v1/staging/session-reconciliation'
    _require(not any(getattr(route, 'path', None) == path for route in app.routes))

    def actor(request, mutation=False):
        if request.headers.get('authorization'):
            raise HTTPException(403, detail={'code': 'human_session_required'})
        identity = require_human(request, config, sessions, mutation=mutation)
        try:
            broker._authorize(identity)
        except SessionAcceptanceError:
            raise HTTPException(403, detail={'code': 'operator_required'}) from None
        return identity

    def unavailable(error):
        raise HTTPException(error.status, detail={'code': 'session_acceptance_unavailable'},
                            headers={'Cache-Control': 'no-store'}) from None

    @app.get(path)
    async def current(request: Request):
        identity = actor(request)
        try:
            from fastapi.responses import JSONResponse
            return JSONResponse(broker.snapshot(actor=identity), headers={'Cache-Control': 'no-store'})
        except SessionAcceptanceError as error:
            unavailable(error)

    @app.post(path)
    async def answer(request: Request):
        identity = actor(request, mutation=True)
        try:
            _require(request.headers.get('content-type', '').split(';')[0].strip().lower()
                     == 'application/json', 415)
            raw = bytearray()
            try:
                async with asyncio.timeout(_BODY_READ_TIMEOUT_SECONDS):
                    async for chunk in request.stream():
                        _require(len(raw) + len(chunk) <= 2048, 413)
                        raw.extend(chunk)
            except TimeoutError:
                raise SessionAcceptanceError(408) from None
            def unique(pairs):
                value = {}
                for key, item in pairs:
                    _require(key not in value, 422)
                    value[key] = item
                return value
            try:
                payload = _Answer.model_validate(json.loads(raw, object_pairs_hook=unique))
            except Exception:
                raise SessionAcceptanceError(422) from None
            # No await between gate opening and synchronous result audit. This
            # serializes event-loop admissions, not arbitrary other threads.
            from fastapi.responses import JSONResponse
            return JSONResponse(await broker.decide_async(actor=identity, **payload.model_dump()),
                                headers={'Cache-Control': 'no-store'})
        except SessionAcceptanceError as error:
            unavailable(error)
