"""Explicit read-only local content and versioned TEST-secret providers.

Content hashes identify bytes, not trusted builders. These providers never
approve provenance, enroll machines, create files, discover credentials, start
services or activate the executor. Operator-owned provisioning stays separate.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import threading
from types import MappingProxyType

from engineering_hub.pilot_ownership import _local_filesystem


class LocalProviderError(RuntimeError):
    """Safe failure without paths, digests, secret values or file contents."""


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SLOT = re.compile(r"test-[a-z0-9][a-z0-9-]*\Z")
_MAX_BLOB = 1024 * 1024
_MAX_SECRET_BYTES = 4096 * 4


def _digest(value):
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise LocalProviderError("Exact content digest required")


def _slot(slot_id, version):
    if (type(slot_id) is not str or not 6 <= len(slot_id) <= 96 or not _SLOT.fullmatch(slot_id)
            or type(version) is not int or not 1 <= version <= 2**63 - 1):
        raise LocalProviderError("Exact versioned test-secret slot required")


def _file_identity(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise LocalProviderError("Private regular content file required")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class PrivateContentStore:
    """Bounded ContentStore.get over pre-provisioned ``root/<64hex>`` files.

    Use separate explicit instances with maximum_bytes=65536 for build records
    and maximum_bytes=1048576 for metadata. A digest match is NOT authentication:
    verify_image_record still needs an independently enrolled trusted verifier.
    Local operator administration is trusted; this is not a same-UID sandbox.
    """

    def __init__(self, root, *, maximum_bytes, allow_ephemeral_test_storage=False):
        self._fd = None
        self._closed = True
        self._pid = os.getpid()
        self._mutex = threading.RLock()
        try:
            if (sys.platform != "linux" or type(maximum_bytes) is not int
                    or not 1 <= maximum_bytes <= _MAX_BLOB
                    or type(allow_ephemeral_test_storage) is not bool):
                raise LocalProviderError("Explicit Linux content-store configuration required")
            self._root = Path(root)
            if not self._root.is_absolute() or ".." in self._root.parts:
                raise LocalProviderError("Absolute private content root required")
            self._maximum_bytes = maximum_bytes
            if self._root.resolve(strict=True) != self._root:
                raise LocalProviderError("Private content root unavailable")
            self._fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            info = os.fstat(self._fd)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise LocalProviderError("Private content root unavailable")
            self._identity = (info.st_dev, info.st_ino)
            _local_filesystem(self._fd, allow_ephemeral_test_storage=allow_ephemeral_test_storage)
            self._closed = False
            self._check_root()
        except BaseException as error:
            self._closed = True
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise LocalProviderError("Private content store unavailable") from None

    def _check_root(self):
        if self._closed or self._fd is None or self._pid != os.getpid():
            raise LocalProviderError("Private content store is not active")
        try:
            opened = os.fstat(self._fd)
            named = self._root.stat(follow_symlinks=False)
            if self._root.resolve(strict=True) != self._root:
                raise LocalProviderError("Private content root changed")
            for info in (opened, named):
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o700
                        or (info.st_dev, info.st_ino) != self._identity):
                    raise LocalProviderError("Private content root changed")
        except LocalProviderError:
            raise
        except Exception:
            raise LocalProviderError("Private content root unavailable") from None

    def get(self, digest: str) -> bytes:
        _digest(digest)
        # Never touch a potentially inherited locked mutex in a forked child.
        if self._pid != os.getpid():
            raise LocalProviderError("Private content store is process bound")
        with self._mutex:
            fd = None
            try:
                self._check_root()
                name = digest[7:]
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                             dir_fd=self._fd)
                before = _file_identity(os.fstat(fd))
                if not 1 <= before[2] <= self._maximum_bytes:
                    raise LocalProviderError("Content byte limit exceeded")
                if _file_identity(os.stat(name, dir_fd=self._fd, follow_symlinks=False)) != before:
                    raise LocalProviderError("Content identity changed")
                data = bytearray()
                while len(data) <= self._maximum_bytes:
                    chunk = os.read(fd, min(65536, self._maximum_bytes + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                if (len(data) != before[2] or len(data) > self._maximum_bytes
                        or _file_identity(os.fstat(fd)) != before
                        or _file_identity(os.stat(name, dir_fd=self._fd, follow_symlinks=False)) != before):
                    raise LocalProviderError("Content changed during bounded read")
                self._check_root()
                if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
                    raise LocalProviderError("Content digest mismatch")
                return bytes(data)
            except LocalProviderError:
                raise
            except Exception:
                raise LocalProviderError("Private content unavailable") from None
            finally:
                if fd is not None:
                    os.close(fd)

    def close(self):
        if self._pid != os.getpid():
            self._closed = True
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            return
        with self._mutex:
            self._closed = True
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        self._check_root()
        return self

    def __exit__(self, *args):
        self.close()


class ImmutableTestSecretResolver:
    """Explicit immutable (test-slot, version) -> content digest bindings.

    The constructor accepts a tuple of (slot_id, integer_version, sha256_digest)
    tuples, never raw secret values. Unknown versions have no latest/fallback.
    Provision distinct synthetic test credentials, never production agent tokens.
    This provider cannot determine whether operator-provisioned bytes are real
    credentials; that separate enrollment gate must be enforced by the caller.
    """

    def __init__(self, root, *, bindings, allow_ephemeral_test_storage=False):
        if type(bindings) is not tuple or not 1 <= len(bindings) <= 1024:
            raise LocalProviderError("Explicit immutable test-secret bindings required")
        values = {}
        for item in bindings:
            if type(item) is not tuple or len(item) != 3:
                raise LocalProviderError("Explicit immutable test-secret bindings required")
            slot_id, version, digest = item
            _slot(slot_id, version)
            _digest(digest)
            if (slot_id, version) in values:
                raise LocalProviderError("Duplicate versioned test-secret binding")
            values[slot_id, version] = digest
        self._bindings = MappingProxyType(values)
        self._store = PrivateContentStore(root, maximum_bytes=_MAX_SECRET_BYTES,
                                          allow_ephemeral_test_storage=allow_ephemeral_test_storage)

    def resolve_test_secret(self, slot_id: str, version: int) -> str:
        _slot(slot_id, version)
        digest = self._bindings.get((slot_id, version))
        if digest is None:
            raise LocalProviderError("Versioned test-secret unavailable")
        try:
            value = self._store.get(digest).decode("utf-8", errors="strict")
            if not 1 <= len(value) <= 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise LocalProviderError("Versioned test-secret unavailable")
            return value
        except Exception:
            raise LocalProviderError("Versioned test-secret unavailable") from None

    def close(self):
        self._store.close()

    def __enter__(self):
        self._store._check_root()
        return self

    def __exit__(self, *args):
        self.close()
