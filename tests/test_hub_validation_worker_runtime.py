"""Lifecycle unit tests: real queue/SQLite, FAKE native source and backend."""
import asyncio
import os
import sqlite3
import threading
from uuid import uuid4

import pytest

from engineering_hub.events import NativeHubEventFeed
from engineering_hub.repository import HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget, WorkWorker
from hub_validation_worker.core import (ValidationEnrollment, ValidationError,
    ValidationLedger, ValidationObservation, ValidationProfile, ValidationWorker)
from hub_validation_worker.runtime import ValidationRuntime


class FakeNativeSource:
    def __init__(self, path, reader, deliver, *, on_error):
        self.deliver, self.on_error = deliver, on_error
        self.healthy = True
        self.stats = {'cursor': 0}
    def start(self):
        return self.stats
    def stop(self):
        self.healthy = False


class BlockingFakeBackend:
    def __init__(self):
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()
        self.finished = threading.Event()
        self.calls = []
        self.subscribers_at_effect = []
        self.feed = None
        self.fail = False
    def execute(self, enrollment, job, intent_id, *, deadline_monotonic):
        self.calls.append(job.job_id)
        self.subscribers_at_effect.append(self.feed.broker.subscriber_count)
        self.started.set()
        assert self.release.wait(5), 'unit test did not release fake backend'
        self.finished.set()
        if self.fail:
            raise RuntimeError('private host detail never reaches status')
        return ValidationObservation('d' * 64, enrollment.image_config_id,
                                     enrollment.profile.digest(), 0, True)


async def until(predicate):
    # Bounded unit-test scheduling, not a production timer or worker poller.
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
async def rig(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    queue = WorkQueue(repo, {'engineering-hub.validate': WorkAction('staging-pc', project='engineering-hub')},
                      target_verifier=lambda *args: True)
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    ledger = ValidationLedger(private / 'receipts.sqlite3', create=True,
                               allow_windows_test_storage=os.name != 'posix')
    queue.completion_verifier = ledger.verify_completion
    enrollment = ValidationEnrollment('engineering-hub.validate', 'engineering-hub', 'staging-pc',
        'a' * 40, 'sha256:' + 'b' * 64, 'sha256:' + 'c' * 64,
        'linux/amd64', ('python', '-m', 'pytest', '-q'), ValidationProfile(), 10)
    backend = BlockingFakeBackend()
    worker = ValidationWorker(queue, ledger, backend, (enrollment,))
    feed = NativeHubEventFeed(repo.path, source_factory=FakeNativeSource,
                              support_check=lambda _: (True, 'fake unit-test source'))
    await feed.start()
    backend.feed = feed
    runtimes = []
    clock = [1000.0]
    queue.clock = lambda: clock[0]
    def runtime(**kwargs):
        identity = WorkWorker('executor:software-validation', str(uuid4()))
        value = ValidationRuntime(queue, worker, identity, feed, **kwargs)
        runtimes.append(value)
        return value
    def submit(request=None):
        return queue.submit(actor='codex', request_id=request or str(uuid4()), action_key=enrollment.action_key,
                            environment='staging', target=WorkTarget(str(uuid4()), enrollment.source_commit, enrollment.artifact_digest))
    def invalidate():
        with repo._connection() as db:
            head = db.execute('SELECT MAX(sequence) FROM hub_outbox').fetchone()[0] or 0
        feed._source.deliver(('hub',), head)
    yield repo, queue, ledger, enrollment, backend, worker, feed, runtime, submit, invalidate, clock
    backend.release.set()
    for value in runtimes:
        try:
            await value.stop()
        except asyncio.CancelledError:
            assert value._task.done()
    await feed.stop()


async def test_initial_subscribe_before_durable_startup_drain(rig):
    _, queue, _, _, backend, _, feed, build, submit, *_ = rig
    job = submit()
    runtime = build()
    await runtime.start()
    await until(lambda: runtime.last_job is not None)
    assert backend.calls == [job['id']]
    assert backend.subscribers_at_effect == [1]
    assert queue.get_job(job['id'])['state'] == 'succeeded'
    await runtime.stop()
    assert runtime.status['state'] == 'stopped'
    assert feed.broker.subscriber_count == 0


async def test_idle_runtime_wakes_only_on_invalidation_then_drains_all_jobs(rig):
    _, queue, _, _, backend, _, _, build, submit, invalidate, _ = rig
    runtime = build()
    await runtime.start()
    await until(lambda: runtime.feed.broker.subscriber_count == 1)
    await asyncio.sleep(0.03)
    assert backend.calls == []
    jobs = [submit() for _ in range(3)]
    assert backend.calls == []
    invalidate()
    await until(lambda: len(backend.calls) == 3 and queue.get_job(jobs[-1]['id'])['state'] == 'succeeded'
                and runtime.last_job is not None and runtime.last_job['job_id'] == jobs[-1]['id'])
    assert backend.calls == [job['id'] for job in jobs]
    assert runtime.status['last_job'] == {'job_id': jobs[-1]['id'], 'state': 'succeeded'}


async def test_stop_during_active_thread_waits_without_next_admission(rig):
    _, queue, _, _, backend, _, _, build, submit, *_ = rig
    first, second = submit(), submit()
    backend.release.clear()
    runtime = build()
    await runtime.start()
    await until(backend.started.is_set)
    stopped = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0.02)
    assert not stopped.done() and not backend.finished.is_set()
    backend.release.set()
    await asyncio.wait_for(stopped, 5)
    assert backend.calls == [first['id']]
    assert queue.get_job(second['id'])['state'] == 'queued'
    assert runtime.status['state'] == 'stopped'


