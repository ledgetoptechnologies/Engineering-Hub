from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STATUSES = {"pending", "claimed", "acknowledged", "rejected"}
AGENTS = {"hermes", "codex", "claude"}
PRIORITIES = {"low", "normal", "high", "urgent"}
MAX_CORRELATED_HOPS = 6


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Message:
    id: str
    sender: str
    recipient: str
    message_type: str
    priority: str
    body: Any
    status: str
    correlation_id: str | None
    in_reply_to: str | None
    hop_count: int
    requires_user_approval: bool
    created_at: str
    claimed_by: str | None = None
    claim_expires_at: str | None = None
    acknowledged_at: str | None = None
    rejected_at: str | None = None
    rejection_reason: str | None = None


class MessageError(ValueError):
    pass


class NotFound(MessageError):
    pass


class InvalidTransition(MessageError):
    pass


class MessageRepository:
    def __init__(self, path: str | Path, lease_seconds: int = 600):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = max(1, int(lease_seconds))
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            self._migrate_messages(conn)
            conn.executescript(Path(__file__).with_name("schema.sql").read_text())
            self._migrate_delivery_sequence(conn)

    @staticmethod
    def _migrate_delivery_sequence(conn: sqlite3.Connection) -> None:
        """Idempotently assign stable order without updating legacy message rows."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""INSERT INTO message_delivery_sequence (message_id)
                SELECT m.id FROM messages AS m
                WHERE NOT EXISTS (
                    SELECT 1 FROM message_delivery_sequence AS d WHERE d.message_id = m.id
                ) ORDER BY m.created_at, m.id""")
            head = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='message_delivery_sequence'"
            ).fetchone()
            head_value = int(head[0]) if head is not None else 0
            max_mapping = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM message_delivery_sequence"
            ).fetchone()[0]
            if head_value < max_mapping or head_value < 0:
                raise MessageError("delivery sequence high-water mark is inconsistent")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _migration_table_sql() -> str:
        return """CREATE TABLE messages__migration (
            id TEXT PRIMARY KEY,
            sender TEXT NOT NULL CHECK (sender IN ('hermes', 'codex', 'claude')),
            recipient TEXT NOT NULL CHECK (recipient IN ('hermes', 'codex', 'claude')),
            message_type TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
            body_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'claimed', 'acknowledged', 'rejected')),
            correlation_id TEXT,
            in_reply_to TEXT,
            hop_count INTEGER NOT NULL DEFAULT 0 CHECK (hop_count BETWEEN 0 AND 6),
            requires_user_approval INTEGER NOT NULL DEFAULT 0 CHECK (requires_user_approval IN (0, 1)),
            created_at TEXT NOT NULL,
            claimed_by TEXT,
            claim_expires_at TEXT,
            acknowledged_at TEXT,
            rejected_at TEXT,
            rejection_reason TEXT
        )"""

    @staticmethod
    def _allows_agent(table_sql: str, column: str, agent: str) -> bool:
        match = re.search(
            rf"CHECK\s*\(\s*{column}\s+IN\s*\(([^)]*)\)\s*\)",
            table_sql,
            re.IGNORECASE,
        )
        return bool(match and re.search(rf"'{re.escape(agent)}'", match.group(1), re.IGNORECASE))

    def _migrate_messages(self, conn: sqlite3.Connection) -> None:
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        if table is None:
            return
        table_sql = table["sql"] or ""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        needs_migration = (
            not self._allows_agent(table_sql, "sender", "claude")
            or not self._allows_agent(table_sql, "recipient", "claude")
            or not {"in_reply_to", "hop_count"}.issubset(columns)
        )
        if not needs_migration:
            return

        conn.execute("BEGIN IMMEDIATE")
        try:
            # Preserve user-created indexes and triggers as well as the built-in indexes.
            objects = conn.execute(
                "SELECT type, sql FROM sqlite_master "
                "WHERE tbl_name='messages' AND type IN ('index', 'trigger') AND sql IS NOT NULL"
            ).fetchall()
            old_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
            target_columns = (
                "id", "sender", "recipient", "message_type", "priority", "body_json", "status",
                "correlation_id", "in_reply_to", "hop_count", "requires_user_approval", "created_at",
                "claimed_by", "claim_expires_at", "acknowledged_at", "rejected_at", "rejection_reason",
            )
            select_expressions = [
                column if column in old_columns else ("NULL" if column == "in_reply_to" else "0")
                for column in target_columns
            ]
            conn.execute(self._migration_table_sql())
            conn.execute(
                f"INSERT INTO messages__migration ({','.join(target_columns)}) "
                f"SELECT {','.join(select_expressions)} FROM messages"
            )
            conn.execute("DROP TABLE messages")
            conn.execute("ALTER TABLE messages__migration RENAME TO messages")
            for obj in objects:
                conn.execute(obj["sql"])
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _validate_agent(agent: str) -> None:
        if agent not in AGENTS:
            raise MessageError("invalid agent")

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Message:
        return Message(
            id=row["id"], sender=row["sender"], recipient=row["recipient"],
            message_type=row["message_type"], priority=row["priority"],
            body=json.loads(row["body_json"]), status=row["status"],
            correlation_id=row["correlation_id"], in_reply_to=row["in_reply_to"],
            hop_count=row["hop_count"],
            requires_user_approval=bool(row["requires_user_approval"]),
            created_at=row["created_at"], claimed_by=row["claimed_by"],
            claim_expires_at=row["claim_expires_at"], acknowledged_at=row["acknowledged_at"],
            rejected_at=row["rejected_at"], rejection_reason=row["rejection_reason"],
        )

    def insert(self, *, sender: str, recipient: str, message_type: str, body: Any,
               priority: str = "normal", correlation_id: str | None = None,
               requires_user_approval: bool = False, in_reply_to: str | None = None) -> Message:
        self._validate_agent(sender); self._validate_agent(recipient)
        if sender == recipient: raise MessageError("sender and recipient must differ")
        if not message_type or priority not in PRIORITIES: raise MessageError("invalid message type or priority")
        message_id = str(uuid.uuid4()); created = iso(now_utc())
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                hop_count = 0
                if in_reply_to is not None:
                    parent = conn.execute(
                        "SELECT sender, recipient, hop_count FROM messages WHERE id=?", (in_reply_to,)
                    ).fetchone()
                    if parent is None:
                        raise MessageError("parent message not found")
                    if parent["recipient"] != sender:
                        raise MessageError("sender must match parent recipient")
                    if parent["sender"] != recipient:
                        raise MessageError("recipient must be the parent sender peer")
                    hop_count = parent["hop_count"] + 1
                    if hop_count > MAX_CORRELATED_HOPS:
                        raise MessageError(f"correlated message hop limit is {MAX_CORRELATED_HOPS}")
                conn.execute("""INSERT INTO messages
                    (id,sender,recipient,message_type,priority,body_json,correlation_id,in_reply_to,hop_count,
                     requires_user_approval,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (message_id, sender, recipient, message_type, priority,
                    json.dumps(body, separators=(",", ":"), ensure_ascii=False), correlation_id, in_reply_to,
                    hop_count, int(requires_user_approval), created))
                conn.execute("INSERT INTO message_delivery_sequence (message_id) VALUES (?)", (message_id,))
                message = self.get(message_id, conn=conn)
                conn.commit()
                return message
            except Exception:
                conn.rollback()
                raise

    def get(self, message_id: str, *, conn: sqlite3.Connection | None = None) -> Message:
        owned = conn is None
        connection = conn or self.connect()
        try:
            row = connection.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            if row is None: raise NotFound("message not found")
            return self._from_row(row)
        finally:
            if owned: connection.close()

    def list_for_recipient(self, recipient: str, *, status: str = "pending", limit: int = 50,
                           actionable_only: bool = False) -> list[Message]:
        self._validate_agent(recipient)
        if status not in STATUSES: raise MessageError("invalid status")
        limit = min(max(1, int(limit)), 100)
        with self.connect() as conn:
            sql = "SELECT * FROM messages WHERE recipient=? AND status=?"
            params: list[Any] = [recipient, status]
            if actionable_only:
                sql += " AND lower(message_type) IN ('task', 'request')"
            rows = conn.execute(sql + " ORDER BY created_at LIMIT ?", (*params, limit)).fetchall()
            return [self._from_row(row) for row in rows]

    @staticmethod
    def _delivery_head(conn: sqlite3.Connection) -> int:
        # Both scalar reads must share one statement snapshot. Two autocommit
        # SELECTs can straddle a concurrent message commit and falsely flag corruption.
        row = conn.execute("""SELECT
            (SELECT seq FROM sqlite_sequence WHERE name='message_delivery_sequence'),
            (SELECT COALESCE(MAX(seq), 0) FROM message_delivery_sequence)
        """).fetchone()
        try:
            head = int(row[0]) if row[0] is not None else 0
            max_mapping = int(row[1])
        except (TypeError, ValueError, OverflowError) as exc:
            raise MessageError("delivery sequence high-water mark is inconsistent") from exc
        if head < max_mapping or head < 0:
            raise MessageError("delivery sequence high-water mark is inconsistent")
        return head

    def delivery_head(self) -> int:
        with self.connect() as conn:
            return self._delivery_head(conn)

    def oldest_available_seq(self) -> int | None:
        with self.connect() as conn:
            row = conn.execute("""SELECT MIN(d.seq) FROM message_delivery_sequence AS d
                INNER JOIN messages AS m ON m.id=d.message_id""").fetchone()
            return int(row[0]) if row is not None and row[0] is not None else None

    def sequence_for(self, message_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT seq FROM message_delivery_sequence WHERE message_id=?", (message_id,)
            ).fetchone()
            if row is None:
                raise NotFound("message delivery sequence not found")
            return int(row[0])

    def list_after(self, recipient: str, *, after: int, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
        """Return retained recipient messages after a global exclusive delivery cursor."""
        self._validate_agent(recipient)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise MessageError("after must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise MessageError("limit must be between 1 and 100")
        with self.connect() as conn:
            # Validate against the durable global head, not recipient MAX(seq).
            # One read transaction keeps validation and paging on one snapshot.
            conn.execute("BEGIN")
            if after > self._delivery_head(conn):
                raise MessageError("after exceeds current delivery sequence head")
            rows = conn.execute("""SELECT m.*, d.seq FROM message_delivery_sequence AS d
                INNER JOIN messages AS m ON m.id=d.message_id
                WHERE m.recipient=? AND d.seq>? ORDER BY d.seq ASC LIMIT ?""",
                (recipient, after, limit)).fetchall()
            page = [{**message_dict(self._from_row(row)), "seq": int(row["seq"])} for row in rows]
            return page, int(rows[-1]["seq"]) if rows else after

    def claim(self, message_id: str, agent: str) -> Message:
        self._validate_agent(agent); expires = iso(now_utc() + timedelta(seconds=self.lease_seconds))
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None: conn.rollback(); raise NotFound("message not found")
            if row["recipient"] != agent: conn.rollback(); raise InvalidTransition("message belongs to another recipient")
            eligible = row["status"] == "pending" or (row["status"] == "claimed" and row["claim_expires_at"] and parse_time(row["claim_expires_at"]) <= now_utc())
            if not eligible: conn.rollback(); raise InvalidTransition("message is not claimable")
            conn.execute("UPDATE messages SET status='claimed',claimed_by=?,claim_expires_at=? WHERE id=?", (agent, expires, message_id))
            conn.commit(); return self.get(message_id, conn=conn)

    def acknowledge(self, message_id: str, agent: str) -> Message:
        return self._finish(message_id, agent, "acknowledged", None)

    def reject(self, message_id: str, agent: str, reason: str) -> Message:
        if not reason: raise MessageError("rejection reason is required")
        return self._finish(message_id, agent, "rejected", reason)

    def _finish(self, message_id: str, agent: str, status: str, reason: str | None) -> Message:
        self._validate_agent(agent); timestamp = iso(now_utc())
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None: conn.rollback(); raise NotFound("message not found")
            if row["recipient"] != agent or row["status"] != "claimed" or row["claimed_by"] != agent:
                conn.rollback(); raise InvalidTransition("message is not claimed by this agent")
            if status == "acknowledged":
                conn.execute("UPDATE messages SET status=?,acknowledged_at=?,claim_expires_at=NULL WHERE id=?", (status, timestamp, message_id))
            else:
                conn.execute("UPDATE messages SET status=?,rejected_at=?,rejection_reason=?,claim_expires_at=NULL WHERE id=?", (status, timestamp, reason, message_id))
            conn.commit(); return self.get(message_id, conn=conn)

    def cleanup(self, *, acknowledged_days: int = 30, rejected_days: int = 90) -> dict[str, int]:
        acknowledged_days = max(1, int(acknowledged_days)); rejected_days = max(1, int(rejected_days)); current = now_utc()
        a_cutoff = iso(current - timedelta(days=acknowledged_days)); r_cutoff = iso(current - timedelta(days=rejected_days))
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            a = conn.execute("DELETE FROM messages WHERE status='acknowledged' AND acknowledged_at < ?", (a_cutoff,)).rowcount
            r = conn.execute("DELETE FROM messages WHERE status='rejected' AND rejected_at < ?", (r_cutoff,)).rowcount
            conn.commit(); return {"acknowledged_deleted": a, "rejected_deleted": r}

    def expire_claims(self) -> int:
        with self._lock, self.connect() as conn:
            return conn.execute("UPDATE messages SET status='pending',claimed_by=NULL,claim_expires_at=NULL WHERE status='claimed' AND claim_expires_at <= ?", (iso(now_utc()),)).rowcount


def message_dict(message: Message) -> dict[str, Any]:
    return asdict(message)
