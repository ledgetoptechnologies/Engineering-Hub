"""Accepted pilot work survives detached callers and shutdown cancellation."""
import asyncio
import uuid

import pytest

from engineering_hub.owned_tasks import OwnedPilotTasks, finish_owned
from engineering_hub.repository import HubError


async def settle(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 2)


async def test_dedupe_detach_and_repeat_after_completion():
    owner = OwnedPilotTasks()
    await owner.start()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def work():
        calls.append(1)
        entered.set()
        await release.wait()
        return 42

    key = str(uuid.uuid4())
    first = asyncio.create_task(owner.run('advance', key, work))
    await entered.wait()
    second = asyncio.create_task(owner.run('advance', key, work))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert owner.status['inflight'] == 1 and len(calls) == 1
    release.set()
    assert await second == 42
    assert await owner.run('advance', key, work) == 42
    assert calls == [1, 1]
    await owner.stop()
    assert owner.status == {'state': 'stopped', 'inflight': 0, 'max_inflight': 16}


async def test_capacity_allows_duplicate_but_not_different_key():
    owner = OwnedPilotTasks(max_inflight=1)
    await owner.start()
    release = asyncio.Event()
    key = str(uuid.uuid4())
    first = asyncio.create_task(owner.run('recover', key, release.wait))
    await settle(lambda: owner.status['inflight'] == 1)
    duplicate = asyncio.create_task(owner.run('recover', key, release.wait))
    with pytest.raises(HubError, match='pilot_tasks_capacity'):
        await owner.run('advance', key, release.wait)
    release.set()
    await asyncio.gather(first, duplicate)
    await owner.stop()


async def test_detached_failure_is_retrieved_and_capacity_released():
    owner = OwnedPilotTasks(max_inflight=1)
    await owner.start()
    release = asyncio.Event()
    errors = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    try:
        async def work():
            await release.wait()
            raise RuntimeError('secret must not reach status')
        caller = asyncio.create_task(owner.run('recover', str(uuid.uuid4()), work))
        await settle(lambda: owner.status['inflight'] == 1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        await settle(lambda: owner.status['inflight'] == 0)
        assert 'secret' not in str(owner.status)
        await owner.stop()
        assert errors == []
    finally:
        loop.set_exception_handler(previous)


async def test_stop_defers_repeated_cancellation_and_closes_admission():
    owner = OwnedPilotTasks()
    await owner.start()
    release = asyncio.Event()
    caller = asyncio.create_task(owner.run('advance', str(uuid.uuid4()), release.wait))
    await settle(lambda: owner.status['inflight'] == 1)
    stopping = asyncio.create_task(owner.stop())
    await settle(lambda: owner.status['state'] == 'stopping')
    for _ in range(3):
        stopping.cancel()
        await asyncio.sleep(0)
        assert not stopping.done()
    with pytest.raises(HubError, match='pilot_tasks_unavailable'):
        await owner.run('advance', str(uuid.uuid4()), release.wait)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert await caller is True
    assert owner.status['state'] == 'stopped'


async def test_finish_owned_preserves_result_or_failure():
    async def value():
        return 7
    async def fail():
        raise ValueError('private')
    assert await finish_owned(value()) == 7
    with pytest.raises(ValueError):
        await finish_owned(fail())


async def test_finish_owned_retrieves_failure_after_parent_cancellation():
    release = asyncio.Event()
    async def fail():
        await release.wait()
        raise ValueError('private')
    task = asyncio.create_task(finish_owned(fail()))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_finish_owned_propagates_child_cancellation_without_spinning():
    child = asyncio.create_task(asyncio.sleep(0))
    child.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finish_owned(child)


async def test_stop_waits_for_all_children_even_after_one_fails():
    owner = OwnedPilotTasks()
    await owner.start()
    first_release, second_release = asyncio.Event(), asyncio.Event()
    async def fail():
        await first_release.wait()
        raise ValueError('private')
    first = asyncio.create_task(owner.run('recover', str(uuid.uuid4()), fail))
    second = asyncio.create_task(owner.run('advance', str(uuid.uuid4()), second_release.wait))
    await settle(lambda: owner.status['inflight'] == 2)
    stopping = asyncio.create_task(owner.stop())
    await settle(lambda: owner.status['state'] == 'stopping')
    first_release.set()
    with pytest.raises(ValueError):
        await first
    assert not stopping.done()
    second_release.set()
    await second
    await stopping
    await owner.stop()  # Idempotent and still closed.
    assert owner.status['inflight'] == 0


async def test_start_once_and_stop_before_start_fail_closed():
    owner = OwnedPilotTasks()
    with pytest.raises(HubError, match='pilot_tasks_unavailable'):
        await owner.run('advance', str(uuid.uuid4()), asyncio.sleep)
    await owner.start()
    with pytest.raises(HubError, match='pilot_tasks_already_started'):
        await owner.start()
    await owner.stop()
    with pytest.raises(HubError, match='pilot_tasks_already_started'):
        await owner.start()
    other = OwnedPilotTasks()
    await other.stop()
    with pytest.raises(HubError, match='pilot_tasks_already_started'):
        await other.start()


@pytest.mark.parametrize('limit', [0, 65, True, 1.0, None, '16'])
def test_invalid_capacity(limit):
    with pytest.raises(HubError, match='invalid_pilot_task_limit'):
        OwnedPilotTasks(max_inflight=limit)


@pytest.mark.parametrize('kind,run_id,factory', [
    ('inspect', str(uuid.uuid4()), asyncio.sleep),
    (None, str(uuid.uuid4()), asyncio.sleep),
    ('recover', str(uuid.uuid4()).upper(), asyncio.sleep),
    ('advance', 'not-a-uuid', asyncio.sleep),
    ('advance', uuid.uuid4(), asyncio.sleep),
    ('advance', str(uuid.uuid4()), None),
])
async def test_invalid_requests(kind, run_id, factory):
    owner = OwnedPilotTasks()
    await owner.start()
    with pytest.raises(HubError):
        await owner.run(kind, run_id, factory)
    assert owner.status['inflight'] == 0
    await owner.stop()


async def test_bad_factory_and_synchronous_factory_exception_are_cleaned():
    owner = OwnedPilotTasks()
    await owner.start()
    with pytest.raises(HubError, match='invalid_pilot_task_factory'):
        await owner.run('advance', str(uuid.uuid4()), lambda: 5)
    def fail():
        raise ValueError('private')
    with pytest.raises(ValueError):
        await owner.run('advance', str(uuid.uuid4()), fail)
    await settle(lambda: owner.status['inflight'] == 0)
    await owner.stop()
