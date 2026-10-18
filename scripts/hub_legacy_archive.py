#!/usr/bin/env python3
"""Validate an inert, checkpointed legacy MCP archive without importing it."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Any, NoReturn, TypeGuard
from urllib.parse import quote

MANIFEST_NAME = "archive-manifest.json"
MANIFEST_SCHEMA = "hub_legacy_archive_v1"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SINGLE_FILE_BYTES = 8 * 1024**3
MAX_TOTAL_BYTES = 128 * 1024**3
MAX_INVENTORY_ENTRIES = 200_000
MAX_SQL_ROWS = 1_000_000
MAX_AUDIT_RECORDS = 1_000_000
MAX_AUDIT_RECORD_BYTES = 1024 * 1024
MAX_VALIDATION_SECONDS = 3600
MAX_KNOWLEDGE_CONTENT_BYTES = 1024 * 1024
CHUNK_BYTES = 1024 * 1024

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE_REFERENCE = re.compile(
    r"ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*/internal-mcp"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[0-9a-f]{64})\Z"
)
_VAULTBRIDGE_MANIFEST = "snapshots/20261014T010743Z/MANIFEST.json"
_VAULTBRIDGE_COUNT = 65

_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "messages": (
        "id", "sender", "recipient", "message_type", "priority", "body_json", "status",
        "correlation_id", "in_reply_to", "hop_count", "requires_user_approval", "created_at",
        "claimed_by", "claim_expires_at", "acknowledged_at", "rejected_at", "rejection_reason",
    ),
    "message_delivery_sequence": ("seq", "message_id"),
    "send_idempotency": (
        "sender", "client_request_id", "message_id", "recipient", "created_at", "seq",
        "fingerprint_version", "fingerprint",
    ),
    "files": (
        "path", "size", "sha256", "content_type", "created_at", "updated_at", "version",
        "created_by", "updated_by", "storage_key",
    ),
    "file_revisions": (
        "path", "version", "size", "sha256", "content_type", "created_at", "updated_at",
        "created_by", "updated_by", "storage_key",
    ),
    "knowledge": (
        "project", "path", "title", "content", "version", "sha256", "created_by", "updated_by",
        "created_at", "updated_at",
    ),
    "topic_memberships": (
        "topic", "agent", "session_hash", "subscription_generation", "stream_generation",
        "generation", "registered_at",
    ),
    "feedback_entries": (
        "id", "author", "kind", "title", "description", "project", "severity", "status", "revision",
        "created_at", "updated_at", "client_request_id", "payload_json", "payload_hash",
    ),
    "feedback_history": ("feedback_id", "revision", "actor", "status", "note", "created_at"),
    "sqlite_sequence": ("name", "seq"),
}
_TABLE_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "messages": ("id",),
    "message_delivery_sequence": ("seq",),
    "send_idempotency": ("sender", "client_request_id"),
    "files": ("path",),
    "file_revisions": ("path", "version"),
    "knowledge": ("project", "path"),
    "topic_memberships": ("topic", "agent"),
    "feedback_entries": ("id",),
    "feedback_history": ("feedback_id", "revision"),
    "sqlite_sequence": (),
}
_REQUIRED_AUDIT_KEYS = {
    "timestamp", "operation", "agent", "message_id", "recipient", "result",
    "correlation_id", "request_id", "prev_hash", "hash",
}


class ArchiveValidationError(Exception):
    """A bounded, metadata-only validation failure code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise ArchiveValidationError(code)


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        _fail("validation_time_limit")


