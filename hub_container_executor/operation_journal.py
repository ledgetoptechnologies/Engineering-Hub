"""Opt-in private durable operation intent/receipt journal, not an executor.

This records uncertainty, never health, recovery authority or retry permission.
Callers must use a dedicated journal database on approved local Linux storage.
No environment discovery, service activation, tokens or Docker labels are used.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
from contextlib import contextmanager
from uuid import UUID

from engineering_hub.pilot_ownership import PilotOwner
from .backend import OperationObservation
from .state import _private_dir, _private_file


class OperationJournalError(RuntimeError):
    """Safe failure with no path, database contents or operator inputs."""


class OperationJournalConflict(OperationJournalError):
    """A prior intent or conflicting receipt forbids authorizing an effect."""


_KINDS = frozenset({"create_candidate", "create_rollback", "start", "stop", "probe"})
_CONTAINER = re.compile(r"[a-f0-9]{64}\Z")
_SCHEMA = ("CREATE TABLE private_operation_journal (operation_id TEXT PRIMARY KEY, "
           "kind TEXT NOT NULL, state TEXT NOT NULL, container_id TEXT)")
_TABLES = {"hub_pilot_owner", "hub_pilot_owner_epochs", "private_operation_journal"}
_BUSY_SECONDS = .25


def _operation(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise OperationJournalError("Invalid journal operation identity") from None


def _kind(value):
    if type(value) is not str or value not in _KINDS:
        raise OperationJournalError("Invalid journal operation kind")


def _receipt(value):
    if type(value) is not str or not _CONTAINER.fullmatch(value):
        raise OperationJournalError("Exact container receipt identity required")


def _row(row):
    if row is None:
        return
    if len(row) != 4:
        raise OperationJournalError("Invalid operation journal record")
    operation_id, kind, state, container_id = row
    _operation(operation_id)
    _kind(kind)
    if state == "pending" and container_id is None:
        return
    if state == "completed":
        _receipt(container_id)
        return
    raise OperationJournalError("Invalid operation journal record")


def _schema(db, *, inspect_rows=False):
    rows = db.execute("SELECT name, type, sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchall()
    tables = {name for name, kind, _ in rows if kind == "table"}
    if tables != _TABLES or any(kind != "table" for _, kind, _ in rows):
        raise OperationJournalError("Dedicated operation journal schema required")
    if next(sql for name, _, sql in rows if name == "private_operation_journal") != _SCHEMA:
        raise OperationJournalError("Dedicated operation journal schema required")
    if db.execute("PRAGMA user_version").fetchone() != (1,):
        raise OperationJournalError("Unsupported operation journal version")
    if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise OperationJournalError("Operation journal integrity unavailable")
    if inspect_rows:
        for row in db.execute("SELECT operation_id,kind,state,container_id FROM private_operation_journal"):
            _row(row)


class SQLiteOperationJournal:
    """Dedicated, single-Linux-process journal with explicit creation/reopen.

    ``begin`` is deliberately NOT idempotent: the daemon proceeds with an effect
    whenever it returns. Any prior operation ID therefore raises a conflict.
    Only an exactly identical completed receipt may be re-recorded idempotently.
    The 250ms SQLite busy timeout bounds individual waits, not total disk I/O or
    constructor duration. Private-path guards do not sandbox hostile same-UID
    processes that intentionally race filesystem replacement between checks.
    """

    def __init__(self, database_path, *, create: bool, allow_ephemeral_test_storage=False):
        self._owner = None
        self._closed = True
        self._pid = os.getpid()
        self._mutex = threading.RLock()
        try:
            if sys.platform != "linux" or type(create) is not bool:
                raise OperationJournalError("Linux explicit journal configuration required")
            self.path = Path(database_path)
            if not self.path.is_absolute() or ".." in self.path.parts:
                raise OperationJournalError("Absolute dedicated journal path required")
            _private_dir(self.path.parent)
            self._sidecars()
            if create:
                if self.path.exists() or self.path.is_symlink():
                    raise OperationJournalError("Fresh operation journal required")
                expected = None
            else:
                info = _private_file(self.path)
                expected = (info.st_dev, info.st_ino)
                # Read-only preflight BEFORE PilotOwner can update enrollment.
                # Unrelated Hub/ledger databases are never migrated or adopted.
                with self._connection(readonly=True) as db:
                    _schema(db, inspect_rows=True)
                after = _private_file(self.path)
                if (after.st_dev, after.st_ino) != expected:
                    raise OperationJournalError("Operation journal replaced")
            self._owner = PilotOwner(self.path, allow_ephemeral_test_storage=allow_ephemeral_test_storage)
            if create and self._owner.previous_epoch is not None:
                raise OperationJournalError("Fresh operation journal required")
            if expected is not None and self._owner._database_identity != expected:
                raise OperationJournalError("Operation journal replaced")
            self._closed = False
            if create:
                with self._transaction(initializing=True) as db:
                    db.execute(_SCHEMA)
                    db.execute("PRAGMA user_version=1")
            with self._transaction() as db:
                _schema(db, inspect_rows=True)
        except BaseException as error:
            if self._owner is not None:
                self._owner.close()
            self._closed = True
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise OperationJournalError("Private operation journal unavailable") from None

    @contextmanager
    def _connection(self, *, readonly=False):
        db = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                             uri=True, timeout=_BUSY_SECONDS, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=250")
            db.execute("PRAGMA trusted_schema=OFF")
            if not readonly:
                db.execute("PRAGMA synchronous=FULL")
                if db.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                    raise OperationJournalError("Operation journal mode unavailable")
            yield db
        finally:
            db.close()

    def _paths(self):
        self._owner._check_paths()
        self._sidecars()

    def _sidecars(self):
        # SQLite's rollback journal can survive a crash. Validate rather than
        # remove it; SQLite recovery runs only after private identity admission.
        for suffix in ("-journal", "-wal", "-shm"):
            path = self.path.with_name(self.path.name + suffix)
            if path.exists() or path.is_symlink():
                _private_file(path)
                if suffix != "-journal":
                    raise OperationJournalError("Unexpected operation journal sidecar")

    def _active(self):
        if self._closed or self._owner is None or os.getpid() != self._pid:
            raise OperationJournalError("Operation journal is not active")
        self._owner.assert_active(self.path)
        self._paths()

    @contextmanager
    def _transaction(self, *, initializing=False):
        if os.getpid() != self._pid:
            raise OperationJournalError("Operation journal authority is process bound")
        with self._mutex:
            try:
                self._active()
                with self._connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        self._paths()
                        if self._owner._read(db) != self._owner._row:
                            raise OperationJournalError("Operation journal ownership changed")
                        if not initializing:
                            _schema(db)
                        yield db
                        self._paths()
                        if self._owner._read(db) != self._owner._row:
                            raise OperationJournalError("Operation journal ownership changed")
                        db.commit()
                        os.fsync(self._owner._parent_fd)
                    except BaseException:
                        db.rollback()
                        raise
                self._active()
            except OperationJournalError:
                raise
            except Exception:
                raise OperationJournalError("Private operation journal unavailable") from None

    def begin(self, operation_id: str, kind: str) -> None:
        _operation(operation_id)
        _kind(kind)
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM private_operation_journal WHERE operation_id=?", (operation_id,)).fetchone():
                raise OperationJournalConflict("Operation intent already exists; effect must not replay")
            db.execute("INSERT INTO private_operation_journal VALUES (?,?,'pending',NULL)", (operation_id, kind))

    def completed(self, operation_id: str, kind: str, container_id: str | None = None) -> None:
        _operation(operation_id)
        _kind(kind)
        _receipt(container_id)
        with self._transaction() as db:
            row = db.execute("SELECT operation_id,kind,state,container_id FROM private_operation_journal WHERE operation_id=?",
                             (operation_id,)).fetchone()
            _row(row)
            if row is None or row[1] != kind or (row[2] == "completed" and row[3] != container_id):
                raise OperationJournalConflict("Operation receipt conflicts with durable intent")
            if row[2] == "pending":
                db.execute("UPDATE private_operation_journal SET state='completed', container_id=? WHERE operation_id=?",
                           (container_id, operation_id))

    def inspect(self, operation_id: str) -> OperationObservation | None:
        _operation(operation_id)
        with self._transaction() as db:
            row = db.execute("SELECT operation_id,kind,state,container_id FROM private_operation_journal WHERE operation_id=?",
                             (operation_id,)).fetchone()
            _row(row)
            if row is None:
                return None
            return OperationObservation(operation_id, row[2], row[2] == "completed", row[3])

    def close(self):
        # A forked child must not acquire an inherited thread mutex. PilotOwner
        # closes only its inherited FDs and never unlocks its parent's flock.
        if os.getpid() != self._pid:
            self._closed = True
            if self._owner is not None:
                self._owner.close()
            return
        with self._mutex:
            self._closed = True
            if self._owner is not None:
                self._owner.close()

    def __enter__(self):
        self._active()
        return self

    def __exit__(self, *args):
        self.close()
