"""Real Linux ownership/feed and unified service; synthetic executor responses.

No host executor, NAS, production credential, remote stop or identity renewal.
"""
import asyncio
import sys

import pytest

if sys.platform != 'linux':
    pytest.skip('Requires real Linux PilotOwner and native SQLite feed', allow_module_level=True)

from engineering_hub.repository import HubError
from engineering_hub.pilot_ownership import PilotOwnershipError
from engineering_hub import unified_queued_pilot_service as service
from tests.test_hub_queued_pilot_runtime import (
    rig as runtime_rig, pilot_rig, start as runtime_start, block_phase, until,
)
from tests.test_hub_queued_pilot import submit as queue_submit, machine_job
from tests.test_hub_unified_queued_pilot_service import (
    rig as unified_rig, start as service_start, options, connections,
    submit as http_submit, closed,
)

GEN = '38d7cd63-08c5-4213-95f9-f4999ce85c75'
REQ = 'cf90a001-ab26-4406-b858-9611d7c26769'
OTHER = '81423e32-4e49-4fe9-8196-ebbb47a49432'


@pytest.mark.asyncio
async def test_real_owner_feed_drains_phase_and_subscription_not_whole_workflow(runtime_rig):
    rig = runtime_rig
    cap = rig.runtime.bind_generation_drain(rig.owner, GEN)
    entered, release, finished = block_phase(rig)
    _, first = queue_submit(rig, 'generation-first')
    _, second = queue_submit(rig, 'generation-second')
    await runtime_start(rig)
    await asyncio.wait_for(entered.wait(), 5)
    with pytest.raises(HubError): await cap.drain(generation_id=OTHER, request_id=REQ)
    assert rig.runtime.admission_allowed()
    draining = asyncio.create_task(cap.drain(generation_id=GEN, request_id=REQ))
    await until(lambda: not rig.runtime.admission_allowed())
    draining.cancel(); await asyncio.sleep(0); draining.cancel()
    assert not draining.done() and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError): await draining
    ack = await cap.drain(generation_id=GEN, request_id=REQ)
    assert ack.owner_epoch == rig.owner.epoch and ack.request_id == REQ
    assert not ack.whole_workflow_completed and not ack.native_executor_stopped
    assert not ack.remote_authenticated
    assert rig.feed.broker.subscriber_count == 0
    assert rig.feed.available and finished.is_set()
    assert rig.queue.get_job(first['id'])['state'] == 'held'
    assert rig.queue.get_job(second['id'])['state'] == 'queued'
    assert machine_job(rig) == first['id']
    assert [row.action for row in rig.client.submissions] == ['deploy_container']
    rig.owner.assert_active(rig.repo.path)


@pytest.mark.asyncio
async def test_real_owner_closed_after_clean_drain_invalidates_retrieval(runtime_rig):
    rig = runtime_rig
    cap = rig.runtime.bind_generation_drain(rig.owner, GEN)
    await runtime_start(rig)
    await until(lambda: rig.feed.broker.subscriber_count >= 1)
    await cap.drain(generation_id=GEN, request_id=REQ)
    rig.owner.close()
    with pytest.raises(PilotOwnershipError): await cap.drain(generation_id=GEN, request_id=REQ)


@pytest.mark.asyncio
async def test_unified_ready_handoff_actual_drain_and_teardown_revokes(unified_rig):
    rig = unified_rig
    caps = []
    entered, release = asyncio.Event(), asyncio.Event()
    async def block(_):
        entered.set()
        await release.wait()
    rig.block = block
    stop, serving, addresses = await service_start(rig, generation_id=GEN,
                                                  on_generation_ready=caps.append)
    assert len(caps) == 1 and rig.core.state.queued_pilot_runtime.admission_allowed()
    cap = caps[0]
    try:
        async with connections(addresses, rig) as (agent, browser):
            _, job = await http_submit(agent, browser, 'generation-http')
            await asyncio.wait_for(entered.wait(), 8)
            draining = asyncio.create_task(cap.drain(generation_id=GEN, request_id=REQ))
            await until(lambda: not rig.core.state.queued_pilot_runtime.admission_allowed())
            assert not draining.done() and rig.owner.close_count == 0
            release.set()
            ack = await draining
            assert ack.generation_id == GEN and not ack.whole_workflow_completed
            assert rig.core.state.event_feed.broker.subscriber_count == 0
            assert rig.core.state.work_queue.get_job(job['id'])['state'] == 'held'
            # Queue-only drain does not pretend that HTTP/bus/owner were closed.
            assert (await agent.get('/health')).status_code == 200
            assert rig.owner.close_count == rig.clients[0].close_count == 0
            assert len(rig.requests) == 1
    finally:
        release.set(); stop.set()
        await asyncio.wait_for(serving, 8)
    closed(rig)
    with pytest.raises(HubError): await cap.drain(generation_id=GEN, request_id=REQ)


