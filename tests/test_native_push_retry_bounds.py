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
