import asyncio
from contextlib import closing
import json
import sqlite3
import threading

import pytest

from agent_message_bus.sqlite_events import WatcherFailed, native_event_support
from engineering_hub.events import EventBroker, HubOutboxCursorReader, NativeHubEventFeed
from engineering_hub.repository import HubRepository


def test_cursor_reader_only_reads_head_and_accepts_retention_gaps(tmp_path):
    path = tmp_path / "hub.db"
    repo = HubRepository(path)
    task = repo.create_task("human:42", "engineering-hub", "private title", "one")
    reader = HubOutboxCursorReader()
    with closing(sqlite3.connect(path)) as db, db:
        statements = []
        db.set_trace_callback(statements.append)
        assert reader(db, None) == ((), 1)
        assert reader(db, 0) == (("hub",), 1)
        assert reader(db, 1) == ((), 1)
        assert not any("hub_entities" in value or task["id"] in value for value in statements)
        db.execute("DELETE FROM hub_outbox")
        assert reader(db, 0) == (("hub",), 1)
        for cursor in (-1, 2, True, "1"):
            with pytest.raises(WatcherFailed):
                reader(db, cursor)


async def test_subscribe_before_snapshot_and_cancel_unsubscribes():
    broker = EventBroker()
    broker.activate(5)
    events = broker.events()
    first = await anext(events)
    assert broker.subscriber_count == 1
    broker.publish(6)  # change immediately after snapshot cannot be stranded
    assert (await anext(events))["cursor"] == 6
    pending = asyncio.create_task(anext(events))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert broker.subscriber_count == 0
    assert set(first) == {"observer_epoch", "cursor", "kind"}


async def test_slow_subscriber_overflow_is_terminal_unavailable():
    broker = EventBroker(queue_size=2)
    broker.activate(0)
    events = broker.events()
    await anext(events)
    for cursor in range(1, 100):
        broker.publish(cursor)
    event = await anext(events)
    assert event["kind"] == "source_unavailable"
    assert event["cursor"] == 3
    with pytest.raises(StopAsyncIteration):
        await anext(events)
    assert broker.subscriber_count == 0
    assert broker.available  # one slow consumer cannot disable everyone


async def test_duplicates_coalesce_and_backwards_cursor_fails_closed():
    broker = EventBroker(queue_size=1)
    broker.activate(1)
    events = broker.events()
    await anext(events)
    for _ in range(100):
        broker.publish(1)
    broker.publish(2)
    assert (await anext(events))["cursor"] == 2
    broker.publish(0)
    assert (await anext(events))["kind"] == "source_unavailable"
    await events.aclose()


async def test_sse_has_only_safe_pointers_and_closes_children():
    broker = EventBroker()
    broker.activate(5)
    stream = broker.sse()
    frame = await anext(stream)
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    assert set(json.loads(frame[6:])) == {"observer_epoch", "cursor", "kind"}
    await stream.aclose()
    assert broker.subscriber_count == 0


class FakeSource:
    instances = []

    def __init__(self, path, reader, deliver, *, on_error):
        self.deliver, self.on_error = deliver, on_error
        self.healthy = True
        self.stats = {"cursor": 4}
        self.started_thread = self.stopped_thread = None
        self.instances.append(self)

    def start(self):
        self.started_thread = threading.get_ident()
        return dict(self.stats)

    def stop(self):
        self.stopped_thread = threading.get_ident()
        self.healthy = False


