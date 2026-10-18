"""Opt-in Linux local pilot ownership prerequisite, not a recovery service.

Only newly enrolled databases or databases bearing this exact enrollment are
accepted. This does not exclude legacy writers which ignore the kernel lock,
authorize Docker effects, recover workflow rows, or prove distributed ownership.
"""
from __future__ import annotations

import os
import ctypes
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import stat
import sys
import uuid
import weakref


class PilotOwnershipError(RuntimeError):
    def __init__(self):
        super().__init__('pilot ownership unavailable')


_SCHEMA = ('CREATE TABLE hub_pilot_owner (id INTEGER PRIMARY KEY CHECK(id=1), '
           'version INTEGER NOT NULL, database_id TEXT NOT NULL, device INTEGER NOT NULL, '
           'inode INTEGER NOT NULL, lock_device INTEGER NOT NULL, lock_inode INTEGER NOT NULL, '
           'epoch TEXT NOT NULL, previous_epoch TEXT)')
_OWNERS = weakref.WeakSet()


def _after_fork():
    for owner in list(_OWNERS):
        try:
            owner.close()
        except Exception:
            # Each close already attempts all its FDs. One damaged capability
            # must not prevent cleanup of the other inherited descriptions.
            pass


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


def _local_filesystem(fd, *, allow_ephemeral_test_storage=False):
    # Linux statfs is platform ABI-specific. Restrict to the reviewed 64-bit
    # Linux ABI; an oversized aligned buffer avoids depending on trailing fields.
    if sys.platform != 'linux' or ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ctypes.c_long) != 8:
        raise PilotOwnershipError()
    fields = (ctypes.c_long * 64)()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.fstatfs(ctypes.c_int(fd), ctypes.byref(fields)) != 0:
        raise PilotOwnershipError()
    allowed = {0xEF53, 0x58465342, 0x9123683E, 0x2FC12FC1}
    if allow_ephemeral_test_storage is True:
        allowed |= {0x01021994, 0x794C7630}
    if fields[0] not in allowed:
        raise PilotOwnershipError()


def _identity(value):
    return isinstance(value, str) and str(uuid.UUID(value)) == value


