"""Safe diagnostic evidence; no recovery, retry or transport fallback is added."""
from __future__ import annotations

import json
import os
import sqlite3
import threading

import asyncio
import httpx
import pytest

from agent_message_bus.database import MessageRepository
from agent_message_bus.auth import AuthSettings
from agent_message_bus.sqlite_events import (
    MessageCursorReader, SQLiteEventSource, _sqlite_error_details, sys_platform_linux,
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
        busy_timeout_ms=30, on_error=observed.append)
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
        assert source._thread_done.wait(5), 'Native watcher did not terminate after the bounded SQLite timeout'
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


@pytest.mark.asyncio
@pytest.mark.skipif(not sys_platform_linux(), reason='Actual Linux native-source health integration')
async def test_health_exposes_safe_actual_busy_diagnostics_only(tmp_path, monkeypatch):
    import agent_message_bus.app as bus_app

    real_source = SQLiteEventSource

    def bounded_source(*args, **kwargs):
        return real_source(*args, **kwargs, busy_timeout_ms=30)

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
