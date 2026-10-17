from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .database import AGENTS
from .push import presence_session_fingerprint


_TOPIC_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
_GENERATION_PATTERN = re.compile(r"[0-9a-f]{32}", re.ASCII)
_STREAM_GENERATION_PATTERN = re.compile(r"[0-9a-f]{64}", re.ASCII)


class TopicPresenceError(ValueError):
    """Invalid topic registration input."""


def validate_topic(topic: str) -> str:
    if not isinstance(topic, str) or _TOPIC_PATTERN.fullmatch(topic) is None:
        raise TopicPresenceError(
            "topic must be a 1-128 character identifier using letters, digits, '.', '_', ':' or '-'")
    return topic


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TopicPresenceRepository:
    """Durable per-agent topic associations; transport liveness is always process-observed."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @staticmethod
    def _validate_agent(agent: str) -> None:
        if not isinstance(agent, str) or agent not in AGENTS:
            raise TopicPresenceError("invalid authenticated agent")

    @staticmethod
    def _validate_session(session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise TopicPresenceError("MCP session id required")

    def register(self, *, agent: str, topic: str, session_id: str,
                 subscription_generation: str, stream_generation: str) -> dict[str, str]:
        self._validate_agent(agent)
        topic = validate_topic(topic)
        self._validate_session(session_id)
        if not isinstance(subscription_generation, str) or _GENERATION_PATTERN.fullmatch(subscription_generation) is None:
            raise TopicPresenceError("active inbox subscription required")
        if not isinstance(stream_generation, str) or _STREAM_GENERATION_PATTERN.fullmatch(stream_generation) is None:
            raise TopicPresenceError("one active SSE registration required")
        generation = uuid.uuid4().hex
        registered_at = _now()
        session_hash = presence_session_fingerprint(session_id)
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("""INSERT INTO topic_memberships
                    (topic, agent, session_hash, subscription_generation, stream_generation, generation, registered_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(topic, agent) DO UPDATE SET
                        session_hash=excluded.session_hash,
                        subscription_generation=excluded.subscription_generation,
                        stream_generation=excluded.stream_generation,
                        generation=excluded.generation,
                        registered_at=excluded.registered_at""",
                    (topic, agent, session_hash, subscription_generation, stream_generation,
                     generation, registered_at))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"topic": topic, "agent": agent, "generation": generation,
                "registered_at": registered_at}

    def leave(self, *, agent: str, topic: str, session_id: str, generation: str) -> bool:
        """Delete only the exact registration owned by this agent and native session."""
        self._validate_agent(agent)
        topic = validate_topic(topic)
        self._validate_session(session_id)
        if not isinstance(generation, str) or _GENERATION_PATTERN.fullmatch(generation) is None:
            raise TopicPresenceError("invalid registration generation")
        session_hash = presence_session_fingerprint(session_id)
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute("""DELETE FROM topic_memberships
                    WHERE topic=? AND agent=? AND session_hash=? AND generation=?""",
                    (topic, agent, session_hash, generation))
                conn.commit()
                return cursor.rowcount == 1
            except Exception:
                conn.rollback()
                raise

    def members(self, topic: str) -> list[dict[str, Any]]:
        topic = validate_topic(topic)
        with self.connect() as conn:
            rows = conn.execute("""SELECT agent, session_hash, subscription_generation,
                    stream_generation, generation, registered_at
                FROM topic_memberships WHERE topic=? ORDER BY agent""", (topic,)).fetchall()
        return [dict(row) for row in rows]
