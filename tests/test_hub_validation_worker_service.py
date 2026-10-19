"""Linux ownership/native-feed composition with a FAKE bounded backend.

These tests do not exercise Docker, NAS deployment or reboot acceptance.
"""
import asyncio
from dataclasses import replace
import os
import sqlite3
import sys
import threading
from types import MethodType
from uuid import uuid4

import pytest

from engineering_hub.events import NativeHubEventFeed
from engineering_hub.repository import HubRepository
from engineering_hub.work_queue import SOFTWARE_VALIDATION_EXECUTOR, WorkTarget, WorkWorker
from engineering_hub.work_queue_config import configured_work_queue
from hub_validation_worker.core import ValidationEnrollment, ValidationError, ValidationLedger, ValidationObservation, ValidationProfile
from hub_validation_worker.ownership import ValidationOwner, ValidationOwnershipError
from hub_validation_worker.service import EnrolledValidation, OwnedValidationService


class FakeBackend:
    """Bounded mock only; no subprocess or actual host execution."""
    def __init__(self):
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()
        self.calls = []
    def execute(self, enrollment, job, intent_id, *, deadline_monotonic):
        self.calls.append(job.job_id)
        self.started.set()
        assert self.release.wait(5), 'test did not release fake backend'
        return ValidationObservation('d' * 64, enrollment.image_config_id,
                                     enrollment.profile.digest(), 0, True)


async def until(predicate):
    async with asyncio.timeout(6):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
async def rig(tmp_path):
    if sys.platform != 'linux':
        pytest.skip('real Linux ownership and native SQLite event source required')
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    private = tmp_path / 'executor-private'
    private.mkdir(mode=0o700)
    owner = ValidationOwner(private, allow_ephemeral_test_storage=True).acquire()
    ledger = ValidationLedger(private / 'receipts.sqlite3', create=True)
    backend = FakeBackend()
    feed = NativeHubEventFeed(repo.path)
    task = repo.create_task('human:42', 'engineering-hub', 'Mock execution service task', str(uuid4()))
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], 'a' * 40)
    task = repo.review_task('claude', task['id'], task['revision'], 'a' * 40, 'approve')
    target = WorkTarget(task['id'], 'a' * 40, 'sha256:' + 'b' * 64)
    enrolled = ValidationEnrollment('engineering-hub.validate', 'engineering-hub', 'staging-pc',
        target.source_commit, target.artifact_digest, 'sha256:' + 'c' * 64,
        'linux/amd64', ('python', '-m', 'pytest', '-q'), ValidationProfile(), 10)
    admission = EnrolledValidation(enrolled, target)
    services = []
    def build(**changes):
        arguments = dict(repo=repo, ledger=ledger, backend=backend, admissions=(admission,),
                         owner=owner, feed=feed, supervisor_stop_grace_seconds=40,
                         allow_ephemeral_test_storage=True)
        arguments.update(changes)
        service = OwnedValidationService(**arguments)
        services.append(service)
        return service
    def submit(queue, *, suffix=None, work_target=target, environment='staging'):
        return queue.submit(actor='codex', request_id=suffix or str(uuid4()),
                            action_key=enrolled.action_key, environment=environment, target=work_target)
    yield repo, private, owner, ledger, backend, feed, admission, build, submit
    backend.release.set()
    for service in services:
        try:
            await service.stop()
        except (ValidationError, ValidationOwnershipError, asyncio.CancelledError):
            # Tests deliberately revoke ownership or break database scope.
            assert not service.runtime.status['active']
    owner.close()


async def test_real_linux_owner_native_startup_drain_and_release(rig):
    repo, private, owner, _, backend, feed, _, build, submit = rig
    public = configured_work_queue(repo)
    job = submit(public)
    assert public.claim(worker=WorkWorker('codex', str(uuid4()))) is None
    service = build()
    assert service.state == 'not_started' and backend.calls == []
    assert service.identity.agent == SOFTWARE_VALIDATION_EXECUTOR
    old_instance = owner.instance_id
    state = await service.start()
    assert state['state'] == 'active' and feed.available
    await until(lambda: service.runtime.last_job is not None)
    assert backend.calls == [job['id']]
    assert public.get_job(job['id'])['state'] == 'succeeded'
    assert service.status['supervisor_configuration_verified'] is False
    assert service.status['configuration_source'] == 'operator_assertion'
    await service.stop()
    assert not feed.available and service.status['state'] == 'stopped'
    with pytest.raises(ValidationOwnershipError):
        owner.assert_active()
    replacement = ValidationOwner(private, allow_ephemeral_test_storage=True).acquire()
    try:
        assert replacement.instance_id != old_instance
    finally:
        replacement.close()


