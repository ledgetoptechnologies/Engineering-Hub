"""Real Linux PilotOwner; synthetic operator/identity observer, no host effects."""
from dataclasses import replace
import os
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub import startup_reconciliation as subject
from engineering_hub.startup_reconciliation import (
    ExecutorSessionIdentity, OperatorReconciliation, StartupReconciliation,
    StartupReconciliationError,
)

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='real Linux PilotOwner required')
DIGEST = 'sha256:' + 'a' * 64
TARGET = 'engineering-hub:staging'


@pytest.fixture
def session(tmp_path, monkeypatch):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    database = private / 'hub.sqlite3'
    clock = [100.0]
    monkeypatch.setattr(subject, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    identity = ExecutorSessionIdentity(str(uuid4()), DIGEST)
    observed = [identity]
    calls = []
    def observer():
        calls.append(True)
        value = observed[0]
        return value() if callable(value) else value
    with PilotOwner(database, allow_ephemeral_test_storage=True) as owner:
        kwargs = dict(owner=owner, database=database, target_id=TARGET,
                      manifest_digest=DIGEST, executor=identity, lifetime_seconds=60,
                      current_executor_identity=observer)
        gate = StartupReconciliation(**kwargs)
        report = OperatorReconciliation(gate.challenge, owner.epoch, TARGET, DIGEST,
            identity, 'sha256:' + 'b' * 64, True, True, True)
        yield SimpleNamespace(gate=gate, owner=owner, kwargs=kwargs, report=report,
                              identity=identity, observed=observed, clock=clock, calls=calls)
        gate.close()


def accept(s, gate=None, report=None):
    (gate or s.gate).accept_operator_reconciliation(owner=s.owner, report=report or s.report)


def permit(s, **changes):
    s.gate.assert_permitted(**(dict(owner=s.owner, target_id=TARGET,
        manifest_digest=DIGEST, executor=s.identity) | changes))


def test_closed_until_one_exact_manual_acceptance_and_checked_on_every_permit(session):
    s = session
    # Expected readiness probes while pending cannot poison later acceptance.
    with pytest.raises(StartupReconciliationError):
        permit(s)
    accept(s)
    before = len(s.calls)
    for _ in range(2):
        permit(s)
    assert len(s.calls) == before + 2
    s.gate.close()
    # The component never closes, advances or replaces its caller-owned owner.
    s.owner.assert_active(s.kwargs['database'])
    assert s.owner.epoch == s.report.owner_epoch


@pytest.mark.parametrize('field,value', [
    ('challenge', '0' * 64), ('owner_epoch', str(uuid4())), ('target_id', 'other:staging'),
    ('manifest_digest', 'sha256:' + 'c' * 64),
    ('executor', ExecutorSessionIdentity(str(uuid4()), DIGEST)),
    ('report_digest', True), ('inventory_reconciled', 1), ('writers_excluded', 1),
    ('unresolved_effects_absent', False), ('owner_epoch', True),
    ('executor', ExecutorSessionIdentity(True, DIGEST)),
])
def test_mismatched_or_untyped_report_is_sticky_failure(session, field, value):
    s = session
    with pytest.raises(StartupReconciliationError):
        accept(s, report=replace(s.report, **{field: value}))
    with pytest.raises(StartupReconciliationError):
        accept(s)
    with pytest.raises(StartupReconciliationError):
        permit(s)


def test_report_cannot_replay_on_new_instance_or_reaccept_same_instance(session):
    s = session
    other = StartupReconciliation(**s.kwargs)
    assert other.challenge != s.gate.challenge
    with pytest.raises(StartupReconciliationError):
        accept(s, other)
    accept(s)
    with pytest.raises(StartupReconciliationError):
        accept(s)
    with pytest.raises(StartupReconciliationError):
        permit(s)


@pytest.mark.parametrize('change', [dict(owner=object()), dict(target_id=True),
    dict(manifest_digest=DIGEST + 'x'),
    dict(executor=ExecutorSessionIdentity(str(uuid4()), DIGEST))])
def test_permit_binding_failure_is_sticky(session, change):
    accept(session)
    with pytest.raises(StartupReconciliationError):
        permit(session, **change)
    with pytest.raises(StartupReconciliationError):
        permit(session)


@pytest.mark.parametrize('elapsed', [60, 61, -1, float('nan'), float('inf')])
def test_deadline_and_clock_regression_fail_closed(session, elapsed):
    accept(session)
    session.clock[0] += elapsed
    with pytest.raises(StartupReconciliationError):
        permit(session)
    session.clock[0] = 100
    with pytest.raises(StartupReconciliationError):
        permit(session)


def test_acceptance_time_counts_and_slow_observer_cannot_extend_lifetime(session):
    s = session
    def slow():
        s.clock[0] = 160
        return s.identity
    s.observed[0] = slow
    with pytest.raises(StartupReconciliationError):
        accept(s)


@pytest.mark.parametrize('value', [False, True, 1, None,
    ExecutorSessionIdentity(str(uuid4()), DIGEST)])
def test_identity_observer_is_not_boolean_allowlist(session, value):
    accept(session)
    session.observed[0] = value
    with pytest.raises(StartupReconciliationError):
        permit(session)
    session.observed[0] = session.identity
    with pytest.raises(StartupReconciliationError):
        permit(session)


def test_owner_closure_is_seen_before_next_permit(session):
    accept(session)
    session.owner.close()
    with pytest.raises(StartupReconciliationError):
        permit(session)


def test_async_observer_and_misdeclared_coroutine_rejected_without_execution(session):
    s = session
    ran = []
    async def observer():
        ran.append(True)
        return s.identity
    with pytest.raises(StartupReconciliationError):
        StartupReconciliation(**(s.kwargs | {'current_executor_identity': observer}))
    accept(s)
    result = observer()
    s.observed[0] = result
    with pytest.raises(StartupReconciliationError):
        permit(s)
    assert result.cr_frame is None and ran == []


def test_observer_exception_sanitized_and_close_inside_observer_is_sticky(session):
    s = session
    accept(s)
    def fail():
        raise ValueError('private sensitive observer text')
    s.observed[0] = fail
    with pytest.raises(StartupReconciliationError, match='^manual staging session reconciliation required$'):
        permit(s)
    fresh = StartupReconciliation(**(s.kwargs | {'current_executor_identity': lambda: s.identity}))
    accept(s, fresh, replace(s.report, challenge=fresh.challenge))
    def close_inside():
        fresh.close()
        return s.identity
    fresh._observer = close_inside
    with pytest.raises(StartupReconciliationError):
        fresh.assert_permitted(owner=s.owner, target_id=TARGET, manifest_digest=DIGEST, executor=s.identity)


@pytest.mark.parametrize('during', ['accept', 'permit', 'pending'])
def test_owner_revoked_inside_observer_cannot_open_or_permit(session, during):
    s = session
    if during == 'permit':
        accept(s)
    def revoke_owner():
        s.owner.close()
        return s.identity
    s.observed[0] = revoke_owner
    with pytest.raises(StartupReconciliationError):
        accept(s) if during == 'accept' else permit(s)
    s.observed[0] = s.identity
    with pytest.raises(StartupReconciliationError):
        accept(s)


@pytest.mark.parametrize('during', ['accept', 'permit'])
def test_reentrant_observer_cannot_reopen_or_hide_sticky_failure(session, during):
    s = session
    if during == 'permit':
        accept(s)
    def reenter():
        with pytest.raises(StartupReconciliationError):
            accept(s) if during == 'accept' else permit(s)
        return s.identity
    s.observed[0] = reenter
    with pytest.raises(StartupReconciliationError):
        accept(s) if during == 'accept' else permit(s)
    s.observed[0] = s.identity
    with pytest.raises(StartupReconciliationError):
        permit(s)


def test_pending_identity_failure_is_sticky_not_normal_readiness(session):
    s = session
    s.observed[0] = False
    with pytest.raises(StartupReconciliationError):
        permit(s)
    s.observed[0] = s.identity
    with pytest.raises(StartupReconciliationError):
        accept(s)


def test_status_checks_only_local_deadline_and_expiry_is_sticky(session, monkeypatch):
    s = session
    accept(s)
    def forbidden():
        pytest.fail('status must not perform observer or owner I/O')
    s.observed[0] = forbidden
    monkeypatch.setattr(s.owner, 'assert_active', lambda *_: forbidden())
    assert s.gate.admission_state == 'accepted'
    s.clock[0] = 160
    assert s.gate.admission_state == 'closed'
    s.clock[0] = 100
    assert s.gate.admission_state == 'closed'


@pytest.mark.parametrize('seconds', [True, 1.0, 0, -1, 3601])
def test_constructor_rejects_invalid_lifetime(session, seconds):
    with pytest.raises(StartupReconciliationError):
        StartupReconciliation(**(session.kwargs | {'lifetime_seconds': seconds}))


def test_fork_cannot_use_parent_capability_or_touch_its_mutex(session):
    s = session
    accept(s)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        class ForbiddenLock:
            def __enter__(self):
                os.write(write_fd, b'lock-touched')
                raise AssertionError
        s.gate._lock = ForbiddenLock()
        try:
            permit(s)
        except StartupReconciliationError:
            os.write(write_fd, b'closed')
            os._exit(0)
        os._exit(2)
    os.close(write_fd)
    try:
        assert os.read(read_fd, 64) == b'closed'
        assert os.waitpid(pid, 0)[1] == 0
    finally:
        os.close(read_fd)
    permit(s)
    s.owner.assert_active(s.kwargs['database'])
