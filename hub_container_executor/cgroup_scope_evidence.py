"""Read-only cgroup-v2 scope emptiness evidence, not an effect recovery fence.

An independently provisioned, root-controlled cgroup with no descendants is
required. A retained pidfd must be captured for every original process. This
observes kernel scope state only: escaped workers, external daemons, queued
requests and effect outcomes require separate evidence. It never signals a
process, changes cgroups, clears a hold, or authorizes retry.
"""
from dataclasses import dataclass
import ctypes
import math
import os
from pathlib import Path
import re
import select
import stat
import sys
import time

from .launch_supervisor import _Process, _boot_id

_CGROUP_ROOT = Path('/sys/fs/cgroup')
_CGROUP2_MAGIC = 0x63677270


class ScopeEvidenceError(ValueError):
    pass


def _require(value):
    if not value:
        raise ScopeEvidenceError('cgroup scope evidence unavailable')


def _identity(info):
    return info.st_dev, info.st_ino


def _filesystem(fd):
    fields = (ctypes.c_long * 64)()
    _require(ctypes.sizeof(ctypes.c_void_p) == 8 and ctypes.sizeof(ctypes.c_long) == 8
             and ctypes.CDLL(None, use_errno=True).fstatfs(fd, ctypes.byref(fields)) == 0
             and fields[0] == _CGROUP2_MAGIC)


def _mount_root():
    lines = Path('/proc/self/mountinfo').read_text(encoding='ascii').splitlines()
    matches = []
    for line in lines:
        left, separator, right = line.partition(' - ')
        fields, details = left.split(), right.split()
        if separator and len(fields) >= 6 and len(details) >= 1 and fields[4] == str(_CGROUP_ROOT):
            matches.append((fields[3], details[0]))
    _require(matches == [('/', 'cgroup2')])


def _trusted(info, *, directory=False):
    _require(info.st_uid == 0 and not info.st_mode & 0o022
             and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)))


def _read(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(fd)
        _trusted(before)
        _require(_identity(before) == _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)))
        raw = os.read(fd, 65537)
        _require(len(raw) <= 65536 and _identity(before) == _identity(os.fstat(fd))
                 and _identity(before) == _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)))
        return raw.decode('ascii')
    finally:
        os.close(fd)


def _members(raw):
    values = raw.splitlines()
    _require(all(re.fullmatch(r'[1-9][0-9]*', value) for value in values))
    ids = tuple(sorted(int(value) for value in values))
    _require(len(ids) == len(set(ids)) and all(value < 2**31 for value in ids))
    return ids


def _events(raw):
    values = {}
    for line in raw.splitlines():
        parts = line.split()
        _require(len(parts) == 2 and parts[0] not in values and parts[1] in ('0', '1'))
        values[parts[0]] = int(parts[1])
    _require(set(values) == {'populated', 'frozen'})
    return values


def _membership(process, expected, *, descendants=False):
    raw = Path(f'/proc/{process.pid}/cgroup').read_text(encoding='ascii')
    rows = raw.splitlines()
    _require(len(rows) == 1)
    prefix = '0::/' + expected
    _require(rows[0] == prefix or descendants and rows[0].startswith(prefix + '/'))


def _read_kernel_members(directory):
    """Read a kernel cgroup file in a delegated descendant, never as authority."""
    name = 'cgroup.procs'
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
    try:
        _filesystem(fd)
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode)
                 and _identity(before) == _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)))
        raw = os.read(fd, 65537)
        _require(len(raw) <= 65536 and _identity(before) == _identity(os.fstat(fd))
                 and _identity(before) == _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)))
        return _members(raw.decode('ascii'))
    finally:
        os.close(fd)


