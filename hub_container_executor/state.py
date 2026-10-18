"""Private SQLite generation copy and non-destructive retention planning."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path


class StateCopyError(ValueError):
    """A safe storage validation failure without paths or database contents."""


@dataclass(frozen=True)
class CopiedGeneration:
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class Generation:
    generation_id: str
    size_bytes: int
    verified: bool = False
    current: bool = False
    previous: bool = False


@dataclass(frozen=True)
class RetentionPlan:
    keep_ids: tuple[str, ...]
    delete_ids: tuple[str, ...]
    total_bytes: int


def _private_dir(path: Path):
    try:
        info = path.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(info.st_mode) or path.resolve(strict=True) != path
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError
    except (OSError, ValueError):
        raise StateCopyError("state storage parent must be an existing private directory") from None


def _private_file(path: Path):
    try:
        info = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError
        return info
    except (OSError, ValueError):
        raise StateCopyError("SQLite source must be a private regular file") from None


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return "sha256:" + digest.hexdigest(), size


def copy_sqlite_generation(source_path, destination_path, *, maximum_bytes: int) -> CopiedGeneration:
    """Create a coherent online SQLite backup at an exclusive private destination.

    The source is opened read-only and never modified. The destination leaf must
    not exist; only a file created by this invocation is eligible for cleanup.
    No ownership changes or chmod/chown operations are performed.
    """
    source = Path(source_path)
    destination = Path(destination_path)
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise StateCopyError("invalid state-copy byte limit")
    if not source.is_absolute() or not destination.is_absolute():
        raise StateCopyError("state paths must be absolute operator configuration")
    if source.parent != destination.parent:
        raise StateCopyError("source and generation must share the provisioned private directory")
    _private_dir(source.parent)
    source_info = _private_file(source)
    if destination.exists() or destination.is_symlink():
        raise StateCopyError("generation destination already exists")
    try:
        probe = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            integrity = probe.execute("PRAGMA integrity_check").fetchone()
            page_count = probe.execute("PRAGMA page_count").fetchone()[0]
            page_size = probe.execute("PRAGMA page_size").fetchone()[0]
            if integrity != ("ok",):
                raise StateCopyError("source SQLite integrity check failed")
            if page_count * page_size > maximum_bytes:
                raise StateCopyError("source SQLite snapshot exceeds byte limit")
        finally:
            probe.close()
    except sqlite3.Error:
        raise StateCopyError("source SQLite database is unreadable") from None

    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError:
        raise StateCopyError("generation destination already exists") from None
    except OSError:
        raise StateCopyError("cannot create private generation file") from None
    created_info = os.fstat(descriptor)
    if (not stat.S_ISREG(created_info.st_mode) or created_info.st_uid != os.geteuid()
            or created_info.st_mode & 0o077 or created_info.st_nlink != 1):
        os.close(descriptor)
        raise StateCopyError("new generation file is not private")
    os.close(descriptor)
    created_identity = (created_info.st_dev, created_info.st_ino)
    try:
        src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=2)
        dst = sqlite3.connect(destination, timeout=2)
        try:
            src.backup(dst, pages=64, sleep=0.01)
            dst.execute("PRAGMA synchronous=FULL")
            integrity = dst.execute("PRAGMA integrity_check").fetchone()
            page_count = dst.execute("PRAGMA page_count").fetchone()[0]
            page_size = dst.execute("PRAGMA page_size").fetchone()[0]
            dst.commit()
            if integrity != ("ok",):
                raise StateCopyError("copied SQLite snapshot failed integrity check")
            if page_count * page_size > maximum_bytes:
                raise StateCopyError("copied SQLite snapshot exceeds byte limit")
        finally:
            dst.close()
            src.close()
        after = _private_file(source)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                source_info.st_dev, source_info.st_ino, source_info.st_size, source_info.st_mtime_ns):
            raise StateCopyError("source SQLite file changed during backup")
        with destination.open("rb") as copied:
            os.fsync(copied.fileno())
        directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        identity, size = _hash_file(destination)
        if size > maximum_bytes:
            raise StateCopyError("copied SQLite snapshot exceeds byte limit")
        return CopiedGeneration(identity, size)
    except sqlite3.Error:
        raise StateCopyError("SQLite backup failed") from None
    except BaseException:
        try:
            current = destination.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) == created_identity:
                destination.unlink()
        except OSError:
            pass
        raise


def retention_plan(generations, *, max_generations: int, max_storage_bytes: int) -> RetentionPlan:
    """Plan exact candidates only; never deletes files or generations itself.

    Input order is oldest to newest. Current generations and unverified previous
    generations are protected; the caller must review this plan before cleanup.
    """
    if type(max_generations) is not int or max_generations < 1:
        raise StateCopyError("invalid generation-count limit")
    if type(max_storage_bytes) is not int or max_storage_bytes < 1:
        raise StateCopyError("invalid generation-storage limit")
    items = tuple(generations)
    seen = set()
    for item in items:
        if (not isinstance(item, Generation)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", item.generation_id)
                or item.generation_id in seen or type(item.size_bytes) is not int or item.size_bytes < 0
                or type(item.verified) is not bool or type(item.current) is not bool
                or type(item.previous) is not bool):
            raise StateCopyError("invalid generation inventory")
        seen.add(item.generation_id)
    if sum(item.current for item in items) > 1:
        raise StateCopyError("multiple current generations are ambiguous")
    if sum(item.previous for item in items) > 1:
        raise StateCopyError("multiple previous generations are ambiguous")

    keep = list(items)
    delete = []

    def protected(item):
        return item.current or (item.previous and not item.verified)

    while len(keep) > max_generations or sum(item.size_bytes for item in keep) > max_storage_bytes:
        candidate_index = next((index for index, item in enumerate(keep) if not protected(item)), None)
        if candidate_index is None:
            raise StateCopyError("protected generations exceed retention limits")
        delete.append(keep.pop(candidate_index))
    return RetentionPlan(tuple(item.generation_id for item in keep),
                         tuple(item.generation_id for item in delete),
                         sum(item.size_bytes for item in keep))
