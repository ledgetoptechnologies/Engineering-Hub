"""Inert legacy message history over an independently approved frozen archive.

The operator pins the database's SHA-256 from the COMPLETE offline validator's
approved manifest/report. A newly computed hash alone does not approve a file.
No routes, environment discovery, replay, claiming, acknowledgement or writes.
Principals are supplied ONLY by existing server authentication, never a body.
Linux immutable/readonly access is required; kernel mount flags are not simulated
in production. Deadlines are cooperative, not forced OS-disk cancellation.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import threading
import time
from uuid import UUID


class ArchiveReadError(RuntimeError):
    """One sanitized failure without original SQL, row, credential or path."""
    code = 'archive_read_unavailable'

    def __init__(self):
        super().__init__(self.code)


_AGENTS = {'hermes', 'codex', 'claude'}
_HUMAN = re.compile(r'human:(?:[1-9][0-9]{0,19}|cf:[a-f0-9]{64})\Z')
_COLUMNS = ('id', 'sender', 'recipient', 'message_type', 'priority', 'body_json', 'status',
            'correlation_id', 'in_reply_to', 'hop_count', 'requires_user_approval', 'created_at',
            'claimed_by', 'claim_expires_at', 'acknowledged_at', 'rejected_at', 'rejection_reason')
_BODY_LIMIT = 65536
_PROJECTION = 'id,sender,recipient,message_type,priority,body_json,status,correlation_id,in_reply_to,created_at'


def _fail():
    raise ArchiveReadError() from None


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid, info.st_gid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _readonly(fd):
    return bool(os.fstatvfs(fd).f_flag & os.ST_RDONLY)


def _deadline(value):
    if time.monotonic() >= value:
        _fail()


def _uuid(value):
    try:
        return type(value) is str and len(value) == 36 and str(UUID(value)) == value
    except ValueError:
        return False


def _principal(value):
    if type(value) is not str or not (value in _AGENTS or _HUMAN.fullmatch(value)):
        _fail()
    return value in _AGENTS


def _text(value, maximum, *, nullable=False):
    if nullable and value is None:
        return None
    if (type(value) is not str or not 1 <= len(value) <= maximum
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _fail()
    value.encode('utf-8', errors='strict')
    return value


def _body(raw, deadline):
    if type(raw) is not str or len(raw.encode('utf-8', errors='strict')) > _BODY_LIMIT:
        _fail()
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def reject(value):
        raise ValueError
    result = json.loads(raw, object_pairs_hook=unique, parse_constant=reject)
    pending, count = [(result, 0)], 0
    while pending:
        _deadline(deadline)
        value, depth = pending.pop()
        count += 1
        if depth > 20 or count > 4096:
            _fail()
        if type(value) is dict:
            pending.extend((child, depth + 1) for child in value.values())
            for key in value:
                key.encode('utf-8', errors='strict')
        elif type(value) is list:
            pending.extend((child, depth + 1) for child in value)
        elif type(value) is str:
            value.encode('utf-8', errors='strict')
        elif type(value) is float and not math.isfinite(value):
            _fail()
    return result


class ArchiveMessageReader:
    """One process-bound readonly file binding; no other archive capability.

    expected_sha256 is exactly 64 lowercase hex digits (offline manifest form).
    Pagination is deterministic ascending UUID/id keyset order, not a claim of
    chronological delivery order. Caller owns lifecycle and must call close.
    Private operator administration/root is trusted, not a malicious same-UID
    replacement sandbox. A live Hub database is rejected by schema, not name.
    """

    def __init__(self, path, *, expected_sha256, maximum_database_bytes,
                 timeout_seconds=5):
        self._parent_fd = self._fd = None
        self._closed, self._pid = True, os.getpid()
        self._mutex = threading.RLock()
        try:
            if (sys.platform != 'linux' or type(expected_sha256) is not str
                    or re.fullmatch(r'[a-f0-9]{64}', expected_sha256) is None
                    or type(maximum_database_bytes) is not int or not 512 <= maximum_database_bytes <= 8 * 1024**3
                    or type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                    or not 0 < timeout_seconds <= 30):
                _fail()
            self._path = Path(path)
            if (not self._path.is_absolute() or any(part in {'.', '..'} for part in self._path.parts)
                    or any(ord(c) < 32 or ord(c) == 127 for c in str(self._path))):
                _fail()
            self._maximum, self._timeout, self._expected = maximum_database_bytes, timeout_seconds, expected_sha256
            deadline = time.monotonic() + self._timeout
            self._parent_fd = self._parent()
            self._parent_identity = _identity(os.fstat(self._parent_fd))[:2]
            self._fd = os.open(self._path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                               dir_fd=self._parent_fd)
            info = os.fstat(self._fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 512 <= info.st_size <= self._maximum:
                _fail()
            self._file_identity = _identity(info)
            self._closed = False
            self._check(deadline)
            self._hash(deadline)
            with self._connect(deadline) as db:
                self._schema(db)
            self._check(deadline)
        except Exception:
            self._close()
            _fail()

    def _parent(self):
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in self._path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _check(self, deadline):
        _deadline(deadline)
        if self._closed or self._pid != os.getpid() or self._path.resolve(strict=True) != self._path:
            _fail()
        parent = self._path.parent.stat(follow_symlinks=False)
        if ((parent.st_dev, parent.st_ino) != self._parent_identity
                or _identity(os.fstat(self._fd)) != self._file_identity
                or _identity(os.stat(self._path.name, dir_fd=self._parent_fd, follow_symlinks=False)) != self._file_identity
                or not _readonly(self._fd) or not _readonly(self._parent_fd)):
            _fail()
        for suffix in ('-wal', '-shm', '-journal'):
            try:
                os.stat(self._path.name + suffix, dir_fd=self._parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            _fail()

    def _hash(self, deadline):
        self._check(deadline)
        hasher, offset = hashlib.sha256(), 0
        while offset <= self._maximum:
            _deadline(deadline)
            chunk = os.pread(self._fd, min(1024**2, self._maximum + 1 - offset), offset)
            if not chunk:
                break
            hasher.update(chunk)
            offset += len(chunk)
        self._check(deadline)
        if offset != self._file_identity[6] or hasher.hexdigest() != self._expected:
            _fail()

    @contextmanager
    def _connect(self, deadline):
        self._check(deadline)
        # SQLite opens a separate readonly handle to the pinned inode; it is
        # ALWAYS closed before our owned raw FD. No live writer is supported.
        db = sqlite3.connect(f'file:/proc/self/fd/{self._fd}?mode=ro&immutable=1', uri=True, timeout=0)
        try:
            db.enable_load_extension(False)
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA query_only=ON')
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 131072)
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            db.row_factory = sqlite3.Row
            yield db
            _deadline(deadline)
        finally:
            db.close()

    @staticmethod
    def _schema(db):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name GLOB 'hub_*' LIMIT 1").fetchone():
            _fail()
        table = db.execute("SELECT type,sql FROM sqlite_master WHERE name='messages'").fetchone()
        if (table is None or table['type'] != 'table' or type(table['sql']) is not str
                or re.match(r'CREATE\s+TABLE\s', table['sql'], re.I) is None):
            _fail()
        columns = db.execute('PRAGMA table_xinfo(messages)').fetchall()
        if (tuple(row['name'] for row in columns) != _COLUMNS
                or any(row['hidden'] != 0 for row in columns)
                or [row['name'] for row in columns if row['pk']] != ['id']):
            _fail()

    @staticmethod
    def _project(row, deadline):
        identity, parent = row['id'], row['in_reply_to']
        if (not _uuid(identity) or row['sender'] not in _AGENTS or row['recipient'] not in _AGENTS
                or row['priority'] not in {'low', 'normal', 'high', 'urgent'}
                or row['status'] not in {'pending', 'claimed', 'acknowledged', 'rejected'}
                or (parent is not None and not _uuid(parent))):
            _fail()
        created = _text(row['created_at'], 64)
        if datetime.fromisoformat(created.replace('Z', '+00:00')).tzinfo is None:
            _fail()
        return dict(archive_inert=True, id=identity, sender=row['sender'], recipient=row['recipient'],
                    message_type=_text(row['message_type'], 128), priority=row['priority'],
                    body=_body(row['body_json'], deadline), status=row['status'],
                    correlation_id=_text(row['correlation_id'], 128, nullable=True),
                    in_reply_to=parent, created_at=created)

    def _read(self, principal, *, identity=None, limit=50, after_id=None):
        agent = _principal(principal)
        if self._pid != os.getpid():
            _fail()
        with self._mutex:
            deadline = time.monotonic() + self._timeout
            try:
                self._hash(deadline)
                with self._connect(deadline) as db:
                    self._schema(db)
                    conditions, values = [], []
                    if agent:
                        conditions.append('(sender=? OR recipient=?)')
                        values.extend((principal, principal))
                    if identity is not None:
                        conditions.append('id=?')
                        values.append(identity)
                    if after_id is not None:
                        conditions.append('id>?')
                        values.append(after_id)
                    query = 'SELECT ' + _PROJECTION + ' FROM messages'
                    query += (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
                    query += ' ORDER BY id COLLATE BINARY LIMIT ?'
                    rows = db.execute(query, (*values, limit)).fetchall()
                    result = [self._project(row, deadline) for row in rows]
                self._hash(deadline)
                return result
            except Exception:
                _fail()

    def get_message(self, principal, message_id):
        _principal(principal)
        if not _uuid(message_id):
            _fail()
        rows = self._read(principal, identity=message_id, limit=1)
        if not rows:
            return None
        return rows[0]

    def list_messages(self, principal, *, limit=50, after_id=None):
        _principal(principal)
        if (type(limit) is not int or not 1 <= limit <= 50
                or (after_id is not None and not _uuid(after_id))):
            _fail()
        rows = self._read(principal, limit=limit + 1, after_id=after_id)
        return {'archive_inert': True, 'items': rows[:limit],
                'next_after_id': rows[limit - 1]['id'] if len(rows) > limit else None}

    def _close(self):
        self._closed = True
        for name in ('_fd', '_parent_fd'):
            fd = getattr(self, name)
            setattr(self, name, None)
            if fd is not None:
                os.close(fd)

    def close(self):
        if self._pid != os.getpid():
            self._close()  # No inherited Python mutex or SQLite handle.
            return
        with self._mutex:
            self._close()
