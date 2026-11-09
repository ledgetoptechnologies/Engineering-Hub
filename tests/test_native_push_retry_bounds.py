"""Real-lock bounds when SQLite's own busy_timeout is longer than the retry budget.

Reproduces the review of PR #146: each blocked BEGIN IMMEDIATE waits up to
busy_timeout inside SQLite, so the retry budget and stop() must account for time
spent blocked in SQL, not only the sleeps between attempts.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from agent_message_bus.database import MessageRepository
from agent_message_bus.sqlite_events import MessageCursorReader, SQLiteEventSource, sys_platform_linux

pytestmark = pytest.mark.skipif(not sys_platform_linux(), reason="native SQLite events require Linux")

BUSY_TIMEOUT_MS = 2_000      # SQLite blocks this long per attempt, by itself.
BUDGET_MS = 500              # Shorter than one blocked attempt.
SLACK_S = 0.6                # Scheduling, one backoff and thread hand-off.


def build(tmp_path, **kwargs):
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    deliveries, failures, failed_at = [], [], []
    def on_error(exc):
        failed_at.append(time.monotonic())
        failures.append(exc)
    source = SQLiteEventSource(repository.path, MessageCursorReader(page_size=4),
                               lambda recipients, cursor: deliveries.append((tuple(sorted(recipients)), cursor)),
                               on_error=on_error, busy_timeout_ms=BUSY_TIMEOUT_MS,
                               transient_retry_initial_ms=20, transient_retry_max_ms=100,
                               transient_retry_budget_ms=BUDGET_MS, **kwargs)
    return repository, source, deliveries, failures, failed_at


def hold_lock(repository):
    locker = sqlite3.connect(repository.path, timeout=1, isolation_level=None, check_same_thread=False)
    locker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    os.utime(str(repository.path) + "-wal")  # Genuine inotify hint; the source's BEGIN IMMEDIATE now blocks.
    return locker, started


def wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_budget_includes_time_blocked_inside_sqlite(tmp_path):
    """Fail-closed must come within the budget (plus slack) of the first blocked attempt,
    even though one SQLite busy wait alone is longer than the budget."""
    repository, source, deliveries, failures, failed_at = build(tmp_path)
    source.start()
    locker, started = hold_lock(repository)
    try:
        assert wait_until(lambda: failures, timeout=BUSY_TIMEOUT_MS / 1000 * 4)
        elapsed = failed_at[0] - started
        assert elapsed <= BUDGET_MS / 1000 + SLACK_S, f"failed closed after {elapsed:.2f}s"
        assert len(failures) == 1 and not source.healthy
    finally:
        locker.close()
        source.stop()


def test_late_release_inside_budget_recovers_and_delivers(tmp_path):
    repository, source, deliveries, failures, _ = build(tmp_path)
    source.start()
    locker, _ = hold_lock(repository)
    try:
        time.sleep(BUDGET_MS / 1000 * 0.5)
        locker.execute("INSERT INTO messages (id,sender,recipient,message_type,body_json,created_at) "
                       "VALUES ('late','hermes','codex','event','{}','2026-01-01T00:00:00Z')")
        locker.execute("INSERT INTO message_delivery_sequence (message_id) VALUES ('late')")
        locker.execute("COMMIT")
        assert source.wait_for_cursor(repository.delivery_head(), timeout=5)
        assert failures == [] and source.healthy
        assert ("codex",) in [names for names, _ in deliveries]
    finally:
        locker.close()
        source.stop()


def test_stop_while_blocked_inside_sqlite_is_prompt_and_clean(tmp_path):
    """stop() must not wait out SQLite's busy_timeout while the source is blocked in BEGIN IMMEDIATE."""
    repository, source, deliveries, failures, _ = build(tmp_path, )
    source.start()
    locker, _ = hold_lock(repository)
    try:
        time.sleep(0.15)  # The source is now inside a blocked BEGIN IMMEDIATE.
        began = time.monotonic()
        source.stop(timeout=10)
        took = time.monotonic() - began
        assert took < 0.5, f"stop took {took:.2f}s while SQLite was blocked"
        assert failures == [] and source.failure is None
    finally:
        locker.close()


