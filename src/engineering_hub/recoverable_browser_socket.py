"""Explicit Linux listener recovery, not service or job recovery.

Only this helper's exact, published endpoint generation can be reclaimed. The
permanent lock and private parent must survive a crash unchanged. Missing or
ambiguous provenance fails closed; this is not protection against another
process with the same filesystem authority. Do not duplicate/transfer the
listener or lock descriptors. Forked children close inherited copies without
unlocking or unlinking the parent's endpoint. No owner, queue, or job is adopted.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import socket
import stat
import threading
import uuid
import weakref

from .private_browser_socket import (
    PrivateBrowserSocketError, _check_parent, _identity, _path, _private_parent,
)

_LIMIT = 4096
_MAX_INT = (1 << 64) - 1
_guard = threading.RLock()
_leases = weakref.WeakSet()


class _Lease:
    def __init__(self):
        self.pid = os.getpid()
        self.parent_fd = self.lock_fd = self.listener = None
        self.entry = None
        self.bound = False
        self.child = False

    def child_close(self):
        self.child = True
        self.pid = os.getpid()
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        for name in ('lock_fd', 'parent_fd'):
            fd = getattr(self, name)
            setattr(self, name, None)
            if fd is not None:
                os.close(fd)


def _after_child():
    global _guard
    for lease in list(_leases):
        lease.child_close()
    _leases.clear()
    _guard = threading.RLock()


def _before_fork():
    _guard.acquire()


def _after_parent():
    _guard.release()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(before=_before_fork, after_in_parent=_after_parent,
                        after_in_child=_after_child)


def _absent(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False


def _regular(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or info.st_size > _LIMIT):
        raise PrivateBrowserSocketError()


def _metadata(info, *, directory=False):
    # Directory ctime changes when our own entries are created/removed; inode
    # identity is stable. Lock and socket ctime additionally bind their generation.
    value = {'dev': info.st_dev, 'ino': info.st_ino, 'uid': info.st_uid,
             'mode': stat.S_IMODE(info.st_mode)}
    if not directory:
        value['ctime_ns'] = info.st_ctime_ns
    return value


def _check_lock(lease):
    held = os.fstat(lease.lock_fd)
    named = os.stat(lease.lock_name, dir_fd=lease.parent_fd, follow_symlinks=False)
    _regular(held)
    _regular(named)
    if (held.st_size != 0 or named.st_size != 0
            or _metadata(held) != lease.lock_identity
            or _metadata(named) != lease.lock_identity):
        raise PrivateBrowserSocketError()
    _check_parent(lease.target, lease.parent_fd, lease.parent_identity)


def _open_lock(lease):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        fd = os.open(lease.lock_name, flags, dir_fd=lease.parent_fd)
    except FileNotFoundError:
        # Never recreate a lock alongside a record or an unknown endpoint.
        if not all(_absent(lease.parent_fd, name)
                   for name in (lease.record_name, lease.target.name)):
            raise PrivateBrowserSocketError()
        try:
            fd = os.open(lease.lock_name, flags | os.O_CREAT | os.O_EXCL,
                         0o600, dir_fd=lease.parent_fd)
        except FileExistsError:
            fd = os.open(lease.lock_name, flags, dir_fd=lease.parent_fd)
        else:
            lease.lock_fd = fd
            os.fchmod(fd, 0o600)
            os.fsync(fd)
            os.fsync(lease.parent_fd)
    lease.lock_fd = fd
    info = os.fstat(fd)
    _regular(info)
    if info.st_size != 0 or os.get_inheritable(fd):
        raise PrivateBrowserSocketError()
    lease.lock_identity = _metadata(info)
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _check_lock(lease)


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')


def _valid_metadata(value, *, directory=False):
    fields = {'dev', 'ino', 'uid', 'mode'}
    if not directory:
        fields.add('ctime_ns')
    return (type(value) is dict and set(value) == fields
            and all(type(item) is int and 0 <= item <= _MAX_INT
                    for item in value.values())
            and value['ino'] > 0 and value['uid'] == os.getuid()
            and value['mode'] == (0o700 if directory else 0o600))


def _read_record(lease):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        fd = os.open(lease.record_name, flags, dir_fd=lease.parent_fd)
    except FileNotFoundError:
        return None, None
    try:
        before = os.fstat(fd)
        _regular(before)
        raw = bytearray()
        while len(raw) <= _LIMIT:
            chunk = os.read(fd, _LIMIT + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        named = os.stat(lease.record_name, dir_fd=lease.parent_fd, follow_symlinks=False)
        _regular(after)
        _regular(named)
        if (not raw or len(raw) > _LIMIT or _metadata(before) != _metadata(after)
                or _metadata(after) != _metadata(named) or len(raw) != after.st_size):
            raise PrivateBrowserSocketError()
        value = json.loads(raw.decode('ascii'), object_pairs_hook=_pairs)
        if (type(value) is not dict
                or set(value) != {'version', 'generation', 'parent', 'lock', 'socket'}
                or type(value['version']) is not int or value['version'] != 1
                or type(value['generation']) is not str
                or str(uuid.UUID(value['generation'])) != value['generation']
                or not _valid_metadata(value['parent'], directory=True)
                or not _valid_metadata(value['lock'])
                or not _valid_metadata(value['socket']) or _canonical(value) != raw
                or value['parent'] != _metadata(os.fstat(lease.parent_fd), directory=True)
                or value['lock'] != lease.lock_identity):
            raise PrivateBrowserSocketError()
        return value, _metadata(after)
    finally:
        os.close(fd)


def _socket_info(lease):
    info = os.stat(lease.target.name, dir_fd=lease.parent_fd, follow_symlinks=False)
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise PrivateBrowserSocketError()
    return _metadata(info)


def _check_previous(lease, previous):
    if previous is None:
        if not _absent(lease.parent_fd, lease.record_name):
            raise PrivateBrowserSocketError()
    else:
        current = os.stat(lease.record_name, dir_fd=lease.parent_fd, follow_symlinks=False)
        _regular(current)
        if _metadata(current) != previous:
            raise PrivateBrowserSocketError()


def _publish_record(lease, value, previous):
    """Atomic durable publication seam; never called before exclusive bind."""
    _check_lock(lease)
    if _socket_info(lease) != value['socket']:
        raise PrivateBrowserSocketError()
    _check_previous(lease, previous)
    temporary = lease.record_name + '.' + str(uuid.uuid4()) + '.tmp'
    fd = None
    temporary_identity = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=lease.parent_fd)
        os.fchmod(fd, 0o600)
        temporary_identity = _identity(os.fstat(fd))
        raw = _canonical(value)
        while raw:
            count = os.write(fd, raw)
            if count <= 0:
                raise PrivateBrowserSocketError()
            raw = raw[count:]
        os.fsync(fd)
        _check_lock(lease)
        if _socket_info(lease) != value['socket']:
            raise PrivateBrowserSocketError()
        _check_previous(lease, previous)
        temporary_info = os.stat(temporary, dir_fd=lease.parent_fd, follow_symlinks=False)
        _regular(temporary_info)
        if _identity(temporary_info) != temporary_identity:
            raise PrivateBrowserSocketError()
        os.replace(temporary, lease.record_name, src_dir_fd=lease.parent_fd,
                   dst_dir_fd=lease.parent_fd)
        os.fsync(lease.parent_fd)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary_identity is not None:
            try:
                current = os.stat(temporary, dir_fd=lease.parent_fd, follow_symlinks=False)
                if _identity(current) == temporary_identity:
                    _regular(current)
                    os.unlink(temporary, dir_fd=lease.parent_fd)
            except FileNotFoundError:
                pass


@contextmanager
def recoverable_browser_socket(path):
    """Yield an explicitly owned raw nonblocking listener with crash provenance.

    Does not start a listener service, client, runner, or application. A record
    left after clean exit is retained. An endpoint without an exact valid record
    (including a crash between bind and publication) requires manual inspection;
    this helper will not delete it. Endpoint recovery never releases work leases.
    """
    lease = _Lease()
    try:
        try:
            with _guard:
                _leases.add(lease)
                lease.target = _path(path)
                lease.parent_fd, lease.parent_identity = _private_parent(lease.target)
                lease.lock_name = lease.target.name + '.endpoint.lock'
                lease.record_name = lease.target.name + '.endpoint.json'
                _open_lock(lease)
                record, previous = _read_record(lease)
                if not _absent(lease.parent_fd, lease.target.name):
                    if record is None or _socket_info(lease) != record['socket']:
                        raise PrivateBrowserSocketError()
                    _check_lock(lease)
                    if _socket_info(lease) != record['socket']:
                        raise PrivateBrowserSocketError()
                    os.unlink(lease.target.name, dir_fd=lease.parent_fd)
                    os.fsync(lease.parent_fd)
                _check_lock(lease)
                lease.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                lease.listener.set_inheritable(False)
                lease.listener.setblocking(False)
                pinned = f'/proc/self/fd/{lease.parent_fd}/{lease.target.name}'
                if len(pinned.encode('utf-8')) > 107:
                    raise PrivateBrowserSocketError()
                lease.listener.bind(pinned)
                lease.bound = True
                created = os.stat(lease.target.name, dir_fd=lease.parent_fd,
                                  follow_symlinks=False)
                if not stat.S_ISSOCK(created.st_mode) or created.st_uid != os.getuid():
                    raise PrivateBrowserSocketError()
                lease.entry = _identity(created)
                os.chmod(pinned, 0o600, follow_symlinks=False)
                lease.socket_identity = _socket_info(lease)
                value = {'version': 1, 'generation': str(uuid.uuid4()),
                         'parent': _metadata(os.fstat(lease.parent_fd), directory=True),
                         'lock': lease.lock_identity, 'socket': lease.socket_identity}
                _publish_record(lease, value, previous)
                _check_lock(lease)
                if _socket_info(lease) != lease.socket_identity:
                    raise PrivateBrowserSocketError()
                lease.listener.listen(128)
        except Exception:
            raise PrivateBrowserSocketError() from None
        yield lease.listener
    finally:
        with _guard:
            unknown = lease.bound and lease.entry is None and not lease.child
            try:
                if lease.listener is not None:
                    lease.listener.close()
                    lease.listener = None
                if lease.entry is not None and not lease.child and lease.pid == os.getpid():
                    try:
                        _check_lock(lease)
                        if _socket_info(lease) != lease.socket_identity:
                            raise PrivateBrowserSocketError()
                        os.unlink(lease.target.name, dir_fd=lease.parent_fd)
                        os.fsync(lease.parent_fd)
                    except Exception:
                        unknown = True
            finally:
                # Closing (never LOCK_UN) preserves another inherited OFD owner;
                # inherited copies are closed by the at-fork hook before user code.
                for name in ('lock_fd', 'parent_fd'):
                    fd = getattr(lease, name)
                    setattr(lease, name, None)
                    if fd is not None:
                        os.close(fd)
                _leases.discard(lease)
            if unknown:
                raise PrivateBrowserSocketError(cleanup_unknown=True) from None
