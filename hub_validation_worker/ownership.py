"""Explicit Linux local sidecar lifetime lock, separate from every database.

No files, locks or hooks are opened by import/construction. Call acquire before
building the executor and close only after its safe shutdown boundary. This
excludes cooperating owners of this exact path, not hostile same-UID/root
administrators, non-cooperating writers or distributed machines. It grants no
Docker, approval, Hub database or PilotOwner enrollment authority.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import threading
import uuid
import weakref


class ValidationOwnershipError(RuntimeError):
    """Sanitized ownership denial without paths or process details."""


_MARKER = b'engineering-hub-validation-sidecar-owner-v1\n'
_LOCK_NAME = 'validation-owner.lock'
_OWNERS = weakref.WeakSet()
_REGISTRATION_LOCK = threading.Lock()
_REGISTERED = False


def _after_fork():
    global _REGISTRATION_LOCK, _REGISTERED
    for owner in list(_OWNERS):
        try:
            owner.close()  # Child branch never takes an inherited mutex/LOCK_UN.
        except Exception:
            pass
    _REGISTRATION_LOCK = threading.Lock()
    _REGISTERED = True


def _register(owner):
    global _REGISTERED
    with _REGISTRATION_LOCK:
        if not _REGISTERED:
            os.register_at_fork(after_in_child=_after_fork)
            _REGISTERED = True
        _OWNERS.add(owner)


class ValidationOwner:
    """One acquisition attempt and fresh instance UUID, held until close.

    The operator pre-creates the private 0700 directory on an allowed local
    filesystem. Existing foreign/partial lock files are not adopted or repaired.
    Closing leaves the fixed marker file intact; a new object may acquire its
    released inode and obtains a different instance UUID. Test storage override
    permits Linux tmpfs/overlay only, never Windows production emulation.
    """

    def __init__(self, directory, *, allow_ephemeral_test_storage=False):
        try:
            path = Path(directory)
            if (not path.is_absolute() or any(c in str(path) for c in '\x00\r\n')
                    or type(allow_ephemeral_test_storage) is not bool):
                raise ValueError
        except Exception:
            raise ValidationOwnershipError('Explicit validation state directory required') from None
        self._directory = path
        self._test_storage = allow_ephemeral_test_storage
        self._pid = os.getpid()
        self._parent_fd = self._lock_fd = None
        self._closed = True
        self._attempted = False
        self._instance_id = None
        self._mutex = threading.RLock()

    def _process(self):
        if os.getpid() != self._pid:
            raise ValidationOwnershipError('Validation owner is process bound')

    def _check_directory(self):
        opened = os.fstat(self._parent_fd)
        named = self._directory.lstat()
        if (self._directory.resolve(strict=True) != self._directory
                or not stat.S_ISDIR(opened.st_mode) or not stat.S_ISDIR(named.st_mode)
                or opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o700
                or named.st_uid != os.geteuid() or stat.S_IMODE(named.st_mode) != 0o700
                or (opened.st_dev, opened.st_ino) != self._parent_identity
                or (named.st_dev, named.st_ino) != self._parent_identity):
            raise ValueError

    def _check_lock(self, *, marker=True):
        opened = os.fstat(self._lock_fd)
        named = os.stat(_LOCK_NAME, dir_fd=self._parent_fd, follow_symlinks=False)
        identity = (opened.st_dev, opened.st_ino)
        if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode)
                or opened.st_uid != os.geteuid() or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_dev != self._parent_identity[0]
                or identity != (named.st_dev, named.st_ino)
                or (self._lock_identity is not None and identity != self._lock_identity)
                or not self._fcntl.fcntl(self._lock_fd, self._fcntl.F_GETFD) & self._fcntl.FD_CLOEXEC
                or (marker and os.pread(self._lock_fd, len(_MARKER) + 1, 0) != _MARKER)):
            raise ValueError
        return identity

    def acquire(self):
        self._process()  # Before an inherited mutex can ever be acquired.
        with self._mutex:
            if self._attempted:
                raise ValidationOwnershipError('Validation owner acquisition already attempted')
            self._attempted = True
            try:
                if sys.platform != 'linux':
                    raise ValueError
                import fcntl
                from engineering_hub.pilot_ownership import _local_filesystem
                self._fcntl = fcntl
                _register(self)  # Explicit acquire, not import/constructor.
                self._parent_fd = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                parent = os.fstat(self._parent_fd)
                self._parent_identity = (parent.st_dev, parent.st_ino)
                self._check_directory()
                _local_filesystem(self._parent_fd, allow_ephemeral_test_storage=self._test_storage)
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                fresh = False
                try:
                    self._lock_fd = os.open(_LOCK_NAME, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self._parent_fd)
                    fresh = True
                except FileExistsError:
                    self._lock_fd = os.open(_LOCK_NAME, flags, dir_fd=self._parent_fd)
                self._lock_identity = None
                self._lock_identity = self._check_lock(marker=not fresh)
                _local_filesystem(self._lock_fd, allow_ephemeral_test_storage=self._test_storage)
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._check_directory()
                self._check_lock(marker=not fresh)
                if fresh:
                    offset = 0
                    while offset < len(_MARKER):
                        written = os.write(self._lock_fd, _MARKER[offset:])
                        if written <= 0:
                            raise ValueError
                        offset += written
                    os.fsync(self._lock_fd)
                    os.fsync(self._parent_fd)
                self._check_directory()
                self._check_lock()
                self._instance_id = str(uuid.uuid4())
                self._closed = False
                self.assert_active()
                return self
            except Exception:
                self._close_fds()
                raise ValidationOwnershipError('Validation ownership unavailable') from None

    def assert_active(self):
        self._process()
        with self._mutex:
            try:
                if self._closed or self._parent_fd is None or self._lock_fd is None:
                    raise ValueError
                self._check_directory()
                self._check_lock()
            except Exception:
                # Do NOT release the original lock on replacement/mismatch.
                # Explicit safe shutdown close remains the release boundary.
                raise ValidationOwnershipError('Validation ownership no longer valid') from None

    @property
    def instance_id(self):
        self.assert_active()
        return self._instance_id

    @property
    def directory(self):
        """Pinned private directory, only while this owner is still active."""
        self.assert_active()
        return self._directory

    def _close_fds(self):
        self._closed = True
        failed = False
        for name in ('_lock_fd', '_parent_fd'):
            fd = getattr(self, name)
            setattr(self, name, None)
            if fd is not None:
                try:
                    os.close(fd)  # NEVER LOCK_UN: inherited open descriptions.
                except OSError:
                    failed = True
        _OWNERS.discard(self)
        if failed:
            raise ValidationOwnershipError('Validation ownership close unavailable')

    def close(self):
        if os.getpid() != self._pid:
            self._close_fds()  # No inherited Python mutex, no flock unlock.
            return
        with self._mutex:
            self._close_fds()