def test_success_after_budget_is_not_delivered_as_recovery(tmp_path):
    """A lock released just after the budget expired must leave the source failed closed,
    not recover through a late successful attempt."""
    repository, source, deliveries, failures, failed_at = build(tmp_path)
    source.start()
    locker, started = hold_lock(repository)
    release = threading.Timer(BUDGET_MS / 1000 + 0.3, lambda: locker.execute("COMMIT"))
    release.start()
    try:
        assert wait_until(lambda: failures, timeout=BUSY_TIMEOUT_MS / 1000 * 4)
        time.sleep(0.3)
        assert not source.healthy and len(failures) == 1
    finally:
        release.join()
        locker.close()
        source.stop()


def test_stop_during_multi_second_busy_wait_is_bounded_by_the_slice_not_interrupt(tmp_path, monkeypatch):
    """With interrupt disabled, stop latency comes from the per-attempt SQL slice alone."""
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    failures = []
    source = SQLiteEventSource(repository.path, MessageCursorReader(page_size=4), lambda *_: None,
                               on_error=failures.append, busy_timeout_ms=5_000,
                               transient_retry_initial_ms=50, transient_retry_max_ms=200,
                               transient_retry_budget_ms=60_000, transient_busy_slice_ms=100)
    monkeypatch.setattr(source, "_interrupt_sql", lambda: None)
    source.start()
    locker, _ = hold_lock(repository)
    try:
        assert wait_until(lambda: source.stats["transient_retries"] >= 2, timeout=3)
        began = time.monotonic()
        source.stop(timeout=10)
        took = time.monotonic() - began
        assert took < 0.35, f"stop took {took:.2f}s with a 5 s busy_timeout"
        assert failures == [] and source.failure is None
    finally:
        locker.close()


def test_non_interrupt_error_during_stop_still_fails_closed(tmp_path):
    """A real failure is reported even if a stop was requested at the same moment."""
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    failures = []
    reader = MessageCursorReader(page_size=4)
    holder = {}

    def failing_reader(conn, cursor):
        if cursor is not None and holder.get("armed"):
            holder["source"]._stop_requested.set()  # The stop flag is set when the I/O error lands.
            exc = sqlite3.OperationalError("disk I/O error")
            exc.sqlite_errorcode = 10  # SQLITE_IOERR
            raise exc
        return reader(conn, cursor)

    source = SQLiteEventSource(repository.path, failing_reader, lambda *_: None, on_error=failures.append,
                               busy_timeout_ms=200, transient_busy_slice_ms=50)
    holder["source"] = source
    source.start()
    try:
        holder["armed"] = True
        repository.insert(sender="hermes", recipient="codex", message_type="event", body={})
        assert wait_until(lambda: failures, timeout=3)
        assert len(failures) == 1 and failures[0].sqlite_errorcode == 10 and not source.healthy
    finally:
        source.stop()


def test_late_success_is_bounded_to_one_slice_past_the_deadline(tmp_path):
    """A snapshot that began before the deadline may finish after it, but by at most one slice."""
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    deliveries, failures = [], []
    delivered_at = []
    def deliver(recipients, cursor):
        delivered_at.append(time.monotonic())
        deliveries.append(tuple(recipients))
    source = SQLiteEventSource(repository.path, MessageCursorReader(page_size=4), deliver,
                               on_error=failures.append, busy_timeout_ms=5_000,
                               transient_retry_initial_ms=20, transient_retry_max_ms=50,
                               transient_retry_budget_ms=400, transient_busy_slice_ms=100)
    source.start()
    locker, started = hold_lock(repository)
    try:
        time.sleep(0.35)  # Release close to the 400 ms deadline.
        locker.execute("INSERT INTO messages (id,sender,recipient,message_type,body_json,created_at) "
                       "VALUES ('edge','hermes','claude','event','{}','2026-01-01T00:00:00Z')")
        locker.execute("INSERT INTO message_delivery_sequence (message_id) VALUES ('edge')")
        locker.execute("COMMIT")
        assert wait_until(lambda: deliveries or failures, timeout=3)
        if deliveries:
            assert delivered_at[0] - started <= 0.4 + 0.1 + SLACK_S
            assert failures == [] and source.healthy
        else:
            assert len(failures) == 1 and not source.healthy  # Lost the race: failed closed, never both.
            assert not deliveries
    finally:
        locker.close()
        source.stop()