async def test_native_invalidation_wakes_after_idle_startup_without_polling(rig):
    repo, _, _, _, backend, _, _, build, submit = rig
    service = build()
    await service.start()
    await until(lambda: service.feed.broker.subscriber_count == 1)
    assert backend.calls == []
    job = submit(configured_work_queue(repo))
    await until(lambda: service.runtime.last_job is not None)
    assert backend.calls == [job['id']]
    assert service.queue.get_job(job['id'])['state'] == 'succeeded'


async def test_shutdown_retains_real_owner_until_accepted_fake_work_finishes(rig):
    _, private, owner, _, backend, feed, _, build, submit = rig
    service = build()
    submit(service.queue)
    backend.release.clear()
    await service.start()
    await until(backend.started.is_set)
    stopping = asyncio.create_task(service.stop())
    await asyncio.sleep(0.02)
    stopping.cancel()
    await asyncio.sleep(0.01)
    stopping.cancel()
    await asyncio.sleep(0.01)
    assert not stopping.done() and feed.available
    owner.assert_active()
    duplicate = ValidationOwner(private, allow_ephemeral_test_storage=True)
    with pytest.raises(ValidationOwnershipError):
        duplicate.acquire()
    backend.release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert not service.runtime.status['active'] and not feed.available
    assert service.status['state'] == 'stopped'


async def test_duplicate_owner_and_reusing_transferred_owner_are_denied(rig):
    _, private, owner, _, _, _, _, build, _ = rig
    service = build()
    duplicate = ValidationOwner(private, allow_ephemeral_test_storage=True)
    with pytest.raises(ValidationOwnershipError):
        duplicate.acquire()
    with pytest.raises(ValidationError, match='already_transferred'):
        build()
    owner.assert_active()
    assert service.runtime.status['state'] == 'not_started'


@pytest.mark.parametrize('grace', [0, 39, True, 40.0, None])
async def test_declared_grace_must_cover_enrollment_deadline(rig, grace):
    _, _, owner, _, backend, _, _, build, _ = rig
    with pytest.raises(ValidationError, match='stop_grace_required'):
        build(supervisor_stop_grace_seconds=grace)
    owner.assert_active()  # Constructor did not transfer lifecycle on failure.
    assert backend.calls == []


async def test_invalid_feed_path_and_receipt_directory_scope(rig, tmp_path):
    _, _, owner, _, _, _, _, build, _ = rig
    with pytest.raises(ValidationError, match='database_scope_invalid'):
        build(feed=NativeHubEventFeed(tmp_path / 'not-hub.db'))
    other = tmp_path / 'other-private'
    other.mkdir(mode=0o700)
    wrong_ledger = ValidationLedger(other / 'receipts.sqlite3', create=True)
    with pytest.raises(ValidationError, match='private_ledger_scope_required'):
        build(ledger=wrong_ledger)
    owner.assert_active()


async def test_admissions_must_bind_exact_target_and_one_machine(rig):
    _, _, owner, _, _, _, admission, build, _ = rig
    with pytest.raises(ValidationError, match='target_mismatch'):
        EnrolledValidation(admission.enrollment, replace(admission.target, source_commit='e' * 40))
    another = EnrolledValidation(replace(admission.enrollment, action_key='treedar.validate',
        project='treedar', machine_id='other-machine'), admission.target)
    with pytest.raises(ValidationError, match='one_physical_machine_required'):
        build(admissions=(admission, another))
    with pytest.raises(ValidationError, match='one_physical_machine_required'):
        build(admissions=(admission, admission))
    owner.assert_active()


async def test_unregistered_action_rejects_without_transferring_owner(rig):
    _, _, owner, _, backend, _, admission, build, _ = rig
    wrong = EnrolledValidation(replace(admission.enrollment, action_key='unregistered.validate'), admission.target)
    with pytest.raises(Exception):
        build(admissions=(wrong,))
    # A failed constructor removed its attempted lifecycle transfer.
    service = build()
    owner.assert_active()
    assert backend.calls == [] and service.state == 'not_started'


