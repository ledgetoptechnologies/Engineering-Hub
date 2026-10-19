"""Explicit first-baseline SQLite freeze; neither health nor deployment proof.

Linux UID/GID 10001 only. Initially the private local generation root must contain
only the selected source directory; that directory must contain only the exact
SQLite DB/WAL/SHM/journal leaves. This is not a general retention/snapshot tool.
The unchanged copier creates a UUID temporary sibling. Only that owned temporary
database is normalized to DELETE journal mode. Source inventory (including SHM)
must remain byte/identity-identical. A published frozen leaf is never removed,
including on subsequent uncertainty. Local same-UID administration is trusted.

Run in a dedicated operator process, NEVER inside the Hub/SQLite writer. Closing
an unmanaged raw DB FD can release that process's POSIX SQLite locks. A kernel
single-task and existing-source-FD preflight enforces this cooperative boundary;
it is not protection against an operator racing its own threads/signal handlers.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
from uuid import uuid4

from engineering_hub.pilot_ownership import _local_filesystem
from .state import copy_sqlite_generation


class BaselineSnapshotError(RuntimeError):
    """Sanitized snapshot failure; uncertain artifacts are not retry authority."""


_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_SOURCE = frozenset({'hub.sqlite3', 'hub.sqlite3-wal', 'hub.sqlite3-shm', 'hub.sqlite3-journal'})
_RESERVED = _SOURCE | {'validation-owner.lock', 'pilot-owner.lock'}


@dataclass(frozen=True)
class FrozenBaselineSnapshot:
    sha256: str
    size_bytes: int
    source_inventory: tuple[tuple[str, int, str], ...]
    source_identities: tuple[tuple[str, int, int, int, int], ...]

    def canonical_source_inventory(self):
        return json.dumps({'files': self.source_inventory, 'identities': self.source_identities},
                          sort_keys=True, separators=(',', ':')).encode()


def _deadline(value):
    if time.monotonic() >= value:
        raise BaselineSnapshotError('baseline_snapshot_deadline_exceeded')


def _directory(path, *, allow_ephemeral_test_storage):
    if path.resolve(strict=True) != path:
        raise BaselineSnapshotError('private_snapshot_directory_required')
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 10001 or info.st_gid != 10001
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise BaselineSnapshotError('private_snapshot_directory_required')
        _local_filesystem(fd, allow_ephemeral_test_storage=allow_ephemeral_test_storage)
        return fd, (info.st_dev, info.st_ino)
    except BaseException:
        os.close(fd)
        raise


def _guard(path, fd, identity):
    named, opened = path.lstat(), os.fstat(fd)
    if (path.resolve(strict=True) != path or any(not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 10001 or info.st_gid != 10001
            or stat.S_IMODE(info.st_mode) != 0o700
            or (info.st_dev, info.st_ino) != identity for info in (named, opened))):
        raise BaselineSnapshotError('snapshot_directory_changed')


def _file(info, *, links=1):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 10001 or info.st_gid != 10001
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != links):
        raise BaselineSnapshotError('private_snapshot_file_required')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _hash(fd, maximum, deadline):
    _deadline(deadline)
    digest, size = hashlib.sha256(), 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        _deadline(deadline)
        chunk = os.read(fd, min(1024 * 1024, maximum + 1 - size))
        _deadline(deadline)
        if not chunk:
            return 'sha256:' + digest.hexdigest(), size
        size += len(chunk)
        if size > maximum:
            raise BaselineSnapshotError('snapshot_byte_limit_exceeded')
        digest.update(chunk)


def _inventory(fd, *, maximum, deadline, excluded=None):
    leaves = []
    excluded = excluded or {}
    with os.scandir(fd) as entries:
        for entry in entries:
            if entry.name in excluded:
                current = entry.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) != excluded[entry.name]:
                    raise BaselineSnapshotError('snapshot_temporary_changed')
                continue
            if entry.name not in _SOURCE or len(leaves) >= 4:
                raise BaselineSnapshotError('foreign_baseline_entry')
            leaves.append(entry.name)
    if 'hub.sqlite3' not in leaves:
        raise BaselineSnapshotError('baseline_database_required')
    inventory, identities, total = [], [], 0
    for name in sorted(leaves):
        _deadline(deadline)
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = _file(os.fstat(file_fd))
            if before[2] > maximum - total:
                raise BaselineSnapshotError('snapshot_byte_limit_exceeded')
            sha, size = _hash(file_fd, maximum - total, deadline)
            after = _file(os.fstat(file_fd))
            named = _file(os.stat(name, dir_fd=fd, follow_symlinks=False))
            if before != after or after != named or size != before[2]:
                raise BaselineSnapshotError('baseline_inventory_changed')
            total += size
            inventory.append((name, size, sha))
            identities.append((name, before[0], before[1], before[3], before[4]))
        finally:
            os.close(file_fd)
    _deadline(deadline)
    return tuple(inventory), tuple(identities)


def _root_entries(fd, generation_id, frozen_leaf=None, owned=None):
    seen = set()
    with os.scandir(fd) as entries:
        for entry in entries:
            if entry.name == generation_id:
                seen.add(entry.name)
            elif frozen_leaf is not None and entry.name == frozen_leaf:
                if _file(entry.stat(follow_symlinks=False))[:2] != owned:
                    raise BaselineSnapshotError('snapshot_publication_changed')
                seen.add(entry.name)
            else:
                raise BaselineSnapshotError('first_baseline_root_not_isolated')
    if seen != {generation_id} | ({frozen_leaf} if frozen_leaf is not None else set()):
        raise BaselineSnapshotError('first_baseline_root_not_isolated')


def _operator_process_gate(generation_fd, deadline):
    """Inspect existing FDs with fstat only; never close an inspected DB FD."""
    def single_task():
        with os.scandir('/proc/self/task') as entries:
            count = 0
            for _ in entries:
                _deadline(deadline)
                count += 1
                if count > 1:
                    raise BaselineSnapshotError('dedicated_snapshot_operator_process_required')
            if count != 1:
                raise BaselineSnapshotError('dedicated_snapshot_operator_process_required')
    single_task()
    identities = set()
    with os.scandir(generation_fd) as entries:
        names = []
        for entry in entries:
            if entry.name not in _SOURCE or len(names) >= 4:
                raise BaselineSnapshotError('foreign_baseline_entry')
            names.append(entry.name)
            info = _file(entry.stat(follow_symlinks=False))
            identities.add(info[:2])
    if 'hub.sqlite3' not in names:
        raise BaselineSnapshotError('baseline_database_required')
    with os.scandir('/proc/self/fd') as entries:
        for entry in entries:
            _deadline(deadline)
            if not entry.name.isdecimal():
                raise BaselineSnapshotError('snapshot_operator_descriptor_unverified')
            try:
                info = os.fstat(int(entry.name))
            except OSError:
                raise BaselineSnapshotError('snapshot_operator_descriptor_changed') from None
            if (info.st_dev, info.st_ino) in identities:
                raise BaselineSnapshotError('snapshot_operator_owns_source_descriptor')
    single_task()


def freeze_baseline_snapshot(*, generation_root, source_generation_id, frozen_leaf,
                             maximum_bytes, deadline_monotonic,
                             allow_ephemeral_test_storage=False):
    """Freeze one initially isolated baseline without changing its SQLite mode.

    ``maximum_bytes`` bounds both the total original SQLite inventory and the
    frozen logical database. The absolute monotonic deadline is cooperative,
    never a claim that a kernel disk call can be forcibly interrupted. Existing
    frozen artifacts/collisions are never overwritten, adopted or deleted.
    """
    root_fd = generation_fd = temporary_fd = None
    owned = None
    temporary = None
    try:
        if (sys.platform != 'linux' or os.getuid() != 10001 or os.getgid() != 10001
                or type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 2 * 1024**3
                or type(deadline_monotonic) not in {int, float} or not math.isfinite(deadline_monotonic)
                or type(allow_ephemeral_test_storage) is not bool
                or type(source_generation_id) is not str or not _NAME.fullmatch(source_generation_id)
                or type(frozen_leaf) is not str or not _NAME.fullmatch(frozen_leaf)
                or frozen_leaf == source_generation_id
                or frozen_leaf in _RESERVED or source_generation_id in _RESERVED
                or source_generation_id.startswith('snapshot-') or frozen_leaf.startswith('snapshot-')):
            raise BaselineSnapshotError('explicit_private_linux_snapshot_configuration_required')
        deadline = deadline_monotonic
        _deadline(deadline)
        root = Path(generation_root)
        if not root.is_absolute() or '..' in root.parts:
            raise BaselineSnapshotError('absolute_snapshot_root_required')
        generation = root / source_generation_id
        root_fd, root_identity = _directory(root, allow_ephemeral_test_storage=allow_ephemeral_test_storage)
        generation_fd, generation_identity = _directory(generation, allow_ephemeral_test_storage=allow_ephemeral_test_storage)
        if root_identity[0] != generation_identity[0]:
            raise BaselineSnapshotError('same_filesystem_snapshot_required')
        _root_entries(root_fd, source_generation_id)
        _operator_process_gate(generation_fd, deadline)
        before = _inventory(generation_fd, maximum=maximum_bytes, deadline=deadline)
        temporary = 'snapshot-' + str(uuid4()) + '.sqlite3'
        temp_path = generation / temporary
        copied = copy_sqlite_generation(generation / 'hub.sqlite3', temp_path,
                                       maximum_bytes=maximum_bytes, deadline_monotonic=deadline)
        temporary_fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=generation_fd)
        initial = _file(os.fstat(temporary_fd))
        owned = initial[:2]
        if initial[0] != generation_identity[0] or _hash(temporary_fd, maximum_bytes, deadline) != (copied.sha256, copied.size_bytes):
            raise BaselineSnapshotError('copied_snapshot_changed')
        if _inventory(generation_fd, maximum=maximum_bytes, deadline=deadline, excluded={temporary: owned}) != before:
            raise BaselineSnapshotError('baseline_inventory_changed')
        _guard(root, root_fd, root_identity)
        _guard(generation, generation_fd, generation_identity)
        _root_entries(root_fd, source_generation_id)
        _deadline(deadline)
        with closing(sqlite3.connect(temp_path.as_uri() + '?mode=rw', uri=True,
                                      timeout=min(1, max(0, deadline - time.monotonic())))) as db:
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            db.execute('PRAGMA synchronous=FULL')
            if db.execute('PRAGMA journal_mode=DELETE').fetchone() != ('delete',):
                raise BaselineSnapshotError('snapshot_delete_mode_required')
            if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise BaselineSnapshotError('snapshot_integrity_failed')
            if db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0] > maximum_bytes:
                raise BaselineSnapshotError('snapshot_byte_limit_exceeded')
            db.commit()
        _deadline(deadline)
        normalized = _file(os.fstat(temporary_fd))
        if normalized[:2] != owned or _file(os.stat(temporary, dir_fd=generation_fd, follow_symlinks=False)) != normalized:
            raise BaselineSnapshotError('snapshot_temporary_changed')
        sha, size = _hash(temporary_fd, maximum_bytes, deadline)
        if _file(os.fstat(temporary_fd)) != normalized:
            raise BaselineSnapshotError('snapshot_temporary_changed')
        os.fsync(temporary_fd)
        _guard(root, root_fd, root_identity)
        _guard(generation, generation_fd, generation_identity)
        _root_entries(root_fd, source_generation_id)
        if _inventory(generation_fd, maximum=maximum_bytes, deadline=deadline, excluded={temporary: owned}) != before:
            raise BaselineSnapshotError('baseline_inventory_changed')
        _deadline(deadline)
        # Atomic exclusive publication, never replace/rename-overwrite and never
        # a cross-device copy fallback. A published inode is preserved on error.
        os.link(temporary, frozen_leaf, src_dir_fd=generation_fd, dst_dir_fd=root_fd, follow_symlinks=False)
        published = _file(os.stat(frozen_leaf, dir_fd=root_fd, follow_symlinks=False), links=2)
        if published[:2] != owned:
            raise BaselineSnapshotError('snapshot_publication_changed')
        current = os.stat(temporary, dir_fd=generation_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != owned:
            raise BaselineSnapshotError('snapshot_temporary_changed')
        os.unlink(temporary, dir_fd=generation_fd)
        temporary = None
        os.fsync(generation_fd)
        os.fsync(root_fd)
        _deadline(deadline)
        _guard(root, root_fd, root_identity)
        _guard(generation, generation_fd, generation_identity)
        if _file(os.stat(frozen_leaf, dir_fd=root_fd, follow_symlinks=False))[:2] != owned:
            raise BaselineSnapshotError('snapshot_publication_changed')
        _root_entries(root_fd, source_generation_id, frozen_leaf, owned)
        final = _file(os.fstat(temporary_fd))
        if _hash(temporary_fd, maximum_bytes, deadline) != (sha, size) or _file(os.fstat(temporary_fd)) != final:
            raise BaselineSnapshotError('snapshot_publication_changed')
        if _inventory(generation_fd, maximum=maximum_bytes, deadline=deadline) != before:
            raise BaselineSnapshotError('baseline_inventory_changed')
        return FrozenBaselineSnapshot(sha, size, before[0], before[1])
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(error, BaselineSnapshotError):
            raise
        raise BaselineSnapshotError('baseline_snapshot_failed') from None
    finally:
        # Unknown copier sidecars or replaced entries are never guessed to be
        # ours. Only a successfully pinned temporary inode may be unlinked.
        if temporary is not None and owned is not None and generation_fd is not None:
            try:
                current = os.stat(temporary, dir_fd=generation_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == owned:
                    os.unlink(temporary, dir_fd=generation_fd)
                    os.fsync(generation_fd)
            except OSError:
                pass
        for fd in (temporary_fd, generation_fd, root_fd):
            if fd is not None:
                os.close(fd)