def test_executing_snapshot_is_not_bounded_and_stop_waits_for_it(tmp_path):
    """Documented limit: a snapshot that is executing (not waiting on a lock) runs to completion
    and is published even past the budget; stop is serviced at the next check afterwards."""
    import threading
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    deliveries, failures = [], []
    inner = MessageCursorReader(page_size=4)
    entered, release = threading.Event(), threading.Event()
    slow = {"on": False}

    def reader(conn, cursor):
        result = inner(conn, cursor)
        if cursor is not None and slow["on"] and "codex" in result[0] and not entered.is_set():
            entered.set()
            release.wait(5)  # Deterministic: hold the one snapshot carrying the commit until released.
        return result

    source = SQLiteEventSource(repository.path, reader, lambda recipients, cursor: deliveries.append(tuple(recipients)),
                               on_error=failures.append, busy_timeout_ms=2_000, transient_retry_budget_ms=100,
                               transient_busy_slice_ms=50)
    source.start()
    try:
        slow["on"] = True
        repository.insert(sender="hermes", recipient="codex", message_type="event", body={})
        assert entered.wait(3)
        time.sleep(0.3)  # Well past the 100 ms budget while executing, not waiting.
        stopper = threading.Thread(target=source.stop, kwargs={"timeout": 5})
        stopper.start()
        time.sleep(0.1)
        assert stopper.is_alive()  # Stop cannot cut an executing snapshot short.
        release.set()
        stopper.join(5)
        assert not stopper.is_alive()
        assert deliveries == [("codex",)]  # The executing snapshot was published, not discarded.
        assert failures == [] and source.failure is None
    finally:
        release.set()
        source.stop()


def test_budget_zero_keeps_full_busy_timeout_and_fails_closed_without_retry(tmp_path):
    """transient_retry_budget_ms=0: no retries; a lock longer than busy_timeout fails closed after it."""
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    failures, failed_at = [], []
    def on_error(exc):
        failed_at.append(time.monotonic())
        failures.append(exc)
    source = SQLiteEventSource(repository.path, MessageCursorReader(page_size=4), lambda *_: None,
                               on_error=on_error, busy_timeout_ms=600, transient_retry_budget_ms=0)
    source.start()
    locker, started = hold_lock(repository)
    try:
        assert wait_until(lambda: failures, timeout=5)
        elapsed = failed_at[0] - started
        assert 0.5 <= elapsed <= 0.6 + SLACK_S, f"failed after {elapsed:.2f}s"  # Full busy_timeout, not a slice.
        assert source.stats["transient_retries"] == 0 and len(failures) == 1 and not source.healthy
    finally:
        locker.close()
        source.stop()


def test_budget_zero_lock_shorter_than_busy_timeout_is_absorbed_silently(tmp_path):
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    deliveries, failures = [], []
    source = SQLiteEventSource(repository.path, MessageCursorReader(page_size=4),
                               lambda recipients, cursor: deliveries.append(tuple(recipients)),
                               on_error=failures.append, busy_timeout_ms=2_000, transient_retry_budget_ms=0)
    source.start()
    locker, _ = hold_lock(repository)
    try:
        time.sleep(0.4)
        locker.execute("INSERT INTO messages (id,sender,recipient,message_type,body_json,created_at) "
                       "VALUES ('z','hermes','claude','event','{}','2026-01-01T00:00:00Z')")
        locker.execute("INSERT INTO message_delivery_sequence (message_id) VALUES ('z')")
        locker.execute("COMMIT")
        assert source.wait_for_cursor(repository.delivery_head(), timeout=5)
        assert ("claude",) in deliveries and failures == [] and source.stats["transient_retries"] == 0
    finally:
        locker.close()
        source.stop()


def _busy_source(tmp_path, code, on_attempt):
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    failures = []
    inner = MessageCursorReader(page_size=4)
    state = {"armed": False}

    def reader(conn, cursor):
        if cursor is not None and state["armed"]:
            on_attempt()
            exc = sqlite3.OperationalError("database is locked")
            exc.sqlite_errorcode = code
            raise exc
        return inner(conn, cursor)

    source = SQLiteEventSource(repository.path, reader, lambda *_: None, on_error=failures.append,
                               busy_timeout_ms=200, transient_retry_initial_ms=20, transient_retry_max_ms=40,
                               transient_retry_budget_ms=100, transient_busy_slice_ms=50)
    return repository, source, failures, state


