"""Reconciliation of PR 146 stop and non-barrier failure semantics.

Real Linux watcher tests only; database and processes are test-owned.
"""
from __future__ import annotations

import os
import sqlite3
import threading

import pytest

from agent_message_bus.database import MessageRepository
from agent_message_bus.sqlite_events import MessageCursorReader, SQLiteEventSource, sys_platform_linux

pytestmark = pytest.mark.skipif(not sys_platform_linux(), reason="Linux native watcher required")


def _source(repository, deliveries, reader=None):
    return SQLiteEventSource(
        repository.path, reader or MessageCursorReader(),
        lambda recipients, cursor: deliveries.append((tuple(recipients), cursor)),
        busy_timeout_ms=100, busy_retry_interval_ms=50, busy_retry_deadline_ms=3000,
    )


def _invalidate(repository):
    os.close(os.open(repository.path, os.O_WRONLY | os.O_CLOEXEC))


def _wait_retry(source):
    with source._condition:
        assert source._condition.wait_for(lambda: source.stats["busy_retry_count"] > 0, timeout=3)


def test_stop_while_real_writer_holds_barrier(tmp_path):
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    deliveries = []
    source = _source(repository, deliveries)
    writer = sqlite3.connect(repository.path, isolation_level=None)
    try:
        source.start()
        source.flush_pending()
        cursor = source.stats["cursor"]
        writer.execute("BEGIN IMMEDIATE")
        _invalidate(repository)
        _wait_retry(source)
        source.stop(timeout=2)
        assert source._thread_done.is_set() and not source.running
        assert source.failure is None
        assert source.stats["cursor"] == cursor and deliveries == []
    finally:
        writer.rollback()
        writer.close()
        source.stop()


def test_stop_during_startup_never_becomes_healthy(tmp_path):
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    deliveries = []
    source = _source(repository, deliveries)
    writer = sqlite3.connect(repository.path, isolation_level=None)
    results, errors = [], []
    def start():
        try:
            results.append(source.start())
        except Exception as exc:
            errors.append(exc)
    starter = threading.Thread(target=start)
    try:
        writer.execute("BEGIN IMMEDIATE")
        starter.start()
        _wait_retry(source)
        source.stop(timeout=2)
        starter.join(3)
        assert not starter.is_alive() and source._thread_done.is_set()
        assert results == [] and len(errors) == 1
        assert not source.healthy and source.stats["startup_catchups"] == 0
        assert source.stats["cursor"] is None and deliveries == []
    finally:
        writer.rollback()
        writer.close()
        starter.join(3)
        source.stop()


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, None])
def test_nonbarrier_read_errors_fail_closed_without_cursor_advance(tmp_path, code):
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    deliveries = []
    underlying = MessageCursorReader()
    armed = threading.Event()
    def read_after(conn, cursor):
        if armed.is_set():
            exc = sqlite3.OperationalError("synthetic private diagnostic")
            if code is not None:
                exc.sqlite_errorcode = code
            raise exc
        return underlying(conn, cursor)
    source = _source(repository, deliveries, read_after)
    try:
        source.start()
        source.flush_pending()
        cursor = source.stats["cursor"]
        armed.set()
        _invalidate(repository)
        assert source._thread_done.wait(3)
        assert isinstance(source.failure, sqlite3.OperationalError)
        assert source.stats["failure_stage"] == "read_committed_cursor"
        assert source.stats["cursor"] == cursor and deliveries == []
        assert source.stats["busy_retry_count"] == 0
    finally:
        source.stop()
