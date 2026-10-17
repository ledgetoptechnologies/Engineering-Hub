"""Event-driven runtime ownership: no timers, unsafe retries or cancellations."""
import asyncio

import pytest

from engineering_hub.autonomy import StagingRuntime
from engineering_hub.events import EventBroker


class Feed:
    def __init__(self, available=True):
        self.broker = EventBroker()
        if available:
            self.broker.activate(0)

    @property
    def available(self):
        return self.broker.available


class Dispatcher:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []
        self.called = asyncio.Event()

    async def drain_batch(self, *, limit):
        self.calls.append(limit)
        result = self.results.pop(0) if self.results else batch()
        self.called.set()
        return result


def batch(**changes):
    return {"examined": 1, "dispatched": 0, "cursor": 1, "paused": False, "more": False, **changes}


async def settle(predicate):
    async def check():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(check(), timeout=2)


async def test_unavailable_source_never_dispatches_or_registers_subscriber():
    feed, dispatcher = Feed(False), Dispatcher()
    runtime = StagingRuntime(dispatcher, feed)
    status = await runtime.start()
    assert status["state"] == "native_source_unavailable"
    assert status["running"] is False and dispatcher.calls == []
    assert feed.broker.subscriber_count == 0
    await runtime.stop()


async def test_startup_snapshot_catches_up_then_only_new_pointer_drains():
    feed, dispatcher = Feed(), Dispatcher()
    runtime = StagingRuntime(dispatcher, feed)
    try:
        await runtime.start()
        await settle(lambda: len(dispatcher.calls) == 1 and feed.broker.subscriber_count == 1)
        before = len(dispatcher.calls)
        await asyncio.sleep(0.01)  # Bounded test observation, not application polling.
        assert len(dispatcher.calls) == before
        feed.broker.publish(1)
        await settle(lambda: len(dispatcher.calls) == 2)
        assert dispatcher.calls == [1, 1]
    finally:
        await runtime.stop()
    assert feed.broker.subscriber_count == 0
    assert runtime.status["state"] == "stopped"


async def test_coalesced_pointer_drains_all_more_batches_without_another_event():
    feed, dispatcher = Feed(), Dispatcher([batch(more=True), batch(more=True), batch()])
    runtime = StagingRuntime(dispatcher, feed)
    try:
        await runtime.start()
        await settle(lambda: len(dispatcher.calls) == 3)
        assert dispatcher.calls == [1, 1, 1]
        assert runtime.last_result["more"] is False
        await asyncio.sleep(0.01)
        assert len(dispatcher.calls) == 3
    finally:
        await runtime.stop()


async def test_pause_with_more_does_not_hot_loop_and_new_event_can_resume():
    feed, dispatcher = Feed(), Dispatcher([batch(paused=True, more=True), batch()])
    runtime = StagingRuntime(dispatcher, feed)
    try:
        await runtime.start()
        await settle(lambda: len(dispatcher.calls) == 1)
        await asyncio.sleep(0.01)
        assert len(dispatcher.calls) == 1
        feed.broker.publish(1)
        await settle(lambda: len(dispatcher.calls) == 2)
    finally:
        await runtime.stop()


async def test_source_failure_wakes_idle_wait_and_exits_without_dispatch():
    feed, dispatcher = Feed(), Dispatcher()
    runtime = StagingRuntime(dispatcher, feed)
    try:
        await runtime.start()
        await settle(lambda: len(dispatcher.calls) == 1)
        feed.broker.fail()
        await settle(lambda: not runtime.status["running"])
        assert runtime.status["state"] == "native_source_unavailable"
        assert len(dispatcher.calls) == 1 and feed.broker.subscriber_count == 0
    finally:
        await runtime.stop()


class BlockingDispatcher(Dispatcher):
    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()
        self.cancelled = False

    async def drain_batch(self, *, limit):
        self.calls.append(limit)
        self.called.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return batch(more=True)


async def test_stop_waits_active_bounded_workflow_and_never_starts_next_batch():
    feed, dispatcher = Feed(), BlockingDispatcher()
    runtime = StagingRuntime(dispatcher, feed)
    await runtime.start()
    await asyncio.wait_for(dispatcher.called.wait(), 2)
    stopped = asyncio.create_task(runtime.stop())
    try:
        await asyncio.sleep(0)
        assert not stopped.done() and not dispatcher.cancelled
    finally:
        dispatcher.release.set()
        await asyncio.wait_for(stopped, 2)
    assert dispatcher.calls == [1]
    assert not dispatcher.cancelled and feed.broker.subscriber_count == 0


async def test_source_failure_during_active_work_stops_at_next_safe_boundary():
    feed, dispatcher = Feed(), BlockingDispatcher()
    runtime = StagingRuntime(dispatcher, feed)
    await runtime.start()
    try:
        await asyncio.wait_for(dispatcher.called.wait(), 2)
        feed.broker.fail()
        dispatcher.release.set()
        await settle(lambda: not runtime.status["running"])
        assert dispatcher.calls == [1]
        assert not dispatcher.cancelled
        assert runtime.status["state"] == "native_source_unavailable"
    finally:
        await runtime.stop()


async def test_unexpected_dispatch_failure_has_safe_terminal_status():
    class BrokenDispatcher(Dispatcher):
        async def drain_batch(self, *, limit):
            raise RuntimeError("private-service-secret-and-host")

    feed, dispatcher = Feed(), BrokenDispatcher()
    runtime = StagingRuntime(dispatcher, feed)
    await runtime.start()
    try:
        await settle(lambda: not runtime.status["running"])
        assert runtime.status["state"] == "failed"
        assert "private-service" not in str(runtime.status)
        assert feed.broker.subscriber_count == 0
    finally:
        await runtime.stop()


async def test_cannot_start_duplicate_subscriber():
    runtime = StagingRuntime(Dispatcher(), Feed())
    await runtime.start()
    try:
        with pytest.raises(RuntimeError, match="already_started"):
            await runtime.start()
    finally:
        await runtime.stop()
