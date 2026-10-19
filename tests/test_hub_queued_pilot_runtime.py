"""Real Linux native-feed/controller lifecycle; typed simulated remote phases.

There is no Docker daemon, actual host deployment, or application health proof.
"""
import asyncio
import os
import sys

import pytest

from agent_message_bus.sqlite_events import native_event_support
from engineering_hub.events import NativeHubEventFeed
from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
from engineering_hub.repository import HubError
from tests.test_hub_queued_pilot import rig as pilot_rig, submit, machine_job

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Real Linux owner/native event source")


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)  # Bounded test observation, not a production poller.


@pytest.fixture
async def rig(pilot_rig):
    supported, reason = native_event_support(pilot_rig.repo.path)
    if not supported:
        pytest.skip(reason)
    pilot_rig.feed = NativeHubEventFeed(pilot_rig.repo.path)
    pilot_rig.runtime = QueuedPilotRuntime(pilot_rig.bridge, pilot_rig.feed)
    pilot_rig.releases = []
    try:
        yield pilot_rig
    finally:
        for release in pilot_rig.releases:
            release.set()
        try:
            await asyncio.wait_for(pilot_rig.runtime.stop(), 5)
        except asyncio.CancelledError:
            pass
        await pilot_rig.feed.stop()


def block_phase(rig):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    rig.releases.append(release)
    original = rig.client.submit_command
    async def blocked(command):
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        result = await original(command)
        finished.set()
        return result
    rig.client.submit_command = blocked
    return entered, release, finished


async def start(rig):
    assert (await rig.feed.start())["available"]
    return await rig.runtime.start()


def test_constructor_is_inert_and_does_not_start_feed_or_client(rig):
    assert rig.feed._source is None
    assert rig.runtime.status["running"] is False
    assert rig.runtime.status["active"] is False
    assert rig.client.submissions == []
    assert rig.feed.broker.subscriber_count == 0


@pytest.mark.asyncio
async def test_startup_drain_processes_existing_job_and_keeps_task_verified(rig):
    task, job = submit(rig)
    await start(rig)
    await until(lambda: rig.queue.get_job(job["id"])["state"] == "succeeded"
                and rig.runtime.status["last_job"] is not None)
    assert len(rig.client.submissions) == 2
    assert rig.repo.get_task(task["id"])["status"] == "verified"
    assert rig.runtime.status["last_job"] == {"id": job["id"], "state": "succeeded"}
    await rig.runtime.stop()
    assert rig.feed.available  # Runtime does not own source, owner, or client.
    rig.owner.assert_active(rig.repo.path)


@pytest.mark.asyncio
async def test_real_native_commit_wakes_idle_runtime_without_manual_publish(rig):
    empty, calls = asyncio.Event(), []
    original = rig.bridge.run_once
    async def observed_drain():
        result = await original()
        calls.append(None if result is None else result["id"])
        if result is None:
            empty.set()
        return result
    rig.bridge.run_once = observed_drain
    await start(rig)
    await asyncio.wait_for(empty.wait(), 5)
    assert rig.client.submissions == []
    empty.clear()
    _, job = submit(rig)
    # Do not poll queue.get_job here: opening/closing SQLite connections itself
    # creates native WAL lifecycle notifications and perturbs the idle baseline.
    await until(lambda: rig.runtime.status["last_job"] == {"id": job["id"], "state": "succeeded"})
    await asyncio.wait_for(empty.wait(), 5)
    assert len(rig.client.submissions) == 2
    source = rig.feed._source
    await asyncio.to_thread(source.flush_pending)
    # Flush the native side, then wait for its scheduled loop callback and the
    # runtime's final empty drain rather than sampling mid-completion.
    await until(lambda: not rig.feed._scheduled and rig.feed._pending_cursor is None
                and not rig.runtime._wakeup.is_set() and not rig.runtime.status["active"])
    before = len(calls)
    await asyncio.sleep(0.03)
    assert len(calls) == before  # Idle runtime issues no additional queue claims.


@pytest.mark.asyncio
async def test_unstarted_native_source_does_not_admit_queued_work(rig):
    _, job = submit(rig)
    status = await rig.runtime.start()
    assert status["state"] == "native_source_unavailable"
    assert status["running"] is False and not rig.runtime.admission_allowed()
    assert rig.queue.get_job(job["id"])["state"] == "queued"
    assert rig.client.submissions == []


