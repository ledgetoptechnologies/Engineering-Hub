"""Opt-in transient write-lock recovery for the native SQLite event source.

Real SQLite and Linux inotify where available: another connection holds the
write lock while a native event arrives, then releases or keeps it.
"""
import asyncio
from contextlib import closing
from functools import partial
import math
import os
import sqlite3
import threading
import time

import pytest

from agent_message_bus.sqlite_events import SQLiteEventSource, WatcherFailed, native_event_support
from engineering_hub.events import HubOutboxCursorReader, NativeHubEventFeed
from engineering_hub.repository import HubRepository


def ledger(tmp_path):
    path = tmp_path / "hub.db"
    HubRepository(path)
    supported, reason = native_event_support(path)
    if not supported:
        pytest.skip(reason)
    # Scratch table for spilling uncommitted pages into the WAL (see poke).
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE spill(x BLOB)")
    return path


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def insert_outbox(db):
    db.execute("INSERT INTO hub_outbox(action,entity_id,revision,created_at) VALUES ('x','x',1,'now')")


class Recorder:
    def __init__(self):
        self.deliveries, self.states, self.errors = [], [], []

    def deliver(self, _recipients, cursor):
        self.deliveries.append(cursor)


def source(path, recorder, **options):
    return SQLiteEventSource(path, HubOutboxCursorReader(), recorder.deliver, busy_timeout_ms=200,
                             on_error=recorder.errors.append, on_state=recorder.states.append, **options)


def hold_lock(path):
    holder = sqlite3.connect(path, isolation_level=None)
    # A tiny page cache makes the holder's uncommitted writes spill into the WAL.
    holder.execute("PRAGMA cache_size=2")
    holder.execute("BEGIN IMMEDIATE")
    return holder


def poke(holder):
    # Real uncommitted WAL writes by the lock holder: a relevant native event,
    # so the source reconciles and meets the held lock. (Touching the database
    # file instead would trip the separate replacement guard.)
    for _ in range(32):
        holder.execute("INSERT INTO spill VALUES (randomblob(65536))")


def test_runtime_lock_suspends_then_resumes_with_changes_committed_meanwhile(tmp_path):
    path = ledger(tmp_path)
    recorder = Recorder()
    events = source(path, recorder, transient_busy_seconds=30)
    events.start()
    holder = hold_lock(path)
    try:
        assert events.stats["cursor"] == 0
        insert_outbox(holder)
        poke(holder)
        assert wait_until(lambda: events.stats["suspended"])
        assert not events.healthy and events.failure is None
        assert recorder.states == ["suspended"] and recorder.errors == []
        assert events.stats["sqlite_errorname"] == "SQLITE_BUSY"
        assert events.stats["failure_stage"] == "acquire_write_barrier"
        # Several retries fail while the lock is held; none of them is fatal.
        time.sleep(0.8)
        assert events.stats["suspended"] and events.failure is None
        holder.commit()
        # A second writer commits before the owed reconcile can run.
        with closing(sqlite3.connect(path)) as other, other:
            insert_outbox(other)
        assert wait_until(lambda: events.healthy and events.stats["cursor"] == 2)
        assert recorder.states == ["suspended", "resumed"] and recorder.errors == []
        assert recorder.deliveries and recorder.deliveries[-1] == 2
        stats = events.stats
        assert stats["suspended"] is False and stats["suspensions"] == 1
        assert stats["sqlite_errorname"] is None and stats["failure_stage"] is None
    finally:
        holder.close()
        events.stop()


def test_lock_held_past_the_bound_fails_closed(tmp_path):
    path = ledger(tmp_path)
    recorder = Recorder()
    events = source(path, recorder, transient_busy_seconds=0.5)
    events.start()
    holder = hold_lock(path)
    try:
        poke(holder)
        assert wait_until(lambda: events.failure is not None, timeout=5)
        assert isinstance(events.failure, WatcherFailed)
        assert not events.healthy and len(recorder.errors) == 1
        assert recorder.states == ["suspended"]
        assert events.stats["failure_stage"] == "transient_lock_bound"
    finally:
        holder.close()
        events.stop()


def test_disabled_by_default_keeps_failing_closed(tmp_path):
    path = ledger(tmp_path)
    recorder = Recorder()
    events = source(path, recorder)
    events.start()
    holder = hold_lock(path)
    try:
        poke(holder)
        assert wait_until(lambda: events.failure is not None)
        assert events.stats["sqlite_errorname"] == "SQLITE_BUSY"
        assert recorder.states == [] and events.stats["suspensions"] == 0
    finally:
        holder.close()
        events.stop()


