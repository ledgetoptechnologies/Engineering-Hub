"""Async effect boundaries with real owned SQLite, synthetic executor identity.

No host deployment or independent Engine/identity attestation is proved here.
"""
import asyncio
import hashlib
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path
from uuid import uuid4

import pytest

from engineering_hub.current_executor_observation import VerifiedExecutorObservation
from engineering_hub.session_acceptance import SessionAcceptanceBroker
from engineering_hub.startup_reconciliation import ExecutorSessionIdentity, OperatorReconciliation, StartupReconciliation
from engineering_hub.repository import HubError
from tests.test_hub_queued_pilot import rig, submit, machine_job

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='real Linux owned SQLite')


@contextmanager
def async_pilot_case(rig):
    state = SimpleNamespace(calls=0, block_at=None, entered=threading.Event(),
                            release=threading.Event(), delay=0, threads=[], reject=False)
    target = rig.bridge.target
    identity = ExecutorSessionIdentity(str(uuid4()), 'sha256:' + 'e' * 64)
    def observe():
        state.calls += 1
        state.threads.append(threading.get_ident())
        started = time.monotonic()
        if state.calls == state.block_at:
            state.entered.set()
            if not state.release.wait(3):
                raise TimeoutError('Synthetic observer timeout')
        if state.delay:
            time.sleep(state.delay)
        if state.reject:
            raise RuntimeError('Synthetic observation rejected; not readiness evidence')
        verified_wall_ms = int(time.time() * 1000)
        return VerifiedExecutorObservation(identity, secrets.token_hex(32), str(uuid4()),
            started, started + 5, verified_wall_ms=verified_wall_ms,
            expires_wall_ms=verified_wall_ms + 5000)
    gate = StartupReconciliation(owner=rig.owner, database=rig.repo.path,
        target_id=target.target_id, manifest_digest=target.manifest_digest, executor=identity,
        lifetime_seconds=60, current_executor_identity=observe, async_admission=True)
    evidence = 'Synthetic host inventory; not real staging acceptance.'
    report = OperatorReconciliation(gate.challenge, rig.owner.epoch, target.target_id,
        target.manifest_digest, identity, 'sha256:' + hashlib.sha256(evidence.encode()).hexdigest(),
        True, True, True)
    broker = SessionAcceptanceBroker(gate=gate, owner=rig.owner, target_id=target.target_id,
        manifest_digest=target.manifest_digest, executor=identity, report=report, evidence=evidence,
        operator_actor='human:123', audit=lambda _: True, lifetime_seconds=60)
    rig.bridge.reconciliation.close()
    rig.bridge.reconciliation = gate
    rig.gate, rig.broker, rig.observation = gate, broker, state
    try:
        yield rig
    finally:
        state.release.set()
        broker.close()


@pytest.fixture
def case(rig):
    with async_pilot_case(rig) as value:
        yield value


async def accept(case):
    return await case.broker.decide_async(actor='human:123', request_id=case.broker._id,
        expected_revision=1, action='accept')


async def entered(case):
    async with asyncio.timeout(2):
        while not case.observation.entered.is_set():
            await asyncio.sleep(.001)


async def test_pending_metadata_submits_but_cannot_claim_or_start_network(case):
    _, job = submit(case)
    assert case.queue.claim(worker=case.bridge.worker) is None
    with pytest.raises(HubError):
        await case.bridge.run_once()
    assert case.observation.calls == 0
    assert case.client.submissions == [] and machine_job(case) is None
    assert case.queue.get_job(job['id'])['state'] == 'queued'


@pytest.mark.parametrize('rollback', [False, True])
async def test_success_and_healthy_rollback_each_use_fresh_phase_and_release(case, rollback):
    if rollback:
        case.client.outcomes['smoke_container'] = 'failed'
    await accept(case)
    _, job = submit(case)
    assert case.queue.claim(worker=case.bridge.worker) is None  # No consumed scope.
    result = await case.bridge.run_once()
    assert result['id'] == job['id']
    assert result['state'] == ('failed' if rollback else 'succeeded')
    assert len(case.client.submissions) == (4 if rollback else 2)
    assert case.observation.calls == (7 if rollback else 5)
    assert machine_job(case) is None
    assert all(thread != threading.get_ident() for thread in case.observation.threads)