@pytest.mark.parametrize("code", [5, 6, 517])
def test_stop_arriving_with_a_lock_error_is_a_clean_stop(tmp_path, code):
    """Precedence: an intentional stop wins over a BUSY/LOCKED error we would only retry."""
    holder = {}
    repository, source, failures, state = _busy_source(tmp_path, code, lambda: holder["source"]._stop_requested.set())
    holder["source"] = source
    source.start()
    state["armed"] = True
    repository.insert(sender="hermes", recipient="codex", message_type="event", body={})
    assert wait_until(lambda: source._thread_done.is_set(), timeout=3)
    source.stop(timeout=3)
    assert failures == [] and source.failure is None


def test_stop_arriving_during_the_last_backoff_before_exhaustion_is_a_clean_stop(tmp_path):
    """The budget runs out right after a backoff in which a stop was requested: clean stop, not a failure.

    Attempts start at about 0, 20, 60 and 100 ms (backoff 20 then 40 capped, budget 100 ms). The stop
    flag is set 5 ms after the third attempt fails, so it lands inside the final 40 ms backoff whose end
    reaches the budget. Counted by attempt, not by wall clock, so scheduling jitter cannot reorder it.
    """
    import threading
    holder = {"attempts": 0, "timer": None}

    def on_attempt():
        holder["attempts"] += 1
        if holder["attempts"] == 3:
            holder["timer"] = threading.Timer(0.005, holder["source"]._stop_requested.set)
            holder["timer"].start()

    repository, source, failures, state = _busy_source(tmp_path, 5, on_attempt)
    holder["source"] = source
    source.start()
    state["armed"] = True
    repository.insert(sender="hermes", recipient="codex", message_type="event", body={})
    assert wait_until(lambda: source._thread_done.is_set(), timeout=3)
    source.stop(timeout=3)
    if holder["timer"] is not None:
        holder["timer"].join()
    assert holder["attempts"] >= 3
    assert failures == [] and source.failure is None and source.stats["transient_retries"] >= 2


def test_budget_exhaustion_without_stop_still_fails_closed(tmp_path):
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    failures = []
    inner = MessageCursorReader(page_size=4)
    armed = {"on": False}

    def reader(conn, cursor):
        if cursor is not None and armed["on"]:
            exc = sqlite3.OperationalError("database is locked")
            exc.sqlite_errorcode = 5
            raise exc
        return inner(conn, cursor)

    source = SQLiteEventSource(repository.path, reader, lambda *_: None, on_error=failures.append,
                               busy_timeout_ms=200, transient_retry_initial_ms=20, transient_retry_max_ms=40,
                               transient_retry_budget_ms=100, transient_busy_slice_ms=50)
    source.start()
    try:
        armed["on"] = True
        repository.insert(sender="hermes", recipient="codex", message_type="event", body={})
        assert wait_until(lambda: failures, timeout=3)
        assert len(failures) == 1 and failures[0].sqlite_errorcode == 5 and not source.healthy
    finally:
        source.stop()


def test_stop_during_startup_lock_never_reports_a_healthy_start(tmp_path):
    """A stop interrupts the verified startup lock wait without a false healthy result."""
    import threading
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    failures = []
    source = SQLiteEventSource(repository.path, MessageCursorReader(page_size=4), lambda *_: None,
                               on_error=failures.append, busy_timeout_ms=1_500)
    locker = sqlite3.connect(repository.path, timeout=1, isolation_level=None, check_same_thread=False)
    locker.execute("BEGIN IMMEDIATE")  # Startup's barrier snapshot blocks on this.
    outcome = {}
    # Fill the bounded command queue before startup so stop() must avoid holding
    # the readiness condition while it waits for command space.
    queued_flushes = [threading.Event(), threading.Event()]
    for done in queued_flushes:
        source._commands.put_nowait(("flush", done))

    def run_start():
        try:
            outcome["stats"] = source.start()
        except BaseException as exc:  # noqa: BLE001 - the outcome is what is asserted
            outcome["error"] = exc

    starter = threading.Thread(target=run_start)
    starter.start()
    try:
        assert wait_until(lambda: source._stage == "acquire_write_barrier", timeout=3), \
            f"startup never reached the barrier; stage={source._stage!r}"
        stopped = {}

        def run_stop():
            try:
                stopped["stats"] = source.stop(timeout=5)
            except BaseException as exc:  # noqa: BLE001 - assert lifecycle result below
                stopped["error"] = exc

        stopper = threading.Thread(target=run_stop)
        stopper.start()
        stopper.join(7)
        starter.join(10)
        assert not stopper.is_alive()
        assert not starter.is_alive()
        assert "error" not in stopped, stopped.get("error")
        assert "error" in outcome, f"start() returned {outcome.get('stats')!r} for a stopped startup"
        assert not source.healthy
        assert source.failure is None
        assert all(done.is_set() for done in queued_flushes)
    finally:
        locker.rollback()
        locker.close()
        source.stop(timeout=5)