@pytest.mark.asyncio
async def test_only_one_accepted_job_runs_and_next_job_waits(rig):
    entered, release, _ = block_phase(rig)
    _, first = submit(rig, "first")
    _, second = submit(rig, "second")
    await start(rig)
    await asyncio.wait_for(entered.wait(), 5)
    assert rig.queue.get_job(first["id"])["state"] == "claimed"
    assert rig.queue.get_job(second["id"])["state"] == "queued"
    release.set()
    await until(lambda: rig.queue.get_job(second["id"])["state"] == "succeeded")
    ids = [c.run_id for c in rig.client.submissions]
    assert len(ids) == 4 and ids[0] == ids[1] and ids[2] == ids[3] and ids[0] != ids[2]
    assert set(ids) == {run["id"] for run in rig.repo.list_runs()}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_stop", [False, True])
async def test_stop_drains_only_accepted_phase_and_repeated_cancellation_cannot_abandon_it(rig, cancel_stop):
    entered, release, finished = block_phase(rig)
    _, first = submit(rig, "first")
    _, second = submit(rig, "second")
    await start(rig)
    await asyncio.wait_for(entered.wait(), 5)
    stopped = asyncio.create_task(rig.runtime.stop())
    await until(lambda: not rig.runtime.admission_allowed())
    if cancel_stop:
        stopped.cancel()
        await asyncio.sleep(0.01)
        stopped.cancel()
    await asyncio.sleep(0.01)
    assert not stopped.done() and not finished.is_set()
    release.set()
    if cancel_stop:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopped, 5)
    else:
        await asyncio.wait_for(stopped, 5)
    assert finished.is_set()
    assert [c.action for c in rig.client.submissions] == ["deploy_container"]
    assert rig.queue.get_job(first["id"])["state"] == "held"
    assert rig.queue.get_job(second["id"])["state"] == "queued"
    assert machine_job(rig) == first["id"]
    assert rig.runtime.status["running"] is False


@pytest.mark.asyncio
async def test_native_failure_during_active_phase_closes_admission_without_interrupting_phase(rig):
    entered, release, finished = block_phase(rig)
    _, job = submit(rig)
    await start(rig)
    await asyncio.wait_for(entered.wait(), 5)
    rig.feed._error(RuntimeError("synthetic source failure"))
    await until(lambda: not rig.runtime.admission_allowed())
    assert not finished.is_set()
    release.set()
    await until(lambda: not rig.runtime.status["running"])
    assert finished.is_set()
    assert [c.action for c in rig.client.submissions] == ["deploy_container"]
    assert rig.queue.get_job(job["id"])["state"] == "held"
    assert machine_job(rig) == job["id"]


@pytest.mark.asyncio
async def test_direct_manager_cancellation_vetoes_next_phase_but_drains_accepted_request(rig):
    entered, release, finished = block_phase(rig)
    _, first = submit(rig, "first")
    _, second = submit(rig, "second")
    await start(rig)
    await asyncio.wait_for(entered.wait(), 5)
    manager = rig.runtime._task
    manager.cancel()
    await until(lambda: not rig.runtime.admission_allowed())
    manager.cancel()
    await asyncio.sleep(0.01)
    assert not manager.done() and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(manager, 5)
    assert finished.is_set()
    assert [c.action for c in rig.client.submissions] == ["deploy_container"]
    assert rig.queue.get_job(first["id"])["state"] == "held"
    assert rig.queue.get_job(second["id"])["state"] == "queued"
    assert machine_job(rig) == first["id"]
    assert rig.feed.available
    rig.owner.assert_active(rig.repo.path)


@pytest.mark.asyncio
async def test_duplicate_start_rejected_without_second_consumer(rig):
    await start(rig)
    await until(lambda: rig.feed.broker.subscriber_count >= 1)
    count = rig.feed.broker.subscriber_count
    with pytest.raises(HubError):
        await rig.runtime.start()
    assert rig.feed.broker.subscriber_count == count


def test_wrong_feed_path_and_second_runtime_binding_rejected(rig, tmp_path):
    with pytest.raises(HubError):
        QueuedPilotRuntime(rig.bridge, NativeHubEventFeed(tmp_path / "different.sqlite3"))
    with pytest.raises(HubError):
        QueuedPilotRuntime(rig.bridge, rig.feed)


@pytest.mark.asyncio
async def test_wrong_process_cannot_start_or_admit(rig, monkeypatch):
    real_pid = os.getpid()
    monkeypatch.setattr("engineering_hub.queued_pilot_runtime.os.getpid", lambda: real_pid + 1)
    with pytest.raises(HubError):
        await rig.runtime.start()
    assert not rig.runtime.admission_allowed() and rig.client.submissions == []


@pytest.mark.asyncio
async def test_wrong_loop_start_rejected_without_new_operation(rig):
    await start(rig)
    def another_loop():
        with pytest.raises(HubError):
            asyncio.run(rig.runtime.start())
    await asyncio.to_thread(another_loop)
    assert rig.client.submissions == []
