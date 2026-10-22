"""Metadata-only admission for the fixed, separately mounted private ledger.

No file contents, SQLite, enrollment, credentials, writes or permission repairs.
This validates one container-namespace snapshot, not host ACLs, bind-source
rename protection, future path stability, reboot or restored-ledger admission.
The caller must still acquire PilotOwner(require_existing=True).
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import sys

from .private_browser_socket import _check_parent, _private_parent


ROOT = Path('/var/lib/hub-private')
_MOUNT_LIMIT = 2 * 1024 * 1024
_ESCAPE = re.compile(r'\\(040|011|012|134)')


class ProtectedLayoutError(ValueError):
    def __init__(self):
        super().__init__('Protected Hub storage boundary unavailable')


def database_path() -> Path:
    """The sole supported protected-layout ledger location, never a fallback."""
    return ROOT / 'hub.sqlite3'


def _proc_text(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        pieces = bytearray()
        while len(pieces) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(pieces)))
            if not chunk:
                break
            pieces.extend(chunk)
        if len(pieces) > limit:
            raise ProtectedLayoutError()
        return bytes(pieces).decode('utf-8')
    finally:
        os.close(fd)


def _read_mountinfo():
    return _proc_text('/proc/self/mountinfo', _MOUNT_LIMIT)


def _mount_id(fd):
    text = _proc_text(f'/proc/self/fdinfo/{fd}', 16384)
    values = [line.split(':', 1)[1].strip() for line in text.splitlines()
              if line.startswith('mnt_id:')]
    if len(values) != 1 or not values[0].isdigit():
        raise ProtectedLayoutError()
    return int(values[0])


def _unescape(value):
    if '\\' in _ESCAPE.sub('', value):
        raise ProtectedLayoutError()
    return _ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _mount_snapshot(fd):
    root = str(ROOT)
    selected = []
    for line in _read_mountinfo().splitlines():
        fields = line.split()
        if len(fields) < 10 or '-' not in fields:
            raise ProtectedLayoutError()
        separator = fields.index('-')
        if separator < 6 or len(fields) != separator + 4:
            raise ProtectedLayoutError()
        point = _unescape(fields[4])
        if point == root:
            selected.append(tuple(fields))
        elif point.startswith(root + '/'):
            # A nested mount could substitute just the ledger or owner lock.
            raise ProtectedLayoutError()
    if len(selected) != 1:
        raise ProtectedLayoutError()
    row = selected[0]
    value = os.fstat(fd)
    if (not row[0].isdigit() or int(row[0]) != _mount_id(fd)
            or row[2] != f'{os.major(value.st_dev)}:{os.minor(value.st_dev)}'):
        raise ProtectedLayoutError()
    return row


def _identity(value):
    return value.st_dev, value.st_ino


def _file_signature(value):
    return (_identity(value), value.st_mode, value.st_uid, value.st_gid,
            value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _private_files(fd):
    parent = os.fstat(fd)
    result = []
    for name in ('hub.sqlite3', 'hub.sqlite3.pilot-owner.lock'):
        value = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                or stat.S_IMODE(value.st_mode) != 0o600 or value.st_nlink != 1
                or value.st_dev != parent.st_dev):
            raise ProtectedLayoutError()
        result.append(value)
    if _identity(result[0]) == _identity(result[1]):
        raise ProtectedLayoutError()
    return result


def _shared_path(value):
    if not isinstance(value, (str, Path)):
        raise ProtectedLayoutError()
    raw = os.fspath(value)
    path = Path(raw)
    if not path.is_absolute() or raw != os.path.normpath(raw) or raw.startswith('//'):
        raise ProtectedLayoutError()
    return path


def _shared_candidates(shared_root, shared_paths):
    paths = []
    if shared_root is not None:
        root = _shared_path(shared_root)
        directory = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(directory.st_mode):
            raise ProtectedLayoutError()
        paths.extend((root, root / 'hub.sqlite3', root / 'agent-message-bus',
            root / 'agent-message-bus/messages.sqlite3', root / 'agent-message-bus/audit.jsonl',
            root / 'agent-message-bus/files'))
    if shared_paths is not None:
        if type(shared_paths) is dict:
            supplied = tuple(shared_paths.values())
        elif type(shared_paths) in (tuple, list):
            supplied = tuple(shared_paths)
        else:
            raise ProtectedLayoutError()
        if len(supplied) > 32:
            raise ProtectedLayoutError()
        paths.extend(_shared_path(value) for value in supplied)
    return tuple(dict.fromkeys(paths))


def _shared_identities(paths, required_root=None):
    identities = []
    for path in paths:
        try:
            value = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            if path == required_root:
                raise ProtectedLayoutError() from None
            continue
        if path == required_root and not stat.S_ISDIR(value.st_mode):
            raise ProtectedLayoutError()
        if not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode)):
            raise ProtectedLayoutError()
        identities.append(_identity(value))
    return tuple(identities)


def validate_existing_boundary(database, shared_root=None, shared_paths=None):
    """Check existing private metadata without opening either ledger or lock.

    A distinct mount ID at the exact root is required. Same-device nested binds
    are valid; device+inode aliases to the supplied shared root/DB, conventional
    bus roles, or explicit shared_paths (a dict of paths or list/tuple) are not.
    Pinned directory/file signatures and the mount identity are rechecked before
    returning. This snapshot is not a transferable ownership capability.
    """
    parent_fd = fresh_fd = None
    try:
        if (sys.platform != 'linux' or not isinstance(database, (str, Path))
                or os.fspath(database) != str(database_path())):
            raise ProtectedLayoutError()
        parent_fd, identity = _private_parent(database_path())
        mount = _mount_snapshot(parent_fd)
        files = _private_files(parent_fd)
        candidates = _shared_candidates(shared_root, shared_paths)
        required_root = _shared_path(shared_root) if shared_root is not None else None
        shared = _shared_identities(candidates, required_root)
        if identity in shared or any(_identity(value) in shared for value in files):
            raise ProtectedLayoutError()
        signatures = tuple(_file_signature(value) for value in files)
        _check_parent(database_path(), parent_fd, identity)
        fresh_fd, fresh_identity = _private_parent(database_path())
        if (fresh_identity != identity or _mount_snapshot(fresh_fd) != mount
                or _mount_snapshot(parent_fd) != mount
                or tuple(_file_signature(value) for value in _private_files(parent_fd)) != signatures
                or tuple(_file_signature(value) for value in _private_files(fresh_fd)) != signatures
                or _shared_identities(candidates, required_root) != shared):
            raise ProtectedLayoutError()
    except Exception:
        raise ProtectedLayoutError() from None
    finally:
        if fresh_fd is not None:
            os.close(fresh_fd)
        if parent_fd is not None:
            os.close(parent_fd)