async def test_public_agents_cannot_claim_internal_admission_or_retarget_it(rig):
    repo, _, _, _, backend, _, admission, build, submit = rig
    public = configured_work_queue(repo)
    wrong = replace(admission.target, artifact_digest='sha256:' + 'e' * 64)
    changed_job = submit(public, work_target=wrong)
    job = submit(public)
    service = build()
    for agent in ('codex', 'hermes', 'claude'):
        assert public.claim(worker=WorkWorker(agent, str(uuid4()))) is None
        assert service.queue.claim(worker=WorkWorker(agent, str(uuid4()))) is None
    assert service.queue.claim(worker=WorkWorker(SOFTWARE_VALIDATION_EXECUTOR, str(uuid4()))) is None
    # Exact internal identity can claim only its exact private enrollment.
    claimed = service.queue.claim(worker=service.identity)
    assert claimed['id'] == job['id']
    service.queue.complete(job_id=claimed['id'], worker=service.identity, fence=claimed['fence'], outcome='held')
    assert public.get_job(changed_job['id'])['state'] == 'queued'
    assert backend.calls == []


async def test_owner_loss_prevents_startup_effects(rig):
    _, _, owner, _, backend, _, _, build, submit = rig
    service = build()
    submit(service.queue)
    owner.close()
    with pytest.raises(ValidationOwnershipError):
        await service.start()
    assert backend.calls == []


async def test_database_replacement_invalidates_service_guard(rig):
    repo, _, _, _, backend, _, _, build, _ = rig
    service = build()
    # Coherent logical copy still has a different physical file identity.
    copied = service.path.with_suffix('.copy')
    source = sqlite3.connect(repo.path)
    destination = sqlite3.connect(copied)
    try:
        source.backup(destination)
    finally:
        source.close()
        destination.close()
    service.path.rename(service.path.with_suffix('.old'))
    copied.rename(service.path)
    with pytest.raises(ValidationError, match='database_changed'):
        service._guard()
    assert backend.calls == []


@pytest.mark.parametrize('table', ['hub_pilot_owner', 'hub_dispatch_cursor'])
async def test_existing_or_late_legacy_control_ledger_is_denied(rig, table):
    repo, _, owner, _, backend, _, _, build, _ = rig
    service = build()
    with sqlite3.connect(repo.path) as db:
        db.execute('CREATE TABLE ' + table + ' (marker TEXT)')
    with pytest.raises(ValidationError, match='legacy_dispatch_forbidden'):
        service._guard()
    assert backend.calls == []


async def test_lifespan_stops_feed_and_owner_on_caller_exception(rig):
    _, _, owner, _, _, feed, _, build, _ = rig
    service = build()
    with pytest.raises(RuntimeError, match='caller failed'):
        async with service.lifespan():
            assert feed.available
            raise RuntimeError('caller failed')
    assert service.state == 'stopped' and not feed.available
    with pytest.raises(ValidationOwnershipError):
        owner.assert_active()


async def test_changed_receipt_verifier_revokes_private_claim_admission(rig):
    _, _, _, ledger, backend, _, _, build, submit = rig
    service = build()
    submit(service.queue)
    service.queue.completion_verifier = None
    assert service.queue.claim(worker=service.identity) is None
    # Merely binding some other permissive method to the same ledger object
    # must not count as installing the independently checked receipt verifier.
    service.queue.completion_verifier = MethodType(lambda self, *args: True, ledger)
    assert service.queue.claim(worker=service.identity) is None
    assert backend.calls == []


async def test_unavailable_native_startup_cleans_feed_and_releases_owner(rig):
    repo, private, owner, _, backend, _, _, build, _ = rig
    unsupported = NativeHubEventFeed(repo.path, support_check=lambda _: (False, 'unit test unsupported'))
    service = build(feed=unsupported)
    with pytest.raises(ValidationError, match='native_source_unavailable'):
        await service.start()
    assert not unsupported.available and backend.calls == []
    with pytest.raises(ValidationOwnershipError):
        owner.assert_active()
    replacement = ValidationOwner(private, allow_ephemeral_test_storage=True).acquire()
    replacement.close()


def test_enrollment_target_shape_is_validated_without_startup():
    enrollment = ValidationEnrollment('engineering-hub.validate', 'engineering-hub', 'staging-pc',
        'a' * 40, 'sha256:' + 'b' * 64, 'sha256:' + 'c' * 64,
        'linux/amd64', ('python', '-m', 'pytest', '-q'), ValidationProfile(), 10)
    with pytest.raises(ValidationError, match='target_mismatch'):
        EnrolledValidation(enrollment, WorkTarget(str(uuid4()), 'a' * 40, 'sha256:' + 'd' * 64))