async def test_changed_review_during_claim_observation_cannot_claim_another_job(case):
    await accept(case)
    task, original = submit(case)
    submit(case, 'next')
    case.observation.block_at = 2
    running = asyncio.create_task(case.bridge.run_once())
    await entered(case)
    current = case.repo.get_task(task['id'])
    case.repo.set_candidate('codex', task['id'], current['revision'], 'f' * 40)
    case.observation.release.set()
    assert await running is None
    assert case.queue.get_job(original['id'])['state'] == 'queued'
    assert not case.client.submissions and machine_job(case) is None


@pytest.mark.parametrize('mode', ['normal', 'cancel', 'observer_rejected'])
async def test_generation_drain_during_first_claim_observation_is_clean(case, mode):
    from engineering_hub.events import NativeHubEventFeed
    from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
    await accept(case)
    _, job = submit(case)
    case.observation.block_at = 2
    feed = NativeHubEventFeed(case.repo.path)
    runtime = QueuedPilotRuntime(case.bridge, feed)
    generation, request = str(uuid4()), str(uuid4())
    cap = runtime.bind_generation_drain(case.owner, generation)
    try:
        assert (await feed.start())['available']
        await runtime.start()
        await entered(case)
        draining = asyncio.create_task(cap.drain(generation_id=generation, request_id=request))
        async with asyncio.timeout(2):
            while not runtime._closed:
                await asyncio.sleep(0)
        if mode == 'cancel':
            draining.cancel()
            await asyncio.sleep(0)
            draining.cancel()
        elif mode == 'observer_rejected':
            case.observation.reject = True
        assert not draining.done() and runtime._active is not None
        case.observation.release.set()
        if mode == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await draining
            ack = await cap.drain(generation_id=generation, request_id=request)
        else:
            ack = await draining
        assert ack.request_id == request and runtime.status['state'] == 'stopped'
        assert not ack.remote_authenticated and not ack.whole_workflow_completed
        assert case.gate.admission_state == 'closed'
        assert case.queue.get_job(job['id'])['state'] == 'queued'
        assert not case.client.submissions and machine_job(case) is None
        assert feed.broker.subscriber_count == 0
        case.owner.assert_active(case.repo.path)
    finally:
        case.observation.release.set()
        await runtime.stop()
        await feed.stop()


@pytest.mark.parametrize('failure', ['owner', 'source', 'source_unobserved', 'gate_without_stop'])
async def test_first_claim_observation_failure_is_not_masked_as_normal_drain(case, failure):
    from engineering_hub.events import NativeHubEventFeed
    from engineering_hub.pilot_ownership import PilotOwnershipError
    from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
    await accept(case)
    _, job = submit(case)
    case.observation.block_at = 2
    feed = NativeHubEventFeed(case.repo.path)
    runtime = QueuedPilotRuntime(case.bridge, feed)
    generation, request = str(uuid4()), str(uuid4())
    cap = runtime.bind_generation_drain(case.owner, generation)
    draining = None
    try:
        assert (await feed.start())['available']
        await runtime.start()
        await entered(case)
        if failure == 'gate_without_stop':
            case.gate.close()
        else:
            draining = asyncio.create_task(cap.drain(generation_id=generation, request_id=request))
            async with asyncio.timeout(2):
                while not runtime._closed:
                    await asyncio.sleep(0)
            if failure == 'owner':
                case.owner.close()
            elif failure == 'source':
                runtime._unavailable()
            else:
                # Lose the native source without delivering its invalidation
                # yet. A normal stop cannot race that failure into a clean ack.
                feed.broker.available = False
        case.observation.release.set()
        if draining is None:
            await runtime._task
            draining = asyncio.create_task(cap.drain(generation_id=generation, request_id=request))
        with pytest.raises((HubError, PilotOwnershipError)):
            await draining
        assert cap._ack is None and runtime.status['state'] != 'stopped'
        assert case.queue.get_job(job['id'])['state'] == 'queued'
        assert not case.client.submissions and machine_job(case) is None
    finally:
        case.observation.release.set()
        await runtime.stop()
        await feed.stop()


