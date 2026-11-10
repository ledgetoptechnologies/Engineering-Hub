"""Safe diagnostics and bounded SQLite busy-retry evidence; no fallback transport."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

import asyncio
import httpx
import pytest

from agent_message_bus.database import MessageRepository
from agent_message_bus.auth import AuthSettings
from agent_message_bus.sqlite_events import (
    MessageCursorReader, SQLiteEventSource, WatcherFailed, _sqlite_error_details,
    sys_platform_linux,
)


def busy_error(path):
    writer = sqlite3.connect(path, isolation_level=None)
    blocked = sqlite3.connect(path, isolation_level=None, timeout=0.01)
    try:
        writer.execute('BEGIN IMMEDIATE')
        try:
            blocked.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            return exc
        pytest.fail('Held writer unexpectedly allowed another write transaction')
    finally:
        writer.rollback()
        blocked.close()
        writer.close()


def test_real_busy_error_has_safe_code_and_canonical_name(tmp_path):
    exc = busy_error(tmp_path / 'private-message-database.sqlite3')
    exc.add_note('private SQL and message material must not appear')
    details = _sqlite_error_details(exc)
    assert details == {'sqlite_errorcode': sqlite3.SQLITE_BUSY, 'sqlite_errorname': 'SQLITE_BUSY'}
    assert 'private' not in json.dumps(details)


@pytest.mark.parametrize('code', [True, -1, 2**31, '5', None])
def test_arbitrary_sqlite_code_is_not_public_diagnostic(code):
    exc = sqlite3.OperationalError('secret exception text')
    exc.sqlite_errorcode = code
    exc.sqlite_errorname = 'SQLITE_BUSY'
    assert _sqlite_error_details(exc) == {}


@pytest.mark.parametrize('name', ['private SQL', 'SQLITE_CREATE_TABLE', 'SQLITE_BUSY_PRIVATE', None])
def test_unknown_or_mismatched_sqlite_name_is_not_published(name):
    exc = sqlite3.OperationalError('secret exception text')
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    exc.sqlite_errorname = name
    assert _sqlite_error_details(exc) == {'sqlite_errorcode': sqlite3.SQLITE_BUSY}


def test_non_sqlite_error_cannot_inject_sqlite_metadata():
    exc = RuntimeError('private SQL')
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    exc.sqlite_errorname = 'SQLITE_BUSY'
    assert _sqlite_error_details(exc) == {}


def test_first_failure_stage_and_code_are_retained(tmp_path):
    source = object.__new__(SQLiteEventSource)
    source._condition = threading.Condition()
    source._stats = {'healthy': True}
    source._failure = None
    source._event_fd = None
    source._thread = None
    source._stage = 'acquire_write_barrier'
    observed = []
    source.on_error = observed.append
    failure = busy_error(tmp_path / 'fixture.sqlite3')
    source._fail(failure)
    source._stage = 'wait_native_event'
    source._fail(RuntimeError('later unrelated failure'))
    assert source.failure is failure and observed == [failure]
    assert source.stats['failure_stage'] == 'acquire_write_barrier'
    assert source.stats['sqlite_errorcode'] == sqlite3.SQLITE_BUSY
    assert source.stats['sqlite_errorname'] == 'SQLITE_BUSY'
    assert source.healthy is False


@pytest.mark.skipif(not sys_platform_linux(), reason='Actual Linux inotify/epoll integration')
def test_actual_busy_writer_fails_source_without_advancing_or_publishing(tmp_path):
    repository = MessageRepository(tmp_path / 'messages.sqlite3')
    observed = []
    deliveries = []
    source = SQLiteEventSource(repository.path, MessageCursorReader(),
        lambda recipients, cursor: deliveries.append((recipients, cursor)),
        busy_timeout_ms=10, busy_retry_interval_ms=20,
        busy_retry_deadline_ms=100, on_error=observed.append)
    writer = sqlite3.connect(repository.path, isolation_level=None)
    try:
        source.start()
        source.flush_pending()
        original_cursor = source.stats['cursor']
        original_reads = source.stats['snapshot_reads']
        writer.execute('BEGIN IMMEDIATE')
        # A real filesystem invalidation while an independent writer holds the
        # barrier. This is a hint only; it must not be treated as a commit.
        # Closing a write-capable descriptor generates IN_CLOSE_WRITE without
        # changing database bytes or its directory entry. An ATTRIB change is
        # deliberately fatal to the existing path-identity guard instead.
        os.close(os.open(repository.path, os.O_WRONLY | os.O_CLOEXEC))
        assert source._thread_done.wait(5), 'Native watcher did not terminate after the bounded SQLite retry deadline'
        assert source.running is False and source.healthy is False
        assert len(observed) == 1 and type(observed[0]) is sqlite3.OperationalError
        assert source.stats['sqlite_errorcode'] == sqlite3.SQLITE_BUSY
        assert source.stats['sqlite_errorname'] == 'SQLITE_BUSY'
        assert source.stats['failure_stage'] == 'acquire_write_barrier'
        assert source.stats['cursor'] == original_cursor
        assert source.stats['snapshot_reads'] == original_reads
        assert deliveries == []
        writer.rollback()
        # The database becomes writable again; the failed source stays failed.
        with repository.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            assert conn.execute('SELECT 1').fetchone()[0] == 1
            conn.rollback()
        assert source.healthy is False and source.running is False
    finally:
        writer.rollback()
        writer.close()
        source.stop()


@pytest.mark.skipif(not sys_platform_linux(), reason='Actual Linux inotify/epoll integration')
def test_startup_busy_writer_recovers_before_ready_without_replaying_history(tmp_path):
    repository = MessageRepository(tmp_path / 'messages.sqlite3')
    deliveries = []
    source = SQLiteEventSource(repository.path, MessageCursorReader(),
        lambda recipients, cursor: deliveries.append((tuple(recipients), cursor)),
        busy_timeout_ms=10, busy_retry_interval_ms=20,
        busy_retry_deadline_ms=2_000)
    writer = sqlite3.connect(repository.path, isolation_level=None)
    start_results = []
    start_errors = []
    try:
        writer.execute('BEGIN IMMEDIATE')
        starter = threading.Thread(
            target=lambda: _capture_start_result(source, start_results, start_errors),
            daemon=True)
        starter.start()
        with source._condition:
            assert source._condition.wait_for(
                lambda: source.stats['busy_retry_count'] > 0, timeout=3)
        assert source._ready.is_set() is False
        assert source.healthy is False
        assert source.stats['degraded'] is True
        assert deliveries == []

        writer.commit()
        starter.join(5)
        assert not starter.is_alive(), 'source did not finish startup after writer released the barrier'
        assert start_errors == []
        assert len(start_results) == 1
        assert source.healthy is True
        assert source.stats['cursor'] == 0
        assert source.stats['startup_catchups'] == 1
        assert source.stats['degraded'] is False
        assert deliveries == [], 'startup snapshot must not replay messages created before readiness'
    finally:
        writer.rollback()
        writer.close()
        source.stop()


@pytest.mark.skipif(not sys_platform_linux(), reason='Actual Linux inotify/epoll integration')
def test_startup_busy_writer_expires_fail_closed_without_becoming_ready(tmp_path):
    repository = MessageRepository(tmp_path / 'messages.sqlite3')
    deliveries = []
    source = SQLiteEventSource(repository.path, MessageCursorReader(),
        lambda recipients, cursor: deliveries.append((tuple(recipients), cursor)),
        busy_timeout_ms=10, busy_retry_interval_ms=20,
        busy_retry_deadline_ms=100)
    writer = sqlite3.connect(repository.path, isolation_level=None)
    try:
        writer.execute('BEGIN IMMEDIATE')
        with pytest.raises(WatcherFailed):
            source.start()
        assert source._thread_done.wait(5)
        assert source.healthy is False
        assert source.stats['degraded'] is False
        assert source.stats['failure_stage'] == 'acquire_write_barrier'
        assert source.stats['sqlite_errorcode'] == sqlite3.SQLITE_BUSY
        assert source.stats['cursor'] is None
        assert source.stats['startup_catchups'] == 0
        assert deliveries == []
    finally:
        writer.rollback()
        writer.close()
        source.stop()


@pytest.mark.skipif(not sys_platform_linux(), reason='Actual Linux inotify/epoll integration')
def test_startup_first_busy_wait_is_bounded_by_retry_deadline(tmp_path, monkeypatch):
    repository = MessageRepository(tmp_path / 'messages.sqlite3')
    deliveries = []
    source = SQLiteEventSource(repository.path, MessageCursorReader(),
        lambda recipients, cursor: deliveries.append((tuple(recipients), cursor)),
        busy_timeout_ms=10_000, busy_retry_interval_ms=250,
        busy_retry_deadline_ms=100)
    writer = sqlite3.connect(repository.path, isolation_level=None)
    setup_complete = threading.Event()
    original_setup = source._setup
    startup_errors = []

    def observed_setup():
        original_setup()
        setup_complete.set()

    monkeypatch.setattr(source, '_setup', observed_setup)

    def start_source():
        try:
            source.start()
        except BaseException as exc:
            startup_errors.append(exc)

    starter = threading.Thread(target=start_source, daemon=True)
    try:
        writer.execute('BEGIN IMMEDIATE')
        starter.start()
        assert setup_complete.wait(3), 'watcher did not finish SQLite setup'
        # Keep the lock held through the retry deadline. A 10 s SQLite busy
        # timeout would otherwise outlive the 100 ms source deadline.
        assert source._thread_done.wait(3), 'startup exceeded its busy retry deadline'
        writer.rollback()
        starter.join(timeout=3)
        assert not starter.is_alive(), 'startup did not terminate after the writer was released'
        assert len(startup_errors) == 1 and isinstance(startup_errors[0], WatcherFailed), (
            'startup did not fail closed while the writer remained busy')
        assert source._thread_done.wait(3)
        assert source.healthy is False
        assert source.stats['cursor'] is None
        assert source.stats['startup_catchups'] == 0
        assert deliveries == []
    finally:
        writer.rollback()
        writer.close()
        starter.join(timeout=1)
        source.stop()


def test_retry_busy_timeout_never_exceeds_remaining_deadline(monkeypatch):
    source = object.__new__(SQLiteEventSource)
    source._barrier = None
    source._reader = object()
    source._dirty_since = 1.0
    source.busy_retry_deadline_ms = 100
    source.busy_timeout_ms = 10_000
    source.busy_retry_interval_ms = 250
    source._stage = None
    configured = []
    failure = sqlite3.OperationalError('database is locked')
    failure.sqlite_errorcode = sqlite3.SQLITE_BUSY
    failure.sqlite_errorname = 'SQLITE_BUSY'

    class Barrier:
        def execute(self, statement):
            if statement.startswith('PRAGMA busy_timeout='):
                configured.append(int(statement.partition('=')[2]))
            elif statement == 'BEGIN IMMEDIATE':
                raise failure

    source._barrier = Barrier()
    monkeypatch.setattr('agent_message_bus.sqlite_events.time.monotonic', lambda: 1.075)
    with pytest.raises(sqlite3.OperationalError):
        source._reconcile(reason='busy_retry')
    assert len(configured) == 1
    assert 0 < configured[0] <= 25


def test_first_barrier_acquired_after_deadline_is_rolled_back(monkeypatch):
    source = object.__new__(SQLiteEventSource)
    source._reader = object()
    source._dirty_since = 1.0
    source._retry_at = None
    source._last_busy_error = None
    source.busy_retry_deadline_ms = 100
    source.busy_timeout_ms = 10_000
    source.busy_retry_interval_ms = 250
    commands = []

    class Barrier:
        def execute(self, statement):
            commands.append(statement)

    source._barrier = Barrier()
    ticks = iter((1.0, 1.101))
    monkeypatch.setattr('agent_message_bus.sqlite_events.time.monotonic', lambda: next(ticks))
    with pytest.raises(WatcherFailed, match='busy retry deadline'):
        source._reconcile(reason='startup')
    assert commands == ['PRAGMA busy_timeout=100', 'BEGIN IMMEDIATE', 'ROLLBACK']


def _capture_start_result(source, results, errors):
    try:
        results.append(source.start())
    except BaseException as exc:
        errors.append(exc)


@pytest.mark.skipif(not sys_platform_linux(), reason='Actual Linux inotify/epoll integration')
def test_busy_writer_retry_catches_committed_recipient_without_early_wake(tmp_path):
    repository = MessageRepository(tmp_path / 'messages.sqlite3')
    deliveries = []
    source = SQLiteEventSource(repository.path, MessageCursorReader(),
        lambda recipients, cursor: deliveries.append((tuple(recipients), cursor)),
        busy_timeout_ms=10, busy_retry_interval_ms=20,
        busy_retry_deadline_ms=2_000)
    writer = sqlite3.connect(repository.path, isolation_level=None)
    try:
        source.start()
        source.flush_pending()
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("""INSERT INTO messages
            (id,sender,recipient,message_type,body_json,created_at)
            VALUES ('busy-retry-message','hermes','codex','test','{}','2026-11-09T00:00:00Z')""")
        writer.execute("INSERT INTO message_delivery_sequence(message_id) VALUES ('busy-retry-message')")
        os.close(os.open(repository.path, os.O_WRONLY | os.O_CLOEXEC))
        assert source._condition.acquire(timeout=1)
        try:
            assert source._condition.wait_for(lambda: source.stats['busy_retry_count'] > 0, timeout=3)
        finally:
            source._condition.release()
        assert source.stats['degraded'] is True
        assert deliveries == [], 'uncommitted rows must not publish an invalidation'
        writer.commit()
        assert source.wait_for_cursor(1, timeout=3), 'dirty cursor was not reconciled after lock release'
        assert deliveries == [(('codex',), 1)]
        assert source.healthy and source.stats['degraded'] is False
    finally:
        writer.rollback()
        writer.close()
        source.stop()


@pytest.mark.skipif(not sys_platform_linux(), reason='Actual Linux inotify/epoll integration')
def test_busy_writer_rollback_reconciles_without_false_wake_or_stranded_retry(tmp_path):
    repository = MessageRepository(tmp_path / 'messages.sqlite3')
    deliveries = []
    source = SQLiteEventSource(repository.path, MessageCursorReader(),
        lambda recipients, cursor: deliveries.append((tuple(recipients), cursor)),
        busy_timeout_ms=10, busy_retry_interval_ms=20,
        busy_retry_deadline_ms=2_000)
    writer = sqlite3.connect(repository.path, isolation_level=None)
    try:
        source.start()
        source.flush_pending()
        original_reads = source.stats['snapshot_reads']
        writer.execute('BEGIN IMMEDIATE')
        os.close(os.open(repository.path, os.O_WRONLY | os.O_CLOEXEC))
        with source._condition:
            assert source._condition.wait_for(
                lambda: source.stats['busy_retry_count'] > 0, timeout=3)
        assert source.stats['degraded'] is True
        writer.rollback()
        with source._condition:
            assert source._condition.wait_for(
                lambda: source.stats['busy_retry_count'] > 0 and not source.stats['degraded'],
                timeout=3)
        assert source.healthy is True
        assert source.stats['cursor'] == 0
        assert source.stats['snapshot_reads'] > original_reads
        assert deliveries == []
        reads_after_recovery = source.stats['snapshot_reads']
        time.sleep(0.1)
        assert source.stats['snapshot_reads'] == reads_after_recovery
    finally:
        writer.rollback()
        writer.close()
        source.stop()


@pytest.mark.asyncio
@pytest.mark.skipif(not sys_platform_linux(), reason='Actual Linux native-source health integration')
async def test_health_exposes_safe_actual_busy_diagnostics_only(tmp_path, monkeypatch):
    import agent_message_bus.app as bus_app

    real_source = SQLiteEventSource

    def bounded_source(*args, **kwargs):
        return real_source(*args, **kwargs, busy_timeout_ms=10,
                            busy_retry_interval_ms=20, busy_retry_deadline_ms=500)

    monkeypatch.setattr(bus_app, 'SQLiteEventSource', bounded_source)
    app = bus_app.create_app(db_path=tmp_path / 'messages.sqlite3',
        audit_path=tmp_path / 'audit.jsonl', file_root=tmp_path / 'files',
        auth=AuthSettings('test-busy-hermes-only', 'test-busy-codex-only'),
        push_events_operational=True)
    async with app.router.lifespan_context(app):
        source = app.state.push_event_source
        writer = sqlite3.connect(source.db_path, isolation_level=None)
        try:
            writer.execute('BEGIN IMMEDIATE')
            os.close(os.open(source.db_path, os.O_WRONLY | os.O_CLOEXEC))
            def wait_for_degraded():
                with source._condition:
                    return source._condition.wait_for(
                        lambda: source.stats['busy_retry_count'] > 0, timeout=3)
            assert await asyncio.to_thread(wait_for_degraded)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url='http://localhost') as client:
                degraded = await client.get('/health')
            degraded_health = degraded.json()
            assert degraded_health['ok'] is False
            assert degraded_health['push']['state'] == 'degraded'
            assert degraded_health['push']['degraded'] is True
            assert degraded_health['push']['subscriptions_enabled'] is True
            assert degraded_health['push']['busy_retry_count'] > 0
            assert await asyncio.to_thread(source._thread_done.wait, 5)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url='http://localhost') as client:
                response = await client.get('/health')
            health = response.json()
            assert health['database'] == 'ok' and health['ok'] is False
            assert health['push']['state'] == 'failed'
            assert health['push']['subscriptions_enabled'] is False
            assert health['push']['reason'] == 'OperationalError'
            assert health['push']['failure_stage'] == 'acquire_write_barrier'
            assert health['push']['sqlite_errorcode'] == sqlite3.SQLITE_BUSY
            assert health['push']['sqlite_errorname'] == 'SQLITE_BUSY'
            assert 'failure' not in health['push']
            assert str(tmp_path) not in response.text
            assert 'database is locked' not in response.text
        finally:
            writer.rollback()
            writer.close()
