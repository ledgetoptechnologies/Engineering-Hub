"""Native MCP inbox wake runtime for the opt-in Hermes Desktop plugin.

This module intentionally contains no CLI/process launching. A single Streamable
HTTP MCP session subscribes to the authenticated inbox resource; durable cursors,
per-topic conversation bindings, and an outbox survive backend restarts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

logger = logging.getLogger("hermes.plugin.agent_bus_wake")

_PLUGIN_ID = "agent-bus-wake"
_EVENT_NAME = "message.ready"
_MAX_PAGE_SIZE = 100
_MAX_BODY_BYTES = 48 * 1024
_TOPIC_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class WakeError(RuntimeError):
    """Safe, user-facing wake configuration or delivery error."""


class _ClosingConnection(sqlite3.Connection):
    """sqlite3 context managers commit but do not close; every store handle must be short-lived."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class StateStore:
    """Small private SQLite store for bindings, durable cursors, and pending dispatches."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bindings (
                    id TEXT PRIMARY KEY,
                    server_name TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    stored_session_id TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    target_profile TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_binding_topic_idx
                    ON bindings(server_name,recipient,topic) WHERE enabled=1;
                CREATE TABLE IF NOT EXISTS cursors (
                    server_name TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    cursor INTEGER NOT NULL CHECK(cursor >= 0),
                    PRIMARY KEY(server_name, recipient)
                );
                CREATE TABLE IF NOT EXISTS dispatches (
                    id TEXT PRIMARY KEY,
                    server_name TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    binding_id TEXT NOT NULL,
                    message_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    claim_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(server_name, recipient, seq)
                );
                CREATE INDEX IF NOT EXISTS dispatch_status_idx
                    ON dispatches(status, created_at);
                """
            )
            version = db.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
            if version is None or version["value"] != "2":
                self._compact_existing_dispatches(db)
                db.execute(
                    "INSERT INTO settings(key,value) VALUES('schema_version','2') "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                )
            # A process restart cannot know whether a prompt RPC crossed the
            # desktop/backend boundary. Persist it as uncertain, never replay it.
            db.execute(
                "UPDATE dispatches SET status='uncertain',updated_at=? "
                "WHERE status IN ('claiming','claimed','submitting')",
                (self._now(),),
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0, factory=_ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @staticmethod
    def _message_reference(message: dict[str, Any], *, message_id: str, seq: int, recipient: str) -> dict[str, Any]:
        body = message.get("body")
        topic = body.get("topic") if isinstance(body, dict) else None
        try:
            body_bytes = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError):
            body_bytes = 0
        return {
            "id": message_id,
            "seq": seq,
            "sender": message.get("sender") if isinstance(message.get("sender"), str) else "",
            "recipient": recipient,
            "message_type": message.get("message_type") if isinstance(message.get("message_type"), str) else "",
            "status": message.get("status") if isinstance(message.get("status"), str) else "",
            "requires_user_approval": message.get("requires_user_approval") is True,
            "created_at": message.get("created_at") if isinstance(message.get("created_at"), str) else "",
            "topic": topic if isinstance(topic, str) else "",
            "body_bytes": body_bytes,
        }

    def _compact_existing_dispatches(self, db: sqlite3.Connection) -> None:
        """Purge payload bodies left by an earlier development schema revision."""
        rows = db.execute("SELECT id,message_id,seq,recipient,message_json FROM dispatches").fetchall()
        for row in rows:
            try:
                value = json.loads(row["message_json"])
                message = value if isinstance(value, dict) else {}
            except (TypeError, ValueError):
                message = {}
            reference = self._message_reference(
                message, message_id=row["message_id"], seq=int(row["seq"]),
                recipient=row["recipient"],
            )
            db.execute(
                "UPDATE dispatches SET message_json=? WHERE id=?",
                (json.dumps(reference, ensure_ascii=False, separators=(",", ":")), row["id"]),
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def setting(self, key: str, default: str = "") -> str:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def enabled(self) -> bool:
        return self.setting("enabled", "0") == "1"

    def set_enabled(self, value: bool) -> None:
        self.set_setting("enabled", "1" if value else "0")

    def add_binding(
        self,
        *,
        server_name: str,
        recipient: str,
        topic: str,
        stored_session_id: str,
        connection_id: str,
        profile: str,
        target_profile: str,
    ) -> dict[str, Any]:
        if (not isinstance(topic, str) or not _TOPIC_RE.fullmatch(topic)
                or topic != topic.strip()):
            raise WakeError("topic must be 1–120 printable characters without surrounding whitespace")
        if not isinstance(stored_session_id, str) or not _SESSION_ID_RE.fullmatch(stored_session_id):
            raise WakeError("stored session id is invalid")
        if (connection_id, profile, target_profile) != ("local", "default", "default"):
            raise WakeError("only an exact local/default-profile session can be bound")
        binding_id = str(uuid.uuid4())
        try:
            with self._lock, self._connect() as db:
                db.execute(
                    """INSERT INTO bindings
                       (id,server_name,recipient,topic,stored_session_id,connection_id,profile,
                        target_profile,enabled,created_at)
                       VALUES(?,?,?,?,?,?,?,?,1,?)""",
                    (binding_id, server_name, recipient, topic, stored_session_id,
                     connection_id, profile, target_profile, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise WakeError("this topic is already linked; unlink it before reassigning") from exc
        return {
            "id": binding_id, "server_name": server_name, "recipient": recipient,
            "topic": topic, "stored_session_id": stored_session_id,
            "owner": {"connection_id": connection_id, "profile": profile,
                      "target_profile": target_profile},
        }

    def bindings(self, *, server_name: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM bindings WHERE enabled=1"
        params: tuple[Any, ...] = ()
        if server_name:
            query += " AND server_name=?"
            params = (server_name,)
        query += " ORDER BY created_at,id"
        with self._lock, self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._binding_dict(row) for row in rows]

    @staticmethod
    def _binding_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "server_name": row["server_name"],
            "recipient": row["recipient"], "topic": row["topic"],
            "stored_session_id": row["stored_session_id"],
            "owner": {"connection_id": row["connection_id"], "profile": row["profile"],
                      "target_profile": row["target_profile"]},
        }

    def remove_binding(self, binding_id: str) -> bool:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM bindings WHERE id=? AND enabled=1", (binding_id,)).fetchone()
            if row is None:
                db.rollback()
                return False
            db.execute("UPDATE bindings SET enabled=0 WHERE id=?", (binding_id,))
            db.execute(
                "UPDATE dispatches SET status='cancelled',updated_at=? "
                "WHERE binding_id=? AND status='pending'",
                (self._now(), binding_id),
            )
            db.commit()
            return True

    def cursor(self, server_name: str, recipient: str) -> int | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT cursor FROM cursors WHERE server_name=? AND recipient=?",
                (server_name, recipient),
            ).fetchone()
        return int(row["cursor"]) if row else None

    def set_baseline(self, server_name: str, recipient: str, cursor: int) -> None:
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise WakeError("server returned an invalid initial inbox cursor")
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO cursors(server_name,recipient,cursor) VALUES(?,?,?)",
                (server_name, recipient, cursor),
            )

    def record_page(
        self,
        server_name: str,
        recipient: str,
        *,
        expected_cursor: int,
        messages: list[dict[str, Any]],
        next_cursor: int,
    ) -> list[str]:
        """Durably store eligible items and advance the scan cursor in one transaction."""
        if isinstance(next_cursor, bool) or not isinstance(next_cursor, int) or next_cursor < expected_cursor:
            raise WakeError("server returned a non-monotonic inbox cursor")
        now = self._now()
        new_ids: list[str] = []
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT cursor FROM cursors WHERE server_name=? AND recipient=?",
                (server_name, recipient),
            ).fetchone()
            actual = int(row["cursor"]) if row else None
            if actual is not None and actual != expected_cursor:
                db.rollback()
                raise WakeError("inbox cursor changed during reconciliation")
            for message in messages:
                self._record_message(db, server_name, recipient, message, now, new_ids)
            db.execute(
                """INSERT INTO cursors(server_name,recipient,cursor) VALUES(?,?,?)
                   ON CONFLICT(server_name,recipient) DO UPDATE SET cursor=excluded.cursor""",
                (server_name, recipient, next_cursor),
            )
            db.commit()
        return new_ids

    def _record_message(
        self, db: sqlite3.Connection, server_name: str, recipient: str,
        message: dict[str, Any], now: str, new_ids: list[str],
    ) -> None:
        seq = message.get("seq")
        message_id = message.get("id")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise WakeError("server returned a message without a valid sequence")
        if not isinstance(message_id, str) or not message_id:
            raise WakeError("server returned a message without an id")
        if message.get("recipient") != recipient:
            # The server authenticates the recipient; mismatch is a protocol error,
            # never a reason to route the item elsewhere.
            raise WakeError("server returned a message for a different recipient")
        if message.get("status") != "pending" or message.get("requires_user_approval") is True:
            return
        message_type = str(message.get("message_type") or "").lower()
        if message_type not in {"task", "request", "reply", "info", "error"}:
            return
        body = message.get("body")
        if not isinstance(body, dict):
            return
        topic = body.get("topic")
        if not isinstance(topic, str):
            return
        match = db.execute(
            """SELECT * FROM bindings WHERE server_name=? AND recipient=? AND topic=? AND enabled=1""",
            (server_name, recipient, topic),
        ).fetchone()
        if match is None:
            return
        try:
            body_size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError):
            return
        if body_size > _MAX_BODY_BYTES:
            return
        reference = self._message_reference(message, message_id=message_id, seq=seq, recipient=recipient)
        encoded = json.dumps(reference, ensure_ascii=False, separators=(",", ":"))
        dispatch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-bus-wake:{server_name}:{recipient}:{seq}"))
        cursor = db.execute(
            """INSERT OR IGNORE INTO dispatches
               (id,server_name,recipient,seq,message_id,binding_id,message_json,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,'pending',?,?)""",
            (dispatch_id, server_name, recipient, seq, message_id, match["id"], encoded, now, now),
        )
        if cursor.rowcount:
            new_ids.append(dispatch_id)

    def dispatch_batch(self, dispatch_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
            binding = db.execute(
                "SELECT * FROM bindings WHERE id=? AND enabled=1", (row["binding_id"],)
            ).fetchone() if row else None
            rows = db.execute(
                """SELECT * FROM dispatches WHERE server_name=? AND recipient=? AND binding_id=?
                   AND claim_hash IS ? AND status IN ('claiming','claimed','submitting') ORDER BY seq""",
                (row["server_name"], row["recipient"], row["binding_id"], row["claim_hash"]),
            ).fetchall() if row else []
        if not row or not binding:
            return None
        return {
            "id": row["id"], "status": row["status"], "server_name": row["server_name"],
            "recipient": row["recipient"], "seq": int(row["seq"]),
            "binding": self._binding_dict(binding),
            "dispatch_ids": [item["id"] for item in rows],
            "messages": [json.loads(item["message_json"]) for item in rows],
        }

    def pending_dispatch_ids(self, *, limit: int = 100) -> list[str]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                """SELECT d.id FROM dispatches AS d
                   WHERE d.status='pending'
                     AND NOT EXISTS (
                       SELECT 1 FROM dispatches AS active
                       WHERE active.server_name=d.server_name AND active.recipient=d.recipient
                         AND active.binding_id=d.binding_id
                         AND active.status IN ('claiming','claimed','submitting','uncertain')
                     )
                     AND d.seq=(SELECT MIN(first.seq) FROM dispatches AS first
                       WHERE first.server_name=d.server_name AND first.recipient=d.recipient
                         AND first.binding_id=d.binding_id AND first.status='pending')
                   ORDER BY d.created_at,d.id LIMIT ?""",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def begin_claim(self, dispatch_id: str) -> dict[str, Any] | None:
        now = self._now()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM dispatches WHERE id=? AND status='pending'", (dispatch_id,)
            ).fetchone()
            if row is None:
                db.rollback()
                return None
            active = db.execute(
                """SELECT 1 FROM dispatches WHERE server_name=? AND recipient=? AND binding_id=?
                   AND status IN ('claiming','claimed','submitting','uncertain') LIMIT 1""",
                (row["server_name"], row["recipient"], row["binding_id"]),
            ).fetchone()
            if active:
                db.rollback()
                return None
            candidates = db.execute(
                """SELECT id,message_json FROM dispatches WHERE server_name=? AND recipient=?
                   AND binding_id=? AND status='pending' ORDER BY seq LIMIT 10""",
                (row["server_name"], row["recipient"], row["binding_id"]),
            ).fetchall()
            total = 0
            selected = []
            for candidate in candidates:
                reference = json.loads(candidate["message_json"])
                size = int(reference.get("body_bytes", 0))
                if selected and total + size > 96 * 1024:
                    break
                selected.append(candidate["id"])
                total += size
            if not selected or dispatch_id not in selected:
                db.rollback()
                return None
            placeholders = ",".join("?" for _ in selected)
            db.execute(
                f"UPDATE dispatches SET status='claiming',updated_at=? WHERE id IN ({placeholders}) AND status='pending'",
                (now, *selected),
            )
            db.commit()
        return self.dispatch_batch(dispatch_id)

    def set_dispatches_status(
        self, dispatch_ids: list[str], status: str, *, claim_token: str | None = None,
        expected: tuple[str, ...] | None = None,
    ) -> bool:
        if not dispatch_ids:
            return False
        allowed = {"pending", "claiming", "claimed", "submitting", "submitted", "uncertain", "cancelled"}
        if status not in allowed:
            raise ValueError("invalid dispatch status")
        placeholders = ",".join("?" for _ in dispatch_ids)
        values: list[Any] = [status, self._now()]
        sql = f"UPDATE dispatches SET status=?,updated_at=?"
        if claim_token is not None:
            sql += ",claim_hash=?"
            values.append(hashlib.sha256(claim_token.encode("utf-8")).hexdigest())
        sql += f" WHERE id IN ({placeholders})"
        values.extend(dispatch_ids)
        if expected:
            sql += " AND status IN (" + ",".join("?" for _ in expected) + ")"
            values.extend(expected)
        with self._lock, self._connect() as db:
            changed = db.execute(sql, values).rowcount
        return changed == len(dispatch_ids)

    def set_dispatch_status(
        self, dispatch_id: str, status: str, *, claim_token: str | None = None,
        expected: tuple[str, ...] | None = None,
    ) -> bool:
        allowed = {"pending", "claiming", "claimed", "submitting", "submitted", "uncertain", "cancelled"}
        if status not in allowed:
            raise ValueError("invalid dispatch status")
        values: list[Any] = [status, self._now()]
        sql = "UPDATE dispatches SET status=?,updated_at=?"
        if claim_token is not None:
            sql += ",claim_hash=?"
            values.append(hashlib.sha256(claim_token.encode("utf-8")).hexdigest())
        sql += " WHERE id=?"
        values.append(dispatch_id)
        if expected:
            sql += " AND status IN (" + ",".join("?" for _ in expected) + ")"
            values.extend(expected)
        with self._lock, self._connect() as db:
            changed = db.execute(sql, values).rowcount
        return bool(changed)

    def verify_claim(self, dispatch_id: str, claim_token: str) -> bool:
        token_hash = hashlib.sha256(claim_token.encode("utf-8")).hexdigest()
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM dispatches WHERE id=? AND claim_hash=? AND status='claimed'",
                (dispatch_id, token_hash),
            ).fetchone()
        return row is not None

    def counts(self) -> dict[str, int]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT status,COUNT(*) AS n FROM dispatches GROUP BY status").fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}