def test_startup_never_waits_out_a_held_lock(tmp_path):
    path = ledger(tmp_path)
    recorder = Recorder()
    events = source(path, recorder, transient_busy_seconds=30)
    holder = hold_lock(path)
    try:
        with pytest.raises(WatcherFailed):
            events.start()
        assert recorder.states == [] and not events.healthy
    finally:
        holder.close()
        events.stop()


@pytest.mark.parametrize("locked", [False, True])
def test_database_entry_change_still_fails_closed_when_enabled(tmp_path, locked):
    path = ledger(tmp_path)
    recorder = Recorder()
    events = source(path, recorder, transient_busy_seconds=30)
    events.start()
    holder = hold_lock(path) if locked else None
    try:
        if locked:
            poke(holder)
            assert wait_until(lambda: events.stats["suspended"])
        # A database directory entry change is never a transient lock, suspended or not.
        os.utime(path)
        assert wait_until(lambda: events.failure is not None)
        assert isinstance(events.failure, WatcherFailed) and "directory entry" in str(events.failure)
        assert not events.healthy
    finally:
        if holder is not None:
            holder.close()
        events.stop()


def test_stop_is_prompt_while_suspended(tmp_path):
    path = ledger(tmp_path)
    recorder = Recorder()
    events = source(path, recorder, transient_busy_seconds=30)
    events.start()
    holder = hold_lock(path)
    try:
        poke(holder)
        assert wait_until(lambda: events.stats["suspended"])
        began = time.monotonic()
        stats = events.stop(timeout=5)
        assert time.monotonic() - began < 2
        assert stats["watcher_stopped"] and events.failure is None
    finally:
        holder.close()
        events.stop()


def test_release_after_the_bound_never_resumes_or_delivers(tmp_path, monkeypatch):
    from agent_message_bus import sqlite_events
    # Retries are rarer than the bound, so only the bound's own clock can end this.
    monkeypatch.setattr(sqlite_events, "_RETRY_INTERVAL_SECONDS", 3.0)
    path = ledger(tmp_path)
    recorder = Recorder()
    events = source(path, recorder, transient_busy_seconds=0.5)
    events.start()
    holder = hold_lock(path)
    try:
        insert_outbox(holder)
        poke(holder)
        assert wait_until(lambda: events.stats["suspended"])
        time.sleep(0.8)
        holder.commit()
        assert wait_until(lambda: events.failure is not None, timeout=4)
        time.sleep(0.5)
        assert recorder.deliveries == [] and recorder.states == ["suspended"]
        assert events.stats["cursor"] == 0 and not events.healthy
        assert events.stats["failure_stage"] == "transient_lock_bound"
    finally:
        holder.close()
        events.stop()


class Flood:
    """Unrelated directory churn: native readiness that never marks the ledger dirty."""

    def __init__(self, directory):
        self.directory, self.running = directory, True
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        index = 0
        while self.running:
            noise = self.directory / f"noise-{index % 4}"
            noise.write_bytes(b"x")
            noise.unlink()
            index += 1
            time.sleep(0.002)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.running = False
        self.thread.join(timeout=5)


def test_readiness_flood_cannot_postpone_the_bound(tmp_path):
    path = ledger(tmp_path)
    recorder = Recorder()
    events = source(path, recorder, transient_busy_seconds=0.6)
    events.start()
    holder = hold_lock(path)
    try:
        poke(holder)
        assert wait_until(lambda: events.stats["suspended"])
        with Flood(tmp_path):
            began = time.monotonic()
            assert wait_until(lambda: events.failure is not None, timeout=4)
            assert time.monotonic() - began < 2
            assert events.stats["failure_stage"] == "transient_lock_bound"
    finally:
        holder.close()
        events.stop()


def test_readiness_flood_cannot_postpone_the_retry(tmp_path):
    path = ledger(tmp_path)
    recorder = Recorder()
    events = source(path, recorder, transient_busy_seconds=30)
    events.start()
    holder = hold_lock(path)
    try:
        insert_outbox(holder)
        poke(holder)
        assert wait_until(lambda: events.stats["suspended"])
        with Flood(tmp_path):
            holder.commit()
            assert wait_until(lambda: events.healthy and events.stats["cursor"] == 1, timeout=3)
            assert recorder.states == ["suspended", "resumed"] and events.failure is None
            began = time.monotonic()
            events.stop(timeout=5)
            assert time.monotonic() - began < 2
    finally:
        holder.close()
        events.stop()


