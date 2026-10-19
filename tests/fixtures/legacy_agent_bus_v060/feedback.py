from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

AGENTS = frozenset({"hermes", "codex", "claude"})
FEEDBACK_KINDS = frozenset({"bug", "improvement", "info"})
FEEDBACK_SEVERITIES = frozenset({"low", "normal", "high", "urgent"})
FEEDBACK_STATUSES = frozenset({"open", "triaged", "planned", "resolved", "dismissed"})
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 16_000
MAX_NOTE_LENGTH = 16_000
MAX_PROJECT_LENGTH = 100
MAX_CLIENT_REQUEST_ID_LENGTH = 200
MAX_LIST_LIMIT = 100
_PROJECT_SLUG = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?\Z")
_CURSOR_PREFIX = "fb1_"


class FeedbackError(Exception):
    """Base error with stable, non-sensitive fields for HTTP/MCP adapters."""

    code = "feedback_error"
    status_code = 500
    safe_message = "feedback operation failed"

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class FeedbackValidationError(FeedbackError):
    code = "invalid_feedback"
    status_code = 400
    safe_message = "feedback input is invalid"


class FeedbackNotFound(FeedbackError):
    code = "feedback_not_found"
    status_code = 404
    safe_message = "feedback item was not found"


class FeedbackForbidden(FeedbackError):
    code = "feedback_forbidden"
    status_code = 403
    safe_message = "feedback item cannot be updated by this actor"


class FeedbackConflict(FeedbackError):
    code = "feedback_conflict"
    status_code = 409
    safe_message = "feedback request conflicts with current state"


class FeedbackStorageError(FeedbackError):
    code = "feedback_storage_error"
    status_code = 500
    safe_message = "feedback storage operation failed"


@dataclass(frozen=True)
class Feedback:
    id: str
    author: str
    kind: str
    title: str
    description: str
    project: str
    severity: str
    status: str
    revision: int
    created_at: str
    updated_at: str

    def metadata(self) -> dict[str, Any]:
        """Return safe operational metadata, excluding report text."""
        return feedback_metadata(self)


@dataclass(frozen=True)
class FeedbackRevision:
    feedback_id: str
    revision: int
    actor: str
    status: str
    note: str
    created_at: str

    def metadata(self) -> dict[str, Any]:
        """Return revision metadata without the free-form evidence note."""
        return feedback_revision_metadata(self)


@dataclass(frozen=True)
class FeedbackPage:
    items: tuple[Feedback, ...]
    next_cursor: str | None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_text(value: object, max_length: int, *, allow_empty: bool) -> bool:
    if not isinstance(value, str) or len(value) > max_length:
        return False
    if not allow_empty and not value:
        return False
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _validate_agent(actor: object) -> str:
    if not isinstance(actor, str) or actor not in AGENTS:
        raise FeedbackValidationError()
    return actor