@asynccontextmanager
async def _default_connector(config: Mapping[str, Any], message_handler):
    """Open a native MCP Streamable HTTP client using Hermes' configured server credentials."""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    try:
        from tools.mcp_tool import sdk_httpx
        httpx = sdk_httpx() or httpx
    except Exception:
        pass

    url = str(config.get("url") or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise WakeError("selected MCP server has an invalid HTTP URL")
    headers = config.get("headers") or {}
    if not isinstance(headers, Mapping):
        raise WakeError("selected MCP server headers are invalid")
    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(
        headers={str(key): str(value) for key, value in headers.items()},
        timeout=timeout, trust_env=False, follow_redirects=False,
        verify=config.get("ssl_verify", True),
    ) as client:
        async with streamable_http_client(url, http_client=client, terminate_on_close=True) as streams:
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(
                read_stream, write_stream, message_handler=message_handler,
            ) as session:
                yield session


def _resource_uri(resources: Any) -> tuple[str, str]:
    values = getattr(resources, "resources", None)
    if values is None and isinstance(resources, Mapping):
        values = resources.get("resources")
    matches: list[tuple[str, str]] = []
    for resource in values or []:
        uri = str(getattr(resource, "uri", None) or (resource.get("uri") if isinstance(resource, Mapping) else ""))
        parsed = urlsplit(uri)
        recipient = parsed.path.removeprefix("/")
        if (parsed.scheme == "agentbus" and parsed.netloc == "inbox" and recipient
                and "/" not in recipient and not parsed.query and not parsed.fragment
                and not parsed.username and not parsed.password):
            matches.append((uri, recipient))
    if len(matches) != 1:
        raise WakeError("selected server must expose exactly one authenticated inbox resource")
    return matches[0]


def _notification_uri(message: Any) -> str | None:
    payload = getattr(message, "root", message)
    params = getattr(payload, "params", None)
    uri = getattr(params, "uri", None) if params is not None else None
    if uri is None and isinstance(payload, Mapping):
        params = payload.get("params")
        uri = params.get("uri") if isinstance(params, Mapping) else None
    return str(uri) if uri is not None else None


def _unavailable_notification(message: Any) -> bool:
    payload = getattr(message, "root", message)
    params = getattr(payload, "params", None)
    data = getattr(params, "data", None) if params is not None else None
    if data is None and isinstance(payload, Mapping):
        params = payload.get("params")
        data = params.get("data") if isinstance(params, Mapping) else None
    return "resource_subscriptions_unavailable" in str(data or "")


def _json_result(result: Any) -> dict[str, Any]:
    if bool(getattr(result, "isError", False)):
        raise WakeError("MCP server rejected an inbox request")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            value = json.loads(text)
            if isinstance(value, dict):
                return value
    if isinstance(result, dict):
        return result
    raise WakeError("MCP server returned an unreadable inbox response")


def _resource_page(result: Any) -> dict[str, Any]:
    contents = getattr(result, "contents", None)
    if contents is None and isinstance(result, Mapping):
        contents = result.get("contents")
    for item in contents or []:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, Mapping):
            text = item.get("text")
        if isinstance(text, str):
            value = json.loads(text)
            if isinstance(value, dict):
                return value
    raise WakeError("MCP inbox resource returned invalid JSON")


