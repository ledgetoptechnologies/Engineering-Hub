"""Bounded invalidation pointers, never a second Hub record store or poller.

The Linux source installs inode watches before its barrier-protected snapshot.
Subscribers register before receiving the broker snapshot. A reconnect always
invalidates the client's cache; cursors are not a durable replay protocol.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from agent_message_bus.sqlite_events import SQLiteEventSource, WatcherFailed, native_event_support


class HubOutboxCursorReader:
    """Read only the monotonically allocated outbox head behind native barrier.

    No entities, titles, message bodies or historical rows are selected. Gaps
    caused by future retention remain legal because sqlite_sequence is durable.
    """

    def __call__(self, conn: sqlite3.Connection, cursor: int | None):
        row = conn.execute("""SELECT
            COALESCE((SELECT seq FROM sqlite_sequence WHERE name='hub_outbox'), 0),
            COALESCE(MAX(sequence), 0) FROM hub_outbox""").fetchone()
        head, retained = int(row[0]), int(row[1])
        if head < 0 or retained > head:
            raise WatcherFailed("Hub outbox head is inconsistent")
        if cursor is None:
            return (), head
        if type(cursor) is not int or not 0 <= cursor <= head:
            raise WatcherFailed("Hub outbox cursor is outside durable head")
        return (("hub",) if head > cursor else ()), head


class EventBroker:
    """Event-loop-owned fanout; a slow subscriber fails closed, not unbounded.

    One invalidation represents every state change through its cursor. Duplicate
    heads are coalesced. Exhausted subscriber capacity emits a final unavailable
    pointer and closes that subscriber; clients must reconnect/refetch.
    """

    def __init__(self, *, queue_size: int = 8):
        if type(queue_size) is not int or not 1 <= queue_size <= 1024:
            raise ValueError("queue_size must be between 1 and 1024")
        self.queue_size = queue_size
        self.observer_epoch = uuid.uuid4().hex
        self.cursor = 0
        self.available = False
        self._subscribers: set[asyncio.Queue] = set()

    @property
    def subscriber_count(self):
        return len(self._subscribers)

    def pointer(self, kind="invalidate"):
        return {"observer_epoch": self.observer_epoch, "cursor": self.cursor, "kind": kind}

    def activate(self, cursor: int):
        if type(cursor) is not int or cursor < 0:
            raise ValueError("invalid cursor")
        if self._subscribers:
            raise RuntimeError("cannot activate with existing subscribers")
        self.observer_epoch = uuid.uuid4().hex
        self.cursor = cursor
        self.available = True

    @staticmethod
    def _replace(queue, pointer):
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(pointer)

    def publish(self, cursor: int):
        if not self.available:
            return
        if type(cursor) is not int or cursor < self.cursor:
            self.fail()
            return
        if cursor == self.cursor:
            return
        self.cursor = cursor
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(self.pointer())
            except asyncio.QueueFull:
                self._replace(queue, self.pointer("source_unavailable"))
                self._subscribers.discard(queue)

    def fail(self):
        self.available = False
        for queue in tuple(self._subscribers):
            self._replace(queue, self.pointer("source_unavailable"))
        self._subscribers.clear()

    async def events(self) -> AsyncIterator[dict]:
        queue = asyncio.Queue(maxsize=self.queue_size)
        # No await between registration and snapshot: writes cannot slip into
        # a subscribe-after-snapshot gap on this event loop.
        self._subscribers.add(queue)
        try:
            initial = self.pointer("invalidate" if self.available else "source_unavailable")
            yield initial
            if initial["kind"] == "source_unavailable":
                return
            while True:
                event = await queue.get()
                yield event
                if event["kind"] == "source_unavailable":
                    return
        finally:
            self._subscribers.discard(queue)

    async def sse(self) -> AsyncIterator[str]:
        events = self.events()
        try:
            async for pointer in events:
                yield "data: " + json.dumps(pointer, separators=(",", ":")) + "\n\n"
        finally:
            await events.aclose()


class NativeHubEventFeed:
    """ASGI lifespan adapter. Unsupported deployments remain explicitly disabled.

    ``await start()`` / ``await stop()`` run blocking native lifecycle off-loop.
    Check ``available`` before creating StreamingResponse(broker.sse(),
    media_type='text/event-stream'); return 503 when false. Authenticate first.
    No replay/Last-Event-ID claim is made; each connection refetches a snapshot.
    """

    def __init__(self, path: str | Path, *, queue_size=8,
                 source_factory: Callable = SQLiteEventSource,
                 support_check: Callable = native_event_support):
        self.path = Path(path)
        self.broker = EventBroker(queue_size=queue_size)
        self._factory = source_factory
        self._support_check = support_check
        self._source = None
        self._loop = None
        self._lock = threading.Lock()
        self._scheduled = False
        self._pending_cursor = None
        self._pending_error = False
        self.reason = "not_started"

    @property
    def available(self):
        return self.broker.available and self._source is not None and self._source.healthy

    @property
    def status(self):
        return {"available": self.available, "reason": self.reason,
                "observer_epoch": self.broker.observer_epoch, "cursor": self.broker.cursor}

    def _notify(self, _channels, cursor):
        self._schedule(cursor=cursor)

    def _error(self, _exc):
        self._schedule(error=True)

    def _schedule(self, *, cursor=None, error=False):
        # Native writes may burst while the ASGI loop is occupied: retain only
        # the latest head and at most one scheduled callback, not an unbounded
        # call_soon_threadsafe backlog. Errors always outrank invalidations.
        with self._lock:
            if self._loop is None:
                return
            if cursor is not None:
                self._pending_cursor = max(cursor, self._pending_cursor or 0)
            self._pending_error |= error
            if not self._scheduled:
                self._scheduled = True
                self._loop.call_soon_threadsafe(self._drain)

    def _drain(self):
        with self._lock:
            cursor, error = self._pending_cursor, self._pending_error
            self._pending_cursor = None
            self._pending_error = self._scheduled = False
        if error:
            self.reason = "native_source_failed"
            self.broker.fail()
        elif cursor is not None:
            self.broker.publish(cursor)

    async def start(self):
        if self._source is not None:
            raise RuntimeError("Hub feed is already started")
        supported, _reason = self._support_check(self.path)
        if not supported:
            self.reason = "native_source_unsupported"
            self.broker.fail()
            return self.status
        self._loop = asyncio.get_running_loop()
        try:
            self._source = self._factory(self.path, HubOutboxCursorReader(), self._notify,
                                         on_error=self._error)
            stats = await asyncio.to_thread(self._source.start)
            self.broker.activate(int(stats["cursor"]))
            self.reason = "native_source_active"
            # Catch up changes delivered while start() was returning. The
            # source's stats is memory only, never another SQLite query.
            if not self._source.healthy:
                self.reason = "native_source_failed"
                self.broker.fail()
            else:
                self.broker.publish(int(self._source.stats["cursor"]))
        except Exception:
            self.reason = "native_source_failed"
            self.broker.fail()
            if self._source is not None:
                await asyncio.to_thread(self._source.stop)
            self._source = None
        return self.status

    async def stop(self):
        self.broker.fail()
        source, self._source = self._source, None
        if source is not None:
            await asyncio.to_thread(source.stop)
        with self._lock:
            self._loop = None
            self._pending_cursor = None
            self._pending_error = self._scheduled = False
        self.reason = "stopped"
