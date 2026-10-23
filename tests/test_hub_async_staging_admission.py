"""Owned async preflight and same-turn admission, synthetic host facts only."""
import asyncio
from dataclasses import FrozenInstanceError, replace
import os
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip('fcntl', reason='Linux PilotOwner fixtures')

from engineering_hub.current_executor_observation import VerifiedExecutorObservation
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.startup_reconciliation import (
    AdmissionSubject, ExecutorSessionIdentity, OperatorReconciliation,
    StartupReconciliation, StartupReconciliationError,
)


@pytest.fixture
def state(tmp_path):
    tmp_path.chmod(0o700)
    with PilotOwner(tmp_path / 'hub.sqlite3', allow_ephemeral_test_storage=True) as owner:
        identity = ExecutorSessionIdentity(str(uuid4()), 'sha256:' + 'b' * 64)
        calls = []
        def observe():
            started = time.monotonic()
            calls.append(threading.get_ident())
            return VerifiedExecutorObservation(identity, uuid4().hex + uuid4().hex,
                                                str(uuid4()), started, started + 2)
        gate = StartupReconciliation(owner=owner, database=tmp_path / 'hub.sqlite3',
            target_id='engineering-hub:staging', manifest_digest='sha256:' + 'c' * 64,
            executor=identity, lifetime_seconds=30, current_executor_identity=observe,
            async_admission=True)
        result = SimpleNamespace(owner=owner, gate=gate, identity=identity, calls=calls)
        try:
            yield result
        finally:
            gate.close()


def subject(state, kind='operator_accept', **changes):
    return AdmissionSubject(kind, 'engineering-hub:staging', 'sha256:' + 'c' * 64,
        'sha256:' + 'd' * 64, **(dict(phase='operator_accept', action='accept') if kind == 'operator_accept'
        else dict(job_id=str(uuid4()), run_id=None if kind == 'claim' else str(uuid4()),
                  phase=kind, action='validate')) | changes)


def report(state):
    return OperatorReconciliation(state.gate.challenge, state.owner.epoch,
        'engineering-hub:staging', 'sha256:' + 'c' * 64, state.identity,
        'sha256:' + 'e' * 64, True, True, True)


async def accept(state):
    item, evidence = subject(state), report(state)
    prepared = await state.gate.prepare_admission(owner=state.owner, subject=item,
                                                 deadline=time.monotonic() + 2)
    with state.gate.admission_scope(prepared, owner=state.owner, subject=item):
        state.gate.assert_active_subject(owner=state.owner, subject=item)
        state.gate.accept_operator_reconciliation(owner=state.owner, report=evidence)
        state.gate.assert_active_subject(owner=state.owner, subject=item)
    return item, prepared


def test_async_construction_and_metadata_are_not_observation_or_permission(state):
    state.gate.assert_local_binding(owner=state.owner, target_id='engineering-hub:staging',
                                   manifest_digest='sha256:' + 'c' * 64)
    assert state.calls == [] and state.gate.async_admission_required
    with pytest.raises(StartupReconciliationError):
        state.gate.assert_local_permitted(owner=state.owner, target_id='engineering-hub:staging',
                                         manifest_digest='sha256:' + 'c' * 64)


@pytest.mark.parametrize('method', ['accept', 'binding', 'effect'])
async def test_every_legacy_authority_entry_denies_without_scope(state, method):
    with pytest.raises(StartupReconciliationError):
        if method == 'accept':
            state.gate.accept_operator_reconciliation(owner=state.owner, report=report(state))
        elif method == 'binding':
            state.gate.assert_binding(owner=state.owner, target_id='engineering-hub:staging',
                                      manifest_digest='sha256:' + 'c' * 64)
        else:
            state.gate.assert_current_permitted(owner=state.owner, target_id='engineering-hub:staging',
                                               manifest_digest='sha256:' + 'c' * 64)
    assert not state.calls and state.gate.admission_state == 'closed'


async def test_pending_to_accepted_transition_and_distinct_new_operation(state):
    _, original = await accept(state)
    assert state.gate.admission_state == 'accepted' and len(state.calls) == 1
    assert state.calls[0] != threading.get_ident()
    item = subject(state, 'command')
    prepared = await state.gate.prepare_admission(owner=state.owner, subject=item,
                                                 deadline=time.monotonic() + 2)
    with pytest.raises(FrozenInstanceError):
        prepared.deadline += 100
    with state.gate.admission_scope(prepared, owner=state.owner, subject=item):
        state.gate.assert_active_subject(owner=state.owner, subject=item)
        state.gate.assert_current_permitted(owner=state.owner, target_id=item.target_id,
                                           manifest_digest=item.manifest_digest)
    assert len(state.calls) == 2
    with pytest.raises(StartupReconciliationError):
        with state.gate.admission_scope(original, owner=state.owner, subject=subject(state)):
            pytest.fail('Replayed acceptance')