class WakeManager:
    """Own one event-driven MCP subscription and its durable dispatch queue."""

    def __init__(
        self,
        state: StateStore,
        *,
        config_loader: Callable[[], dict[str, dict[str, Any]]],
        connector: Callable[..., Any] = _default_connector,
        notifier: Callable[[str], None] | None = None,
        session_validator: Callable[[str], bool] | None = None,
    ):
        self.state = state
        self._config_loader = config_loader
        self._connector = connector
        self._notifier = notifier or self._broadcast
        self._session_validator = session_validator or self._stored_session_exists
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._claim_lock = asyncio.Lock()
        self._session: Any = None
        self._fatal = False
        self._error = ""
        self._server_name = state.setting("server_name")
        self._recipient = ""
        self._subscribed_uri: str | None = None
        self._connection_state = "stopped"
        self._last_event_at: str | None = None
        self._topic_presence_availability = "unknown"
        self._topic_presence_generations: dict[str, str] = {}
        self._topic_presence_session: Any = None
        self._topic_presence: dict[str, dict[str, Any]] = {}

    def validate_stored_session(self, stored_session_id: str) -> bool:
        return bool(self._session_validator(stored_session_id))

    @staticmethod
    def _stored_session_exists(stored_session_id: str) -> bool:
        try:
            from hermes_state import SessionDB
            db = SessionDB()
            try:
                return db.get_session(stored_session_id) is not None
            finally:
                db.close()
        except Exception:
            return False

    @staticmethod
    def _broadcast(dispatch_id: str) -> None:
        try:
            from hermes_cli.plugin_events import broadcast_plugin_event
            broadcast_plugin_event(_PLUGIN_ID, _EVENT_NAME, {"dispatch_id": dispatch_id})
        except Exception as exc:
            # The durable outbox remains available to a later desktop registration.
            logger.warning("Desktop inbox event broadcast failed (%s)", type(exc).__name__)

    def available_servers(self) -> list[dict[str, str]]:
        try:
            servers = self._config_loader()
        except Exception:
            return []
        return [
            {"name": name}
            for name, cfg in servers.items()
            if isinstance(name, str) and isinstance(cfg, dict)
            and isinstance(cfg.get("url"), str) and cfg.get("url", "").strip()
        ]

    async def start(self, server_name: str | None = None, *, wait_seconds: float = 10.0) -> dict[str, Any]:
        requested = server_name if server_name is not None else (self._server_name or self.state.setting("server_name"))
        if not requested or requested not in {row["name"] for row in self.available_servers()}:
            raise WakeError("choose a configured HTTP MCP server")
        if self._task and not self._task.done() and requested == self._server_name:
            self.state.set_enabled(True)
            return self.status()
        if self._task and not self._task.done():
            await self.stop(persist=False)
        self.state.set_setting("server_name", requested)
        self._server_name = requested
        self.state.set_enabled(True)
        self._stop.clear()
        self._connected.clear()
        self._fatal = False
        self._error = ""
        self._connection_state = "connecting"
        self._task = asyncio.create_task(self._connection_loop(), name="agent-bus-wake-subscription")
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=max(0.1, wait_seconds))
        except asyncio.TimeoutError:
            # The listener remains in transport-reconnect backoff; no polling or
            # legacy wait endpoint is substituted.
            self._error = "connection_pending"
        return self.status()

    async def stop(self, *, persist: bool = True) -> dict[str, Any]:
        if persist:
            self.state.set_enabled(False)
        self._stop.set()
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._mark_topic_presence_disconnected()
        self._session = None
        self._subscribed_uri = None
        self._connected.clear()
        self._connection_state = "stopped"
        return self.status()

    async def pause(self) -> dict[str, Any]:
        self.state.set_enabled(False)
        return await self.stop(persist=False)

    def status(self) -> dict[str, Any]:
        attached = bool(
            self._connection_state == "subscribed" and self._session is not None
            and self._subscribed_uri is not None
        )
        cursor = self.state.cursor(self._server_name, self._recipient) if self._server_name and self._recipient else None
        return {
            "enabled": self.state.enabled(), "server_name": self._server_name or None,
            "recipient": self._recipient or None, "state": self._connection_state,
            "cursor": cursor, "error": self._error or None,
            "last_event_at": self._last_event_at,
            "resource_subscription_attached": attached,
            "resource_uri": self._subscribed_uri if attached else None,
            "topic_presence_tools": self._topic_presence_availability,
            "dispatch_counts": self.state.counts(),
        }

    def topic_presence_status(self) -> dict[str, Any]:
        """Report per-binding registration state without inferring global liveness."""
        topics: dict[str, dict[str, Any]] = {}
        for binding in self.state.bindings(server_name=self._server_name or None):
            if binding["recipient"] == self._recipient:
                topics[binding["topic"]] = self._topic_presence_metadata(binding["topic"])
        return {"availability": self._topic_presence_availability, "topics": topics}

    def _topic_presence_metadata(self, topic: str) -> dict[str, Any]:
        value = dict(self._topic_presence.get(topic, {}))
        value.setdefault("state", "not_registered")
        value["supported"] = self._topic_presence_availability == "supported"
        generation = self._topic_presence_generations.get(topic)
        if generation is not None:
            value["generation"] = generation
        else:
            value.pop("generation", None)
        return value

    def binding_statuses(self) -> list[dict[str, Any]]:
        rows = self.state.bindings()
        status = self.status()
        for binding in rows:
            exact_uri = f"agentbus://inbox/{binding['recipient']}"
            attached = bool(
                status["resource_subscription_attached"]
                and binding["server_name"] == self._server_name
                and binding["recipient"] == self._recipient
                and self._subscribed_uri == exact_uri
            )
            binding["transport_attachment"] = {
                "kind": "recipient_inbox_resource",
                "topic": binding["topic"],
                "uri": exact_uri,
                "attached": attached,
            }
            if binding["server_name"] == self._server_name and binding["recipient"] == self._recipient:
                binding["topic_presence"] = self._topic_presence_metadata(binding["topic"])
            else:
                binding["topic_presence"] = {"state": "inactive_binding", "supported": None}
        return rows

    def _mark_topic_presence_disconnected(self) -> None:
        for topic in self._topic_presence_generations:
            previous = self._topic_presence.get(topic, {})
            self._topic_presence[topic] = {
                "state": "disconnected", "registered": True,
                "agent": previous.get("agent"), "registered_at": previous.get("registered_at"),
            }
        self._topic_presence_generations.clear()
        self._topic_presence_session = None

    def _set_bound_topic_presence(self, recipient: str, state: str, **extra: Any) -> None:
        for binding in self.state.bindings(server_name=self._server_name):
            if binding["recipient"] == recipient:
                self._topic_presence[binding["topic"]] = {"state": state, **extra}

    async def _discover_and_join_topics(self, session: Any, recipient: str) -> None:
        """Discover optional tools once, then register saved bindings on this native session."""
        self._topic_presence_generations.clear()
        self._topic_presence_session = None
        self._topic_presence_availability = "discovering"
        list_tools = getattr(session, "list_tools", None)
        if not callable(list_tools):
            self._topic_presence_availability = "unsupported"
            self._set_bound_topic_presence(recipient, "unsupported")
            return
        try:
            result = await list_tools()
            values = getattr(result, "tools", None)
            if values is None and isinstance(result, Mapping):
                values = result.get("tools")
            names = {
                str(getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, Mapping) else ""))
                for tool in values or []
            }
        except Exception as exc:
            self._topic_presence_availability = "discovery_failed"
            self._set_bound_topic_presence(recipient, "discovery_failed", error=type(exc).__name__)
            return
        required = {"topic_presence_join", "topic_presence_status", "topic_presence_leave"}
        if not required.issubset(names):
            self._topic_presence_availability = "unsupported"
            self._set_bound_topic_presence(recipient, "unsupported")
            return
        self._topic_presence_availability = "supported"
        self._topic_presence_session = session
        for binding in self.state.bindings(server_name=self._server_name):
            if binding["recipient"] == recipient:
                await self._join_topic(session, binding["topic"])

    async def _join_topic(self, session: Any, topic: str) -> dict[str, Any]:
        if self._topic_presence_availability != "supported" or self._topic_presence_session is not session:
            return self._topic_presence_metadata(topic)
        try:
            joined = _json_result(await session.call_tool("topic_presence_join", {"topic": topic}))
            generation = joined.get("generation")
            if (joined.get("topic") != topic or not isinstance(generation, str) or not generation
                    or not isinstance(joined.get("agent"), str)
                    or not isinstance(joined.get("registered_at"), str)):
                raise WakeError("server returned an invalid topic registration")
            self._topic_presence_generations[topic] = generation
            entry: dict[str, Any] = {
                "state": "joined", "registered": True,
                "agent": joined.get("agent"), "registered_at": joined.get("registered_at"),
            }
            try:
                server_status = _json_result(await session.call_tool("topic_presence_status", {"topic": topic}))
                if server_status.get("topic") != topic or not isinstance(server_status.get("members"), list):
                    raise WakeError("server returned invalid topic status")
                entry["server_status"] = server_status
            except Exception as exc:
                entry["server_status_error"] = type(exc).__name__
            self._topic_presence[topic] = entry
        except Exception as exc:
            self._topic_presence_generations.pop(topic, None)
            self._topic_presence[topic] = {"state": "error", "registered": False, "error": type(exc).__name__}
            logger.warning("Optional topic registration failed for a bound topic (%s)", type(exc).__name__)
        return self._topic_presence_metadata(topic)

    async def binding_added(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        """Synchronize a newly persisted binding when its owning native session is live."""
        if (self._session is not None and self._connection_state == "subscribed"
                and binding.get("server_name") == self._server_name
                and binding.get("recipient") == self._recipient):
            if self._topic_presence_availability == "supported":
                await self._join_topic(self._session, str(binding["topic"]))
            elif self._topic_presence_availability in {"unsupported", "discovery_failed"}:
                self._topic_presence[str(binding["topic"])] = {"state": self._topic_presence_availability}
        return self._topic_presence_metadata(str(binding["topic"]))

    async def binding_removed(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        """Leave only a generation returned by this manager's current native session."""
        topic = str(binding["topic"])
        generation = self._topic_presence_generations.get(topic)
        session = self._session
        if (generation is None or session is None or self._topic_presence_session is not session
                or binding.get("server_name") != self._server_name
                or binding.get("recipient") != self._recipient):
            return self._topic_presence_metadata(topic)
        result = _json_result(await session.call_tool(
            "topic_presence_leave", {"topic": topic, "generation": generation},
        ))
        if result.get("topic") != topic or not isinstance(result.get("removed"), bool):
            raise WakeError("server returned an invalid topic leave response")
        self._topic_presence_generations.pop(topic, None)
        self._topic_presence.pop(topic, None)
        return {"state": "left" if result["removed"] else "not_registered",
                "supported": True, "removed": result["removed"]}

    async def _connection_loop(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            if self._fatal:
                self._connection_state = "unavailable"
                return
            try:
                self._connection_state = "connecting"
                await self._connect_once()
                delay = 1.0
                if not self._stop.is_set():
                    self._connection_state = "reconnecting"
                    await self._wait_or_stop(delay)
                    delay = min(delay * 2, 60.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error = type(exc).__name__
                if self._fatal:
                    self._connection_state = "unavailable"
                    self._connected.set()
                    return
                self._connection_state = "reconnecting"
                logger.warning("Native inbox subscription disconnected (%s)", type(exc).__name__)
                await self._wait_or_stop(delay)
                delay = min(delay * 2, 60.0)

    async def _wait_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _connect_once(self) -> None:
        self._mark_topic_presence_disconnected()
        self._topic_presence_availability = "unknown"
        self._session = None
        self._subscribed_uri = None
        servers = self._config_loader()
        config = servers.get(self._server_name)
        if not isinstance(config, dict) or not config.get("url"):
            self._fatal = True
            self._error = "selected_server_unavailable"
            raise WakeError("selected MCP server is not configured")
        lost = asyncio.Event()
        dirty = asyncio.Event()
        fatal_event = asyncio.Event()
        expected_uri: str | None = None

        async def on_message(message: Any) -> None:
            nonlocal expected_uri
            if isinstance(message, Exception):
                lost.set()
                return
            if _unavailable_notification(message):
                fatal_event.set()
                lost.set()
                return
            uri = _notification_uri(message)
            if expected_uri is not None and uri == expected_uri:
                dirty.set()
                self._last_event_at = datetime.now(timezone.utc).isoformat()

        async with self._connector(config, on_message) as session:
            initialized = await session.initialize()
            capabilities = getattr(initialized, "capabilities", None)
            resources_cap = getattr(capabilities, "resources", None) if capabilities is not None else None
            if not bool(getattr(resources_cap, "subscribe", False)):
                self._fatal = True
                self._error = "native_resource_subscriptions_unavailable"
                raise WakeError("selected server does not advertise native resource subscriptions")
            resources = await session.list_resources()
            expected_uri, recipient = _resource_uri(resources)
            self._recipient = recipient
            await session.subscribe_resource(expected_uri)
            self._session = session
            self._subscribed_uri = expected_uri
            self._connection_state = "subscribed"
            self._error = ""
            await self._discover_and_join_topics(session, recipient)

            existing_cursor = self.state.cursor(self._server_name, recipient)
            if existing_cursor is None:
                await self._initialize_cursor(session, expected_uri, recipient)
            else:
                # Reconcile only beyond the durably persisted cursor, after the
                # subscription is live so concurrent commits are covered.
                dirty.set()
            self._connected.set()
            self._publish_pending()
            drain = asyncio.create_task(
                self._drain_loop(session, expected_uri, recipient, dirty),
                name="agent-bus-wake-resource-drain",
            )
            lost_task = asyncio.create_task(lost.wait())
            fatal_task = asyncio.create_task(fatal_event.wait())
            stop_task = asyncio.create_task(self._stop.wait())
            try:
                done, _ = await asyncio.wait(
                    {drain, lost_task, fatal_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if drain in done:
                    # A failed resource read must tear down and reconnect, not
                    # leave a silent dead consumer labelled subscribed forever.
                    drain.result()
                if fatal_task in done and fatal_event.is_set():
                    self._fatal = True
                    self._error = "resource_subscriptions_unavailable"
                    self._connection_state = "unavailable"
                elif lost_task in done and lost.is_set():
                    self._connection_state = "reconnecting"
            finally:
                drain.cancel()
                lost_task.cancel()
                fatal_task.cancel()
                stop_task.cancel()
                await asyncio.gather(drain, lost_task, fatal_task, stop_task, return_exceptions=True)
                self._mark_topic_presence_disconnected()
                self._session = None
                self._subscribed_uri = None
                if not self._fatal:
                    self._connected.clear()

    async def _initialize_cursor(self, session: Any, inbox_uri: str, recipient: str) -> None:
        """Take one zero-timeout head snapshot; pre-snapshot retained history is excluded."""
        response = _json_result(await session.call_tool(
            "wait_for_messages",
            {"since_now": True, "timeout": 0, "fields": "full", "limit": _MAX_PAGE_SIZE},
        ))
        messages = response.get("messages", [])
        cursor = response.get("next_cursor")
        if not isinstance(messages, list):
            raise WakeError("server returned an invalid initial inbox snapshot")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise WakeError("server returned an invalid initial inbox cursor")
        self.state.set_baseline(self._server_name, recipient, cursor)
        if messages:
            # The zero-timeout server snapshot contains only commits after its
            # head. Use bodies in memory for topic matching; record_page persists
            # only their ids and bounded routing metadata.
            self.state.record_page(
                self._server_name, recipient, expected_cursor=cursor,
                messages=messages, next_cursor=cursor,
            )
        # The connection setup publishes pending work once after initialization.
        # Any invalidation received during the one-shot snapshot remains latched
        # and will cause one durable catch-up after the drain task starts.

    async def _drain_loop(self, session: Any, inbox_uri: str, recipient: str, dirty: asyncio.Event) -> None:
        while not self._stop.is_set():
            await dirty.wait()
            dirty.clear()
            self._last_event_at = datetime.now(timezone.utc).isoformat()
            await self._drain_pages(session, inbox_uri, recipient)

    async def _drain_pages(self, session: Any, inbox_uri: str, recipient: str) -> None:
        saw_new = False
        while not self._stop.is_set():
            cursor = self.state.cursor(self._server_name, recipient)
            if cursor is None:
                raise WakeError("inbox cursor is not initialized")
            result = await session.read_resource(f"{inbox_uri}?after={cursor}&limit={_MAX_PAGE_SIZE}")
            page = _resource_page(result)
            messages = page.get("messages", [])
            next_cursor = page.get("next_cursor")
            if not isinstance(messages, list):
                raise WakeError("server returned an invalid inbox page")
            if not messages:
                break
            if isinstance(next_cursor, bool) or not isinstance(next_cursor, int) or next_cursor <= cursor:
                raise WakeError("server returned a non-advancing inbox cursor")
            new_ids = self.state.record_page(
                self._server_name, recipient, expected_cursor=cursor,
                messages=messages, next_cursor=next_cursor,
            )
            saw_new = saw_new or bool(new_ids)
        if saw_new:
            # Read every available page before emitting one invalidation per
            # binding; a message burst becomes one bounded chat batch, not N turns.
            self._publish_pending()

    def _publish_pending(self) -> None:
        for dispatch_id in self.state.pending_dispatch_ids():
            self._notifier(dispatch_id)

    async def claim_dispatch(self, dispatch_id: str) -> dict[str, Any]:
        async with self._claim_lock:
            if self._session is None or not self._connected.is_set():
                raise WakeError("native inbox subscription is not connected")
            dispatch = self.state.begin_claim(dispatch_id)
            if dispatch is None:
                raise WakeError("dispatch is no longer pending; inspect its status before retrying")
            claimed_messages: list[dict[str, Any]] = []
            try:
                for reference in dispatch["messages"]:
                    result = _json_result(await self._session.call_tool(
                        "claim_message", {"message_id": reference["id"]},
                    ))
                    body = result.get("body")
                    if (
                        result.get("status") != "claimed"
                        or result.get("id") != reference["id"]
                        or result.get("recipient") != dispatch["recipient"]
                        or str(result.get("message_type") or "").lower() not in {"task", "request", "reply", "info", "error"}
                        or result.get("requires_user_approval") is not False
                        or not isinstance(body, dict)
                        or body.get("topic") != dispatch["binding"]["topic"]
                    ):
                        raise WakeError("claimed message no longer matches its saved routing reference")
                    if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _MAX_BODY_BYTES:
                        raise WakeError("claimed message exceeds the delivery size cap")
                    result["seq"] = reference["seq"]
                    claimed_messages.append(result)
            except Exception as exc:
                self.state.set_dispatches_status(dispatch["dispatch_ids"], "uncertain", expected=("claiming",))
                raise WakeError("message claim failed; automatic retry is disabled") from exc
            claim_token = secrets.token_urlsafe(32)
            if not self.state.set_dispatches_status(
                dispatch["dispatch_ids"], "claimed", claim_token=claim_token, expected=("claiming",),
            ):
                raise WakeError("dispatch state changed during claim")
            dispatch["status"] = "claimed"
            dispatch["claim_token"] = claim_token
            dispatch["messages"] = claimed_messages
            return dispatch

    async def complete_dispatch(self, dispatch_id: str, claim_token: str) -> None:
        if not self.state.verify_claim(dispatch_id, claim_token):
            raise WakeError("dispatch claim is invalid or already completed")
        dispatch = self.state.dispatch_batch(dispatch_id)
        if dispatch is None or self._session is None:
            self.state.set_dispatch_status(dispatch_id, "uncertain", expected=("claimed",))
            raise WakeError("delivery outcome is uncertain; automatic retry is disabled")
        dispatch_ids = dispatch["dispatch_ids"]
        if not self.state.set_dispatches_status(dispatch_ids, "submitting", expected=("claimed",)):
            raise WakeError("dispatch is no longer in a claimable state")
        try:
            for message in dispatch["messages"]:
                result = _json_result(await self._session.call_tool(
                    "ack_message", {"message_id": message["id"]},
                ))
                if result.get("status") != "acknowledged":
                    raise WakeError("message acknowledgement was not confirmed")
        except Exception as exc:
            self.state.set_dispatches_status(dispatch_ids, "uncertain", expected=("submitting",))
            raise WakeError("prompt was accepted but bus acknowledgement is uncertain") from exc
        self.state.set_dispatches_status(dispatch_ids, "submitted", expected=("submitting",))
        self._publish_pending()

    def fail_dispatch(self, dispatch_id: str, claim_token: str) -> None:
        if not self.state.verify_claim(dispatch_id, claim_token):
            raise WakeError("dispatch claim is invalid or already completed")
        # We cannot know whether a timed-out prompt RPC reached the backend. Do
        # not automatically resubmit and risk duplicate turns.
        dispatch = self.state.dispatch_batch(dispatch_id)
        if dispatch:
            self.state.set_dispatches_status(dispatch["dispatch_ids"], "uncertain", expected=("claimed",))

    def pending(self) -> dict[str, Any]:
        return {"dispatch_ids": self.state.pending_dispatch_ids(), "counts": self.state.counts()}


def configured_mcp_servers() -> dict[str, dict[str, Any]]:
    """Read Hermes' already configured MCP servers without returning headers to API callers."""
    from tools.mcp_tool_config import _load_mcp_config
    return _load_mcp_config()