class PilotOwner:
    """A PID-bound, live-FD capability held until close/context exit.

    The operator must supply an existing private 0700 directory. No directory,
    permission, existing database enrollment, or legacy-state migration is made.
    Failure preserves any newly created database for explicit inspection.
    """

    def __init__(self, database_path, *, allow_ephemeral_test_storage=False):
        self._parent_fd = self._lock_fd = None
        self._closed = True
        self._pid = os.getpid()
        _OWNERS.add(self)
        try:
            if type(allow_ephemeral_test_storage) is not bool:
                raise PilotOwnershipError()
            import fcntl
            self._fcntl = fcntl
            path = Path(os.path.abspath(os.fspath(database_path)))
            if path.parent.resolve() != path.parent or path.name in {'', '.', '..'}:
                raise PilotOwnershipError()
            self._path = path
            self._parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            parent = os.fstat(self._parent_fd)
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
                raise PilotOwnershipError()
            self._parent_identity = (parent.st_dev, parent.st_ino)
            _local_filesystem(self._parent_fd, allow_ephemeral_test_storage=allow_ephemeral_test_storage)
            self._lock_name = path.name + '.pilot-owner.lock'
            self._lock_fd = os.open(self._lock_name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    0o600, dir_fd=self._parent_fd)
            self._lock_identity = self._check_file(self._lock_fd, self._lock_name)
            _local_filesystem(self._lock_fd, allow_ephemeral_test_storage=allow_ephemeral_test_storage)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Recheck after acquiring ownership, before any database write.
            self._check_file(self._lock_fd, self._lock_name, self._lock_identity)
            fresh = False
            try:
                created_fd = os.open(path.name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     0o600, dir_fd=self._parent_fd)
                try:
                    self._database_identity = self._check_file(created_fd, path.name)
                    _local_filesystem(created_fd, allow_ephemeral_test_storage=allow_ephemeral_test_storage)
                finally:
                    # Close a newly created file before SQLite can acquire any
                    # locks. Never raw-open an existing database: closing even
                    # an unrelated FD releases this process's POSIX inode locks.
                    os.close(created_fd)
                fresh = True
            except FileExistsError:
                self._database_identity = self._check_database()
            self._check_database(self._database_identity)
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if fresh:
                    db.execute(_SCHEMA)
                    row = (1, 1, str(uuid.uuid4()), *self._database_identity, *self._lock_identity, str(uuid.uuid4()), None)
                    db.execute('INSERT INTO hub_pilot_owner VALUES(?,?,?,?,?,?,?,?,?)', row)
                else:
                    row = self._read(db)
                    previous = row[7]
                    epoch = str(uuid.uuid4())
                    db.execute('UPDATE hub_pilot_owner SET epoch=?, previous_epoch=? WHERE id=1', (epoch, previous))
                    row = (*row[:7], epoch, previous)
                self._check_paths()
            self._row = tuple(row)
            self.epoch = row[7]
            self.previous_epoch = row[8]
            self._closed = False
            self.assert_active(path)
        except Exception:
            self.close()
            raise PilotOwnershipError() from None

    def _check_file(self, fd, name, expected=None):
        opened = os.fstat(fd)
        named = os.stat(name, dir_fd=self._parent_fd, follow_symlinks=False)
        identity = (opened.st_dev, opened.st_ino)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                or opened.st_dev != self._parent_identity[0]
                or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_nlink != 1
                or not stat.S_ISREG(named.st_mode) or identity != (named.st_dev, named.st_ino)
                or (expected is not None and identity != expected)):
            raise PilotOwnershipError()
        if not self._fcntl.fcntl(fd, self._fcntl.F_GETFD) & self._fcntl.FD_CLOEXEC:
            raise PilotOwnershipError()
        return identity

    def _check_paths(self):
        parent = os.stat(self._path.parent, follow_symlinks=False)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) != 0o700
                or (parent.st_dev, parent.st_ino) != self._parent_identity
                or self._path.parent.resolve() != self._path.parent):
            raise PilotOwnershipError()
        self._check_file(self._lock_fd, self._lock_name, self._lock_identity)
        self._check_database(self._database_identity)

    def _check_database(self, expected=None):
        # The private parent FD anchors identity checks without a raw database
        # FD whose close could invalidate SQLite's process-wide POSIX locks.
        named = os.stat(self._path.name, dir_fd=self._parent_fd, follow_symlinks=False)
        identity = (named.st_dev, named.st_ino)
        if (not stat.S_ISREG(named.st_mode) or named.st_uid != os.getuid()
                or named.st_dev != self._parent_identity[0]
                or stat.S_IMODE(named.st_mode) != 0o600 or named.st_nlink != 1
                or (expected is not None and identity != expected)):
            raise PilotOwnershipError()
        return identity

    @contextmanager
    def _connect(self):
        # mode=rw must not recreate a replaced/missing database.
        db = sqlite3.connect(self._path.as_uri() + '?mode=rw', uri=True, timeout=0)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _read(self, db):
        schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='hub_pilot_owner'").fetchone()
        if schema is None or schema[0] != _SCHEMA:
            raise PilotOwnershipError()
        rows = db.execute('SELECT * FROM hub_pilot_owner').fetchall()
        if len(rows) != 1:
            raise PilotOwnershipError()
        row = rows[0]
        if (len(row) != 9 or row[:2] != (1, 1) or not _identity(row[2])
                or row[3:5] != self._database_identity or row[5:7] != self._lock_identity
                or not _identity(row[7])
                or (row[8] is not None and (not _identity(row[8]) or row[8] == row[7]))):
            raise PilotOwnershipError()
        return row

    def assert_active(self, database_path):
        try:
            if self._closed or os.getpid() != self._pid or Path(os.path.abspath(os.fspath(database_path))) != self._path:
                raise PilotOwnershipError()
            self._check_paths()
            with self._connect() as db:
                if self._read(db) != self._row:
                    raise PilotOwnershipError()
            self._check_paths()
        except Exception:
            raise PilotOwnershipError() from None

    def close(self):
        self._closed = True
        # Never LOCK_UN: an inherited child must not unlock its parent's shared
        # open file description. Closing only these FDs preserves that boundary.
        failed = False
        for name in ('_lock_fd', '_parent_fd'):
            fd = getattr(self, name, None)
            if fd is not None:
                setattr(self, name, None)
                try:
                    os.close(fd)
                except OSError:
                    failed = True
        if failed:
            raise PilotOwnershipError() from None

    def __del__(self):
        # Context exit is the normal lifecycle. GC must not strand a kernel lock
        # after the last public capability disappears; never explicitly unlock.
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self):
        self.assert_active(self._path)
        return self

    def __exit__(self, *args):
        self.close()