def _is_sha256(value: object) -> TypeGuard[str]:
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _safe_relative(value: object, *, directory: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        _fail("unsafe_relative_path")
    if value.startswith("/") or "\\" in value or "\x00" in value:
        _fail("unsafe_relative_path")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail("unsafe_relative_path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        _fail("unsafe_relative_path")
    if not directory and value.endswith("/"):
        _fail("unsafe_relative_path")
    if len(value.encode("utf-8", errors="strict")) > 4096:
        _fail("unsafe_relative_path")
    return value


def _signature(st: os.stat_result) -> tuple[int, ...]:
    return (
        st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size,
        st.st_mtime_ns, st.st_ctime_ns,
    )


def _open_absolute_directory(path: str | os.PathLike[str]) -> tuple[int, str]:
    raw = os.fspath(path)
    if not isinstance(raw, str) or "\x00" in raw:
        _fail("unsafe_path_component")
    absolute = os.path.abspath(raw)
    fd = os.open("/", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        for component in absolute.split("/")[1:]:
            if not component:
                continue
            before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                _fail("unsafe_path_component")
            child = os.open(
                component,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=fd,
            )
            if _signature(before) != _signature(os.fstat(child)):
                os.close(child)
                _fail("source_changed")
            os.close(fd)
            fd = child
        return fd, absolute
    except BaseException:
        os.close(fd)
        raise


def _open_relative(root_fd: int, relative: str, *, directory: bool = False) -> int:
    parts = _safe_relative(relative, directory=directory).split("/")
    parent = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
            os.close(parent)
            parent = child
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        else:
            flags |= getattr(os, "O_NONBLOCK", 0)
        result = os.open(parts[-1], flags, dir_fd=parent)
        mode = os.fstat(result).st_mode
        if (directory and not stat.S_ISDIR(mode)) or (not directory and not stat.S_ISREG(mode)):
            os.close(result)
            _fail("unsafe_source_entry")
        return result
    finally:
        os.close(parent)


def _hash_fd(fd: int, deadline: float, *, capture_limit: int = 0) -> tuple[str, int, bytes | None, tuple[int, ...]]:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        _fail("unsafe_source_entry")
    digest = hashlib.sha256()
    size = 0
    captured = bytearray() if capture_limit else None
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        _check_deadline(deadline)
        chunk = os.read(fd, CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_SINGLE_FILE_BYTES:
            _fail("file_size_limit")
        digest.update(chunk)
        if captured is not None:
            if size > capture_limit:
                _fail("manifest_size_limit")
            captured.extend(chunk)
    after = os.fstat(fd)
    if _signature(before) != _signature(after) or size != before.st_size:
        _fail("source_changed")
    return digest.hexdigest(), size, bytes(captured) if captured is not None else None, _signature(after)


def _scan_bundle(root_fd: int, deadline: float) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[int, ...]], bytes]:
    files: dict[str, dict[str, Any]] = {}
    directories: dict[str, tuple[int, ...]] = {}
    totals = {"bytes": 0, "entries": 0}
    manifest_data: bytes | None = None
    root_before = os.fstat(root_fd)

    def visit(directory_fd: int, relative_dir: str) -> None:
        nonlocal manifest_data
        _check_deadline(deadline)
        dir_before = os.fstat(directory_fd)
        if not stat.S_ISDIR(dir_before.st_mode):
            _fail("unsafe_source_entry")
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError:
            _fail("source_unreadable")
        for name in names:
            _check_deadline(deadline)
            if name in {".", ".."} or "/" in name or "\\" in name:
                _fail("unsafe_source_entry")
            relative = f"{relative_dir}/{name}" if relative_dir else name
            _safe_relative(relative, directory=name == "")
            totals["entries"] += 1
            if totals["entries"] > MAX_INVENTORY_ENTRIES:
                _fail("inventory_entry_limit")
            try:
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                _fail("source_changed")
            if stat.S_ISLNK(before.st_mode):
                _fail("symlink_entry")
            if stat.S_ISDIR(before.st_mode):
                try:
                    child_fd = os.open(
                        name,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=directory_fd,
                    )
                except OSError:
                    _fail("unsafe_source_entry")
                try:
                    if _signature(before) != _signature(os.fstat(child_fd)):
                        _fail("source_changed")
                    directories[relative] = _signature(before)
                    visit(child_fd, relative)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(before.st_mode):
                _fail("special_file")
            if before.st_nlink != 1:
                _fail("hardlinked_file")
            if before.st_size < 0 or before.st_size > MAX_SINGLE_FILE_BYTES:
                _fail("file_size_limit")
            totals["bytes"] += before.st_size
            if totals["bytes"] > MAX_TOTAL_BYTES:
                _fail("total_bytes_limit")
            try:
                file_fd = os.open(
                    name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                    | getattr(os, "O_CLOEXEC", 0), dir_fd=directory_fd,
                )
            except OSError:
                _fail("unsafe_source_entry")
            try:
                if _signature(before) != _signature(os.fstat(file_fd)):
                    _fail("source_changed")
                capture_limit = MAX_MANIFEST_BYTES if relative == MANIFEST_NAME else 0
                sha256, size, captured, signature = _hash_fd(file_fd, deadline, capture_limit=capture_limit)
                if relative == MANIFEST_NAME:
                    manifest_data = captured
                files[relative] = {"size": size, "sha256": sha256, "signature": signature}
            finally:
                os.close(file_fd)
        if _signature(dir_before) != _signature(os.fstat(directory_fd)):
            _fail("source_changed")

    visit(root_fd, "")
    if _signature(root_before) != _signature(os.fstat(root_fd)):
        _fail("source_changed")
    if manifest_data is None:
        _fail("manifest_missing")
    return files, directories, manifest_data


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_json_key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    _fail("invalid_json_constant")


def _strict_json(data: bytes) -> Any:
    try:
        return json.loads(
            data.decode("utf-8", errors="strict"), object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except ArchiveValidationError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError):
        _fail("invalid_json")


def _exact_keys(value: object, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _fail("manifest_shape_invalid")
    return value


def _parse_manifest(data: bytes) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    value = _exact_keys(
        _strict_json(data), {"schema", "source", "database", "audit", "files_root", "inventory"},
    )
    if value["schema"] != MANIFEST_SCHEMA:
        _fail("manifest_schema_invalid")
    source = _exact_keys(
        value["source"], {"service", "version", "schema_version", "image_reference", "revision"},
    )
    if (source["service"] != "agent-message-bus" or source["version"] != "0.6.0"
            or type(source["schema_version"]) is not int or source["schema_version"] != 4
            or not isinstance(source["image_reference"], str) or len(source["image_reference"]) > 256
            or _IMAGE_REFERENCE.fullmatch(source["image_reference"]) is None
            or not isinstance(source["revision"], str) or _SHA40.fullmatch(source["revision"]) is None):
        _fail("source_metadata_invalid")
    database = _safe_relative(value["database"])
    audit = _safe_relative(value["audit"])
    files_root = _safe_relative(value["files_root"], directory=True)
    if database == audit or Path(database).name == "hub.sqlite3":
        _fail("source_path_invalid")
    sidecars = {database + suffix for suffix in ("-wal", "-shm", "-journal")}
    inventory = value["inventory"]
    if not isinstance(inventory, list):
        _fail("manifest_inventory_invalid")
    if len(inventory) > MAX_INVENTORY_ENTRIES:
        _fail("inventory_entry_limit")
    entries: dict[str, dict[str, Any]] = {}
    for item in inventory:
        entry = _exact_keys(item, {"path", "size", "sha256"})
        path = _safe_relative(entry["path"])
        if path == MANIFEST_NAME or path in entries or path in sidecars:
            _fail("manifest_inventory_invalid")
        size = entry["size"]
        if type(size) is not int or size < 0 or size > MAX_SINGLE_FILE_BYTES:
            _fail("manifest_inventory_invalid")
        if not _is_sha256(entry["sha256"]):
            _fail("manifest_inventory_invalid")
        entries[path] = {"size": size, "sha256": entry["sha256"]}
    if database in {MANIFEST_NAME, audit} or audit == MANIFEST_NAME:
        _fail("source_path_invalid")
    value["database"] = database
    value["audit"] = audit
    value["files_root"] = files_root
    return value, entries


def _check_mount_is_readonly(root_path: str) -> None:
    readonly_flag = getattr(os, "ST_RDONLY", None)
    if readonly_flag is None:
        _fail("readonly_mount_required")
    try:
        flags = os.statvfs(root_path).f_flag
    except (AttributeError, OSError):
        _fail("readonly_mount_required")
    if flags & readonly_flag == 0:
        _fail("readonly_mount_required")


def _check_inventory(files: dict[str, dict[str, Any]], expected: dict[str, dict[str, Any]]) -> None:
    actual_paths = set(files) - {MANIFEST_NAME}
    if actual_paths != set(expected):
        _fail("inventory_mismatch")
    for path, metadata in expected.items():
        actual = files[path]
        if actual["size"] != metadata["size"] or actual["sha256"] != metadata["sha256"]:
            _fail("inventory_digest_mismatch")


def _quoted_identifier(identifier: str) -> str:
    if identifier not in _TABLE_COLUMNS:
        _fail("database_schema_invalid")
    return '"' + identifier.replace('"', '""') + '"'


def _row_digest(row: tuple[Any, ...]) -> bytes:
    digest = hashlib.sha256()
    for value in row:
        if value is None:
            tag, payload = b"N", b""
        elif type(value) is int:
            tag, payload = b"I", str(value).encode("ascii")
        elif type(value) is float:
            tag, payload = b"R", value.hex().encode("ascii")
        elif type(value) is str:
            tag, payload = b"T", value.encode("utf-8", errors="strict")
        elif type(value) is bytes:
            tag, payload = b"B", value
        else:
            _fail("database_value_invalid")
        digest.update(tag)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.digest()


def _connect_immutable(database_path: str, deadline: float) -> sqlite3.Connection:
    """Open a read-only immutable connection with no trusted schema or extensions."""
    uri = "file:" + quote(database_path, safe="/") + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        connection.enable_load_extension(False)
        if hasattr(connection, "setlimit") and hasattr(sqlite3, "SQLITE_LIMIT_LENGTH"):
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_KNOWLEDGE_CONTENT_BYTES + 1024 * 1024)
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except BaseException:
        connection.close()
        raise


def _logical_digest(connection: sqlite3.Connection, table: str, count: int, deadline: float) -> str:
    cursor = connection.execute(f"SELECT * FROM {_quoted_identifier(table)}")
    row_hashes: list[bytes] = []
    while True:
        _check_deadline(deadline)
        rows = cursor.fetchmany(256)
        if not rows:
            break
        for row in rows:
            row_hashes.append(_row_digest(tuple(row)))
            if len(row_hashes) > MAX_SQL_ROWS:
                _fail("sql_row_limit")
    if len(row_hashes) != count:
        _fail("database_changed")
    row_hashes.sort()
    digest = hashlib.sha256(b"hub-legacy-archive-row-multiset-v1\0")
    for row_hash in row_hashes:
        digest.update(row_hash)
    return digest.hexdigest()


def _validate_database(bundle_path: str, root_fd: int, relative_db: str, deadline: float) -> dict[str, Any]:
    db_fd = _open_relative(root_fd, relative_db)
    db_before = os.fstat(db_fd)
    db_path = os.path.join(bundle_path, *relative_db.split("/"))
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_immutable(db_path, deadline)
        connection.row_factory = None
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            _fail("sqlite_integrity_failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            _fail("sqlite_foreign_key_failed")
        pragma_user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if type(pragma_user_version) is not int:
            _fail("database_schema_invalid")

        table_rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = {row[0] for row in table_rows}
        if table_names != set(_TABLE_COLUMNS):
            _fail("database_schema_invalid")

        schema_pk: dict[str, list[str]] = {}
        for table, expected_columns in _TABLE_COLUMNS.items():
            columns_info = connection.execute(f"PRAGMA table_info({_quoted_identifier(table)})").fetchall()
            if tuple(row[1] for row in columns_info) != expected_columns:
                _fail("database_schema_invalid")
            pk_by_position = sorted((row[5], row[1]) for row in columns_info if row[5])
            primary_key = tuple(name for _position, name in pk_by_position)
            if primary_key != _TABLE_PRIMARY_KEYS[table]:
                _fail("database_schema_invalid")
            schema_pk[table] = list(primary_key)

        counts: dict[str, int] = {}
        logical_digests: dict[str, str] = {}
        total_rows = 0
        for table in _TABLE_COLUMNS:
            count_row = connection.execute(f"SELECT COUNT(*) FROM {_quoted_identifier(table)}").fetchone()
            count = count_row[0]
            if type(count) is not int or count < 0:
                _fail("database_row_count_invalid")
            total_rows += count
            if total_rows > MAX_SQL_ROWS:
                _fail("sql_row_limit")
            counts[table] = count
            logical_digests[table] = _logical_digest(connection, table, count, deadline)

        db_after = os.fstat(db_fd)
        try:
            path_after = os.stat(db_path, follow_symlinks=False)
        except OSError:
            _fail("source_changed")
        if _signature(db_before) != _signature(db_after) or _signature(db_after) != _signature(path_after):
            _fail("source_changed")
        return {
            "pragma_user_version": pragma_user_version,
            "table_counts": counts,
            "schema_primary_keys": schema_pk,
            "logical_digests": logical_digests,
            "rows_verified": total_rows,
        }
    except ArchiveValidationError:
        raise
    except (sqlite3.Error, OSError, UnicodeError, OverflowError, ValueError, TypeError):
        _fail("database_validation_failed")
    finally:
        if connection is not None:
            connection.close()
        os.close(db_fd)


def _hash_relative_file(root_fd: int, relative: str, deadline: float) -> tuple[str, int]:
    fd = _open_relative(root_fd, relative)
    try:
        digest, size, _captured, _signature_value = _hash_fd(fd, deadline)
        return digest, size
    finally:
        os.close(fd)


def _validate_files(connection_path: str, bundle_fd: int, files_root: str, deadline: float) -> dict[str, int]:
    connection = _connect_immutable(connection_path, deadline)
    try:
        verified_objects: dict[tuple[str, str, int, str], bool] = {}

        def verify(path: object, size: object, expected_hash: object, storage_key: object) -> None:
            _check_deadline(deadline)
            file_path = _safe_relative(path)
            if type(size) is not int or size < 0 or not _is_sha256(expected_hash):
                _fail("file_metadata_invalid")
            if storage_key is None:
                relative = f"{files_root}/{file_path}"
            else:
                if not _is_sha256(storage_key):
                    _fail("file_storage_key_invalid")
                relative = f"{files_root}/.agent-message-bus/objects/{storage_key}"
            key = (relative, expected_hash, size, str(storage_key))
            if key in verified_objects:
                return
            actual_hash, actual_size = _hash_relative_file(bundle_fd, relative, deadline)
            if actual_size != size or actual_hash != expected_hash:
                _fail("file_content_mismatch")
            verified_objects[key] = True

        current_count = 0
        current_cursor = connection.execute("SELECT path,size,sha256,storage_key FROM files")
        while batch := current_cursor.fetchmany(128):
            for row in batch:
                verify(*row)
                current_count += 1
                if current_count > MAX_SQL_ROWS:
                    _fail("sql_row_limit")
        revision_count = 0
        revision_cursor = connection.execute(
            "SELECT path,size,sha256,storage_key FROM file_revisions"
        )
        while batch := revision_cursor.fetchmany(128):
            for row in batch:
                verify(*row)
                revision_count += 1
                if current_count + revision_count > MAX_SQL_ROWS:
                    _fail("sql_row_limit")
        return {
            "current_rows_verified": current_count,
            "revision_rows_verified": revision_count,
            "stored_objects_verified": len(verified_objects),
        }
    except ArchiveValidationError:
        raise
    except (sqlite3.Error, OSError, UnicodeError, OverflowError, ValueError, TypeError):
        _fail("file_validation_failed")
    finally:
        connection.close()


def _validate_audit(root_fd: int, relative_audit: str, deadline: float) -> dict[str, Any]:
    fd = _open_relative(root_fd, relative_audit)
    before = os.fstat(fd)
    previous = ""
    count = 0
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            while True:
                _check_deadline(deadline)
                line = handle.readline(MAX_AUDIT_RECORD_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_AUDIT_RECORD_BYTES or not line.endswith(b"\n"):
                    _fail("audit_record_invalid")
                record = _strict_json(line[:-1])
                if type(record) is not dict or set(record) != _REQUIRED_AUDIT_KEYS:
                    _fail("audit_record_invalid")
                if (type(record.get("timestamp")) is not str or type(record.get("operation")) is not str
                        or type(record.get("agent")) is not str or type(record.get("result")) is not str):
                    _fail("audit_record_invalid")
                for field in ("message_id", "recipient", "correlation_id", "request_id"):
                    if record[field] is not None and type(record[field]) is not str:
                        _fail("audit_record_invalid")
                current_hash = record.get("hash")
                if not _is_sha256(current_hash) or record.get("prev_hash") != previous:
                    _fail("audit_chain_invalid")
                payload = dict(record)
                del payload["hash"]
                canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                if not hmac.compare_digest(hashlib.sha256(canonical).hexdigest(), current_hash):
                    _fail("audit_chain_invalid")
                previous = current_hash
                count += 1
                if count > MAX_AUDIT_RECORDS:
                    _fail("audit_record_limit")
        after = os.fstat(fd)
        if _signature(before) != _signature(after):
            _fail("source_changed")
        return {"records_verified": count, "head_sha256": previous}
    except ArchiveValidationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        _fail("audit_validation_failed")
    finally:
        os.close(fd)


def _validate_knowledge(connection_path: str, deadline: float) -> dict[str, Any]:
    connection = _connect_immutable(connection_path, deadline)
    try:
        cursor = connection.execute("SELECT project,path,content,sha256 FROM knowledge")
        vaultbridge: dict[str, str] = {}
        manifest_contents: str | None = None
        total_rows = 0
        while batch := cursor.fetchmany(128):
            for project, path, content, expected in batch:
                _check_deadline(deadline)
                total_rows += 1
                if total_rows > MAX_SQL_ROWS:
                    _fail("sql_row_limit")
                if type(project) is not str or type(path) is not str or type(content) is not str or not _is_sha256(expected):
                    _fail("knowledge_row_invalid")
                encoded = content.encode("utf-8", errors="strict")
                if len(encoded) > MAX_KNOWLEDGE_CONTENT_BYTES:
                    _fail("knowledge_content_limit")
                if not hmac.compare_digest(hashlib.sha256(encoded).hexdigest(), expected):
                    _fail("knowledge_content_mismatch")
                if project == "vaultbridge":
                    _safe_relative(path)
                    if path in vaultbridge:
                        _fail("vaultbridge_archive_invalid")
                    vaultbridge[path] = expected
                    if path == _VAULTBRIDGE_MANIFEST:
                        manifest_contents = content
        if manifest_contents is None:
            _fail("vaultbridge_manifest_missing")
        manifest = _exact_keys(
            _strict_json(manifest_contents.encode("utf-8")), {"count", "entries"},
        )
        if type(manifest["count"]) is not int or manifest["count"] != _VAULTBRIDGE_COUNT:
            _fail("vaultbridge_count_invalid")
        entries = manifest["entries"]
        if type(entries) is not list or len(entries) != _VAULTBRIDGE_COUNT:
            _fail("vaultbridge_count_invalid")
        listed: dict[str, str] = {}
        for item in entries:
            entry = _exact_keys(item, {"destination_path", "source_sha256"})
            path = _safe_relative(entry["destination_path"])
            if path == _VAULTBRIDGE_MANIFEST or path in listed or not _is_sha256(entry["source_sha256"]):
                _fail("vaultbridge_archive_invalid")
            listed[path] = entry["source_sha256"]
        for path, expected_sha in listed.items():
            if not hmac.compare_digest(vaultbridge.get(path, ""), expected_sha):
                _fail("vaultbridge_digest_mismatch")
        return {
            "manifest_path": _VAULTBRIDGE_MANIFEST,
            "listed_entries_verified": len(listed),
            "project": "vaultbridge",
        }
    except ArchiveValidationError:
        raise
    except (sqlite3.Error, OSError, UnicodeError, ValueError, TypeError, RecursionError):
        _fail("knowledge_validation_failed")
    finally:
        connection.close()


def validate_bundle(bundle: str | os.PathLike[str]) -> dict[str, Any]:
    """Validate a read-only bundle and return only non-content metadata."""
    deadline = time.monotonic() + MAX_VALIDATION_SECONDS
    root_fd: int | None = None
    root_path = ""
    try:
        root_fd, root_path = _open_absolute_directory(bundle)
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            _fail("bundle_not_directory")
        _check_mount_is_readonly(root_path)
        files_before, dirs_before, manifest_data = _scan_bundle(root_fd, deadline)
        manifest, expected_inventory = _parse_manifest(manifest_data)
        _check_inventory(files_before, expected_inventory)
        for relative in (manifest["database"], manifest["audit"]):
            if relative not in files_before:
                _fail("source_path_missing")
        if manifest["files_root"] not in dirs_before:
            _fail("files_root_missing")
        sidecars = {manifest["database"] + suffix for suffix in ("-wal", "-shm", "-journal")}
        if sidecars & set(files_before):
            _fail("sqlite_sidecar_present")
        if manifest["database"] == manifest["audit"]:
            _fail("source_path_invalid")

        db_path = os.path.join(root_path, *manifest["database"].split("/"))
        database_report = _validate_database(root_path, root_fd, manifest["database"], deadline)
        file_report = _validate_files(db_path, root_fd, manifest["files_root"], deadline)
        audit_report = _validate_audit(root_fd, manifest["audit"], deadline)
        knowledge_report = _validate_knowledge(db_path, deadline)

        files_after, dirs_after, manifest_after = _scan_bundle(root_fd, deadline)
        if (files_before != files_after or dirs_before != dirs_after
                or not hmac.compare_digest(manifest_data, manifest_after)):
            _fail("source_changed")
        _check_inventory(files_after, expected_inventory)
        return {
            "archive_inert": True,
            "bundle": root_path,
            "manifest_sha256": files_before[MANIFEST_NAME]["sha256"],
            "source_captured_unverified": dict(manifest["source"]),
            "database": database_report,
            "files": file_report,
            "audit": audit_report,
            "vaultbridge": knowledge_report,
            "validation_scope": "offline archive integrity only; captured metadata is not runtime identity or authorization proof",
        }
    except ArchiveValidationError:
        raise
    except Exception:
        _fail("archive_validation_failed")
    finally:
        if root_fd is not None:
            os.close(root_fd)


def _open_report_parent(report_path: str | os.PathLike[str]) -> tuple[int, str, str]:
    raw = os.fspath(report_path)
    if not isinstance(raw, str) or "\x00" in raw:
        _fail("report_path_invalid")
    absolute = os.path.abspath(raw)
    parent_path, leaf = os.path.split(absolute)
    if not leaf or leaf in {".", ".."}:
        _fail("report_path_invalid")
    parent_fd, _ = _open_absolute_directory(parent_path)
    return parent_fd, leaf, absolute


def _write_report_exclusive(path: str | os.PathLike[str], report: dict[str, Any], bundle: str) -> None:
    parent_fd, leaf, absolute = _open_report_parent(path)
    try:
        try:
            if os.path.commonpath((bundle, absolute)) == bundle:
                _fail("report_must_be_outside_bundle")
        except ValueError:
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(leaf, flags, 0o600, dir_fd=parent_fd)
        created = os.fstat(fd)
        try:
            if not stat.S_ISREG(created.st_mode) or created.st_nlink != 1:
                _fail("report_creation_failed")
            payload = (json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    _fail("report_creation_failed")
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            try:
                current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(leaf, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
    except FileExistsError:
        _fail("report_already_exists")
    except ArchiveValidationError:
        raise
    except OSError:
        _fail("report_creation_failed")
    finally:
        os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an inert offline legacy MCP archive")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate", help="validate a read-only source bundle")
    validate_parser.add_argument("--bundle", required=True, help="read-only mounted archive bundle")
    validate_parser.add_argument("--report", required=True, help="new report path outside the bundle")
    args = parser.parse_args(argv)
    try:
        report = validate_bundle(args.bundle)
        _write_report_exclusive(args.report, report, report["bundle"])
    except ArchiveValidationError as exc:
        print(f"validation failed: {exc.code}", file=sys.stderr)
        return 2
    except Exception:
        print("validation failed: archive_validation_failed", file=sys.stderr)
        return 2
    print(f"validation passed: archive_inert=true manifest_sha256={report['manifest_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
