from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import sqlite3
import stat
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Iterator

from fastapi import UploadFile


class FileError(ValueError):
    """Base class for file service errors."""


class FilePathError(FileError):
    """The requested path is unsafe or conflicts with an incompatible file."""


class FileNotFound(FileError):
    """The requested active file or revision does not exist."""


class FileConflict(FileError):
    """A compare-and-swap write/delete observed a different version."""


class FileTooLarge(FileError):
    """A read or upload exceeds its configured byte limit."""


class FileIntegrityError(FileError):
    """Stored bytes do not match their recorded size or SHA-256 digest."""


@dataclass(frozen=True)
class FileMetadata:
    path: str
    size: int
    sha256: str
    content_type: str
    created_at: str
    updated_at: str
    version: int = 1
    created_by: str | None = None
    updated_by: str | None = None


@dataclass
class _MirrorChange:
    parent_fd: int
    leaf: str
    backup: str | None
    installed: bool

    def rollback(self) -> None:
        try:
            if self.installed:
                try:
                    os.unlink(self.leaf, dir_fd=self.parent_fd)
                except FileNotFoundError:
                    pass
            if self.backup is not None:
                os.rename(self.backup, self.leaf, src_dir_fd=self.parent_fd,
                          dst_dir_fd=self.parent_fd)
        finally:
            os.close(self.parent_fd)

    def finish(self) -> None:
        try:
            if self.backup is not None:
                try:
                    os.unlink(self.backup, dir_fd=self.parent_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(self.parent_fd)


@dataclass
class _RemovedMirror:
    parent_fd: int
    leaf: str
    backup: str

    def rollback(self) -> None:
        try:
            os.rename(self.backup, self.leaf, src_dir_fd=self.parent_fd,
                      dst_dir_fd=self.parent_fd)
        finally:
            os.close(self.parent_fd)

    def finish(self) -> None:
        try:
            try:
                os.unlink(self.backup, dir_fd=self.parent_fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(self.parent_fd)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_PROCESS_MUTATION_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_MUTATION_LOCKS_GUARD = threading.Lock()
_MUTATION_LOCK_DEPTH = threading.local()


@contextmanager
def _serialize_file_mutations(db_path: Path) -> Iterator[None]:
    """Serialize mirror-changing operations even if SQLite auto-rolls back.

    SQLite's writer lock normally protects the mirror transaction, but errors
    such as ``RAISE(ROLLBACK)`` release it before recovery. A per-database
    thread lock coordinates repository instances in this process, and an
    advisory lock file extends the same guarantee to other processes.
    """
    database = db_path.resolve()
    key = str(database)
    lock_path = database.with_name(database.name + ".mirror.lock")
    with _PROCESS_MUTATION_LOCKS_GUARD:
        process_lock = _PROCESS_MUTATION_LOCKS.setdefault(key, threading.RLock())

    with process_lock:
        depths = getattr(_MUTATION_LOCK_DEPTH, "by_database", None)
        if depths is None:
            depths = {}
            _MUTATION_LOCK_DEPTH.by_database = depths
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return

        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_path, flags, 0o600)
        locked = False
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise FileError("file mutation lock is not a regular file")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            locked = True
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
        finally:
            try:
                if locked:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


def _serialize_file_mutation(method):
    """Hold the per-database lock across a synchronous mirror mutation."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with _serialize_file_mutations(self.db_path):
            return method(self, *args, **kwargs)

    return wrapped


class FileRepository:
    """Versioned shared file storage backed by SQLite and immutable blobs.

    ``files`` rows point at immutable SHA-256 objects below
    ``root/.agent-message-bus/objects``. Legacy flat files remain readable and
    are kept as compatibility mirrors for existing REST consumers.
    """

    INTERNAL_DIR = ".agent-message-bus"
    OBJECT_DIR = "objects"
    DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024

    def __init__(
        self,
        db_path: str | Path,
        root: str | Path = "data/files",
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ):
        if (isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int)
                or max_file_bytes < 0 or max_file_bytes > self.DEFAULT_MAX_FILE_BYTES):
            raise ValueError(f"max_file_bytes must be between 0 and {self.DEFAULT_MAX_FILE_BYTES}")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise FilePathError("file root must be a directory")
        self._root = self.root.resolve()
        self.max_file_bytes = max_file_bytes
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _initialize(self) -> None:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
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
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(files)")}
            migrations = (
                ("version", "INTEGER NOT NULL DEFAULT 1"),
                ("created_by", "TEXT"),
                ("updated_by", "TEXT"),
                ("storage_key", "TEXT"),
            )
            for name, declaration in migrations:
                if name not in columns:
                    conn.execute(f"ALTER TABLE files ADD COLUMN {name} {declaration}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_updated_at ON files (updated_at)")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS file_revisions (
                    path TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    size INTEGER NOT NULL CHECK (size >= 0),
                    sha256 TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_by TEXT,
                    updated_by TEXT,
                    storage_key TEXT NOT NULL,
                    PRIMARY KEY (path, version)
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_file_revisions_path_version "
                         "ON file_revisions(path, version DESC)")
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        root_fd = self._open_root_fd()
        try:
            internal_fd = self._open_dir_at(root_fd, self.INTERNAL_DIR, create=True)
            try:
                objects_fd = self._open_dir_at(internal_fd, self.OBJECT_DIR, create=True)
                os.close(objects_fd)
            finally:
                os.close(internal_fd)
        finally:
            os.close(root_fd)

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
        if parts[0] == FileRepository.INTERNAL_DIR:
            raise FilePathError("path uses a reserved storage directory")
        if str(PurePosixPath(*parts)) != value:
            raise FilePathError("path must be a safe relative path")
        return parts

    @staticmethod
    def _validate_prefix(value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str) or value.startswith("/") or "\\" in value:
            raise FilePathError("prefix must be a safe relative path prefix")
        trailing_slash = value.endswith("/")
        path = value[:-1] if trailing_slash else value
        FileRepository._validate_path(path)
        return path + ("/" if trailing_slash else "")

    @staticmethod
    def _validate_author(author: str | None) -> str | None:
        if author is None:
            return None
        if (not isinstance(author, str) or not author or len(author) > 255
                or any(ord(char) < 32 or ord(char) == 127 for char in author)):
            raise FileError("author must be a non-empty authenticated identity")
        return author

    @staticmethod
    def _normalize_content_type(content_type: str | None) -> str:
        if (not isinstance(content_type, str) or not content_type or len(content_type) > 255
                or "\r" in content_type or "\n" in content_type):
            return "application/octet-stream"
        return content_type

    def _open_root_fd(self) -> int:
        try:
            return os.open(self._root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                           | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise FilePathError("file root is unavailable or unsafe") from exc

    @staticmethod
    def _open_dir_at(parent_fd: int, name: str, *, create: bool = False) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise FileNotFound("file not found") from exc
        except OSError as exc:
            raise FilePathError("path parent is not a safe directory") from exc

    def _open_parent_fd(self, parts: tuple[str, ...], *, create: bool = False) -> int:
        fd = self._open_root_fd()
        try:
            for part in parts[:-1]:
                next_fd = self._open_dir_at(fd, part, create=create)
                os.close(fd)
                fd = next_fd
            return fd
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _check_leaf_at(parent_fd: int, leaf: str, *, missing_ok: bool = True) -> os.stat_result | None:
        try:
            result = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise FileNotFound("file not found")
        if stat.S_ISLNK(result.st_mode):
            raise FilePathError("path points to a symlink")
        if not stat.S_ISREG(result.st_mode):
            raise FilePathError("path does not name a regular file")
        return result

    def _open_flat_fd(self, path: str, *, missing_ok: bool = False) -> int | None:
        parts = self._validate_path(path)
        parent_fd = self._open_parent_fd(parts)
        try:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
            except FileNotFoundError as exc:
                if missing_ok:
                    return None
                raise FileNotFound("file not found") from exc
            except OSError as exc:
                raise FilePathError("path does not name a regular file") from exc
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise FilePathError("path does not name a regular file")
            return fd
        finally:
            os.close(parent_fd)

    @staticmethod
    def _metadata_from_row(row: sqlite3.Row | dict[str, object]) -> FileMetadata:
        keys = row.keys()
        return FileMetadata(
            path=str(row["path"]), size=int(row["size"]), sha256=str(row["sha256"]),
            content_type=str(row["content_type"]), created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]), version=int(row["version"]),
            created_by=row["created_by"] if "created_by" in keys else None,
            updated_by=row["updated_by"] if "updated_by" in keys else None,
        )

    @staticmethod
    def _validate_expected_version(expected_version: int | None) -> None:
        if (expected_version is not None and (isinstance(expected_version, bool)
                or not isinstance(expected_version, int) or expected_version < 0)):
            raise FileError("expected_version must be a non-negative integer")

    @staticmethod
    def _validate_read_limit(max_bytes: int) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
            raise FileError("max_bytes must be a non-negative integer")

    def _open_object_fd(self, storage_key: str) -> int:
        if (not isinstance(storage_key, str) or len(storage_key) != 64
                or any(ch not in "0123456789abcdef" for ch in storage_key)):
            raise FileIntegrityError("invalid content-addressed storage key")
        root_fd = self._open_root_fd()
        try:
            internal_fd = self._open_dir_at(root_fd, self.INTERNAL_DIR)
        finally:
            os.close(root_fd)
        try:
            objects_fd = self._open_dir_at(internal_fd, self.OBJECT_DIR)
        finally:
            os.close(internal_fd)
        try:
            fd = os.open(storage_key, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0), dir_fd=objects_fd)
        except OSError as exc:
            os.close(objects_fd)
            if isinstance(exc, FileNotFoundError):
                raise FileNotFound("stored file bytes not found") from exc
            raise FilePathError("stored object is not a regular file") from exc
        os.close(objects_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise FileIntegrityError("stored object is not a regular file")
        return fd

    def _object_directory_fd(self, *, create: bool = False) -> int:
        root_fd = self._open_root_fd()
        try:
            internal_fd = self._open_dir_at(root_fd, self.INTERNAL_DIR, create=create)
        finally:
            os.close(root_fd)
        try:
            return self._open_dir_at(internal_fd, self.OBJECT_DIR, create=create)
        finally:
            os.close(internal_fd)

    @staticmethod
    def _write_all(fd: int, data: bytes | memoryview) -> None:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]

    @staticmethod
    def _read_verified_fd(fd: int, metadata: FileMetadata) -> bytes:
        digest = hashlib.sha256()
        count = 0
        chunks: list[bytes] = []
        before = os.fstat(fd)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                digest.update(chunk)
                chunks.append(chunk)
            after = os.fstat(fd)
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError as exc:
            raise FileIntegrityError("stored file could not be verified") from exc
        if (count != metadata.size or digest.hexdigest() != metadata.sha256
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise FileIntegrityError("stored file failed size or SHA-256 verification")
        return b"".join(chunks)

    @staticmethod
    def _verify_fd(fd: int, metadata: FileMetadata) -> None:
        digest = hashlib.sha256()
        count = 0
        try:
            before = os.fstat(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                digest.update(chunk)
            after = os.fstat(fd)
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError as exc:
            raise FileIntegrityError("stored file could not be verified") from exc
        if (count != metadata.size or digest.hexdigest() != metadata.sha256
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise FileIntegrityError("stored file failed size or SHA-256 verification")

    def _publish_object_temp(
        self,
        objects_fd: int,
        temporary: str,
        fd: int,
        *,
        size: int,
        digest: str,
        newly_created: set[str] | None = None,
    ) -> str:
        try:
            os.fchmod(fd, 0o444)
            os.fsync(fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_size != size:
                raise FileIntegrityError("staged content object has an unexpected size")
            try:
                os.link(temporary, digest, src_dir_fd=objects_fd, dst_dir_fd=objects_fd,
                        follow_symlinks=False)
                if newly_created is not None:
                    newly_created.add(digest)
                os.fsync(objects_fd)
            except FileExistsError:
                existing = os.open(digest, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                   | getattr(os, "O_NONBLOCK", 0), dir_fd=objects_fd)
                try:
                    if not stat.S_ISREG(os.fstat(existing).st_mode):
                        raise FileIntegrityError("existing content object is not a regular file")
                    expected = FileMetadata("", size, digest, "", "", "")
                    self._verify_fd(existing, expected)
                finally:
                    os.close(existing)
            return digest
        except OSError as exc:
            raise FileError("could not store immutable file bytes") from exc

    def _put_object(self, data: bytes, *, newly_created: set[str] | None = None) -> str:
        digest = hashlib.sha256(data).hexdigest()
        objects_fd = self._object_directory_fd(create=True)
        temporary = f".tmp-{secrets.token_hex(16)}"
        fd: int | None = None
        try:
            fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=objects_fd)
            self._write_all(fd, data)
            return self._publish_object_temp(
                objects_fd, temporary, fd, size=len(data), digest=digest,
                newly_created=newly_created,
            )
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=objects_fd)
            except FileNotFoundError:
                pass
            os.close(objects_fd)

    def _put_object_from_fd(
        self,
        source_fd: int,
        metadata: FileMetadata,
        *,
        newly_created: set[str] | None = None,
    ) -> str:
        objects_fd = self._object_directory_fd(create=True)
        temporary = f".tmp-{secrets.token_hex(16)}"
        staged_fd: int | None = None
        try:
            staged_fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL
                                | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=objects_fd)
            digest = hashlib.sha256()
            count = 0
            try:
                os.lseek(source_fd, 0, os.SEEK_SET)
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    digest.update(chunk)
                    self._write_all(staged_fd, chunk)
            except OSError as exc:
                raise FileIntegrityError("legacy file could not be copied for checkpoint") from exc
            actual_digest = digest.hexdigest()
            if count != metadata.size or actual_digest != metadata.sha256:
                raise FileIntegrityError("stored file failed size or SHA-256 verification")
            return self._publish_object_temp(
                objects_fd, temporary, staged_fd, size=count, digest=actual_digest,
                newly_created=newly_created,
            )
        finally:
            if staged_fd is not None:
                os.close(staged_fd)
            try:
                os.unlink(temporary, dir_fd=objects_fd)
            except FileNotFoundError:
                pass
            os.close(objects_fd)

    def _read_row_bytes(self, row: sqlite3.Row | dict[str, object], *, max_bytes: int | None = None) -> bytes:
        metadata = self._metadata_from_row(row)
        if max_bytes is None:
            max_bytes = 1024 * 1024
        if max_bytes is not None and metadata.size > max_bytes:
            raise FileTooLarge(f"file exceeds max_bytes ({max_bytes})")
        storage_key = row["storage_key"] if "storage_key" in row.keys() else None
        if storage_key:
            fd = self._open_object_fd(str(storage_key))
        else:
            fd = self._open_flat_fd(metadata.path)
            assert fd is not None
        try:
            return self._read_verified_fd(fd, metadata)
        finally:
            os.close(fd)

    def _checkpoint_legacy_object(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        path: str,
        expected_version: int | None,
        newly_created: set[str] | None = None,
    ) -> sqlite3.Row:
        """Durably point a legacy flat-file row at a verified immutable blob.

        This separate checkpoint ensures a crash during the first update or
        delete cannot leave the old metadata pointing only at a replaced flat
        file. It is a storage migration only: the user-visible version and
        provenance remain unchanged.
        """
        if row["storage_key"]:
            return row
        fd = self._open_flat_fd(path)
        assert fd is not None
        try:
            storage_key = self._put_object_from_fd(
                fd, self._metadata_from_row(row), newly_created=newly_created,
            )
        finally:
            os.close(fd)
        conn.execute(
            "UPDATE files SET storage_key=? WHERE path=? AND version=? AND storage_key IS NULL",
            (storage_key, path, int(row["version"])),
        )
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        if current is None:
            raise FileNotFound("file not found")
        actual_version = int(current["version"])
        if expected_version is not None and actual_version != expected_version:
            raise FileConflict(
                f"version conflict for {path}: expected {expected_version}, current {actual_version}"
            )
        return current

    def _record(self, path: str, version: int | None = None) -> sqlite3.Row | None:
        normalized = "/".join(self._validate_path(path))
        if version is not None:
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise FileError("version must be a positive integer")
        conn = self.connect()
        try:
            if version is None:
                return conn.execute("SELECT * FROM files WHERE path=?", (normalized,)).fetchone()
            row = conn.execute("SELECT * FROM files WHERE path=? AND version=?",
                               (normalized, version)).fetchone()
            if row is not None:
                return row
            return conn.execute("SELECT * FROM file_revisions WHERE path=? AND version=?",
                                (normalized, version)).fetchone()
        finally:
            conn.close()

    def get(self, path: str, version: int | None = None) -> FileMetadata:
        row = self._record(path, version)
        if row is None:
            raise FileNotFound("file not found")
        return self._metadata_from_row(row)

    def list(
        self,
        prefix: str | None = None,
        after: str | None = None,
        limit: int = 1000,
    ) -> list[FileMetadata]:
        prefix = self._validate_prefix(prefix)
        if after is not None:
            after = "/".join(self._validate_path(after))
            if prefix is not None and not after.startswith(prefix):
                raise FilePathError("after cursor must be inside the requested prefix")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise FileError("limit must be a positive integer")
        limit = min(limit, 1000)
        clauses: list[str] = []
        params: list[object] = []
        if prefix is not None:
            clauses.append("substr(path, 1, ?) = ?")
            params.extend((len(prefix), prefix))
        if after is not None:
            clauses.append("path > ?")
            params.append(after)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        conn = self.connect()
        try:
            rows = conn.execute(f"SELECT * FROM files{where} ORDER BY path LIMIT ?", params).fetchall()
        finally:
            conn.close()
        return [self._metadata_from_row(row) for row in rows]

    def history(self, path: str, limit: int = 100) -> list[FileMetadata]:
        normalized = "/".join(self._validate_path(path))
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise FileError("limit must be a positive integer")
        limit = min(limit, 1000)
        conn = self.connect()
        try:
            current = conn.execute("SELECT * FROM files WHERE path=?", (normalized,)).fetchone()
            revision_rows = conn.execute(
                "SELECT * FROM file_revisions WHERE path=? ORDER BY version DESC LIMIT ?",
                (normalized, max(0, limit - int(current is not None))),
            ).fetchall()
        finally:
            conn.close()
        rows = ([current] if current is not None else []) + revision_rows
        rows.sort(key=lambda row: int(row["version"]), reverse=True)
        return [self._metadata_from_row(row) for row in rows]

    def read_bytes(
        self,
        path: str,
        max_bytes: int = 1024 * 1024,
        version: int | None = None,
    ) -> tuple[FileMetadata, bytes]:
        self._validate_read_limit(max_bytes)
        row = self._record(path, version)
        if row is None:
            raise FileNotFound("file not found")
        metadata = self._metadata_from_row(row)
        data = self._read_row_bytes(row, max_bytes=max_bytes)
        return metadata, data

    def _cleanup_unreferenced_objects(self, conn: sqlite3.Connection, keys: set[str]) -> None:
        """Remove only blobs newly published by a rolled-back operation and unreferenced in DB."""
        if not keys or conn.in_transaction:
            return
        objects_fd: int | None = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            objects_fd = self._object_directory_fd()
            for key in keys:
                referenced = conn.execute(
                    """SELECT 1 FROM files WHERE storage_key=?
                       UNION ALL SELECT 1 FROM file_revisions WHERE storage_key=? LIMIT 1""",
                    (key, key),
                ).fetchone()
                if referenced is not None:
                    continue
                try:
                    os.unlink(key, dir_fd=objects_fd)
                except FileNotFoundError:
                    pass
            os.fsync(objects_fd)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
        finally:
            if objects_fd is not None:
                os.close(objects_fd)

    @staticmethod
    def _max_revision_version(conn: sqlite3.Connection, path: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM file_revisions WHERE path=?",
            (path,),
        ).fetchone()
        return int(row["version"])

    @_serialize_file_mutation
    def put_bytes(
        self,
        path: str,
        data: bytes,
        author: str,
        expected_version: int | None,
        content_type: str = "application/octet-stream",
    ) -> FileMetadata:
        normalized = "/".join(self._validate_path(path))
        self._validate_expected_version(expected_version)
        author = self._validate_author(author)
        if author is None:
            raise FileError("author is required")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise FileError("data must be bytes-like")
        data_size = data.nbytes if isinstance(data, memoryview) else len(data)
        if data_size > self.max_file_bytes:
            raise FileTooLarge(f"file exceeds max_file_bytes ({self.max_file_bytes})")
        content = bytes(data)
        content_type = self._normalize_content_type(content_type)
        digest = hashlib.sha256(content).hexdigest()
        timestamp = _now()
        conn = self.connect()
        mirror: _MirrorChange | None = None
        newly_created: set[str] = set()
        committed = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM files WHERE path=?", (normalized,)).fetchone()
            actual_version = int(row["version"]) if row is not None else 0
            if expected_version is not None and actual_version != expected_version:
                raise FileConflict(
                    f"version conflict for {normalized}: expected {expected_version}, current {actual_version}"
                )
            if row is not None and not row["storage_key"]:
                row = self._checkpoint_legacy_object(
                    conn, row, normalized, expected_version, newly_created,
                )
                actual_version = int(row["version"])
            if row is not None:
                conn.execute(
                    """INSERT OR IGNORE INTO file_revisions
                       (path,version,size,sha256,content_type,created_at,updated_at,
                        created_by,updated_by,storage_key) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (normalized, int(row["version"]), int(row["size"]), row["sha256"],
                     row["content_type"], row["created_at"], row["updated_at"],
                     row["created_by"], row["updated_by"], row["storage_key"]),
                )
            history_max = self._max_revision_version(conn, normalized)
            next_version = max(actual_version, history_max) + 1
            storage_key = self._put_object(content, newly_created=newly_created)
            mirror = self._install_mirror(normalized, content, allow_replace=row is not None)
            if row is None:
                conn.execute(
                    """INSERT INTO files(path,size,sha256,content_type,created_at,updated_at,
                                         version,created_by,updated_by,storage_key)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (normalized, len(content), digest, content_type, timestamp, timestamp,
                     next_version, author, author, storage_key),
                )
            else:
                conn.execute(
                    """UPDATE files SET size=?,sha256=?,content_type=?,updated_at=?,version=?,
                                         updated_by=?,storage_key=? WHERE path=?""",
                    (len(content), digest, content_type, timestamp, next_version,
                     author, storage_key, normalized),
                )
            result = conn.execute("SELECT * FROM files WHERE path=?", (normalized,)).fetchone()
            assert result is not None
            metadata = self._metadata_from_row(result)
            conn.commit()
            committed = True
            return metadata
        except BaseException:
            try:
                if mirror is not None:
                    failed_mirror, mirror = mirror, None
                    failed_mirror.rollback()
            finally:
                try:
                    if conn.in_transaction:
                        conn.rollback()
                finally:
                    self._cleanup_unreferenced_objects(conn, newly_created)
            raise
        finally:
            conn.close()
            if mirror is not None and committed:
                mirror.finish()

    async def save_upload(
        self,
        path: str,
        upload: UploadFile,
        *,
        author: str | None = None,
        expected_version: int | None = None,
    ) -> FileMetadata:
        normalized = "/".join(self._validate_path(path))
        self._validate_expected_version(expected_version)
        validated_author = self._validate_author("unknown" if author is None else author)
        if validated_author is None:
            raise FileError("author is required")
        content_type = self._normalize_content_type(upload.content_type or "application/octet-stream")
        objects_fd = self._object_directory_fd(create=True)
        temporary = f".tmp-{secrets.token_hex(16)}"
        staged_fd: int | None = None
        conn: sqlite3.Connection | None = None
        mirror: _MirrorChange | None = None
        newly_created: set[str] = set()
        committed = False
        mutation_lock = ExitStack()
        try:
            staged_fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL
                                | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=objects_fd)
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise FileError("upload.read must return bytes")
                if size + len(chunk) > self.max_file_bytes:
                    raise FileTooLarge(f"upload exceeds max_file_bytes ({self.max_file_bytes})")
                digest.update(chunk)
                size += len(chunk)
                self._write_all(staged_fd, chunk)
            sha256 = digest.hexdigest()
            timestamp = _now()
            mutation_lock.enter_context(_serialize_file_mutations(self.db_path))
            conn = self.connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM files WHERE path=?", (normalized,)).fetchone()
            actual_version = int(row["version"]) if row is not None else 0
            if expected_version is not None and actual_version != expected_version:
                raise FileConflict(
                    f"version conflict for {normalized}: expected {expected_version}, current {actual_version}"
                )
            if row is not None and not row["storage_key"]:
                row = self._checkpoint_legacy_object(
                    conn, row, normalized, expected_version, newly_created,
                )
                actual_version = int(row["version"])
            if row is not None:
                conn.execute(
                    """INSERT OR IGNORE INTO file_revisions
                       (path,version,size,sha256,content_type,created_at,updated_at,
                        created_by,updated_by,storage_key) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (normalized, int(row["version"]), int(row["size"]), row["sha256"],
                     row["content_type"], row["created_at"], row["updated_at"],
                     row["created_by"], row["updated_by"], row["storage_key"]),
                )
            history_max = self._max_revision_version(conn, normalized)
            next_version = max(actual_version, history_max) + 1
            storage_key = self._publish_object_temp(
                objects_fd, temporary, staged_fd, size=size, digest=sha256,
                newly_created=newly_created,
            )
            mirror = self._install_mirror(normalized, staged_fd, allow_replace=row is not None)
            if row is None:
                conn.execute(
                    """INSERT INTO files(path,size,sha256,content_type,created_at,updated_at,
                                         version,created_by,updated_by,storage_key)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (normalized, size, sha256, content_type, timestamp, timestamp,
                     next_version, validated_author, validated_author, storage_key),
                )
            else:
                conn.execute(
                    """UPDATE files SET size=?,sha256=?,content_type=?,updated_at=?,version=?,
                                         updated_by=?,storage_key=? WHERE path=?""",
                    (size, sha256, content_type, timestamp, next_version,
                     validated_author, storage_key, normalized),
                )
            result = conn.execute("SELECT * FROM files WHERE path=?", (normalized,)).fetchone()
            assert result is not None
            metadata = self._metadata_from_row(result)
            conn.commit()
            committed = True
            return metadata
        except BaseException:
            try:
                if mirror is not None:
                    failed_mirror, mirror = mirror, None
                    failed_mirror.rollback()
            finally:
                try:
                    if conn is not None and conn.in_transaction:
                        conn.rollback()
                finally:
                    if conn is not None:
                        self._cleanup_unreferenced_objects(conn, newly_created)
            raise
        finally:
            try:
                if conn is not None:
                    conn.close()
            finally:
                try:
                    if staged_fd is not None:
                        os.close(staged_fd)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=objects_fd)
                    except FileNotFoundError:
                        pass
                    finally:
                        try:
                            os.close(objects_fd)
                        finally:
                            try:
                                if mirror is not None and committed:
                                    mirror.finish()
                            finally:
                                mutation_lock.close()

    def _install_mirror(self, path: str, data: bytes | int, *, allow_replace: bool) -> _MirrorChange:
        parts = self._validate_path(path)
        parent_fd = self._open_parent_fd(parts, create=True)
        temporary = f".upload-{secrets.token_hex(16)}.tmp"
        backup: str | None = None
        installed = False
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
            try:
                if isinstance(data, int):
                    os.lseek(data, 0, os.SEEK_SET)
                    while chunk := os.read(data, 1024 * 1024):
                        self._write_all(fd, chunk)
                else:
                    self._write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            current = self._check_leaf_at(parent_fd, parts[-1])
            if current is not None:
                if not allow_replace:
                    raise FilePathError("an unregistered file already exists at this path")
                backup = f".backup-{secrets.token_hex(16)}.tmp"
                os.rename(parts[-1], backup, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                moved = self._check_leaf_at(parent_fd, backup)
                if moved is None:
                    raise FilePathError("file changed during replacement")
            os.rename(temporary, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            installed = True
            os.fsync(parent_fd)
            return _MirrorChange(parent_fd, parts[-1], backup, installed)
        except Exception:
            if installed:
                try:
                    os.unlink(parts[-1], dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            if backup is not None:
                try:
                    os.rename(backup, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
            raise

    def _snapshot_row(self, row: sqlite3.Row, newly_created: set[str] | None = None) -> str:
        storage_key = row["storage_key"]
        if storage_key:
            return str(storage_key)
        fd = self._open_flat_fd(str(row["path"]))
        assert fd is not None
        try:
            return self._put_object_from_fd(
                fd, self._metadata_from_row(row), newly_created=newly_created,
            )
        finally:
            os.close(fd)

    @_serialize_file_mutation
    def delete(self, path: str, expected_version: int | None = None) -> FileMetadata:
        normalized = "/".join(self._validate_path(path))
        self._validate_expected_version(expected_version)
        conn = self.connect()
        removed: _RemovedMirror | None = None
        committed = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM files WHERE path=?", (normalized,)).fetchone()
            if row is None:
                raise FileNotFound("file not found")
            actual_version = int(row["version"])
            if expected_version is not None and expected_version != actual_version:
                raise FileConflict(
                    f"version conflict for {normalized}: expected {expected_version}, current {actual_version}"
                )
            if not row["storage_key"]:
                row = self._checkpoint_legacy_object(conn, row, normalized, expected_version)
                actual_version = int(row["version"])
            storage_key = self._snapshot_row(row)
            conn.execute(
                """INSERT OR IGNORE INTO file_revisions
                   (path,version,size,sha256,content_type,created_at,updated_at,
                    created_by,updated_by,storage_key) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (normalized, actual_version, int(row["size"]), row["sha256"], row["content_type"],
                 row["created_at"], row["updated_at"], row["created_by"], row["updated_by"], storage_key),
            )
            metadata = self._metadata_from_row(row)
            parts = self._validate_path(normalized)
            parent_fd = self._open_parent_fd(parts)
            try:
                existing = self._check_leaf_at(parent_fd, parts[-1])
                if existing is not None:
                    backup = f".delete-{secrets.token_hex(16)}.tmp"
                    os.rename(parts[-1], backup, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    removed = _RemovedMirror(parent_fd, parts[-1], backup)
                    moved = self._check_leaf_at(parent_fd, backup)
                    if moved is None:
                        raise FilePathError("file changed during deletion")
                else:
                    os.close(parent_fd)
                    parent_fd = -1
            finally:
                if parent_fd >= 0 and removed is None:
                    os.close(parent_fd)
            conn.execute("DELETE FROM files WHERE path=?", (normalized,))
            conn.commit()
            committed = True
            return metadata
        except BaseException:
            try:
                if removed is not None:
                    failed_mirror, removed = removed, None
                    failed_mirror.rollback()
            finally:
                if conn.in_transaction:
                    conn.rollback()
            raise
        finally:
            conn.close()
            if removed is not None and committed:
                removed.finish()

    def open_for_read(self, path: str) -> tuple[FileMetadata, int]:
        row = self._record(path)
        if row is None:
            raise FileNotFound("file not found")
        metadata = self._metadata_from_row(row)
        storage_key = row["storage_key"]
        fd = self._open_object_fd(str(storage_key)) if storage_key else self._open_flat_fd(metadata.path)
        assert fd is not None
        try:
            self._verify_fd(fd, metadata)
            return metadata, fd
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def iter_fd(fd: int, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        with os.fdopen(fd, "rb") as handle:
            while chunk := handle.read(chunk_size):
                yield chunk


def file_dict(metadata: FileMetadata) -> dict[str, object]:
    return asdict(metadata)
