"""Real Linux owner/native feed/controllers; simulated remotes, no host jobs."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from engineering_hub.events import NativeHubEventFeed
from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
from engineering_hub.repository import HubError
from engineering_hub.shared_staging_lifecycle import SharedStagingLifecycle
from engineering_hub.staging_client import StagingConnectionConfig
from engineering_hub.staging_module import StagingModule
from tests.manual_session_fixtures import synthetic_reconciliation
from tests.test_shared_pilot_modular_integration import joint_case, original_hold_rows
from tests.test_hub_queued_pilot import machine_job, submit
from tests.test_hub_staging_controller import payload

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux owner/native feed')


@asynccontextmanager
async def lifecycle_case(path, *, reopening=False, clients=None, auto_run=False, accepted=True):
    with joint_case(path, reopening=reopening, clients=clients, activate=False) as case:
        if not accepted:
            case.bridge.reconciliation.close()
            case.bridge.reconciliation = synthetic_reconciliation(case.owner, case.bridge.target, accepted=False)
        counts = SimpleNamespace(pilot=0, modular=0)
        async def close_pilot(): counts.pilot += 1
        async def close_modular(): counts.modular += 1
        case.client.aclose = close_pilot
        case.modular_client.aclose = close_modular
        case.feed = NativeHubEventFeed(case.repo.path)
        case.runtime = QueuedPilotRuntime(case.bridge, case.feed)
        config = StagingConnectionConfig('https://127.0.0.1:9443', Path('/private/test.token'),
            ca_file=Path('/private/test.ca'), enabled=True, auto_run=auto_run,
            projects={'engineering-hub': {'recipes': ['static']}})
        module = StagingModule(controller=case.modular, config=config, client=case.modular_client)
        case.lifecycle = SharedStagingLifecycle(case.owner, case.repo, case.queue,
                                               case.policies, case.runtime, module)
        case.counts = counts
        releases = []
        case.releases = releases
        try:
            yield case
        finally:
            for event in releases: event.set()
            try:
                await asyncio.wait_for(case.lifecycle.close(), 5)
            except HubError:
                pass  # Negative tests assert closed state/retained holds themselves.
            await case.feed.stop()
            await case.client.aclose()  # Outer service owns this client and owner.


async def pending(case):
    assert (await case.feed.start())['available']
    return await case.lifecycle.start_pending()


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate(): await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_constructor_and_pending_are_inert_and_return_before_human_acceptance(tmp_path):
    async with lifecycle_case(tmp_path, accepted=False) as case:
        assert case.lifecycle.status == {'state': 'not_started', 'reason': None}
        assert case.feed._source is None and case.runtime._task is None
        assert (await pending(case))['state'] == 'pending'
        assert (await case.lifecycle.recover_and_open())['state'] == 'pending'
        assert case.lifecycle._recovery_task is None
        assert not case.policies._open and not case.policies._ready
        assert case.modular.background is None and case.runtime._task is None
        assert not case.client.submissions and case.modular_client.posts == 0


@pytest.mark.asyncio
async def test_explicit_barrier_opens_then_starts_pilot_only_when_modular_auto_run_false(tmp_path):
    async with lifecycle_case(tmp_path) as case:
        await pending(case)
        assert (await case.lifecycle.recover_and_open())['state'] == 'active'
        assert case.policies._open and case.policies._ready == frozenset({'pilot', 'modular'})
        assert case.runtime.status['running'] and case.modular.background is None
        _, job = submit(case)
        await until(lambda: case.queue.get_job(job['id'])['state'] == 'succeeded')
        assert case.modular_client.posts == 0
        await case.lifecycle.close()
        assert case.lifecycle.status['state'] == 'stopped'
        assert case.feed.available and case.counts.pilot == 0 and case.counts.modular == 1
        case.owner.assert_active(case.repo.path)
        await case.lifecycle.close()
        assert case.counts.pilot == 0 and case.counts.modular == 1


@pytest.mark.asyncio
async def test_auto_run_starts_modular_after_both_ready_and_shared_open(tmp_path):
    async with lifecycle_case(tmp_path, auto_run=True) as case:
        await pending(case)
        await case.lifecycle.recover_and_open()
        submitted = await case.modular.submit('codex', payload(case.repo))
        await until(lambda: case.queue.get_job(submitted['job']['id'])['state'] == 'succeeded')
        assert case.modular_client.posts == 1 and case.runtime.status['running']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['owner', 'module', 'client', 'feed', 'registry-callback'])
async def test_identity_or_callback_drift_cannot_open(tmp_path, change):
    async with lifecycle_case(tmp_path) as case:
        await pending(case)
        if change == 'owner': case.lifecycle.owner = object()
        elif change == 'module': case.lifecycle.staging_module = object()
        elif change == 'client': case.modular.client = object()
        elif change == 'feed': case.runtime.feed = object()
        else: case.queue.claim_verifier = None
        with pytest.raises(HubError): await case.lifecycle.recover_and_open()
        assert not case.policies._open and case.runtime._task is None
        assert case.modular.background is None and case.modular_client.posts == 0
        # Restore exact references so test cleanup exercises original owned drain.
        case.lifecycle.owner = case.owner
        case.lifecycle.staging_module = case.lifecycle._snapshot[5]
        case.modular.client = case.modular_client
        case.runtime.feed = case.feed
        if change == 'registry-callback': case.queue.claim_verifier = case.policies._dispatchers[1]


@pytest.mark.asyncio
async def test_application_guard_rejects_forged_or_foreign_context(tmp_path):
    async with lifecycle_case(tmp_path) as case:
        case.lifecycle.validate_application(case.repo, case.queue, case.runtime, case.feed)
        for index in range(4):
            values = [case.repo, case.queue, case.runtime, case.feed]; values[index] = object()
            with pytest.raises(HubError): case.lifecycle.validate_application(*values)
        forged = object.__new__(SharedStagingLifecycle)
        with pytest.raises(HubError): forged.validate_application(case.repo, case.queue, case.runtime, case.feed)


async def create_held(path):
    with joint_case(path) as initial:
        initial.modular_client.lost_ack = True
        submitted = await initial.modular.submit('codex', payload(initial.repo))
        assert (await initial.modular.run_once())['job']['state'] == 'held'
        clients = initial.client, initial.modular_client
        identity = submitted['job']['id']
        initial.policies.close()
    return clients, identity


@pytest.mark.asyncio
async def test_recovery_uses_original_modular_get_without_repost_before_open(tmp_path):
    clients, identity = await create_held(tmp_path)
    async with lifecycle_case(tmp_path, reopening=True, clients=clients) as case:
        await pending(case)
        assert (await case.lifecycle.recover_and_open())['state'] == 'active'
        assert case.queue.get_job(identity)['state'] == 'succeeded'
        assert case.modular_client.posts == 1 and machine_job(case) is None


@pytest.mark.asyncio
async def test_unknown_receipt_keeps_hold_closed_then_explicit_original_id_retry(tmp_path):
    clients, identity = await create_held(tmp_path)
    async with lifecycle_case(tmp_path, reopening=True, clients=clients) as case:
        await pending(case)
        original = case.modular_client.jobs.pop(identity)
        assert (await case.lifecycle.recover_and_open())['state'] == 'recovery_required'
        assert not case.policies._open and not case.policies._ready
        assert machine_job(case) == identity and case.modular_client.posts == 1
        assert case.runtime._task is None and case.modular.background is None
        case.modular_client.jobs[identity] = original
        assert (await case.lifecycle.recover_and_open())['state'] == 'active'
        assert case.modular_client.posts == 1


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_abandon_accepted_recovery(tmp_path):
    clients, identity = await create_held(tmp_path)
    async with lifecycle_case(tmp_path, reopening=True, clients=clients) as case:
        await pending(case)
        entered, release = asyncio.Event(), asyncio.Event(); case.releases.append(release)
        original = case.modular_client.job
        async def blocked(job_id):
            assert job_id == identity
            entered.set(); await release.wait()
            return await original(job_id)
        case.modular_client.job = blocked
        caller = asyncio.create_task(case.lifecycle.recover_and_open())
        await entered.wait(); caller.cancel(); caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done() and case.lifecycle.status['state'] == 'recovering'
        assert not case.policies._open and case.counts.modular == case.counts.pilot == 0
        release.set()
        with pytest.raises(asyncio.CancelledError): await caller
        assert case.lifecycle.status['state'] == 'active'
        assert case.queue.get_job(identity)['state'] == 'succeeded' and case.modular_client.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('unexpected', [False, True])
@pytest.mark.parametrize('async_mode', [False, True])
async def test_shutdown_distinguishes_pilot_preclaim_veto_from_failure(tmp_path, monkeypatch, unexpected, async_mode):
    async with lifecycle_case(tmp_path) as case:
        entered, release = asyncio.Event(), asyncio.Event()
        case.releases.append(release)
        original = case.bridge.run_once

        async def delayed_preclaim():
            entered.set()
            await release.wait()
            if unexpected:
                raise HubError('execution_policy_admission_closed', 409)
            return await original()

        monkeypatch.setattr(case.bridge, 'run_once', delayed_preclaim)
        await pending(case)
        await case.lifecycle.recover_and_open()
        await asyncio.wait_for(entered.wait(), 5)
        closing = asyncio.create_task(case.lifecycle.close())
        await until(lambda: case.runtime._claims_suspended)
        assert not closing.done() and not case.policies._open
        if async_mode:
            # Select the async entry point only after shutdown has vetoed
            # admission; its first guard must run before any observation.
            monkeypatch.setattr(type(case.bridge), 'async_admission_required', property(lambda _: True))
        release.set()
        if unexpected:
            with pytest.raises(HubError, match='shared_staging_shutdown_failed'):
                await closing
            assert case.lifecycle.status['state'] == 'shutdown_failed'
        else:
            await closing
            assert case.lifecycle.status['state'] == 'stopped'
        assert case.lifecycle.is_drained and case.client.submissions == []


@pytest.mark.asyncio
async def test_shutdown_reports_same_code_failure_after_pilot_claim(tmp_path, monkeypatch):
    async with lifecycle_case(tmp_path) as case:
        entered, release = asyncio.Event(), asyncio.Event()
        case.releases.append(release)

        async def failing_advance(_run_id):
            entered.set()
            await release.wait()
            raise RuntimeError('simulated accepted work failure')

        def failing_hold(_job):
            raise HubError('execution_policy_admission_closed', 409)

        monkeypatch.setattr(case.bridge.workflow, 'advance', failing_advance)
        monkeypatch.setattr(case.bridge, '_hold', failing_hold)
        await pending(case)
        await case.lifecycle.recover_and_open()
        _, job = submit(case)
        await asyncio.wait_for(entered.wait(), 5)
        assert case.queue.get_job(job['id'])['state'] == 'claimed'
        closing = asyncio.create_task(case.lifecycle.close())
        await until(lambda: case.runtime._claims_suspended)
        release.set()
        with pytest.raises(HubError, match='shared_staging_shutdown_failed'):
            await closing
        assert case.lifecycle.status['state'] == 'shutdown_failed'
        assert case.lifecycle.is_drained


@pytest.mark.asyncio
async def test_shutdown_drains_accepted_modular_before_client_or_owner_close(tmp_path):
    async with lifecycle_case(tmp_path) as case:
        await pending(case); await case.lifecycle.recover_and_open()
        entered, release = asyncio.Event(), asyncio.Event(); case.releases.append(release)
        original = case.modular_client.submit
        async def blocked(request):
            entered.set(); await release.wait(); return await original(request)
        case.modular_client.submit = blocked
        await case.modular.submit('codex', payload(case.repo))
        running = asyncio.create_task(case.modular.run_once())
        await entered.wait()
        caller = asyncio.create_task(case.lifecycle.close())
        await asyncio.sleep(0); caller.cancel(); caller.cancel(); await asyncio.sleep(0)
        assert not caller.done() and not case.policies._open
        assert case.counts.modular == case.counts.pilot == 0 and case.feed.available
        case.owner.assert_active(case.repo.path)
        release.set(); await running
        with pytest.raises(asyncio.CancelledError): await caller
        assert case.lifecycle.status['state'] == 'stopped'
        assert case.counts.modular == 1 and case.counts.pilot == 0 and case.feed.available
        case.owner.assert_active(case.repo.path)


@pytest.mark.asyncio
async def test_other_live_worker_lease_is_not_inspected_adopted_or_expired(tmp_path):
    with joint_case(tmp_path) as initial:
        submitted = await initial.modular.submit('codex', payload(initial.repo))
        claim = initial.queue.claim(worker=initial.modular.worker,
            action_keys=tuple(initial.modular.actions.values()), lease_seconds=600)
        identity = submitted['job']['id']
        before = original_hold_rows(initial, identity)
        clients = initial.client, initial.modular_client
        initial.policies.close()
    async with lifecycle_case(tmp_path, reopening=True, clients=clients) as case:
        async def forbidden(_): raise AssertionError('live other-generation GET')
        case.modular_client.job = forbidden
        await pending(case)
        assert (await case.lifecycle.recover_and_open())['state'] == 'recovery_required'
        assert original_hold_rows(case, identity) == before
        assert case.queue.get_job(identity)['worker'] == claim['worker']
        assert case.modular_client.posts == 0 and not case.policies._open
        assert case.runtime._task is None and case.modular.background is None


@pytest.mark.asyncio
async def test_second_coordinator_cannot_capture_same_owner_queue_and_runtime(tmp_path):
    async with lifecycle_case(tmp_path) as case:
        with pytest.raises(HubError):
            SharedStagingLifecycle(case.owner, case.repo, case.queue, case.policies,
                                   case.runtime, case.lifecycle.staging_module)
        await pending(case)
        assert not case.policies._open and case.runtime._task is None


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['runtime-feed', 'modular-client'])
async def test_shutdown_retains_actual_work_after_identity_drift_and_closes_original_client(tmp_path, drift):
    async with lifecycle_case(tmp_path) as case:
        await pending(case); await case.lifecycle.recover_and_open()
        entered, release = asyncio.Event(), asyncio.Event(); case.releases.append(release)
        original = case.modular_client.submit
        foreign = SimpleNamespace(closes=0)
        async def foreign_close(): foreign.closes += 1
        foreign.aclose = foreign_close
        async def blocked(request):
            entered.set(); await release.wait(); return await original(request)
        case.modular_client.submit = blocked
        await case.modular.submit('codex', payload(case.repo))
        running = asyncio.create_task(case.modular.run_once())
        await entered.wait()
        if drift == 'runtime-feed': case.runtime.feed = object()
        else: case.modular.client = foreign
        caller = asyncio.create_task(case.lifecycle.close())
        await asyncio.sleep(0); await asyncio.sleep(0)
        assert case.lifecycle._close_task is not None and not caller.done()
        assert not case.lifecycle.is_drained and not case.policies._open
        assert case.counts.modular == case.counts.pilot == foreign.closes == 0
        case.owner.assert_active(case.repo.path)
        release.set()
        result = await asyncio.gather(running, return_exceptions=True)
        if drift == 'modular-client': assert isinstance(result[0], HubError)
        with pytest.raises(HubError, match='shared_staging_shutdown_failed'):
            await asyncio.wait_for(caller, 5)
        assert case.lifecycle.status['state'] == 'shutdown_failed'
        assert case.lifecycle.is_drained and case.runtime._task.done()
        assert case.counts.modular == 1 and case.counts.pilot == foreign.closes == 0
        case.owner.assert_active(case.repo.path)
        assert case.feed.available


@pytest.mark.asyncio
async def test_listener_veto_never_closes_client_before_external_http_upload_drain(tmp_path):
    async with lifecycle_case(tmp_path) as case:
        await pending(case); await case.lifecycle.recover_and_open()
        entered, release = asyncio.Event(), asyncio.Event(); case.releases.append(release)
        original = case.modular_client.put_source
        async def upload(request):
            entered.set(); await release.wait(); return await original(request)
        case.modular_client.put_source = upload
        # This direct upload represents an accepted HTTP request not contained
        # in controller.inflight. Actual server request drain is tested elsewhere.
        request = asyncio.create_task(case.modular.submit('codex', payload(case.repo)))
        await entered.wait()
        assert not case.modular.inflight
        assert case.lifecycle.close_admission() is None
        assert case.lifecycle.close_admission() is None
        await asyncio.sleep(0)
        assert not request.done() and case.lifecycle._close_task is None
        assert not case.lifecycle.is_drained and not case.policies._open
        assert case.counts.modular == case.counts.pilot == 0
        case.owner.assert_active(case.repo.path)
        # Only after the outer service drains this request may it close clients.
        release.set()
        with pytest.raises(HubError): await request
        assert case.modular_client.deposits == 1 and case.modular_client.posts == 0
        await case.lifecycle.close()
        assert case.lifecycle.is_drained and case.counts.modular == 1
        assert case.counts.pilot == 0 and case.feed.available
        case.owner.assert_active(case.repo.path)