@pytest.mark.asyncio
async def test_generation_ready_callback_failure_drains_and_revokes(unified_rig):
    rig = unified_rig
    caps = []
    def reject(capability):
        caps.append(capability)
        raise RuntimeError('synthetic ready callback failure')
    with pytest.raises(service.QueuedPilotServiceError):
        await service.serve_owned_unified_queued_pilot(rig.settings, rig.manifest,
            rig.client_factory, **options(rig, generation_id=GEN, on_generation_ready=reject))
    assert len(caps) == 1
    closed(rig)
    with pytest.raises(HubError): await caps[0].drain(generation_id=GEN, request_id=REQ)


@pytest.mark.asyncio
async def test_generation_ready_returning_current_task_rejected_without_deadlock(unified_rig):
    rig = unified_rig
    with pytest.raises(service.QueuedPilotServiceError):
        await asyncio.wait_for(service.serve_owned_unified_queued_pilot(
            rig.settings, rig.manifest, rig.client_factory,
            **options(rig, generation_id=GEN,
                      on_generation_ready=lambda _: asyncio.current_task())), 8)
    closed(rig)


@pytest.mark.asyncio
async def test_generation_ready_returned_task_finishes_before_owner_cleanup(unified_rig):
    rig = unified_rig
    entered, cleanup_entered, release, finished = (asyncio.Event() for _ in range(4))
    async def callback_work():
        try:
            entered.set()
            await asyncio.Future()
        finally:
            cleanup_entered.set()
            await release.wait()
            rig.owner.assert_active(rig.settings.database)
            assert rig.owner.close_count == rig.clients[0].close_count == 0
            finished.set()
    returned = asyncio.create_task(callback_work())
    await entered.wait()
    serving = asyncio.create_task(service.serve_owned_unified_queued_pilot(
        rig.settings, rig.manifest, rig.client_factory,
        **options(rig, generation_id=GEN, on_generation_ready=lambda _: returned)))
    try:
        await asyncio.wait_for(cleanup_entered.wait(), 8)
        assert not serving.done() and rig.owner.close_count == 0
    finally:
        release.set()
        with pytest.raises(service.QueuedPilotServiceError):
            await asyncio.wait_for(serving, 8)
        if not returned.done():
            returned.cancel()
            await asyncio.gather(returned, return_exceptions=True)
    assert returned.cancelled() and finished.is_set()
    closed(rig)


@pytest.mark.asyncio
@pytest.mark.parametrize('generation_enabled', [False, True])
async def test_existing_async_on_ready_is_awaited(unified_rig, generation_enabled):
    rig = unified_rig
    entered, release, finished, stop = (asyncio.Event() for _ in range(4))
    caps = []
    async def ready(*addresses):
        assert len(addresses) == 2
        entered.set()
        await release.wait()
        finished.set()
        stop.set()
    changes = ({'generation_id': GEN, 'on_generation_ready': caps.append}
               if generation_enabled else {})
    serving = asyncio.create_task(service.serve_owned_unified_queued_pilot(
        rig.settings, rig.manifest, rig.client_factory,
        **options(rig, stop_event=stop, on_ready=ready, **changes)))
    try:
        await asyncio.wait_for(entered.wait(), 8)
        assert not serving.done() and not finished.is_set()
        assert len(caps) == int(generation_enabled)
    finally:
        release.set()
        await asyncio.wait_for(serving, 8)
    assert finished.is_set()
    closed(rig)


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'generation_id': GEN}, {'on_generation_ready': lambda _: None},
    {'generation_id': 'invalid', 'on_generation_ready': lambda _: None},
])
async def test_invalid_handoff_configuration_never_transfers_owner(unified_rig, changes):
    rig = unified_rig
    with pytest.raises(ValueError):
        await service.serve_owned_unified_queued_pilot(rig.settings, rig.manifest,
            rig.client_factory, **options(rig, **changes))
    assert rig.owner.close_count == 0 and not rig.clients
    rig.owner.assert_active(rig.settings.database)
