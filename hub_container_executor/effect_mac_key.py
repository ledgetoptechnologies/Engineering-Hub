"""Opt-in private effect-binding MAC custody; not a transport or fence.

The opt-in bound-profile runtime composes this module. Callers must arrange a
journal owner lock, authenticate the effect binding, and provide a canonical
domain-specific input.
This does not protect against hostile same-UID processes or prove any Docker
effect. A creation or fsync failure preserves partial files for inspection.
The creation key ID must be stored and supplied from an independently trusted
enrollment; a caller-supplied ID alone does not prove key continuity.
Because the key is held alongside the journal, it must not be used to protect
low-entropy secret inputs from offline guessing by a reader of that directory.
Canonical binding fields must exclude environment values and other secrets.
Restoring a journal under a different inode requires separately trusted
re-enrollment; this module never silently adopts a replacement journal.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat

from engineering_hub.pilot_ownership import _local_filesystem

_DOMAIN = b"engineering-hub.effect-binding.v1\0"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class EffectMacKeyError(RuntimeError):
    """Sanitized key custody failure."""


def _private_dir(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise EffectMacKeyError("Private key directory unavailable")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            raise EffectMacKeyError("Private key directory unavailable")
        _local_filesystem(fd, allow_ephemeral_test_storage=False)
        return fd, (info.st_dev, info.st_ino)
    except BaseException:
        os.close(fd)
        raise


def _regular_private(fd, dirfd, name, *, expected=None):
    opened = os.fstat(fd)
    named = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    identity = (opened.st_dev, opened.st_ino)
    if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_nlink != 1
            or opened.st_dev != os.fstat(dirfd).st_dev
            or not stat.S_ISREG(named.st_mode) or identity != (named.st_dev, named.st_ino)
            or (expected is not None and identity != expected)):
        raise EffectMacKeyError("Private key material unavailable")
    _local_filesystem(fd, allow_ephemeral_test_storage=False)
    return identity


def create_for_new_journal(directory, journal_name):
    """Create one key only when both journal and key are absent; never repair."""
    if type(journal_name) is not str or not _NAME.fullmatch(journal_name):
        raise EffectMacKeyError("Invalid journal identity")
    directory = Path(directory)
    try:
        dirfd, _ = _private_dir(directory)
    except (OSError, ValueError):
        raise EffectMacKeyError("Private key directory unavailable") from None
    key_name = journal_name + ".effect-binding.key"
    try:
        try:
            os.stat(journal_name, dir_fd=dirfd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise EffectMacKeyError("Journal already exists")
        fd = os.open(key_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=dirfd)
        try:
            _regular_private(fd, dirfd, key_name)
            key = secrets.token_bytes(32)
            view = memoryview(key)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise EffectMacKeyError("Key write incomplete")
                view = view[count:]
            os.fsync(fd)
            os.fsync(dirfd)
            return "sha256:" + hashlib.sha256(key).hexdigest()
        finally:
            os.close(fd)
    except EffectMacKeyError:
        raise
    except (OSError, ValueError):
        raise EffectMacKeyError("Private key creation unavailable") from None
    finally:
        os.close(dirfd)


class EffectMacKey:
    """Open a key against an existing private journal, bound to this process."""

    def __init__(self, directory, journal_name, *, expected_key_id):
        if type(journal_name) is not str or not _NAME.fullmatch(journal_name):
            raise EffectMacKeyError("Invalid journal identity")
        if type(expected_key_id) is not str or not re.fullmatch(r"sha256:[a-f0-9]{64}", expected_key_id):
            raise EffectMacKeyError("Trusted key identity required")
        self._dirfd = self._keyfd = None
        self._pid = os.getpid()
        self._directory = Path(directory)
        self._journal_name = journal_name
        self._key_name = journal_name + ".effect-binding.key"
        try:
            self._dirfd, self._dir_identity = _private_dir(self._directory)
            journal = os.stat(journal_name, dir_fd=self._dirfd, follow_symlinks=False)
            if (not stat.S_ISREG(journal.st_mode) or journal.st_uid != os.geteuid()
                    or stat.S_IMODE(journal.st_mode) != 0o600 or journal.st_nlink != 1
                    or journal.st_dev != self._dir_identity[0]):
                raise EffectMacKeyError("Private journal unavailable")
            self._journal_identity = (journal.st_dev, journal.st_ino)
            self._keyfd = os.open(self._key_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self._dirfd)
            self._key_identity = _regular_private(self._keyfd, self._dirfd, self._key_name)
            self._key = os.read(self._keyfd, 33)
            if len(self._key) != 32:
                raise EffectMacKeyError("Invalid effect-binding key")
            self._key_digest = hashlib.sha256(self._key).digest()
            if not hmac.compare_digest("sha256:" + self._key_digest.hex(), expected_key_id):
                raise EffectMacKeyError("Effect-binding key identity mismatch")
            self._check()
        except BaseException as error:
            self.close()
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise EffectMacKeyError("Private key custody unavailable") from None

    def _check(self):
        if os.getpid() != self._pid or self._dirfd is None or self._keyfd is None:
            raise EffectMacKeyError("Key custody is not active")
        named = os.stat(self._directory, follow_symlinks=False)
        if (not stat.S_ISDIR(named.st_mode) or named.st_uid != os.geteuid()
                or stat.S_IMODE(named.st_mode) != 0o700
                or (named.st_dev, named.st_ino) != self._dir_identity
                or self._directory.resolve() != self._directory):
            raise EffectMacKeyError("Private key directory changed")
        journal = os.stat(self._journal_name, dir_fd=self._dirfd, follow_symlinks=False)
        if (not stat.S_ISREG(journal.st_mode) or journal.st_uid != os.geteuid()
                or stat.S_IMODE(journal.st_mode) != 0o600 or journal.st_nlink != 1
                or (journal.st_dev, journal.st_ino) != self._journal_identity):
            raise EffectMacKeyError("Private journal changed")
        _regular_private(self._keyfd, self._dirfd, self._key_name, expected=self._key_identity)
        if hashlib.sha256(os.pread(self._keyfd, 33, 0)).digest() != self._key_digest:
            raise EffectMacKeyError("Key material changed")

    def mac(self, canonical_binding):
        if type(canonical_binding) is not bytes or not 1 <= len(canonical_binding) <= 1024 * 1024:
            raise EffectMacKeyError("Invalid canonical binding")
        try:
            self._check()
            value = hmac.new(self._key, _DOMAIN + canonical_binding, hashlib.sha256).hexdigest()
            self._check()
            return "hmac-sha256:" + value
        except EffectMacKeyError:
            raise
        except (OSError, ValueError):
            raise EffectMacKeyError("Private key custody unavailable") from None

    def close(self):
        error = None
        for name in ("_keyfd", "_dirfd"):
            fd = getattr(self, name, None)
            if fd is not None:
                setattr(self, name, None)
                try:
                    os.close(fd)
                except OSError as exc:
                    error = error or exc
        self._key = b""
        if error is not None:
            raise EffectMacKeyError("Private key close unavailable") from None