def test_stop_is_prompt_while_suspended_under_a_flood(tmp_path):
    path = ledger(tmp_path)
    events = source(path, Recorder(), transient_busy_seconds=30)
    events.start()
    holder = hold_lock(path)
    try:
        poke(holder)
        assert wait_until(lambda: events.stats["suspended"])
        with Flood(tmp_path):
            began = time.monotonic()
            events.stop(timeout=5)
            assert time.monotonic() - began < 2 and events.failure is None
    finally:
        holder.close()
        events.stop()


@pytest.mark.parametrize("failing", ["suspended", "resumed"])
def test_a_failing_state_callback_fails_the_source_closed(tmp_path, failing):
    path = ledger(tmp_path)
    recorder = Recorder()

    def on_state(state):
        recorder.states.append(state)
        if state == failing:
            raise RuntimeError("consumer could not record the transition")

    events = SQLiteEventSource(path, HubOutboxCursorReader(), recorder.deliver, busy_timeout_ms=200,
                               on_error=recorder.errors.append, on_state=on_state,
                               transient_busy_seconds=30)
    events.start()
    holder = hold_lock(path)
    try:
        insert_outbox(holder)
        poke(holder)
        if failing == "resumed":
            assert wait_until(lambda: events.stats["suspended"])
            holder.commit()
        assert wait_until(lambda: events.failure is not None)
        assert isinstance(events.failure, WatcherFailed) and not events.healthy
        assert len(recorder.errors) == 1 and recorder.states[-1] == failing
    finally:
        holder.close()
        events.stop()


@pytest.mark.parametrize("outcome", ["accept", "raise"])
def test_never_healthy_until_the_resume_callback_is_accepted(tmp_path, outcome):
    path = ledger(tmp_path)
    recorder = Recorder()
    gate, entered = threading.Event(), threading.Event()
    healthy_seen = []

    def on_state(state):
        recorder.states.append(state)
        if state == "resumed":
            entered.set()
            gate.wait(5)
            if outcome == "raise":
                raise RuntimeError("consumer refused the transition")

    events = SQLiteEventSource(path, HubOutboxCursorReader(), recorder.deliver, busy_timeout_ms=200,
                               on_error=recorder.errors.append, on_state=on_state,
                               transient_busy_seconds=30)
    events.start()
    holder = hold_lock(path)
    try:
        insert_outbox(holder)
        poke(holder)
        assert wait_until(lambda: events.stats["suspended"])
        holder.commit()
        assert entered.wait(5)
        # The callback is blocked: the changes were read, but nothing may look healthy yet.
        for _ in range(10):
            healthy_seen.append(events.healthy)
            time.sleep(0.02)
        assert healthy_seen == [False] * 10 and events.stats["cursor"] == 1
        gate.set()
        if outcome == "accept":
            assert wait_until(lambda: events.healthy)
            assert events.failure is None and recorder.errors == []
        else:
            assert wait_until(lambda: events.failure is not None)
            assert isinstance(events.failure, WatcherFailed) and not events.healthy
            assert len(recorder.errors) == 1
    finally:
        gate.set()
        holder.close()
        events.stop()


@pytest.mark.parametrize("options", [
    {"transient_busy_seconds": -1}, {"transient_busy_seconds": 3601}, {"transient_busy_seconds": True},
    {"transient_busy_seconds": math.nan}, {"transient_busy_seconds": "1"},
    {"retry_busy_timeout_ms": 0}, {"retry_busy_timeout_ms": 201}, {"retry_busy_timeout_ms": True},
])
def test_options_are_validated(tmp_path, options):
    path = ledger(tmp_path)
    with pytest.raises(ValueError):
        source(path, Recorder(), **options)


async def test_hub_feed_tells_viewers_and_reopens_under_a_new_epoch(tmp_path):
    path = ledger(tmp_path)
    feed = NativeHubEventFeed(path, transient_busy_seconds=30,
                              source_factory=partial(SQLiteEventSource, busy_timeout_ms=200))
    holder = None
    try:
        assert (await feed.start())["available"]
        epoch = feed.broker.observer_epoch
        stream = feed.broker.events()
        assert (await anext(stream))["cursor"] == 0
        holder = hold_lock(path)
        insert_outbox(holder)
        poke(holder)
        ended = await asyncio.wait_for(anext(stream), timeout=5)
        assert ended["kind"] == "source_unavailable"
        assert feed.status["reason"] == "native_source_suspended" and not feed.status["available"]
        refused = feed.broker.events()
        assert (await anext(refused))["kind"] == "source_unavailable"
        await refused.aclose()
        holder.commit()
        deadline = time.monotonic() + 5
        while not feed.available and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert feed.available and feed.status["reason"] == "native_source_active"
        assert feed.broker.observer_epoch != epoch
        reopened = feed.broker.events()
        assert (await anext(reopened))["cursor"] == 1
        await reopened.aclose()
    finally:
        if holder is not None:
            holder.close()
        await feed.stop()


