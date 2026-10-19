"""Opt-in Linux Unix-socket helpers; never start a service or create directories.

The caller owns listener/client lifetime and supplies an existing private socket
directory. Crash residue fails closed: this module never adopts or removes a
stale socket. Filesystem checks do not authenticate the peer or isolate an equally
privileged UID. Deployment must keep coordinator state and executor credentials
out of the public gateway's mounts, and protect the socket namespace separately.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import socket
import stat
import sys

import httpx


class PrivateBrowserSocketError(RuntimeError):
    """Sanitized local transport failure, without paths or credentials."""

    def __init__(self, *, cleanup_unknown=False):
        super().__init__('private browser socket cleanup unknown' if cleanup_unknown
                         else 'private browser socket unavailable')


def _path(value):
    if sys.platform != 'linux':
        raise PrivateBrowserSocketError()
    raw = os.fspath(value)
    if (not isinstance(raw, str) or not raw.startswith('/') or raw.startswith('//')
            or os.path.normpath(raw) != raw or '\x00' in raw
            or len(raw.encode('utf-8')) > 107):
        raise PrivateBrowserSocketError()
    path = Path(raw)
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', path.name) is None:
        raise PrivateBrowserSocketError()
    return path


def _identity(info):
    return info.st_dev, info.st_ino


def _trusted_ancestor(info):
    # A private leaf's name can still be renamed through an unsafe ancestor.
    # Root-owned sticky directories such as /tmp restrict foreign-UID rename;
    # arbitrary group/world-writable parents do not. Equally privileged root
    # or same-UID mutation is not isolated by these filesystem checks.
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}
            or (info.st_mode & 0o022 and not (
                info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
        raise PrivateBrowserSocketError()


def _private_parent(path):
    """Walk every directory without following symlinks; return a pinned FD."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        _trusted_ancestor(os.fstat(fd))
        for component in path.parent.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            try:
                opened = os.fstat(next_fd)
                named = os.stat(component, dir_fd=fd, follow_symlinks=False)
                _trusted_ancestor(opened)
                if _identity(opened) != _identity(named) or not stat.S_ISDIR(named.st_mode):
                    raise PrivateBrowserSocketError()
            except BaseException:
                os.close(next_fd)
                raise
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700 or os.get_inheritable(fd)):
            raise PrivateBrowserSocketError()
        _check_parent(path, fd, _identity(info))
        return fd, _identity(info)
    except BaseException:
        os.close(fd)
        raise


def _check_parent(path, fd, expected):
    held = os.fstat(fd)
    named = os.stat(path.parent, follow_symlinks=False)
    if (path.parent.resolve(strict=True) != path.parent
            or not stat.S_ISDIR(held.st_mode) or not stat.S_ISDIR(named.st_mode)
            or held.st_uid != os.getuid() or named.st_uid != os.getuid()
            or stat.S_IMODE(held.st_mode) != 0o700
            or stat.S_IMODE(named.st_mode) != 0o700
            or _identity(held) != expected or _identity(named) != expected):
        raise PrivateBrowserSocketError()


def _socket_entry(path, fd, expected=None, *, mode=0o600):
    info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != mode or info.st_nlink != 1
            or (expected is not None and _identity(info) != expected)):
        raise PrivateBrowserSocketError()
    return _identity(info)


@contextmanager
def private_browser_socket(path):
    """Yield a bound, listening, nonblocking, noninheritable AF_UNIX socket.

    Suitable for a caller's explicit ``uvicorn.Server.serve(sockets=[listener])``.
    No event loop, server, application, database, or background worker is started.
    Cleanup uses the pinned original directory, not a substituted pathname. If
    the canonical directory or original entry was removed, replaced, or changed,
    preserve that state and report unknown cleanup instead of deleting it.
    """
    parent_fd = None
    listener = None
    entry = None
    bound = False
    pid = os.getpid()
    try:
        try:
            target = _path(path)
            parent_fd, parent_identity = _private_parent(target)
            try:
                os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise PrivateBrowserSocketError()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.set_inheritable(False)
            listener.setblocking(False)
            pinned = f'/proc/self/fd/{parent_fd}/{target.name}'
            if len(pinned.encode('utf-8')) > 107:
                raise PrivateBrowserSocketError()
            listener.bind(pinned)
            bound = True
            # Record identity immediately after our successful exclusive bind.
            created = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISSOCK(created.st_mode) or created.st_uid != os.getuid():
                raise PrivateBrowserSocketError()
            entry = _identity(created)
            os.chmod(pinned, 0o600, follow_symlinks=False)
            _socket_entry(target, parent_fd, entry)
            _check_parent(target, parent_fd, parent_identity)
            listener.listen(128)
            if listener.get_inheritable() or listener.getblocking():
                raise PrivateBrowserSocketError()
        except Exception:
            raise PrivateBrowserSocketError() from None
        yield listener
    finally:
        cleanup_unknown = bound and entry is None and os.getpid() == pid
        try:
            if listener is not None:
                listener.close()
            # An inherited child may close its own descriptors, but must never
            # unlink the parent's live listening endpoint.
            if entry is not None and os.getpid() == pid:
                try:
                    _check_parent(target, parent_fd, parent_identity)
                    _socket_entry(target, parent_fd, entry)
                    os.unlink(target.name, dir_fd=parent_fd)
                except Exception:
                    cleanup_unknown = True
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
        if cleanup_unknown:
            raise PrivateBrowserSocketError(cleanup_unknown=True) from None


def create_private_browser_client(path):
    """Return an inert, dedicated UDS AsyncClient; caller must ``aclose`` it.

    The checked pathname is not an unforgeable capability: another process with
    the same filesystem authority can replace it after construction. No peer UID
    verification or namespace isolation is claimed. There is no TCP fallback,
    retry, environment proxy/auth, redirect following, or caller-supplied hook.
    """
    fd = None
    try:
        target = _path(path)
        fd, parent_identity = _private_parent(target)
        entry = _socket_entry(target, fd)
        _check_parent(target, fd, parent_identity)
        _socket_entry(target, fd, entry)
        transport = httpx.AsyncHTTPTransport(uds=str(target), retries=0, trust_env=False)
        return httpx.AsyncClient(transport=transport, trust_env=False, auth=None,
                                 follow_redirects=False, event_hooks={})
    except Exception:
        raise PrivateBrowserSocketError() from None
    finally:
        if fd is not None:
            os.close(fd)