async def test_repeated_runtime_cancellation_does_not_abandon_executor_thread(rig):
    _, queue, _, _, backend, _, _, build, submit, *_ = rig
    first, second = submit(), submit()
    backend.release.clear()
    runtime = build()
    await runtime.start()
    await until(backend.started.is_set)
    runtime._task.cancel()
    await asyncio.sleep(0.01)
    runtime._task.cancel()
    await asyncio.sleep(0.01)
    assert not runtime._task.done() and not backend.finished.is_set()
    backend.release.set()
    with pytest.raises(asyncio.CancelledError):
        await runtime._task
    assert backend.finished.is_set()
    assert queue.get_job(first['id'])['state'] == 'succeeded'
    assert queue.get_job(second['id'])['state'] == 'queued'
    assert runtime.status['last_job'] == {'job_id': first['id'], 'state': 'succeeded'}


async def test_cancelled_stop_still_finishes_accepted_job(rig):
    _, queue, _, _, backend, _, _, build, submit, *_ = rig
    job = submit()
    backend.release.clear()
    runtime = build()
    await runtime.start()
    await until(backend.started.is_set)
    stopped = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0)
    stopped.cancel()
    await asyncio.sleep(0.01)
    stopped.cancel()
    await asyncio.sleep(0.01)
    assert not stopped.done()
    backend.release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopped
    assert queue.get_job(job['id'])['state'] == 'succeeded'
    assert not runtime.status['running']


async def test_native_failure_finishes_inflight_but_stops_next_admission(rig):
    _, queue, _, _, backend, _, feed, build, submit, *_ = rig
    first, second = submit(), submit()
    backend.release.clear()
    runtime = build()
    await runtime.start()
    await until(backend.started.is_set)
    feed._source.healthy = False
    feed._source.on_error(RuntimeError('private native details'))
    await until(lambda: runtime._source_failed)
    assert not backend.finished.is_set()
    backend.release.set()
    await until(lambda: not runtime.status['running'])
    assert runtime.status['state'] == 'native_source_unavailable'
    assert queue.get_job(first['id'])['state'] == 'succeeded'
    assert queue.get_job(second['id'])['state'] == 'queued'
    assert 'private' not in str(runtime.status)


async def test_pointer_burst_coalesces_during_blocked_backend(rig):
    _, _, _, _, backend, _, feed, build, submit, *_ = rig
    submit()
    backend.release.clear()
    runtime = build()
    await runtime.start()
    await until(backend.started.is_set)
    for head in range(1, 100):
        feed._source.deliver(('hub',), head)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert not runtime._source_failed
    assert feed.broker.subscriber_count == 1
    backend.release.set()
    await until(lambda: runtime.last_job is not None)
    assert runtime.status['state'] == 'active'


