from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator

from fastapi import UploadFile


class FileError(ValueError):
    """Base class for file service errors."""


class FilePathError(FileError):
    pass


class FileNotFound(FileError):
    pass


@dataclass(frozen=True)
class FileMetadata:
    path: str
    size: int
    sha256: str
    content_type: str
    created_at: str
    updated_at: str



def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class FileRepository:
    """Persist file metadata in SQLite and bytes below a dedicated root."""

    def __init__(self, db_path: str | Path, root: str | Path = "data/files"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise FilePathError("file root must be a directory")
        self._root = self.root.resolve()
        self._lock = threading.RLock()
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL CHECK (size >= 0),
                    sha256 TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_updated_at ON files (updated_at)")

    @staticmethod
    def _validate_path(value: str) -> tuple[str, ...]:
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise FilePathError("path must be a non-empty relative path")
        if ("\x00" in value or "\\" in value or value.startswith("/")
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise FilePathError("path must be a safe relative path")
        parts = tuple(value.split("/"))
        if any(part in {"", ".", ".."} for part in parts):
            raise FilePathError("path must be a safe relative path")
        # PurePosixPath also rejects any surprising normalization that would
        # make the path differ from the path recorded in SQLite.
        if str(PurePosixPath(*parts)) != value:
            raise FilePathError("path must be a safe relative path")
        return parts

    def _candidate(self, value: str, *, create_parents: bool = False) -> tuple[str, Path, Path]:
        parts = self._validate_path(value)
        current = self._root
        for part in parts[:-1]:
            next_path = current / part
            if next_path.is_symlink():
                raise FilePathError("path contains a symlink")
            if next_path.exists() and not next_path.is_dir():
                raise FilePathError("path parent is not a directory")
            if create_parents and not next_path.exists():
                next_path.mkdir()
            current = next_path
        candidate = current / parts[-1]
        if candidate.is_symlink():
            raise FilePathError("path points to a symlink")
        if candidate.exists() and not candidate.is_file():
            raise FilePathError("path does not name a regular file")
        # Resolve after checking every existing component. This catches a
        # symlink introduced at a component not present during validation and
        # protects against roots configured outside the current working dir.
        resolved = candidate.resolve(strict=False)
        if resolved != self._root and self._root not in resolved.parents:
            raise FilePathError("path escapes file root")
        return "/".join(parts), candidate, current

    @staticmethod
    def _from_row(row: sqlite3.Row) -> FileMetadata:
        return FileMetadata(
            path=row["path"], size=int(row["size"]), sha256=row["sha256"],
            content_type=row["content_type"], created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list(self, *, limit: int = 1000) -> list[FileMetadata]:
        limit = min(max(1, int(limit)), 1000)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM files ORDER BY path LIMIT ?", (limit,)
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, path: str) -> FileMetadata:
        normalized, _, _ = self._candidate(path)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM files WHERE path=?", (normalized,)).fetchone()
        if row is None:
            raise FileNotFound("file not found")
        return self._from_row(row)

    async def save_upload(self, path: str, upload: UploadFile) -> FileMetadata:
        normalized, candidate, parent = self._candidate(path, create_parents=True)
        # Re-check after creating parents; no existing component may be a link.
        normalized, candidate, parent = self._candidate(normalized)
        temp_name = f".upload-{uuid.uuid4().hex}.tmp"
        temp_path = parent / temp_name
        digest = hashlib.sha256()
        size = 0
        try:
            with temp_path.open("xb") as handle:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            content_type = upload.content_type or "application/octet-stream"
            if (len(content_type) > 255 or "\r" in content_type or "\n" in content_type):
                content_type = "application/octet-stream"
            timestamp = _now()
            backup_path: Path | None = None
            with self._lock, self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    # Never follow or overwrite a symlink. A regular existing
                    # file is moved aside so replacement remains rollback-safe.
                    if candidate.is_symlink():
                        raise FilePathError("path points to a symlink")
                    if candidate.exists():
                        if not candidate.is_file():
                            raise FilePathError("path does not name a regular file")
                        backup_path = parent / f".backup-{uuid.uuid4().hex}.tmp"
                        os.replace(candidate, backup_path)
                    os.replace(temp_path, candidate)
                    conn.execute(
                        """INSERT INTO files(path,size,sha256,content_type,created_at,updated_at)
                           VALUES(?,?,?,?,?,?)
                           ON CONFLICT(path) DO UPDATE SET size=excluded.size,
                             sha256=excluded.sha256, content_type=excluded.content_type,
                             updated_at=excluded.updated_at""",
                        (normalized, size, digest.hexdigest(), content_type, timestamp, timestamp),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    if candidate.exists() and not candidate.is_symlink():
                        candidate.unlink()
                    if backup_path is not None and backup_path.exists():
                        os.replace(backup_path, candidate)
                    raise
                finally:
                    if backup_path is not None and backup_path.exists():
                        backup_path.unlink()
            return self.get(normalized)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def open_for_read(self, path: str) -> tuple[FileMetadata, int]:
        metadata = self.get(path)
        _, candidate, _ = self._candidate(metadata.path)
        try:
            fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                os.close(fd)
                raise FileNotFound("file not found")
            return metadata, fd
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            raise FileNotFound("file not found") from exc

    @staticmethod
    def iter_fd(fd: int, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        with os.fdopen(fd, "rb") as handle:
            while chunk := handle.read(chunk_size):
                yield chunk

    def delete(self, path: str) -> FileMetadata:
        normalized, candidate, _ = self._candidate(path)
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM files WHERE path=?", (normalized,)).fetchone()
            if row is None:
                conn.rollback()
                raise FileNotFound("file not found")
            metadata = self._from_row(row)
            if candidate.is_symlink():
                conn.rollback()
                raise FilePathError("path points to a symlink")
            try:
                if candidate.exists():
                    candidate.unlink()
                conn.execute("DELETE FROM files WHERE path=?", (normalized,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return metadata


def file_dict(metadata: FileMetadata) -> dict[str, object]:
    return asdict(metadata)