class StateFake:
    instances = []

    def __init__(self, path, reader, deliver, *, on_error, **options):
        self.deliver, self.on_error, self.options = deliver, on_error, options
        self.on_state = options.get("on_state")
        self.healthy = True
        self.stats = {"cursor": 4, "suspended": False}
        self.instances.append(self)

    def start(self):
        return dict(self.stats)

    def stop(self):
        self.healthy = False


async def test_feed_passes_transient_options_only_when_enabled(tmp_path):
    off = NativeHubEventFeed(tmp_path / "ignored", source_factory=StateFake,
                             support_check=lambda _: (True, "fixture"))
    await off.start()
    assert StateFake.instances[-1].options == {}
    await off.stop()
    on = NativeHubEventFeed(tmp_path / "ignored", source_factory=StateFake,
                            support_check=lambda _: (True, "fixture"), transient_busy_seconds=5)
    await on.start()
    assert StateFake.instances[-1].options["transient_busy_seconds"] == 5
    assert callable(StateFake.instances[-1].options["on_state"])
    await on.stop()


async def test_suspend_then_resume_before_drain_still_forces_reconnect(tmp_path):
    feed = NativeHubEventFeed(tmp_path / "ignored", source_factory=StateFake,
                              support_check=lambda _: (True, "fixture"), transient_busy_seconds=5)
    await feed.start()
    fake = StateFake.instances[-1]
    epoch = feed.broker.observer_epoch
    stream = feed.broker.events()
    await anext(stream)
    # Both transitions land before the event loop drains them.
    fake.on_state("suspended")
    fake.stats["cursor"] = 9
    fake.deliver(("hub",), 9)
    fake.on_state("resumed")
    await asyncio.sleep(0)
    assert (await anext(stream))["kind"] == "source_unavailable"
    assert feed.available and feed.broker.observer_epoch != epoch and feed.broker.cursor == 9
    await stream.aclose()
    await feed.stop()


@pytest.mark.parametrize("transitions", [["suspended"], ["suspended", "resumed"]])
async def test_feed_is_unavailable_until_the_loop_has_moved_the_broker(tmp_path, transitions):
    feed = NativeHubEventFeed(tmp_path / "ignored", source_factory=StateFake,
                              support_check=lambda _: (True, "fixture"), transient_busy_seconds=5)
    await feed.start()
    fake = StateFake.instances[-1]
    epoch = feed.broker.observer_epoch
    assert feed.available
    # The event loop hasn't run yet: the broker still holds the old epoch and
    # the source reports healthy, yet the reported transition shuts the gate.
    for state in transitions:
        fake.on_state(state)
    assert fake.healthy and feed.broker.available and feed.broker.observer_epoch == epoch
    assert not feed.available and not feed.status["available"]
    await asyncio.sleep(0)
    if transitions[-1] == "resumed":
        assert feed.available and feed.broker.observer_epoch != epoch
    else:
        assert not feed.available and feed.status["reason"] == "native_source_suspended"
    await feed.stop()


async def test_a_transition_reported_during_a_drain_keeps_the_gate_shut(tmp_path):
    feed = NativeHubEventFeed(tmp_path / "ignored", source_factory=StateFake,
                              support_check=lambda _: (True, "fixture"), transient_busy_seconds=5)
    await feed.start()
    fake = StateFake.instances[-1]
    original = feed._apply

    def apply_then_report_again(cursor, error, state):
        original(cursor, error, state)
        if state == "resumed":
            fake.on_state("suspended")   # lands after the snapshot, before the gate clears

    feed._apply = apply_then_report_again
    fake.on_state("suspended")
    fake.on_state("resumed")
    await asyncio.sleep(0)
    assert not feed.available           # the later suspension still holds the gate
    await asyncio.sleep(0)
    assert not feed.available and feed.status["reason"] == "native_source_suspended"
    await feed.stop()


async def test_an_error_during_a_pending_transition_leaves_the_feed_failed(tmp_path):
    feed = NativeHubEventFeed(tmp_path / "ignored", source_factory=StateFake,
                              support_check=lambda _: (True, "fixture"), transient_busy_seconds=5)
    await feed.start()
    fake = StateFake.instances[-1]
    fake.on_state("suspended")
    fake.on_state("resumed")
    fake.on_error(RuntimeError("private detail"))
    assert not feed.available
    await asyncio.sleep(0)
    # The error outranks the resume: no reopened epoch, and the gate's clearing
    # can't make a failed broker available.
    assert not feed.available and not feed.broker.available
    assert feed.status["reason"] == "native_source_failed"
    await feed.stop()