@pytest.mark.parametrize('change', ['close', 'cancel', 'review', 'lease'])
async def test_veto_during_command_preparation_sends_no_post_and_keeps_reservation(case, change):
    await accept(case)
    task, job = submit(case)
    case.observation.block_at = 3
    running = asyncio.create_task(case.bridge.run_once())
    await entered(case)
    if change == 'close':
        case.gate.close()
    elif change == 'cancel':
        running.cancel()
        await asyncio.sleep(.005)
        running.cancel()
        assert not running.done()
    elif change == 'review':
        # Normal candidate edits correctly reject an active run. Inject a
        # corrupted ledger review to exercise the final independent recheck.
        import json
        with case.repo._transaction() as db:
            current = case.repo._get(db, task['id'], 'task')
            current['review']['verdict'] = 'reject'
            db.execute('UPDATE hub_entities SET data=? WHERE id=?',
                       (json.dumps(current), task['id']))
    else:
        case.clock[0] += 601
    case.observation.release.set()
    if change == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await running
    else:
        assert (await running)['state'] in {'held', 'claimed'}
    assert not case.client.submissions
    assert machine_job(case) == job['id']


async def test_direct_cancellation_drains_admitted_remote_phase_and_retains_evidence(case):
    await accept(case)
    _, job = submit(case)
    entered_remote, release_remote = asyncio.Event(), asyncio.Event()
    original = case.client.submit_command
    async def submit_command(command):
        entered_remote.set()
        await release_remote.wait()
        return await original(command)
    case.client.submit_command = submit_command
    running = asyncio.create_task(case.bridge.run_once())
    await asyncio.wait_for(entered_remote.wait(), 2)
    running.cancel()
    await asyncio.sleep(.005)
    running.cancel()
    case.gate.close()
    assert not running.done()
    release_remote.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    with case.repo._connection() as db:
        row = db.execute('SELECT state,response_json FROM hub_container_pilot_commands').fetchone()
        assert row['state'] == 'succeeded' and row['response_json']
    assert len(case.client.submissions) == 1 and machine_job(case) == job['id']


async def test_slow_valid_observations_leave_shared_event_loop_responsive(case):
    case.observation.delay = .1
    await accept(case)
    submit(case)
    running = asyncio.create_task(case.bridge.run_once())
    gaps = []
    last = time.monotonic()
    while not running.done():
        await asyncio.sleep(.005)
        now = time.monotonic()
        gaps.append(now - last)
        last = now
        assert case.broker.snapshot(actor='human:123')['state'] == 'accepted'
    assert (await running)['state'] == 'succeeded'
    assert len(gaps) >= 10 and max(gaps) < .25


@pytest.mark.parametrize('expiry_clock', ['monotonic', 'wall'])
async def test_release_proof_expiry_during_local_validation_keeps_capacity_held(case, monkeypatch, expiry_clock):
    from engineering_hub import startup_reconciliation as gate_module
    await accept(case)
    _, job = submit(case)
    original = case.bridge._proof
    def proof(db, intent):
        result = original(db, intent)
        scope = case.gate._active_scope
        if scope is not None and scope.subject.kind == 'reconcile':
            # Only one gate clock advances; the other remains live. Neither an
            # absent clock method nor renewing the proof can explain the veto.
            if expiry_clock == 'monotonic':
                after_proof = scope.deadline + .001
                clocks = SimpleNamespace(monotonic=lambda: after_proof, time=time.time)
            else:
                after_proof = scope.expires_wall_ms / 1000 + .001
                clocks = SimpleNamespace(monotonic=time.monotonic, time=lambda: after_proof)
            monkeypatch.setattr(gate_module, 'time', clocks)
        return result
    monkeypatch.setattr(case.bridge, '_proof', proof)
    assert (await case.bridge.run_once())['state'] == 'held'
    assert machine_job(case) == job['id'] and len(case.client.submissions) == 2
    assert case.gate.admission_state == 'closed'


def test_real_unified_mcp_ping_and_overview_remain_responsive_during_observation():
    """Real in-process Hub/bus routes, synthetic delayed identity; not NAS latency."""
    from tests.responsiveness_probe import run_isolated_probe
    report = run_isolated_probe('unified')
    assert report['latency_accepted'] and report['functional_verified']


def test_synchronous_observer_negative_control_rejects_latency_only():
    from tests.responsiveness_probe import run_isolated_probe
    report = run_isolated_probe('unified', negative_control=True)
    assert report['workflow_state'] == 'succeeded'
    assert report['session_deleted'] and report['lifespan_drained']
    assert report['inline_observations'] > 0
    assert report['inline_observation_overlaps_request'] is True
    assert report['max_seconds'] >= .3
    assert report['rejection'] == 'latency_limit_exceeded'