@pytest.mark.parametrize('change', ['digest', 'task', 'yield-before', 'yield-inside', 'owner', 'pid'])
async def test_preparation_is_not_transferable_or_reusable_authority(state, change, monkeypatch):
    item = subject(state)
    prepared = await state.gate.prepare_admission(owner=state.owner, subject=item,
                                                 deadline=time.monotonic() + 2)
    if change == 'yield-before':
        await asyncio.sleep(0)
    if change == 'pid':
        real = os.getpid()
        monkeypatch.setattr('engineering_hub.startup_reconciliation.os.getpid', lambda: real + 1)
    async def consume():
        with state.gate.admission_scope(prepared,
                owner=object() if change == 'owner' else state.owner,
                subject=replace(item, subject_digest='sha256:' + 'f' * 64) if change == 'digest' else item):
            if change == 'yield-inside':
                await asyncio.sleep(0)
            state.gate.assert_active_subject(owner=state.owner, subject=item)
    with pytest.raises(StartupReconciliationError):
        if change == 'task':
            await asyncio.create_task(consume())
        else:
            await consume()


async def test_network_preparation_is_responsive_and_cancellation_drains_thread(state):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = state.gate._observer
    def blocked():
        entered.set()
        try:
            assert release.wait(2)
            return original()
        finally:
            finished.set()
    state.gate._observer = blocked
    async def attempt():
        return await state.gate.prepare_admission(owner=state.owner, subject=subject(state),
                                                  deadline=time.monotonic() + 2)
    task = asyncio.create_task(attempt())
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done() and not finished.is_set()
        state.gate.assert_local_binding(owner=state.owner, target_id='engineering-hub:staging',
                                        manifest_digest='sha256:' + 'c' * 64)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() and state.gate.admission_state == 'closed'
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('change', ['identity', 'expiry', 'future-start', 'close', 'deadline'])
async def test_failure_or_late_proof_never_creates_admission(state, change):
    original = state.gate._observer
    def changed():
        proof = original()
        if change == 'identity':
            return replace(proof, identity=ExecutorSessionIdentity(str(uuid4()), proof.identity.artifact_digest))
        if change == 'expiry': return replace(proof, expires_monotonic=proof.started_monotonic)
        if change == 'future-start': return replace(proof, started_monotonic=proof.started_monotonic + 1)
        if change == 'close': state.gate.close()
        if change == 'deadline': time.sleep(.03)
        return proof
    state.gate._observer = changed
    with pytest.raises(StartupReconciliationError):
        await state.gate.prepare_admission(owner=state.owner, subject=subject(state),
            deadline=time.monotonic() + (.01 if change == 'deadline' else 2))
    assert state.gate.admission_state == 'closed'


@pytest.mark.parametrize('changes', [dict(kind='other'), dict(job_id=str(uuid4())),
    dict(phase=None), dict(action='other'), dict(subject_digest='not-a-digest')])
def test_closed_subject_rejects_ambiguous_operator_metadata(state, changes):
    with pytest.raises(StartupReconciliationError):
        replace(subject(state), **changes)


async def test_bare_identity_or_created_coroutine_is_not_a_verified_proof(state):
    started = []
    async def unexpected():
        started.append(True)
        return state.identity
    state.gate._observer = unexpected  # Deliberate trusted-callback declaration error.
    with pytest.raises(StartupReconciliationError):
        await state.gate.prepare_admission(owner=state.owner, subject=subject(state),
                                           deadline=time.monotonic() + 2)
    assert started == [] and state.gate.admission_state == 'closed'


async def test_consumed_scope_expiry_and_generation_change_deny(state):
    item = subject(state)
    prepared = await state.gate.prepare_admission(owner=state.owner, subject=item,
                                                 deadline=time.monotonic() + 2)
    with state.gate.admission_scope(prepared, owner=state.owner, subject=item):
        state.gate._generation += 1  # Synthetic local generation invalidation.
        with pytest.raises(StartupReconciliationError):
            state.gate.assert_active_subject(owner=state.owner, subject=item)


async def test_accepted_metadata_does_not_authorize_scope_free_effect(state):
    await accept(state)
    state.gate.assert_local_permitted(owner=state.owner, target_id='engineering-hub:staging',
                                     manifest_digest='sha256:' + 'c' * 64)
    with pytest.raises(StartupReconciliationError):
        state.gate.assert_current_permitted(owner=state.owner, target_id='engineering-hub:staging',
                                           manifest_digest='sha256:' + 'c' * 64)
    assert len(state.calls) == 1


def test_foreign_process_scope_check_does_not_touch_inherited_mutex(state):
    class Forbidden:
        def __enter__(self): pytest.fail('inherited mutex entered')
        def __exit__(self, *args): pytest.fail('inherited mutex released')
    state.gate._pid += 1
    state.gate._lock = Forbidden()
    with pytest.raises(StartupReconciliationError):
        state.gate.assert_active_subject(owner=state.owner, subject=subject(state))
