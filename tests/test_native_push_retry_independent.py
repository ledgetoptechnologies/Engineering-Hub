"""Independent checks of the bounded BUSY/LOCKED retry, using only real SQLite locks.

Written separately from test_native_push_transient_retry.py (no injected reader
errors): a second connection holds a real write lock while the source is woken by a
genuine WAL change, so every BUSY here comes from SQLite itself.
"""
from __future__ import annotations

import os
import sqlite3
import time
from collections import Counter

import pytest

from agent_message_bus.database import MessageRepository
from agent_message_bus.sqlite_events import MessageCursorReader, SQLiteEventSource, sys_platform_linux

pytestmark = pytest.mark.skipif(not sys_platform_linux(), reason="native SQLite events require Linux")


def build(tmp_path, **kwargs):
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    deliveries, failures = [], []
    source = SQLiteEventSource(repository.path, MessageCursorReader(page_size=4),
                               lambda recipients, cursor: deliveries.append((tuple(sorted(recipients)), cursor)),
                               on_error=failures.append, busy_timeout_ms=20, **kwargs)
    return repository, source, deliveries, failures


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def hold_lock(repository):
    locker = sqlite3.connect(repository.path, timeout=1, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    os.utime(str(repository.path) + "-wal")  # A genuine inotify hint while the write lock is held.
    return locker


def locked_insert(locker, message_id, recipient):
    locker.execute("INSERT INTO messages (id,sender,recipient,message_type,body_json,created_at) "
                   "VALUES (?, 'hermes', ?, 'event', '{}', '2026-01-01T00:00:00Z')", (message_id, recipient))
    locker.execute("INSERT INTO message_delivery_sequence (message_id) VALUES (?)", (message_id,))


def test_real_lock_beyond_budget_fails_closed_exactly_once_and_delivers_nothing_more(tmp_path):
    repository, source, deliveries, failures = build(
        tmp_path, transient_retry_initial_ms=10, transient_retry_max_ms=40, transient_retry_budget_ms=250)
    source.start()
    locker = hold_lock(repository)
    try:
        assert wait_until(lambda: failures, timeout=5)
        assert len(failures) == 1 and isinstance(failures[0], sqlite3.OperationalError)
        assert source.healthy is False and source.stats["transient_retries"] >= 2
        assert source.stats["sqlite_errorname"] in {"SQLITE_BUSY", "SQLITE_BUSY_SNAPSHOT"} or \
            source.stats["sqlite_errorcode"] & 0xFF == 5
        locked_insert(locker, "after-failure", "codex")
        locker.execute("COMMIT")
        time.sleep(0.3)
        assert deliveries == [] and len(failures) == 1  # Failed closed: no later delivery, no second report.
    finally:
        locker.close()
        source.stop()


def test_stop_during_real_lock_backoff_is_prompt_and_not_a_failure(tmp_path):
    repository, source, deliveries, failures = build(
        tmp_path, transient_retry_initial_ms=500, transient_retry_max_ms=2_000, transient_retry_budget_ms=60_000)
    source.start()
    locker = hold_lock(repository)
    try:
        assert wait_until(lambda: source.stats["transient_retrying"] is True)
        started = time.monotonic()
        source.stop(timeout=5)
        assert time.monotonic() - started < 2.0
        assert failures == [] and source.failure is None
    finally:
        locker.rollback() if locker.in_transaction else None
        locker.close()


def test_recovery_delivers_every_locked_commit_once_and_no_old_event_again(tmp_path):
    repository, source, deliveries, failures = build(
        tmp_path, transient_retry_initial_ms=10, transient_retry_max_ms=40)
    source.start()
    locker = hold_lock(repository)
    try:
        assert wait_until(lambda: source.stats["transient_retries"] >= 2)
        for index, recipient in enumerate(("codex", "claude", "hermes", "codex")):
            locked_insert(locker, f"locked-{index}", recipient)
        locker.execute("COMMIT")
        head = repository.delivery_head()
        assert source.wait_for_cursor(head, timeout=5)
        recovered = list(deliveries)
        assert {name for names, _ in recovered for name in names} == {"codex", "claude", "hermes"}
        assert all(cursor <= head for _, cursor in recovered)
        # After recovery, one new commit yields one delivery for its recipient only, never a replay of old ones.
        repository.insert(sender="claude", recipient="hermes", message_type="event", body={"n": 1})
        new_head = repository.delivery_head()
        assert source.wait_for_cursor(new_head, timeout=5)
        later = deliveries[len(recovered):]
        assert [names for names, _ in later] == [("hermes",)]
        assert later[-1][1] == new_head
    finally:
        locker.close()
        source.stop()
    assert failures == []


def test_repeated_lock_episodes_reset_the_budget(tmp_path):
    repository, source, deliveries, failures = build(
        tmp_path, transient_retry_initial_ms=10, transient_retry_max_ms=40, transient_retry_budget_ms=400)
    source.start()
    try:
        # Five episodes of ~250 ms each total more than one budget; each resets on success.
        for episode in range(5):
            locker = hold_lock(repository)
            try:
                time.sleep(0.25)
                locked_insert(locker, f"episode-{episode}", "codex")
                locker.execute("COMMIT")
            finally:
                locker.close()
            assert source.wait_for_cursor(repository.delivery_head(), timeout=5)
            assert source.stats["transient_retrying"] is False
        assert failures == [] and source.healthy
        assert Counter(name for names, _ in deliveries for name in names)["codex"] >= 5
        assert deliveries[-1][1] == repository.delivery_head()
    finally:
        source.stop()
