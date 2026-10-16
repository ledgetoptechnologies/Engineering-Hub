from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from typing import Any

from mcp.server.session import ServerSession
from pydantic import AnyUrl

from .database import AGENTS


BASE_RESOURCE_URI = "agentbus://inbox/{}"
MAX_ACTIVE_SUBSCRIPTIONS = 32


class SubscriptionLimit(RuntimeError):
    """Raised when the bounded in-process subscription registry is full."""


class SubscriptionsUnavailable(RuntimeError):
    """Raised while no complete post-commit event source is enabled."""


class PushPublisher:
    """Bounded, in-process fanout of recipient-only MCP resource invalidations.

    The publisher never reads SQLite. Its caller must invoke
    ``publish_committed`` only after a durable message commit has completed.
    It is disabled by default until every writer has a reliable post-commit
    event path.
    """

    def __init__(self, *, enabled: bool = False, max_subscriptions: int = MAX_ACTIVE_SUBSCRIPTIONS):
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if isinstance(max_subscriptions, bool) or not isinstance(max_subscriptions, int):
            raise ValueError("max_subscriptions must be an integer")
        if not 1 <= max_subscriptions <= MAX_ACTIVE_SUBSCRIPTIONS:
            raise ValueError(f"max_subscriptions must be between 1 and {MAX_ACTIVE_SUBSCRIPTIONS}")
        self._requested_enabled = enabled
        self._source_healthy = True
        self._source_failure: str | None = None
        self.max_subscriptions = max_subscriptions
        self._subscribers: dict[tuple[str, str], ServerSession] = {}
        # A bool per in-flight subscription is a bounded dirty bit: multiple
        # commits collapse to one follow-up invalidation, never a dropped edge.
        self._pending: dict[tuple[str, str], tuple[ServerSession, bool]] = {}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scheduled_lock = threading.Lock()
        self._scheduled_recipients: set[str] = set()
        self._scheduled_dirty: set[str] = set()
        self.coalesced_notifications = 0
        self.sent_notifications = 0

    @property
    def enabled(self) -> bool:
        return self._requested_enabled and self._source_healthy

    @property
    def healthy(self) -> bool:
        return self._source_healthy

    @property
    def source_failure(self) -> str | None:
        return self._source_failure

    def activate(self) -> None:
        if not self._source_healthy:
            raise SubscriptionsUnavailable("native message event source is unhealthy")
        self._requested_enabled = True

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is not None and self._loop is not loop and self._subscribers:
            raise RuntimeError("publisher subscriptions must share one application event loop")
        self._loop = loop

    def fail_source(self, reason: str) -> None:
        """Fail closed and notify active clients instead of leaving live-looking subscriptions."""
        if self._source_failure is not None:
            return
        self._source_healthy = False
        self._source_failure = "native SQLite event source failed"
        logging.getLogger("agent_message_bus.push").critical(
            "Native SQLite event source failed closed: %s", reason
        )
        loop = self._loop
        if loop is not None and not loop.is_closed() and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._schedule_failure_notice)
            except RuntimeError:
                pass

    def _schedule_failure_notice(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            task = loop.create_task(self._notify_source_failure())
            task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)

    async def _notify_source_failure(self) -> None:
        async with self._lock:
            targets = list(self._subscribers.items())
            self._subscribers.clear()
            self._pending.clear()
        async def notify(key, session):
            try:
                await session.send_log_message(
                    level="error",
                    data={"event": "resource_subscriptions_unavailable",
                          "reason": self._source_failure},
                    logger="agent-message-bus",
                )
            except Exception:
                pass
        await asyncio.gather(*(notify(key, session) for key, session in targets))

    @property
    def subscription_count(self) -> int:
        return len(self._subscribers)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def subscribe(self, session_id: str, recipient: str, session: ServerSession) -> None:
        if not self.enabled:
            raise SubscriptionsUnavailable("resource subscriptions are disabled until a post-commit event source is operational")
        self._validate_recipient(recipient)
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("MCP session id required")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop and self._subscribers:
            raise RuntimeError("publisher subscriptions must share one application event loop")
        self._loop = loop
        key = (session_id, BASE_RESOURCE_URI.format(recipient))
        async with self._lock:
            if key not in self._subscribers and len(self._subscribers) >= self.max_subscriptions:
                raise SubscriptionLimit("active MCP subscription limit reached")
            self._subscribers[key] = session

    async def unsubscribe(self, session_id: str, recipient: str) -> None:
        self._validate_recipient(recipient)
        key = (session_id, BASE_RESOURCE_URI.format(recipient))
        async with self._lock:
            self._subscribers.pop(key, None)
            self._pending.pop(key, None)

    async def drop_session(self, session_id: str | None) -> None:
        if not session_id:
            return
        async with self._lock:
            for key in tuple(self._subscribers):
                if key[0] == session_id:
                    self._subscribers.pop(key, None)
                    self._pending.pop(key, None)

    async def publish_committed(self, recipient: str) -> int:
        """Publish recipient-only invalidations after a successful commit."""
        self._validate_recipient(recipient)
        if not self.enabled:
            return 0
        uri = BASE_RESOURCE_URI.format(recipient)
        async with self._lock:
            targets: list[tuple[tuple[str, str], ServerSession]] = []
            for key, session in self._subscribers.items():
                if key[1] != uri:
                    continue
                pending = self._pending.get(key)
                if pending is not None and pending[0] is session:
                    self._pending[key] = (session, True)
                    self.coalesced_notifications += 1
                    continue
                self._pending[key] = (session, False)
                targets.append((key, session))

        async def notify(key: tuple[str, str], session: ServerSession) -> int:
            sent = 0
            while True:
                async with self._lock:
                    if self._subscribers.get(key) is not session:
                        pending = self._pending.get(key)
                        if pending is not None and pending[0] is session:
                            self._pending.pop(key, None)
                        return sent
                try:
                    await session.send_resource_updated(AnyUrl(uri))
                    sent += 1
                    self.sent_notifications += 1
                except Exception:
                    # A stale session is not allowed to block another recipient
                    # or turn a successful database write into a failed response.
                    async with self._lock:
                        if self._subscribers.get(key) is session:
                            self._subscribers.pop(key, None)
                        pending = self._pending.get(key)
                        if pending is not None and pending[0] is session:
                            self._pending.pop(key, None)
                    return sent
                async with self._lock:
                    pending = self._pending.get(key)
                    if self._subscribers.get(key) is not session or pending is None or pending[0] is not session:
                        return sent
                    if pending[1]:
                        self._pending[key] = (session, False)
                        continue
                    self._pending.pop(key, None)
                    return sent

        # At most max_subscriptions notification workers can exist at once.
        results = await asyncio.gather(*(notify(key, session) for key, session in targets))
        return sum(results)

    def publish_committed_from_thread(self, recipient: str) -> bool:
        """Schedule a bounded delivery on the app loop, retaining a dirty edge."""
        self._validate_recipient(recipient)
        loop = self._loop
        if not self.enabled or loop is None or loop.is_closed() or not loop.is_running():
            return False
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is not None and running_loop is not loop:
            raise RuntimeError("publisher called from a different application event loop")

        with self._scheduled_lock:
            if recipient in self._scheduled_recipients:
                self._scheduled_dirty.add(recipient)
                return True
            self._scheduled_recipients.add(recipient)

        async def drain() -> None:
            try:
                while True:
                    await self.publish_committed(recipient)
                    with self._scheduled_lock:
                        if recipient in self._scheduled_dirty:
                            self._scheduled_dirty.discard(recipient)
                            continue
                        self._scheduled_recipients.discard(recipient)
                        return
            except BaseException:
                with self._scheduled_lock:
                    self._scheduled_recipients.discard(recipient)
                    self._scheduled_dirty.discard(recipient)
                raise

        coroutine = drain()
        if running_loop is loop:
            task = loop.create_task(coroutine)
            task.add_done_callback(self._observe_scheduled_result)
            return True
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError:
            coroutine.close()
            with self._scheduled_lock:
                self._scheduled_recipients.discard(recipient)
                self._scheduled_dirty.discard(recipient)
            return False
        future.add_done_callback(self._observe_scheduled_result)
        return True

    def _observe_scheduled_result(self, completed: Future | asyncio.Future) -> None:
        try:
            if not completed.cancelled():
                completed.result()
        except Exception:
            logging.getLogger("agent_message_bus.push").exception(
                "Could not schedule native message invalidation"
            )
            self.fail_source("notification scheduling failed")

    @staticmethod
    def _validate_recipient(recipient: Any) -> None:
        if not isinstance(recipient, str) or recipient not in AGENTS:
            raise ValueError("invalid inbox recipient")