async def test_lifecycle_off_loop_native_burst_coalesces_and_error_closes(tmp_path):
    feed = NativeHubEventFeed(tmp_path / "ignored", source_factory=FakeSource,
                              support_check=lambda _: (True, "fixture"))
    loop_thread = threading.get_ident()
    assert (await feed.start())["available"]
    source = FakeSource.instances[-1]
    assert source.started_thread != loop_thread
    stream = feed.broker.events()
    assert (await anext(stream))["cursor"] == 4
    # Synchronous burst occupies the event loop; at most one callback is queued.
    for cursor in range(5, 1005):
        source.deliver(("hub",), cursor)
    assert feed._scheduled
    await asyncio.sleep(0)
    assert (await anext(stream))["cursor"] == 1004
    source.healthy = False
    source.on_error(RuntimeError("secret/path/private title must not escape"))
    await asyncio.sleep(0)
    assert (await anext(stream))["kind"] == "source_unavailable"
    assert feed.status["reason"] == "native_source_failed"
    assert "secret" not in json.dumps(feed.status)
    await stream.aclose()
    await feed.stop()
    assert source.stopped_thread != loop_thread


async def test_restart_uses_new_epoch_and_shutdown_wakes_consumers(tmp_path):
    feed = NativeHubEventFeed(tmp_path / "ignored", source_factory=FakeSource,
                              support_check=lambda _: (True, "fixture"))
    await feed.start()
    epoch = feed.broker.observer_epoch
    stream = feed.broker.events()
    await anext(stream)
    await feed.stop()
    assert (await anext(stream))["kind"] == "source_unavailable"
    await stream.aclose()
    await feed.start()
    assert feed.broker.observer_epoch != epoch
    await feed.stop()


async def test_unsupported_never_constructs_native_source(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("unsupported platform must not construct native source")
    feed = NativeHubEventFeed(tmp_path / "ignored", source_factory=forbidden,
                              support_check=lambda _: (False, "unsupported detail"))
    assert (await feed.start())["reason"] == "native_source_unsupported"
    assert not feed.available
    events = feed.broker.events()
    assert (await anext(events))["kind"] == "source_unavailable"
    with pytest.raises(StopAsyncIteration):
        await anext(events)
    await feed.stop()


async def test_start_failure_sanitized_and_stopped(tmp_path):
    class Broken(FakeSource):
        def start(self):
            raise OSError("private mount and credential")
    feed = NativeHubEventFeed(tmp_path / "ignored", source_factory=Broken,
                              support_check=lambda _: (True, "fixture"))
    status = await feed.start()
    assert not status["available"]
    assert status["reason"] == "native_source_failed"
    assert "credential" not in str(status)
    assert Broken.instances[-1].stopped_thread is not None


async def test_linux_native_external_commit_restart_and_idle_no_reads(tmp_path):
    path = tmp_path / "hub.db"
    repo = HubRepository(path)
    supported, reason = native_event_support(path)
    if not supported:
        pytest.skip(reason)
    feed = NativeHubEventFeed(path)
    try:
        assert (await feed.start())["available"]
        first_epoch = feed.broker.observer_epoch
        stream = feed.broker.events()
        first = await anext(stream)
        assert first["cursor"] == 0
        source = feed._source
        await asyncio.to_thread(source.flush_pending)
        before = source.stats["snapshot_reads"]
        # Bounded acceptance observation, not application polling/timer work.
        await asyncio.sleep(0.05)
        assert source.stats["snapshot_reads"] == before
        # Independent connection/process-style writer, not broker.publish().
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO hub_outbox(action,entity_id,revision,created_at) VALUES ('private','private',1,'now')")
        pointer = await asyncio.wait_for(anext(stream), timeout=5)
        assert pointer["cursor"] == 1
        assert set(pointer) == {"observer_epoch", "cursor", "kind"}
        # WAL writes alone are not commits: native arbitration must wait until
        # the external writer commits, then emit the durable head exactly once.
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO hub_outbox(action,entity_id,revision,created_at) VALUES ('private','private',1,'now')")
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.05)
            assert not pending.done()
            db.commit()
        assert (await asyncio.wait_for(pending, timeout=5))["cursor"] == 2
        await stream.aclose()
        await feed.stop()
        repo.create_task("human:42", "engineering-hub", "offline change", "restart")
        assert (await feed.start())["available"]
        assert feed.broker.observer_epoch != first_epoch
        restarted = feed.broker.events()
        assert (await anext(restarted))["cursor"] == 3
        await restarted.aclose()
    finally:
        await feed.stop()