async def test_restart_new_instance_does_not_adopt_expired_claim(rig):
    _, queue, _, _, backend, _, _, build, submit, _, clock = rig
    previous_job = submit()
    old = WorkWorker('executor:software-validation', str(uuid4()))
    old_claim = queue.claim(worker=old, lease_seconds=1)
    clock[0] += 1
    queued = submit()
    runtime = build()
    await runtime.start()
    await until(lambda: queue.get_job(previous_job['id'])['state'] == 'held')
    assert backend.calls == []
    assert queue.get_job(queued['id'])['state'] == 'queued'
    assert old_claim['worker'] != runtime.identity.encoded()


async def test_unavailable_source_prevents_startup_admission(rig):
    _, _, _, _, backend, _, feed, build, submit, *_ = rig
    submit()
    await feed.stop()
    runtime = build()
    status = await runtime.start()
    assert status['state'] == 'native_source_unavailable'
    assert not status['running'] and backend.calls == []


async def test_owner_enrolled_pilot_ledger_denied_before_effect(rig):
    repo, _, _, _, backend, _, _, build, submit, *_ = rig
    submit()
    runtime = build()
    with sqlite3.connect(repo.path) as db:
        db.execute('CREATE TABLE hub_pilot_owner (marker TEXT)')
    with pytest.raises(Exception):
        await runtime.start()
    assert backend.calls == []


async def test_scope_mismatch_and_agent_identity_rejected(rig, tmp_path):
    _, queue, _, _, _, worker, feed, _, _, *_ = rig
    with pytest.raises(ValidationError, match='identity_mismatch'):
        ValidationRuntime(queue, worker, WorkWorker('codex', str(uuid4())), feed)
    wrong_feed = NativeHubEventFeed(tmp_path / 'other.db')
    with pytest.raises(ValidationError, match='identity_mismatch'):
        ValidationRuntime(queue, worker, WorkWorker('executor:software-validation', str(uuid4())), wrong_feed)


async def test_owner_guard_rechecked_before_effect(rig):
    _, _, _, _, backend, _, _, build, submit, *_ = rig
    submit()
    calls = []
    def guard():
        calls.append(True)
        if len(calls) > 1:
            raise ValidationError('owner_no_longer_live')
    runtime = build(owner_guard=guard)
    await runtime.start()
    await until(lambda: runtime.status['state'] == 'failed')
    assert backend.calls == []


async def test_event_across_empty_drain_boundary_cannot_strand_job(rig, monkeypatch):
    _, queue, _, _, backend, _, _, build, submit, invalidate, _ = rig
    original = queue.claim
    injected = []
    def boundary_claim(**kwargs):
        result = original(**kwargs)
        if result is None and not injected:
            injected.append(submit())
            invalidate()
        return result
    monkeypatch.setattr(queue, 'claim', boundary_claim)
    runtime = build()
    await runtime.start()
    await until(lambda: runtime.last_job is not None)
    assert backend.calls == [injected[0]['id']]
    assert queue.get_job(injected[0]['id'])['state'] == 'succeeded'


async def test_stop_after_claim_before_backend_holds_without_effect(rig, monkeypatch):
    _, queue, _, _, backend, _, _, build, submit, *_ = rig
    job = submit()
    runtime = build()
    original = queue.claim
    def close_after_claim(**kwargs):
        result = original(**kwargs)
        runtime._closed.set()
        return result
    monkeypatch.setattr(queue, 'claim', close_after_claim)
    await runtime.start()
    await until(lambda: not runtime.status['running'])
    assert backend.calls == []
    assert queue.get_job(job['id'])['state'] == 'held'


async def test_cancel_both_parent_and_child_never_abandons_executor_future(rig):
    _, queue, _, _, backend, _, _, build, submit, *_ = rig
    job = submit()
    backend.release.clear()
    runtime = build()
    await runtime.start()
    await until(backend.started.is_set)
    active = runtime._active
    active.cancel()
    runtime._task.cancel()
    await asyncio.sleep(0.01)
    active.cancel()
    runtime._task.cancel()
    await asyncio.sleep(0.01)
    assert not active.done() and not runtime._task.done()
    assert not backend.finished.is_set()
    backend.release.set()
    with pytest.raises(asyncio.CancelledError):
        await runtime._task
    assert active.done() and backend.finished.is_set()
    assert queue.get_job(job['id'])['state'] == 'succeeded'
    assert runtime.status['last_job'] == {'job_id': job['id'], 'state': 'succeeded'}