def _validate_enum(value: object, choices: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise FeedbackValidationError()
    return value


def _validate_id(feedback_id: object) -> str:
    if not isinstance(feedback_id, str) or not _valid_text(feedback_id, 200, allow_empty=False):
        raise FeedbackValidationError()
    return feedback_id


def _validate_project(project: object) -> str:
    if (
        not isinstance(project, str)
        or len(project) > MAX_PROJECT_LENGTH
        or _PROJECT_SLUG.fullmatch(project) is None
    ):
        raise FeedbackValidationError()
    return project


def _validate_limit(limit: object) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise FeedbackValidationError()
    return limit


def _canonical_json(payload: dict[str, str]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _cursor_for(feedback_id: str) -> str:
    encoded = base64.urlsafe_b64encode(feedback_id.encode("utf-8")).decode("ascii").rstrip("=")
    return _CURSOR_PREFIX + encoded


def _decode_cursor(cursor: str) -> str:
    encoded = cursor[len(_CURSOR_PREFIX):]
    if not encoded:
        raise FeedbackValidationError()
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        feedback_id = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise FeedbackValidationError() from None
    if _cursor_for(feedback_id) != cursor:
        raise FeedbackValidationError()
    return feedback_id


def feedback_dict(feedback: Feedback) -> dict[str, Any]:
    """Return the full report for trusted service-layer response construction."""
    return asdict(feedback)


def feedback_metadata(feedback: Feedback) -> dict[str, Any]:
    """Return fields safe for operational/audit metadata; excludes report text."""
    return {
        "id": feedback.id,
        "author": feedback.author,
        "kind": feedback.kind,
        "project": feedback.project,
        "severity": feedback.severity,
        "status": feedback.status,
        "revision": feedback.revision,
        "created_at": feedback.created_at,
        "updated_at": feedback.updated_at,
    }


def feedback_revision_dict(revision: FeedbackRevision) -> dict[str, Any]:
    """Return a full evidence revision; note is user-supplied free-form text."""
    return asdict(revision)


def feedback_revision_metadata(revision: FeedbackRevision) -> dict[str, Any]:
    """Return safe revision metadata without the free-form evidence note."""
    return {
        "feedback_id": revision.feedback_id,
        "revision": revision.revision,
        "actor": revision.actor,
        "status": revision.status,
        "created_at": revision.created_at,
    }


def feedback_page_dict(page: FeedbackPage, *, include_text: bool = False) -> dict[str, Any]:
    """Serialize a list page; metadata-only output is the safe default."""
    converter = feedback_dict if include_text else feedback_metadata
    return {
        "items": [converter(item) for item in page.items],
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


class FeedbackRepository:
    """Durable shared feedback inbox backed by the messaging SQLite database."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            conn = self.connect()
        except sqlite3.Error:
            raise FeedbackStorageError() from None
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        """Apply the tracked schema under a short cross-process writer lock."""
        schema_path = Path(__file__).with_name("feedback_schema.sql")
        try:
            schema = schema_path.read_text(encoding="utf-8")
        except OSError:
            raise FeedbackStorageError() from None
        with self._lock, self._connection() as conn:
            try:
                # executescript commits a pending transaction before running; put
                # BEGIN in the script so DDL itself remains one serialized unit.
                conn.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.rollback()
                raise FeedbackStorageError() from None

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Feedback:
        return Feedback(
            id=row["id"],
            author=row["author"],
            kind=row["kind"],
            title=row["title"],
            description=row["description"],
            project=row["project"],
            severity=row["severity"],
            status=row["status"],
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _revision_from_row(row: sqlite3.Row) -> FeedbackRevision:
        return FeedbackRevision(
            feedback_id=row["feedback_id"],
            revision=row["revision"],
            actor=row["actor"],
            status=row["status"],
            note=row["note"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _get_row(conn: sqlite3.Connection, feedback_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT id,author,kind,title,description,project,severity,status,revision,created_at,updated_at "
            "FROM feedback_entries WHERE id=?",
            (feedback_id,),
        ).fetchone()
        if row is None:
            raise FeedbackNotFound()
        return row

    def submit(
        self,
        actor: str,
        kind: str,
        title: str,
        description: str,
        project: str = "agent-bus",
        severity: str = "normal",
        client_request_id: str | None = None,
    ) -> Feedback:
        author = _validate_agent(actor)
        validated_kind = _validate_enum(kind, FEEDBACK_KINDS)
        if not _valid_text(title, MAX_TITLE_LENGTH, allow_empty=False):
            raise FeedbackValidationError()
        normalized_title = title.strip()
        if not normalized_title:
            raise FeedbackValidationError()
        if len(normalized_title) > MAX_TITLE_LENGTH:
            raise FeedbackValidationError()
        if not _valid_text(description, MAX_DESCRIPTION_LENGTH, allow_empty=True):
            raise FeedbackValidationError()
        validated_project = _validate_project(project)
        validated_severity = _validate_enum(severity, FEEDBACK_SEVERITIES)
        if client_request_id is not None:
            if not _valid_text(client_request_id, MAX_CLIENT_REQUEST_ID_LENGTH, allow_empty=False):
                raise FeedbackValidationError()
            client_request_id = client_request_id.strip()
            if not client_request_id:
                raise FeedbackValidationError()

        payload = {
            "author": author,
            "kind": validated_kind,
            "title": normalized_title,
            "description": description,
            "project": validated_project,
            "severity": validated_severity,
        }
        payload_json = _canonical_json(payload) if client_request_id is not None else None
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest() if payload_json else None
        feedback_id = str(uuid.uuid4())

        with self._lock, self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                created_at = _now()
                if client_request_id is not None:
                    existing = conn.execute(
                        "SELECT id,payload_json,payload_hash FROM feedback_entries "
                        "WHERE author=? AND client_request_id=?",
                        (author, client_request_id),
                    ).fetchone()
                    if existing is not None:
                        if existing["payload_hash"] != payload_hash or existing["payload_json"] != payload_json:
                            raise FeedbackConflict()
                        row = self._get_row(conn, existing["id"])
                        result = self._from_row(row)
                        conn.commit()
                        return result

                conn.execute(
                    "INSERT INTO feedback_entries "
                    "(id,author,kind,title,description,project,severity,status,revision,created_at,updated_at,"
                    "client_request_id,payload_json,payload_hash) "
                    "VALUES (?,?,?,?,?,?,?,'open',1,?,?,?,?,?)",
                    (
                        feedback_id,
                        author,
                        validated_kind,
                        normalized_title,
                        description,
                        validated_project,
                        validated_severity,
                        created_at,
                        created_at,
                        client_request_id,
                        payload_json,
                        payload_hash,
                    ),
                )
                conn.execute(
                    "INSERT INTO feedback_history (feedback_id,revision,actor,status,note,created_at) "
                    "VALUES (?,1,?,'open','',?)",
                    (feedback_id, author, created_at),
                )
                result = self._from_row(self._get_row(conn, feedback_id))
                conn.commit()
                return result
            except FeedbackError:
                if conn.in_transaction:
                    conn.rollback()
                raise
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.rollback()
                raise FeedbackStorageError() from None

    def get(self, feedback_id: str) -> Feedback:
        validated_id = _validate_id(feedback_id)
        with self._connection() as conn:
            try:
                return self._from_row(self._get_row(conn, validated_id))
            except FeedbackError:
                raise
            except sqlite3.Error:
                raise FeedbackStorageError() from None

    def list(
        self,
        filters: Mapping[str, Any] | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> FeedbackPage:
        validated_limit = _validate_limit(limit)
        allowed_filters = {"author", "kind", "project", "severity", "status"}
        normalized_filters: dict[str, str] = {}
        if filters is not None:
            if not isinstance(filters, Mapping) or not set(filters).issubset(allowed_filters):
                raise FeedbackValidationError()
            for key, value in filters.items():
                if key == "author":
                    normalized_filters[key] = _validate_agent(value)
                elif key == "kind":
                    normalized_filters[key] = _validate_enum(value, FEEDBACK_KINDS)
                elif key == "project":
                    normalized_filters[key] = _validate_project(value)
                elif key == "severity":
                    normalized_filters[key] = _validate_enum(value, FEEDBACK_SEVERITIES)
                else:
                    normalized_filters[key] = _validate_enum(value, FEEDBACK_STATUSES)

        cursor_id: str | None = None
        if after is not None:
            if not _valid_text(after, 500, allow_empty=False):
                raise FeedbackValidationError()
            cursor_id = _decode_cursor(after) if after.startswith(_CURSOR_PREFIX) else after

        with self._connection() as conn:
            try:
                cursor_row: sqlite3.Row | None = None
                if cursor_id is not None:
                    cursor_row = conn.execute(
                        "SELECT id,created_at FROM feedback_entries WHERE id=?", (cursor_id,)
                    ).fetchone()
                    if cursor_row is None:
                        raise FeedbackValidationError()

                clauses: list[str] = []
                params: list[Any] = []
                for key in sorted(normalized_filters):
                    clauses.append(f"{key}=?")
                    params.append(normalized_filters[key])
                if cursor_row is not None:
                    clauses.append("(created_at>? OR (created_at=? AND id>?))")
                    params.extend((cursor_row["created_at"], cursor_row["created_at"], cursor_row["id"]))
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                rows = conn.execute(
                    "SELECT id,author,kind,title,description,project,severity,status,revision,created_at,updated_at "
                    "FROM feedback_entries" + where + " ORDER BY created_at,id LIMIT ?",
                    (*params, validated_limit + 1),
                ).fetchall()
                has_more = len(rows) > validated_limit
                selected = rows[:validated_limit]
                items = tuple(self._from_row(row) for row in selected)
                next_cursor = _cursor_for(items[-1].id) if has_more and items else None
                return FeedbackPage(items=items, next_cursor=next_cursor)
            except FeedbackError:
                raise
            except sqlite3.Error:
                raise FeedbackStorageError() from None

    def update(
        self,
        feedback_id: str,
        actor: str,
        expected_revision: int,
        status: str,
        note: str = "",
    ) -> Feedback:
        validated_id = _validate_id(feedback_id)
        updater = _validate_agent(actor)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision <= 0:
            raise FeedbackValidationError()
        validated_status = _validate_enum(status, FEEDBACK_STATUSES)
        if not _valid_text(note, MAX_NOTE_LENGTH, allow_empty=True):
            raise FeedbackValidationError()

        with self._lock, self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = self._get_row(conn, validated_id)
                if updater != "hermes" and updater != current["author"]:
                    raise FeedbackForbidden()
                if current["revision"] != expected_revision:
                    raise FeedbackConflict()
                next_revision = expected_revision + 1
                updated_at = _now()
                changed = conn.execute(
                    "UPDATE feedback_entries SET status=?,revision=?,updated_at=? "
                    "WHERE id=? AND revision=?",
                    (validated_status, next_revision, updated_at, validated_id, expected_revision),
                ).rowcount
                if changed != 1:
                    raise FeedbackConflict()
                conn.execute(
                    "INSERT INTO feedback_history (feedback_id,revision,actor,status,note,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (validated_id, next_revision, updater, validated_status, note, updated_at),
                )
                result = self._from_row(self._get_row(conn, validated_id))
                conn.commit()
                return result
            except FeedbackError:
                if conn.in_transaction:
                    conn.rollback()
                raise
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.rollback()
                raise FeedbackStorageError() from None

    def get_with_history(self, feedback_id: str, *, after_revision: int = 0,
                         limit: int = 20) -> tuple[Feedback, list[FeedbackRevision]]:
        """Read report and history from one SQLite snapshot, including under concurrent updates."""
        validated_id = _validate_id(feedback_id)
        _validate_limit(limit)
        if isinstance(after_revision, bool) or not isinstance(after_revision, int) or after_revision < 0:
            raise FeedbackValidationError()
        with self._connection() as conn:
            try:
                conn.execute("BEGIN")
                report = self._from_row(self._get_row(conn, validated_id))
                rows = conn.execute(
                    "SELECT feedback_id,revision,actor,status,note,created_at FROM feedback_history "
                    "WHERE feedback_id=? AND revision>? ORDER BY revision LIMIT ?",
                    (validated_id, after_revision, limit),
                ).fetchall()
                result = (report, [self._revision_from_row(row) for row in rows])
                conn.commit()
                return result
            except FeedbackError:
                raise
            except sqlite3.Error:
                raise FeedbackStorageError() from None

    def history(self, feedback_id: str, *, after_revision: int = 0,
                limit: int = 100) -> list[FeedbackRevision]:
        validated_id = _validate_id(feedback_id)
        _validate_limit(limit)
        if isinstance(after_revision, bool) or not isinstance(after_revision, int) or after_revision < 0:
            raise FeedbackValidationError()
        with self._connection() as conn:
            try:
                self._get_row(conn, validated_id)
                rows = conn.execute(
                    "SELECT feedback_id,revision,actor,status,note,created_at FROM feedback_history "
                    "WHERE feedback_id=? AND revision>? ORDER BY revision LIMIT ?",
                    (validated_id, after_revision, limit),
                ).fetchall()
                return [self._revision_from_row(row) for row in rows]
            except FeedbackError:
                raise
            except sqlite3.Error:
                raise FeedbackStorageError() from None