async def test_stop_during_a_pending_transition_leaves_nothing_available(tmp_path):
    feed = NativeHubEventFeed(tmp_path / "ignored", source_factory=StateFake,
                              support_check=lambda _: (True, "fixture"), transient_busy_seconds=5)
    await feed.start()
    fake = StateFake.instances[-1]
    fake.on_state("suspended")
    fake.on_state("resumed")
    assert not feed.available
    await feed.stop()
    await asyncio.sleep(0)   # the drain queued before stop must not reopen anything
    assert not feed.available and not feed.broker.available
    assert feed.status["reason"] == "stopped"


async def test_a_failed_broker_transition_fails_closed_until_restart(tmp_path):
    feed = NativeHubEventFeed(tmp_path / "ignored", source_factory=StateFake,
                              support_check=lambda _: (True, "fixture"), transient_busy_seconds=5)
    await feed.start()
    fake = StateFake.instances[-1]
    stream = feed.broker.events()
    await anext(stream)
    original, calls = feed._apply, []

    def broken(cursor, error, state):
        calls.append(state)
        if len(calls) == 1:
            raise RuntimeError("transition broke partway")
        original(cursor, error, state)

    feed._apply = broken
    fake.on_state("resumed")
    await asyncio.sleep(0)
    # The old epoch's stream is ended and nothing is available, with a stable reason.
    assert (await anext(stream))["kind"] == "source_unavailable"
    assert not feed.available and not feed.broker.available
    assert feed.status["reason"] == "native_source_failed"
    # A later resume can't reopen a feed whose transition failed.
    fake.on_state("resumed")
    await asyncio.sleep(0)
    assert not feed.available and not feed.broker.available
    await feed.stop()
    feed._apply = original
    await feed.start()
    assert feed.available
    await feed.stop()


class StartupFake(StateFake):
    script = ()

    def start(self):
        for state in self.script:
            if state == "suspended":
                self.healthy = False
                self.stats["suspended"] = True
            else:
                self.healthy = True
                self.stats["suspended"] = False
            self.on_state(state)
        return dict(self.stats)


@pytest.mark.parametrize("script", [("suspended",), ("suspended", "resumed")])
async def test_a_transition_during_startup_is_gated(tmp_path, script):
    StartupFake.script = script
    feed = NativeHubEventFeed(tmp_path / "ignored", source_factory=StartupFake,
                              support_check=lambda _: (True, "fixture"), transient_busy_seconds=5)
    status = await feed.start()
    fake = StartupFake.instances[-1]
    started_epoch = feed.broker.observer_epoch
    # start() has returned but the reported transitions haven't been drained yet.
    assert not status["available"] and not feed.available
    await asyncio.sleep(0)
    if script[-1] == "suspended":
        assert not feed.available and feed.status["reason"] == "native_source_suspended"
        fake.healthy = True
        fake.stats["suspended"] = False
        fake.on_state("resumed")
        await asyncio.sleep(0)
    assert feed.available and feed.broker.observer_epoch != started_epoch
    await feed.stop()


async def test_callbacks_from_a_stopped_source_cannot_move_a_restarted_feed(tmp_path):
    feed = NativeHubEventFeed(tmp_path / "ignored", source_factory=StateFake,
                              support_check=lambda _: (True, "fixture"), transient_busy_seconds=5)
    await feed.start()
    old = StateFake.instances[-1]
    await feed.stop()
    await feed.start()
    current = StateFake.instances[-1]
    assert current is not old and feed.available
    epoch, cursor = feed.broker.observer_epoch, feed.broker.cursor
    old.on_state("suspended")
    old.on_error(RuntimeError("late failure from the old source"))
    old.deliver(("hub",), cursor + 50)
    assert feed.available
    await asyncio.sleep(0)
    assert feed.available and feed.broker.observer_epoch == epoch and feed.broker.cursor == cursor
    assert feed.status["reason"] == "native_source_active"
    await feed.stop()


@pytest.mark.parametrize("value", [-1, 3601, True, math.nan, "5"])
def test_feed_option_is_validated(tmp_path, value):
    with pytest.raises(ValueError):
        NativeHubEventFeed(tmp_path / "ignored", transient_busy_seconds=value)
