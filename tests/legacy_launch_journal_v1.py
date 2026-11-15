"""Unmodified _Journal class from git commit 58fb35a0; compatibility test only."""
from contextlib import contextmanager
import fcntl
import os
import sqlite3
import stat
from hub_container_executor.launch_supervisor import _path, _root_parent, _require, _identity, _canonical


class _Journal:
    """Root-private local durable single-launch/one-issuance reservation.

    Restart can inspect retained rows but never adopts an earlier launch. The
    lock is held until close; no row or lock file is deleted on uncertainty.
    """
    def __init__(self, path, *, create):
        from engineering_hub.pilot_ownership import _local_filesystem
        _require(type(create) is bool)
        self.path, self.pid = _path(path), os.getpid()
        self.parent = self.lock = None
        try:
            self.parent = _root_parent(self.path, private=True)
            _local_filesystem(self.parent)
            lock_name = self.path.name + '.lock'
            self.lock = os.open(lock_name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC |
                                (os.O_CREAT | os.O_EXCL if create else 0), 0o600, dir_fd=self.parent)
            lock = os.fstat(self.lock)
            _require(stat.S_ISREG(lock.st_mode) and lock.st_uid == 0
                     and stat.S_IMODE(lock.st_mode) == 0o600 and lock.st_nlink == 1)
            self.lock_identity = _identity(lock)
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if create:
                fd = os.open(self.path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=self.parent)
                os.fsync(fd)
                os.close(fd)
                os.fsync(self.parent)
            info = os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
                     and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
            self.identity = _identity(info)
            with self.transaction() as db:
                if create:
                    db.execute('CREATE TABLE launch (id TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL)')
                    db.execute('CREATE TABLE issuance (launch_id TEXT PRIMARY KEY, nonce TEXT UNIQUE NOT NULL, '
                               'state TEXT NOT NULL, receipt_digest TEXT)')
                    db.execute('PRAGMA user_version=1')
                _require(db.execute('PRAGMA user_version').fetchone()[0] == 1)
                _require({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {'launch', 'issuance'})
        except BaseException:
            self.close()
            raise

    def _check(self):
        _require(self.pid == os.getpid() and self.parent is not None and self.lock is not None
                 and _identity(os.stat(self.path.parent, follow_symlinks=False)) == _identity(os.fstat(self.parent))
                 and self.path.parent.resolve(strict=True) == self.path.parent
                 and _identity(os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)) == self.identity
                 and _identity(os.stat(self.path.name + '.lock', dir_fd=self.parent, follow_symlinks=False)) == self.lock_identity)

    @contextmanager
    def transaction(self):
        self._check()
        db = sqlite3.connect(str(self.path), timeout=1, isolation_level=None)
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            self._check()
            db.commit()
            os.fsync(self.parent)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def reserve(self, identity, data):
        with self.transaction() as db:
            _require(db.execute('SELECT count(*) FROM launch').fetchone()[0] == 0)
            db.execute('INSERT INTO launch VALUES (?,?,?)', (identity, 'create_reserved', _canonical(data).decode()))

    def state(self, identity, state, data):
        with self.transaction() as db:
            _require(db.execute('UPDATE launch SET state=?,data=? WHERE id=?',
                (state, _canonical(data).decode(), identity)).rowcount == 1)

    def reserve_issuance(self, identity, nonce):
        with self.transaction() as db:
            _require(db.execute('SELECT state FROM launch WHERE id=?', (identity,)).fetchone() == ('running',))
            db.execute('INSERT INTO issuance VALUES (?,?,?,NULL)', (identity, nonce, 'reserved'))

    def issuance_state(self, identity, state, digest):
        with self.transaction() as db:
            _require(db.execute('UPDATE issuance SET state=?,receipt_digest=? WHERE launch_id=?',
                                (state, digest, identity)).rowcount == 1)

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
        if self.parent is not None:
            os.close(self.parent)
            self.parent = None

