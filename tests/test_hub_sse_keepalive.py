import asyncio
from contextlib import aclosing

import pytest

from engineering_hub.sse_keepalive import with_keepalive


class ControlledSource:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.reads = 0
        self.active_reads = 0
        self.peak_reads = 0
        self.cancelled_reads = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.reads += 1
        self.active_reads += 1
        self.peak_reads = max(self.active_reads, self.peak_reads)
        try:
            value = await self.queue.get()
            if isinstance(value, BaseException):
                raise value
            return value
        except asyncio.CancelledError:
            self.cancelled_reads += 1
            raise
        finally:
            self.active_reads -= 1

    async def aclose(self):
        assert self.active_reads == 0
        self.closed = True


@pytest.mark.asyncio
async def test_repeated_silence_keeps_same_read_then_event_and_eof():
    source = ControlledSource()
    stream = with_keepalive(source, interval=.005)
    for _ in range(3):
        assert await anext(stream) is None
    assert source.reads == 1 and source.active_reads == 1
    item = {"cursor": 12}
    await source.queue.put(item)
    assert await anext(stream) is item
    assert source.reads == 1 and source.cancelled_reads == 0
    await source.queue.put(StopAsyncIteration())
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert source.closed and source.active_reads == 0 and source.peak_reads == 1
    assert source.reads == 2


@pytest.mark.asyncio
async def test_source_error_propagates_without_extra_heartbeat():
    source = ControlledSource()
    error = RuntimeError("original failure")
    await source.queue.put(error)
    stream = with_keepalive(source, interval=.005)
    with pytest.raises(RuntimeError) as caught:
        await anext(stream)
    assert caught.value is error
    assert source.closed and source.active_reads == 0


@pytest.mark.asyncio
async def test_consumer_cancellation_cancels_awaits_and_closes_source():
    source = ControlledSource()
    stream = with_keepalive(source, interval=15)
    consumer = asyncio.create_task(anext(stream))
    for _ in range(10):
        if source.active_reads:
            break
        await asyncio.sleep(0)
    assert source.active_reads == 1
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert source.closed and source.active_reads == 0 and source.cancelled_reads == 1


@pytest.mark.asyncio
async def test_aclose_while_suspended_at_heartbeat_reaps_pending_read():
    source = ControlledSource()
    stream = with_keepalive(source, interval=.005)
    assert await anext(stream) is None
    await stream.aclose()
    assert source.closed and source.active_reads == 0 and source.cancelled_reads == 1


@pytest.mark.asyncio
async def test_context_managed_early_exit_and_backpressure():
    source = ControlledSource()
    item = object()
    await source.queue.put(item)
    async with aclosing(with_keepalive(source, interval=.005)) as stream:
        async for received in stream:
            assert received is item
            await asyncio.sleep(.01)
            assert source.reads == 1 and source.active_reads == 0
            break
    assert source.closed and source.cancelled_reads == 0


@pytest.mark.asyncio
async def test_real_async_generator_finally_runs_after_pending_cancel():
    closed = asyncio.Event()

    async def source():
        try:
            await asyncio.Event().wait()
            yield "unreachable"
        finally:
            closed.set()

    stream = with_keepalive(source(), interval=.005)
    assert await anext(stream) is None
    await stream.aclose()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_empty_source_emits_no_final_keepalive():
    async def source():
        if False:
            yield None

    assert [item async for item in with_keepalive(source(), interval=.005)] == []


@pytest.mark.parametrize("interval", [True, False, 0, -1, float("nan"), float("inf"),
                                     -float("inf"), 301, "15", None])
def test_invalid_intervals_rejected_immediately(interval):
    with pytest.raises(ValueError, match="interval"):
        with_keepalive(ControlledSource(), interval=interval)
