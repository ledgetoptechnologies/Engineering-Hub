"""Bounded SQLITE_BUSY/LOCKED retry in the native SQLite push event source."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time

import httpx
import pytest
from pydantic import AnyUrl

from agent_message_bus.database import MessageRepository
from agent_message_bus.sqlite_events import (
    MessageCursorReader,
    SQLiteEventSource,
    WatcherFailed,
    _transient_sqlite_code,
    sys_platform_linux,
)
from tests.test_mcp_push import BASE_URI, TOKENS, live_app, loopback, mcp_client


pytestmark = pytest.mark.skipif(not sys_platform_linux(), reason="native SQLite events require Linux")


def sqlite_error(code: int | None, cls=sqlite3.OperationalError, text: str = "database is locked"):
    exc = cls(text)
    if code is not None:
        exc.sqlite_errorcode = code
    return exc


class FlakyReader:
    """Wrap the real cursor reader; raise injected errors on steady-state reads.

    ``gate`` set means healthy. While cleared, every non-startup read raises
    ``make_error()``; ``remaining`` bounds that to a number of failures. Each
    successful read is logged as (cursor_in, head_out) to prove continuity.
    """

    def __init__(self, page_size: int = 8):
        self.inner = MessageCursorReader(page_size=page_size)
        self.gate = threading.Event()
        self.gate.set()
        self.remaining: int | None = None
        self.make_error = lambda: sqlite_error(5)
        self.failures_raised = 0
        self.successes: list[tuple[int | None, int]] = []

    def arm(self, *, count: int | None = None, make_error=None):
        if make_error is not None:
            self.make_error = make_error
        self.remaining = count
        self.gate.clear()

    def __call__(self, conn, cursor):
        if cursor is not None and not self.gate.is_set():
            if self.remaining is None or self.remaining > 0:
                if self.remaining is not None:
                    self.remaining -= 1
                    if self.remaining == 0:
                        self.gate.set()
                self.failures_raised += 1
                raise self.make_error()
        recipients, head = self.inner(conn, cursor)
        self.successes.append((cursor, head))
        return recipients, head


def wait_stats(source: SQLiteEventSource, predicate, timeout: float = 5.0) -> bool:
    with source._condition:
        return source._condition.wait_for(lambda: predicate(source._stats), timeout=timeout)


def make_source(tmp_path, reader, **kwargs):
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    deliveries: list[tuple[tuple[str, ...], int]] = []
    failures: list[BaseException] = []
    source = SQLiteEventSource(repository.path, reader,
                               lambda recipients, cursor: deliveries.append((tuple(recipients), cursor)),
                               on_error=failures.append, **kwargs)
    return repository, source, deliveries, failures


def insert(repository: MessageRepository, recipient: str, sender: str = "hermes"):
    return repository.insert(sender=sender, recipient=recipient, message_type="event", body={"n": recipient})


def assert_contiguous(reader: FlakyReader, final_head: int):
    """Every successful read starts where the previous one ended: no seq skipped."""
    assert reader.successes[0][0] is None
    for (_, previous_head), (cursor, _) in zip(reader.successes, reader.successes[1:]):
        assert cursor == previous_head
    assert reader.successes[-1][1] == final_head


def test_injected_busy_during_flush_stays_healthy_then_delivers(tmp_path):
    reader = FlakyReader()
    repository, source, deliveries, failures = make_source(tmp_path, reader)
    source.start()
    try:
        baseline = source.stats["cursor"]
        reader.arm(count=3)
        insert(repository, "codex")
        head = repository.delivery_head()
        assert source.wait_for_cursor(head, timeout=5)
        stats = source.flush_pending()
        assert source.healthy and failures == []
        assert stats["transient_retries"] == 3
        assert stats["last_transient_error"] == "SQLITE_BUSY"
        assert stats["transient_retrying"] is False
        assert stats["failure"] is None and stats["sqlite_errorcode"] is None
        assert ("codex",) in [recipients for recipients, _ in deliveries]
        # The cursor was held at the baseline across failed attempts.
        assert reader.successes[1][0] == baseline
        assert_contiguous(reader, head)
    finally:
        source.stop()
    assert failures == []


def test_real_write_lock_past_busy_timeout_is_retried_and_commit_delivered(tmp_path):
    reader = FlakyReader()
    repository, source, deliveries, failures = make_source(tmp_path, reader, busy_timeout_ms=50,
                                                           transient_retry_initial_ms=20,
                                                           transient_retry_max_ms=50)
    source.start()
    locker = sqlite3.connect(repository.path, timeout=1, isolation_level=None)
    try:
        locker.execute("BEGIN IMMEDIATE")  # A real writer holds the lock past busy_timeout.
        os.utime(str(repository.path) + "-wal")  # Genuine IN_ATTRIB hint -> barrier BUSY.
        assert wait_stats(source, lambda s: s["transient_retries"] >= 2)
        assert source.healthy and wait_stats(source, lambda s: s["transient_retrying"] is True)
        locker.execute("""INSERT INTO messages (id,sender,recipient,message_type,body_json,created_at)
            VALUES ('locked-commit','hermes','claude','event','{}','2026-01-01T00:00:00Z')""")
        locker.execute("INSERT INTO message_delivery_sequence (message_id) VALUES ('locked-commit')")
        locker.execute("COMMIT")
        head = repository.delivery_head()
        assert source.wait_for_cursor(head, timeout=5)
        assert wait_stats(source, lambda s: s["transient_retrying"] is False)
        assert ("claude",) in [recipients for recipients, _ in deliveries]
        stats = source.stats
        assert stats["last_transient_error"] == "SQLITE_BUSY" and stats["healthy"] is True
        assert reader.failures_raised == 0  # Only real SQLite lock errors occurred.
        assert_contiguous(reader, head)
    finally:
        locker.close()
        source.stop()
    assert failures == []


def test_concurrent_commits_while_retrying_all_delivered_without_gaps(tmp_path):
    reader = FlakyReader(page_size=2)
    repository, source, deliveries, failures = make_source(tmp_path, reader, transient_retry_initial_ms=10,
                                                           transient_retry_max_ms=40)
    source.start()
    try:
        reader.arm()  # Persistently BUSY until released.
        insert(repository, "codex")
        assert wait_stats(source, lambda s: s["transient_retries"] >= 1)
        writers = [threading.Thread(target=insert, args=(repository, recipient, sender))
                   for recipient, sender in (("hermes", "codex"), ("claude", "hermes"),
                                             ("codex", "claude"), ("hermes", "claude"))]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(10)
        # A flush requested mid-retry is deferred, then completes after recovery.
        flushed = threading.Event()
        flusher = threading.Thread(target=lambda: (source.flush_pending(timeout=10), flushed.set()))
        flusher.start()
        assert not flushed.wait(0.2)
        assert source.healthy and source.stats["transient_retrying"] is True
        reader.gate.set()
        flusher.join(10)
        assert flushed.is_set()
        head = repository.delivery_head()
        assert source.wait_for_cursor(head, timeout=5)
        delivered = set().union(*(set(recipients) for recipients, _ in deliveries))
        assert delivered == {"codex", "hermes", "claude"}
        assert source.stats["cursor"] == head
        assert source.stats["transient_retrying"] is False
        assert_contiguous(reader, head)
    finally:
        source.stop()
    assert failures == []


def test_persistent_busy_past_budget_fails_closed_once(tmp_path):
    reader = FlakyReader()
    repository, source, deliveries, failures = make_source(tmp_path, reader, transient_retry_initial_ms=10,
                                                           transient_retry_max_ms=40,
                                                           transient_retry_budget_ms=250)
    source.start()
    baseline = source.stats["cursor"]
    reader.arm()
    started = time.monotonic()
    insert(repository, "codex")
    assert source._thread_done.wait(5)
    assert time.monotonic() - started < 3
    stats = source.stats
    assert len(failures) == 1 and isinstance(failures[0], sqlite3.OperationalError)
    assert not source.healthy
    assert stats["failure_stage"] == "read_committed_cursor"
    assert stats["sqlite_errorcode"] == 5
    assert stats["transient_retries"] >= 2 and stats["transient_retrying"] is False
    assert stats["cursor"] == baseline and deliveries == []
    assert stats["watcher_stopped"] is True
    with pytest.raises(WatcherFailed):
        source.raise_if_failed()


@pytest.mark.parametrize("code,name", [(6, "SQLITE_LOCKED"), (517, "SQLITE_BUSY"), (262, "SQLITE_LOCKED")])
def test_locked_and_extended_codes_are_retried(tmp_path, code, name):
    reader = FlakyReader()
    repository, source, deliveries, failures = make_source(tmp_path, reader)
    source.start()
    try:
        reader.arm(count=2, make_error=lambda: sqlite_error(code))
        insert(repository, "hermes", sender="codex")
        assert source.wait_for_cursor(repository.delivery_head(), timeout=5)
        stats = source.flush_pending()
        assert stats["healthy"] and stats["transient_retries"] == 2
        assert stats["last_transient_error"] == name
        assert ("hermes",) in [recipients for recipients, _ in deliveries]
    finally:
        source.stop()
    assert failures == []


@pytest.mark.parametrize("make_error", [
    lambda: sqlite_error(11, text="database disk image is malformed"),  # SQLITE_CORRUPT
    lambda: sqlite_error(None),  # "database is locked" text but no verified code
    lambda: sqlite_error(8),  # SQLITE_READONLY
    lambda: sqlite_error(5, cls=sqlite3.DatabaseError),  # BUSY code, wrong class
    lambda: WatcherFailed("message delivery sequence head is inconsistent"),
], ids=["corrupt", "no-code", "readonly", "database-error", "watcher-failed"])
def test_non_transient_errors_are_fatal_with_zero_retries(tmp_path, make_error):
    reader = FlakyReader()
    repository, source, deliveries, failures = make_source(tmp_path, reader)
    source.start()
    reader.arm(make_error=make_error)
    insert(repository, "codex")
    assert source._thread_done.wait(5)
    assert len(failures) == 1 and not source.healthy
    assert source.stats["transient_retries"] == 0
    assert source.stats["last_transient_error"] is None
    assert deliveries == []


def test_transient_classifier_uses_only_verified_codes():
    assert _transient_sqlite_code(sqlite_error(5)) == "SQLITE_BUSY"
    assert _transient_sqlite_code(sqlite_error(6)) == "SQLITE_LOCKED"
    assert _transient_sqlite_code(sqlite_error(5 | (2 << 8))) == "SQLITE_BUSY"  # SQLITE_BUSY_SNAPSHOT
    for exc in (sqlite_error(None), sqlite_error(11), sqlite_error(10), sqlite_error(True),
                sqlite_error(-5), sqlite_error(5, cls=sqlite3.IntegrityError),
                RuntimeError("database is locked"), WatcherFailed("busy")):
        assert _transient_sqlite_code(exc) is None


def test_startup_busy_remains_fatal(tmp_path):
    class StartupBusy(FlakyReader):
        def __call__(self, conn, cursor):
            raise sqlite_error(5)

    _repository, source, _deliveries, failures = make_source(tmp_path, StartupBusy())
    with pytest.raises(WatcherFailed):
        source.start()
    assert source._thread_done.wait(5)
    assert len(failures) == 1 and source.stats["transient_retries"] == 0


def test_stop_during_retry_wait_is_prompt_and_not_a_failure(tmp_path):
    reader = FlakyReader()
    repository, source, _deliveries, failures = make_source(tmp_path, reader, transient_retry_initial_ms=5_000,
                                                            transient_retry_max_ms=5_000,
                                                            transient_retry_budget_ms=60_000)
    source.start()
    reader.arm()
    insert(repository, "codex")
    assert wait_stats(source, lambda s: s["transient_retrying"] is True)
    flush_result: list[object] = []
    flusher = threading.Thread(target=lambda: flush_result.append(source.flush_pending(timeout=10)))
    flusher.start()
    time.sleep(0.05)  # Let the flush command reach the retry wait and be deferred.
    started = time.monotonic()
    stats = source.stop(timeout=2)
    assert time.monotonic() - started < 2
    flusher.join(2)
    assert not flusher.is_alive() and flush_result  # The deferred flush was released, not hung.
    assert failures == [] and source.failure is None
    assert stats["watcher_stopped"] is True and stats["transient_retrying"] is False
    assert stats["failure"] is None


def test_constructor_validates_transient_retry_arguments(tmp_path):
    repository = MessageRepository(tmp_path / "messages.sqlite3")
    reader = MessageCursorReader()
    deliver = lambda recipients, cursor: None
    for kwargs in (
        {"transient_retry_initial_ms": 0}, {"transient_retry_initial_ms": True},
        {"transient_retry_initial_ms": 1.5}, {"transient_retry_initial_ms": 60_001},
        {"transient_retry_max_ms": 0}, {"transient_retry_max_ms": "2000"},
        {"transient_retry_initial_ms": 100, "transient_retry_max_ms": 50},
        {"transient_retry_budget_ms": -1}, {"transient_retry_budget_ms": 600_001},
        {"transient_retry_budget_ms": None},
    ):
        with pytest.raises(ValueError):
            SQLiteEventSource(repository.path, reader, deliver, **kwargs)
    source = SQLiteEventSource(repository.path, reader, deliver)
    assert (source.transient_retry_initial_ms, source.transient_retry_max_ms,
            source.transient_retry_budget_ms) == (50, 2_000, 30_000)
    assert SQLiteEventSource(repository.path, reader, deliver, transient_retry_budget_ms=0)
    assert source.stats["transient_retries"] == 0 and source.stats["last_transient_error"] is None


def test_zero_budget_disables_retries(tmp_path):
    reader = FlakyReader()
    repository, source, _deliveries, failures = make_source(tmp_path, reader, transient_retry_budget_ms=0)
    source.start()
    reader.arm()
    insert(repository, "codex")
    assert source._thread_done.wait(5)
    assert len(failures) == 1 and source.stats["transient_retries"] == 0


async def next_update(queue: asyncio.Queue, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        assert remaining > 0, "timed out waiting for resources/updated"
        notice = await asyncio.wait_for(queue.get(), remaining)
        if getattr(notice.root, "method", None) == "notifications/resources/updated":
            return notice


async def next_method(queue: asyncio.Queue, method: str, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        assert remaining > 0, f"timed out waiting for {method}"
        notice = await asyncio.wait_for(queue.get(), remaining)
        if getattr(notice.root, "method", None) == method:
            return notice.root


@pytest.mark.asyncio
async def test_mcp_subscription_survives_busy_and_reconnect_receives_later_commits(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")
    app = live_app(tmp_path)
    repository = app.state.repository
    async with loopback(app) as url:
        source = app.state.push_event_source
        reader = FlakyReader()
        source.read_after = reader  # Steady-state reads go through the injector.
        async with mcp_client(url, TOKENS.codex_token) as (codex, events, _, initialized):
            assert initialized.capabilities.resources.subscribe is True
            await codex.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
            reader.arm(count=4)
            committed = await asyncio.to_thread(insert, repository, "codex")
            notice = await next_update(events)
            assert str(notice.root.params.uri) == BASE_URI.format("codex")
            assert committed.id not in notice.root.model_dump_json()
            stats = source.stats
            assert stats["healthy"] and stats["transient_retries"] == 4
            assert app.state.push_publisher.enabled and app.state.push_publisher.subscription_count == 1
            async with httpx.AsyncClient() as health_client:
                health = (await health_client.get(url.removesuffix("/mcp") + "/health")).json()
            assert health["push"]["state"] == "healthy"
        # A client that connects after the recovered episode still gets pushes.
        async with mcp_client(url, TOKENS.codex_token) as (again, again_events, _, again_init):
            assert again_init.capabilities.resources.subscribe is True
            await again.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
            await asyncio.to_thread(insert, repository, "codex")
            notice = await next_update(again_events)
            assert str(notice.root.params.uri) == BASE_URI.format("codex")
        assert source.healthy and source.failure is None


@pytest.mark.asyncio
async def test_mcp_busy_past_budget_fails_closed_through_publisher(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")
    app = live_app(tmp_path)
    repository = app.state.repository
    async with loopback(app) as url:
        source = app.state.push_event_source
        source.transient_retry_initial_ms, source.transient_retry_max_ms = 10, 40
        source.transient_retry_budget_ms = 250
        reader = FlakyReader()
        source.read_after = reader
        async with mcp_client(url, TOKENS.codex_token) as (codex, events, _, _):
            await codex.subscribe_resource(AnyUrl(BASE_URI.format("codex")))
            reader.arm()
            await asyncio.to_thread(insert, repository, "codex")
            assert await asyncio.to_thread(source._thread_done.wait, 5)
            notice = await next_method(events, "notifications/message")
            data = json.loads(notice.model_dump_json(by_alias=True, exclude_none=True))["params"]["data"]
            assert data["event"] == "resource_subscriptions_unavailable"
            publisher = app.state.push_publisher
            assert not source.healthy and not publisher.enabled
            assert publisher.subscription_count == 0
            assert source.stats["sqlite_errorcode"] == 5 and source.stats["transient_retries"] >= 2
            async with httpx.AsyncClient() as health_client:
                health = (await health_client.get(url.removesuffix("/mcp") + "/health")).json()
            assert health["ok"] is False and health["push"]["state"] == "failed"