def test_successful_startup_snapshot_after_stop_request_does_not_report_healthy(tmp_path, monkeypatch):
    """A snapshot that succeeds after stop() wins must still leave startup unready."""
    import threading
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    source = SQLiteEventSource(repository.path, MessageCursorReader(page_size=4), lambda *_: None,
                               busy_timeout_ms=1_500)
    locker = sqlite3.connect(repository.path, timeout=1, isolation_level=None, check_same_thread=False)
    locker.execute("BEGIN IMMEDIATE")
    starter_outcome = {}
    stopper_outcome = {}

    def run_start():
        try:
            starter_outcome["stats"] = source.start()
        except BaseException as exc:  # noqa: BLE001 - assert lifecycle result below
            starter_outcome["error"] = exc

    def run_stop():
        try:
            stopper_outcome["stats"] = source.stop(timeout=5)
        except BaseException as exc:  # noqa: BLE001 - assert lifecycle result below
            stopper_outcome["error"] = exc

    # Let SQLite complete the in-flight statement after stop is requested, so this
    # exercises the successful-snapshot race rather than relying on interrupt().
    monkeypatch.setattr(source, "_interrupt_sql", lambda: None)
    starter = threading.Thread(target=run_start)
    starter.start()
    stopper = None
    try:
        assert wait_until(lambda: source._stage == "acquire_write_barrier", timeout=3), \
            f"startup never reached the barrier; stage={source._stage!r}"
        stopper = threading.Thread(target=run_stop)
        stopper.start()
        assert wait_until(lambda: source._stop_requested.is_set(), timeout=3), \
            "stop() did not publish its request"
        locker.execute("ROLLBACK")
        stopper.join(7)
        starter.join(10)
        assert not stopper.is_alive()
        assert not starter.is_alive()
        assert "error" not in stopper_outcome, stopper_outcome.get("error")
        assert "error" in starter_outcome, \
            f"start() returned {starter_outcome.get('stats')!r} for a stopped startup"
        assert not source.healthy
        assert source.failure is None
    finally:
        if locker.in_transaction:
            locker.rollback()
        locker.close()
        if stopper is not None and stopper.is_alive():
            stopper.join(7)
        if source.running:
            source.stop(timeout=5)


def test_successful_late_snapshot_resets_the_budget_without_failing(tmp_path):
    """No wall-clock guarantee: an attempt that succeeds after the cutoff ends the episode."""
    holder = {"calls": 0}
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    failures, deliveries = [], []
    inner = MessageCursorReader(page_size=4)

    def reader(conn, cursor):
        if cursor is not None and holder.get("armed"):
            holder["calls"] += 1
            if holder["calls"] == 1:
                exc = sqlite3.OperationalError("database is locked")
                exc.sqlite_errorcode = 5
                raise exc
            time.sleep(0.25)  # The retry succeeds, but only after the 100 ms budget has passed.
        return inner(conn, cursor)

    source = SQLiteEventSource(repository.path, reader, lambda recipients, cursor: deliveries.append(tuple(recipients)),
                               on_error=failures.append, busy_timeout_ms=200, transient_retry_initial_ms=20,
                               transient_retry_max_ms=40, transient_retry_budget_ms=100, transient_busy_slice_ms=50)
    source.start()
    try:
        holder["armed"] = True
        repository.insert(sender="hermes", recipient="codex", message_type="event", body={})
        assert source.wait_for_cursor(repository.delivery_head(), timeout=5)
        assert failures == [] and source.healthy and ("codex",) in deliveries
        assert source.stats["transient_retrying"] is False
    finally:
        source.stop()
