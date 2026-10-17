from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import uuid
from concurrent.futures import Future
from datetime import datetime, timezone
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def presence_session_fingerprint(session_id: str) -> str:
    """Return a domain-separated, non-reversible DB reference for an MCP session ID."""
    return hashlib.sha256(b"agent-message-bus/topic-presence-session/v1\0" + session_id.encode("utf-8")).hexdigest()


def presence_stream_fingerprint(stream_generation: str) -> str:
    """Return a private, domain-separated reference to one observed SSE registration."""
    return hashlib.sha256(
        b"agent-message-bus/topic-presence-stream/v1" + bytes([0]) + stream_generation.encode("utf-8")
    ).hexdigest()


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
        self._subscription_since: dict[tuple[str, str], datetime] = {}
        self._subscription_generations: dict[tuple[str, str], object] = {}
        self._subscription_binding_ids: dict[tuple[str, str], str] = {}
        self._presence_streams: dict[str, dict[str, Any]] = {}
        # A bool per in-flight subscription is a bounded dirty bit: multiple
        # commits collapse to one follow-up invalidation, never a dropped edge.
        self._pending: dict[tuple[str, str], tuple[object, bool]] = {}
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
            self._subscription_since.clear()
            self._subscription_generations.clear()
            self._subscription_binding_ids.clear()
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
            self._subscription_since.setdefault(key, _utc_now())
            self._subscribers[key] = session
            # Every successful registration fences earlier in-flight sends, even
            # when the SDK reuses the same ServerSession object.
            self._subscription_generations[key] = object()
            self._subscription_binding_ids[key] = uuid.uuid4().hex
            self._pending.pop(key, None)

    async def unsubscribe(self, session_id: str, recipient: str) -> None:
        self._validate_recipient(recipient)
        key = (session_id, BASE_RESOURCE_URI.format(recipient))
        async with self._lock:
            self._subscribers.pop(key, None)
            self._subscription_since.pop(key, None)
            self._subscription_generations.pop(key, None)
            self._subscription_binding_ids.pop(key, None)
            self._pending.pop(key, None)

    async def drop_session(self, session_id: str | None) -> None:
        if not session_id:
            return
        async with self._lock:
            for key in tuple(self._subscribers):
                if key[0] == session_id:
                    self._subscribers.pop(key, None)
                    self._subscription_since.pop(key, None)
                    self._subscription_generations.pop(key, None)
                    self._subscription_binding_ids.pop(key, None)
                    self._pending.pop(key, None)

    async def topic_subscription_binding(self, session_id: str, agent: str) -> tuple[str, str] | None:
        """Return the subscription and exact SSE generations, only for one unambiguous attachment."""
        self._validate_presence_agent(agent)
        if not isinstance(session_id, str) or not session_id or not self.enabled:
            return None
        key = (session_id, BASE_RESOURCE_URI.format(agent))
        async with self._lock:
            if not self.enabled or key not in self._subscribers:
                return None
            streams = [stream for stream in self._presence_streams.values()
                       if stream["session_id"] == session_id and stream["agent"] == agent]
            if len(streams) != 1:
                return None
            binding_id = self._subscription_binding_ids.get(key)
            if binding_id is None:
                return None
            return binding_id, presence_stream_fingerprint(streams[0]["generation"])

    async def live_topic_bindings(self) -> set[tuple[str, str, str, str]]:
        """Snapshot exact session, inbox-subscription and observed SSE registrations."""
        async with self._lock:
            if not self.enabled:
                return set()
            active_streams = {
                (stream["session_id"], stream["agent"],
                 presence_stream_fingerprint(stream["generation"]))
                for stream in self._presence_streams.values()
            }
            result: set[tuple[str, str, str, str]] = set()
            for (session_id, uri), _session in self._subscribers.items():
                agent = uri.removeprefix("agentbus://inbox/")
                binding_id = self._subscription_binding_ids.get((session_id, uri))
                if binding_id is None:
                    continue
                for stream_session, stream_agent, stream_generation in active_streams:
                    if stream_session == session_id and stream_agent == agent:
                        result.add((presence_session_fingerprint(session_id), agent,
                                    binding_id, stream_generation))
            return result

    async def open_presence_stream(self, session_id: str, agent: str) -> str:
        """Record an authorized persistent MCP GET stream observed at the ASGI boundary."""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("MCP session id required")
        self._validate_presence_agent(agent)
        token = uuid.uuid4().hex
        stream_generation = uuid.uuid4().hex
        async with self._lock:
            self._presence_streams[token] = {
                "session_id": session_id,
                "agent": agent,
                "generation": stream_generation,
                "connected_since": _utc_now(),
                "last_stream_write_at": None,
            }
        return token

    async def record_presence_stream_write(self, token: str) -> None:
        async with self._lock:
            stream = self._presence_streams.get(token)
            if stream is not None:
                stream["last_stream_write_at"] = _utc_now()

    async def close_presence_stream(self, token: str) -> None:
        async with self._lock:
            self._presence_streams.pop(token, None)

    async def presence_status(self) -> dict[str, Any]:
        """Return transport-only presence observed by this server process."""
        async with self._lock:
            observed_at = _utc_now()
            streams = tuple(self._presence_streams.values())
            streams_by_agent: dict[str, list[dict[str, Any]]] = {agent: [] for agent in AGENTS}
            active_session_agents: dict[str, str] = {}
            for stream in streams:
                streams_by_agent[stream["agent"]].append(stream)
                active_session_agents[stream["session_id"]] = stream["agent"]
            subscriptions: dict[str, list[datetime]] = {agent: [] for agent in AGENTS}
            if self.enabled:
                for key in self._subscribers:
                    since = self._subscription_since.get(key)
                    agent = active_session_agents.get(key[0])
                    if since is not None and agent is not None:
                        subscriptions[agent].append(since)

            agents = []
            for agent in sorted(AGENTS):
                agent_streams = streams_by_agent[agent]
                subscription_times = subscriptions[agent]
                agents.append({
                    "agent": agent,
                    "active_streams": len(agent_streams),
                    "inbox_subscriptions": len(subscription_times),
                    "subscribed": bool(subscription_times),
                    "connected_since": _utc_text(min(
                        (stream["connected_since"] for stream in agent_streams), default=None
                    )),
                    "subscribed_since": _utc_text(min(subscription_times, default=None)),
                    "last_stream_write_at": _utc_text(max(
                        (stream["last_stream_write_at"] for stream in agent_streams
                         if stream["last_stream_write_at"] is not None), default=None
                    )),
                })
            return {
                "observed_at": _utc_text(observed_at),
                "scope": "this_server_process",
                "native_push_available": self.enabled,
                "semantics": "Transport observation only; not chat readiness, message reading, delivery, or approval.",
                "agents": agents,
            }

    @staticmethod
    def _validate_presence_agent(agent: Any) -> None:
        if not isinstance(agent, str) or agent not in AGENTS:
            raise ValueError("invalid authenticated agent")

    async def publish_committed(self, recipient: str) -> int:
        """Publish recipient-only invalidations after a successful commit."""
        self._validate_recipient(recipient)
        if not self.enabled:
            return 0
        uri = BASE_RESOURCE_URI.format(recipient)
        async with self._lock:
            targets: list[tuple[tuple[str, str], ServerSession, object]] = []
            for key, session in self._subscribers.items():
                if key[1] != uri:
                    continue
                generation = self._subscription_generations[key]
                pending = self._pending.get(key)
                if pending is not None and pending[0] is generation:
                    self._pending[key] = (generation, True)
                    self.coalesced_notifications += 1
                    continue
                self._pending[key] = (generation, False)
                targets.append((key, session, generation))

        async def notify(key: tuple[str, str], session: ServerSession, generation: object) -> int:
            sent = 0
            while True:
                async with self._lock:
                    if self._subscription_generations.get(key) is not generation:
                        pending = self._pending.get(key)
                        if pending is not None and pending[0] is generation:
                            self._pending.pop(key, None)
                        return sent
                try:
                    await session.send_resource_updated(AnyUrl(uri))
                    sent += 1
                    self.sent_notifications += 1
                except Exception:
                    # Neither failed nor successful old sends may mutate a newer
                    # registration, including one reusing the same SDK session.
                    async with self._lock:
                        if self._subscription_generations.get(key) is generation:
                            self._subscribers.pop(key, None)
                            self._subscription_since.pop(key, None)
                            self._subscription_generations.pop(key, None)
                            self._subscription_binding_ids.pop(key, None)
                        pending = self._pending.get(key)
                        if pending is not None and pending[0] is generation:
                            self._pending.pop(key, None)
                    return sent
                async with self._lock:
                    pending = self._pending.get(key)
                    if (self._subscription_generations.get(key) is not generation
                            or pending is None or pending[0] is not generation):
                        return sent
                    if pending[1]:
                        self._pending[key] = (generation, False)
                        continue
                    self._pending.pop(key, None)
                    return sent

        # Each current registration has at most one notification worker.
        results = await asyncio.gather(*(notify(key, session, generation) for key, session, generation in targets))
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