def _tree_members(directory):
    """Bounded no-follow snapshot of all direct and descendant process IDs."""
    members, visited = [], [0]
    root_device = os.fstat(directory).st_dev
    def walk(fd):
        visited[0] += 1
        _require(visited[0] <= 129)
        _filesystem(fd)
        _require(os.fstat(fd).st_dev == root_device)
        members.extend(_read_kernel_members(fd))
        _require(len(members) <= 4096)
        entries = list(os.scandir(fd))
        _require(len(entries) <= 1024)
        for item in entries:
            _require(not item.is_symlink())
            if not item.is_dir(follow_symlinks=False):
                continue
            child = os.open(item.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                _require(_identity(os.fstat(child)) == _identity(os.stat(item.name, dir_fd=fd, follow_symlinks=False)))
                walk(child)
                _require(_identity(os.fstat(child)) == _identity(os.stat(item.name, dir_fd=fd, follow_symlinks=False)))
            finally:
                os.close(child)
    walk(directory)
    _require(len(set(members)) == len(members))
    return tuple(sorted(members))


@dataclass(frozen=True)
class EmptyScopeObservation:
    """A momentary observation, not a recovery decision or durable authority."""
    boot_id: str
    scope_device: int
    scope_inode: int
    original_members: tuple
    observed_monotonic: float
    effect_outcome_proven: bool = False


class EnrolledCgroupScope:
    """Retain exact scope inode and live pidfds before waiting for emptiness.

    Root and cgroup namespace administrators are trusted not to move a worker
    out of scope. This reader does not prove that all daemon dependencies were
    enrolled or prevent privileged changes after its final check.
    """
    def __init__(self, scope, *, processes, allow_descendants=False):
        self._pid, self._fd, self._root = os.getpid(), None, None
        self._processes = ()
        try:
            _require(sys.platform == 'linux' and os.getuid() == os.geteuid() == 0
                     and type(scope) is str and re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', scope)
                     and all(part not in {'.', '..'} for part in scope.split('/'))
                     and type(processes) is tuple and processes and type(allow_descendants) is bool
                     and all(type(item) is _Process and item.pid != os.getpid() for item in processes))
            _require(len({p.pid for p in processes}) == len(processes))
            self._scope, self._allow_descendants = scope, allow_descendants
            _mount_root()
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            self._root = os.open(_CGROUP_ROOT, flags)
            _filesystem(self._root)
            _trusted(os.fstat(self._root), directory=True)
            self._root_identity = _identity(os.fstat(self._root))
            _require(_identity(os.stat(_CGROUP_ROOT, follow_symlinks=False)) == self._root_identity)
            current = os.dup(self._root)
            try:
                for name in scope.split('/'):
                    following = os.open(name, flags, dir_fd=current)
                    try:
                        _trusted(os.fstat(following), directory=True)
                        _require(_identity(os.fstat(following)) == _identity(os.stat(name, dir_fd=current, follow_symlinks=False)))
                    except BaseException:
                        os.close(following)
                        raise
                    os.close(current)
                    current = following
                self._fd, current = current, None
            finally:
                if current is not None: os.close(current)
            self._identity = _identity(os.fstat(self._fd))
            self._boot = _boot_id()
            self._processes = processes
            self.assert_enrolled_live()
            self._original = tuple(sorted((item.pid, item.start) for item in processes))
        except BaseException:
            self.close()
            raise ScopeEvidenceError('cgroup scope evidence unavailable') from None

    def _check_scope(self):
        _require(os.getpid() == self._pid and self._fd is not None and self._root is not None
                 and _boot_id() == self._boot and _identity(os.fstat(self._fd)) == self._identity)
        _mount_root()
        _require(_identity(os.fstat(self._root)) == self._root_identity
                 and _identity(os.stat(_CGROUP_ROOT, follow_symlinks=False)) == self._root_identity)
        _filesystem(self._fd)
        _trusted(os.fstat(self._fd), directory=True)
        # Every ancestor that could accept a moved process must remain
        # nondelegable as well; check through pinned no-follow descriptors.
        current = os.dup(self._root)
        try:
            for name in self._scope.split('/'):
                _trusted(os.fstat(current), directory=True)
                _read(current, 'cgroup.procs')
                _read(current, 'cgroup.threads')
                following = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    dir_fd=current)
                try:
                    _require(_identity(os.fstat(following)) == _identity(os.stat(name, dir_fd=current, follow_symlinks=False)))
                except BaseException:
                    os.close(following)
                    raise
                os.close(current)
                current = following
            _require(_identity(os.fstat(current)) == self._identity)
        finally:
            os.close(current)
        # The root boundary and all ancestors are root-controlled. Descendant
        # delegation is opt-in and bounded; kernel populated is recursive.
        depth = _read(self._fd, 'cgroup.max.depth').strip()
        count = _read(self._fd, 'cgroup.max.descendants').strip()
        _require(re.fullmatch(r'[0-9]+', depth) and re.fullmatch(r'[0-9]+', count))
        if self._allow_descendants:
            _require(1 <= int(depth) <= 32 and 1 <= int(count) <= 128)
        else:
            _require(depth == '0' and count == '0')
        for name in ('cgroup.procs', 'cgroup.threads', 'cgroup.subtree_control'):
            _read(self._fd, name)
        if not self._allow_descendants:
            _require(not any(entry.is_dir(follow_symlinks=False) or entry.is_symlink()
                             for entry in os.scandir(self._fd)))
        path = _CGROUP_ROOT / self._scope
        _require(path.resolve(strict=True) == path and _identity(os.stat(path, follow_symlinks=False)) == self._identity)

    def assert_enrolled_live(self):
        """Recheck exact retained live members and nondelegable scope."""
        try:
            self._check_scope()
            for item in self._processes:
                item.check()
                _require(item.boot == self._boot)
                if self._allow_descendants:
                    _membership(item, self._scope, descendants=True)
                else:
                    _membership(item, self._scope)
            ids = tuple(sorted(item.pid for item in self._processes))
            read_members = (lambda: _tree_members(self._fd)) if self._allow_descendants else (
                lambda: _members(_read(self._fd, 'cgroup.procs')))
            _require(read_members() == ids)
            for item in self._processes: item.check()
            self._check_scope()
            _require(read_members() == ids)
        except Exception:
            raise ScopeEvidenceError('cgroup scope evidence unavailable') from None

    def observe_empty(self):
        try:
            self._check_scope()
            for item in self._processes:
                _require(item.fd is not None and bool(select.select([item.fd], [], [], 0)[0]))
            _require(_events(_read(self._fd, 'cgroup.events'))['populated'] == 0
                     and not _members(_read(self._fd, 'cgroup.procs')))
            self._check_scope()
            _require(_events(_read(self._fd, 'cgroup.events'))['populated'] == 0
                     and not _members(_read(self._fd, 'cgroup.procs'))
                     and (not self._allow_descendants or not _tree_members(self._fd)))
            now = time.monotonic()
            _require(math.isfinite(now) and now >= 0)
            return EmptyScopeObservation(self._boot, *self._identity, self._original, now)
        except Exception:
            raise ScopeEvidenceError('cgroup scope evidence unavailable') from None

    def close(self):
        for name in ('_fd', '_root'):
            fd = getattr(self, name, None)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)
