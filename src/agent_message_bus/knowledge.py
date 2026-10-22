from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from contextlib import closing, contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MAX_CONTENT_BYTES = 64 * 1024
MAX_TITLE_CHARS = 200
MAX_PATH_CHARS = 512
MAX_SEARCH_CHARS = 200
MAX_SEARCH_TERMS = 20
MAX_RESULTS = 100
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class KnowledgeError(ValueError):
    """Base class for shared-knowledge validation and persistence errors."""


class KnowledgeNotFound(KnowledgeError):
    pass


class KnowledgeProjectArchived(KnowledgeError):
    def __init__(self):
        super().__init__('project_archived')


class KnowledgeConflict(KnowledgeError):
    def __init__(self, current_version: int):
        self.current_version = current_version
        super().__init__(f"version conflict; current version is {current_version}")


class KnowledgeIntegrityError(KnowledgeError):
    pass


@dataclass(frozen=True)
class KnowledgeEntry:
    project: str
    path: str
    title: str
    content: str
    version: int
    sha256: str
    created_by: str
    updated_by: str
    created_at: str
    updated_at: str


class KnowledgeRepository:
    """Durable, shared project/path documents with compare-and-swap writes."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._admission_config_lock = threading.Lock()
        self._write_admission_factory = None
        self._write_admission_configured = False
        self._put_started = False
        self._initialize()

    def configure_write_admission(self, context_factory):
        """Bind trusted application admission once, before this instance's first put.

        The factory receives a validated project namespace and returns a context
        manager held through the knowledge commit/read-back. This is an
        application entrypoint boundary, not protection against arbitrary direct
        SQLite writers or other independently constructed repository instances.
        """
        if not callable(context_factory):
            raise ValueError('Knowledge write admission must be callable')
        with self._admission_config_lock:
            if self._write_admission_configured or self._put_started:
                raise ValueError('Knowledge write admission is already fixed')
            self._write_admission_factory = context_factory
            self._write_admission_configured = True

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 10000")
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def _connection(self):
        """Preserve SQLite transaction exit, then close this operation's handle."""
        with closing(self.connect()) as conn, conn:
            yield conn

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS knowledge (
                    project TEXT NOT NULL,
                    path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    sha256 TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project, path)
                )"""
            )

    @staticmethod
    def validate_project(project: str) -> str:
        if not isinstance(project, str) or not _PROJECT_RE.fullmatch(project):
            raise KnowledgeError("project must be a 1-64 character identifier using letters, digits, '.', '_' or '-'")
        return project

    @staticmethod
    def validate_path(path: str) -> str:
        if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
            raise KnowledgeError("path must be a non-empty relative path of at most 512 characters")
        if ("\x00" in path or "\\" in path or path.startswith("/")
                or any(ord(char) < 32 or ord(char) == 127 for char in path)):
            raise KnowledgeError("path must be a safe relative POSIX path")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise KnowledgeError("path must be a safe relative POSIX path")
        if str(PurePosixPath(*parts)) != path:
            raise KnowledgeError("path must be a safe relative POSIX path")
        try:
            path.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise KnowledgeError("path must be valid UTF-8") from exc
        return path

    @staticmethod
    def _validate_document(title: str, content: str, author: str, expected_version: int) -> None:
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARS:
            raise KnowledgeError("title must be 1-200 characters")
        if not isinstance(content, str) or "\x00" in content:
            raise KnowledgeError("content must be text without NUL characters")
        try:
            content_bytes = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise KnowledgeError("content must be valid UTF-8") from exc
        if len(content_bytes) > MAX_CONTENT_BYTES:
            raise KnowledgeError(f"content exceeds {MAX_CONTENT_BYTES} UTF-8 bytes")
        # Cloudflare human actors include a full SHA-256 identity binding.
        if not isinstance(author, str) or not author or len(author) > 128:
            raise KnowledgeError("author must be a non-empty identity")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise KnowledgeError("expected_version must be a non-negative integer")

    @staticmethod
    def _from_row(row: sqlite3.Row) -> KnowledgeEntry:
        entry = KnowledgeEntry(
            project=row["project"], path=row["path"], title=row["title"], content=row["content"],
            version=int(row["version"]), sha256=row["sha256"], created_by=row["created_by"],
            updated_by=row["updated_by"], created_at=row["created_at"], updated_at=row["updated_at"],
        )
        digest = hashlib.sha256(entry.content.encode("utf-8")).hexdigest()
        if digest != entry.sha256:
            raise KnowledgeIntegrityError("stored knowledge content failed SHA-256 verification")
        return entry

    def put(self, *, project: str, path: str, title: str, content: str,
            author: str, expected_version: int) -> KnowledgeEntry:
        with self._admission_config_lock:
            self._put_started = True
            admission_factory = self._write_admission_factory
        project = self.validate_project(project)
        path = self.validate_path(path)
        self._validate_document(title, content, author, expected_version)
        timestamp = _now()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Admission must precede both this instance's lock and the bus writer
        # transaction: a composed Hub guard may hold its own writer gate.
        admission = nullcontext() if admission_factory is None else admission_factory(project)
        with admission, self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT version, created_by, created_at FROM knowledge WHERE project=? AND path=?",
                (project, path),
            ).fetchone()
            current_version = int(row["version"]) if row is not None else 0
            if current_version != expected_version:
                conn.rollback()
                raise KnowledgeConflict(current_version)
            version = current_version + 1
            created_by = row["created_by"] if row is not None else author
            created_at = row["created_at"] if row is not None else timestamp
            conn.execute(
                """INSERT INTO knowledge
                       (project,path,title,content,version,sha256,created_by,updated_by,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(project,path) DO UPDATE SET title=excluded.title, content=excluded.content,
                     version=excluded.version, sha256=excluded.sha256, updated_by=excluded.updated_by,
                     updated_at=excluded.updated_at""",
                (project, path, title, content, version, digest, created_by, author, created_at, timestamp),
            )
            conn.commit()
            return self.get(project, path)

    def get(self, project: str, path: str) -> KnowledgeEntry:
        project = self.validate_project(project)
        path = self.validate_path(path)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge WHERE project=? AND path=?", (project, path)
            ).fetchone()
        if row is None:
            raise KnowledgeNotFound("knowledge document not found")
        return self._from_row(row)

    def list(self, *, project: str, path_prefix: str | None = None, limit: int = 50) -> list[KnowledgeEntry]:
        project = self.validate_project(project)
        if path_prefix is not None:
            path_prefix = self.validate_path(path_prefix)
        limit = min(max(1, int(limit)), MAX_RESULTS)
        sql = "SELECT * FROM knowledge WHERE project=?"
        params: list[object] = [project]
        if path_prefix is not None:
            sql += " AND (path=? OR substr(path,1,?)=?)"
            params.extend((path_prefix, len(path_prefix) + 1, path_prefix + "/"))
        sql += " ORDER BY path LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]

    def search(self, *, query: str, project: str | None = None, limit: int = 20) -> list[KnowledgeEntry]:
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_SEARCH_CHARS:
            raise KnowledgeError("query must be 1-200 non-whitespace characters")
        terms = query.strip().split()
        if len(terms) > MAX_SEARCH_TERMS:
            raise KnowledgeError(f"query must contain at most {MAX_SEARCH_TERMS} terms")
        if project is not None:
            project = self.validate_project(project)
        limit = min(max(1, int(limit)), MAX_RESULTS)
        # Escape LIKE metacharacters; every term must occur in title, path or content.
        predicates = []
        params: list[object] = []
        for term in terms:
            literal = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            predicates.append("(title LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')")
            params.extend((f"%{literal}%", f"%{literal}%", f"%{literal}%"))
        sql = "SELECT * FROM knowledge WHERE " + " AND ".join(predicates)
        if project is not None:
            sql += " AND project=?"
            params.append(project)
        sql += " ORDER BY project, path LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]


def entry_dict(entry: KnowledgeEntry, *, include_content: bool = True) -> dict[str, object]:
    value = asdict(entry)
    if not include_content:
        value.pop("content")
    return value


def search_result_dict(entry: KnowledgeEntry, query: str) -> dict[str, object]:
    text = f"{entry.title}\n{entry.path}\n{entry.content}"
    first_term = query.strip().split()[0]
    pos = text.casefold().find(first_term.casefold())
    if pos < 0:
        snippet = text[:240]
    else:
        start = max(0, pos - 80)
        snippet = text[start:pos + len(query) + 160]
        if start:
            snippet = "…" + snippet
        if start + len(snippet) < len(text):
            snippet += "…"
    return {
        "project": entry.project,
        "path": entry.path,
        "title": entry.title,
        "version": entry.version,
        "sha256": entry.sha256,
        "created_by": entry.created_by,
        "updated_by": entry.updated_by,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "snippet": snippet[:320],
    }
