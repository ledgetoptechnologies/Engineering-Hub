"""Point-in-time local alias verification; does not establish daemon custody."""
import ctypes
import os
import stat
from pathlib import PurePosixPath

from scripts.hub_local_test_volume import OwnedVolume


class StateAlias:
    """Retain both path walks; caller rechecks before every dependent effect."""
    def __init__(self, alias, volume, *, uid, gid, mode=0o700):
        if os.geteuid() not in (0, 10001) or type(volume) is not OwnedVolume:
            raise ValueError('trusted local caller and owned volume required')
        if type(uid) is not int or type(gid) is not int or (uid, gid) != (10001, 10001) or type(mode) is not int or mode != 0o700:
            raise ValueError('invalid ownership or mode')
        self._paths = (self._canonical(alias), self._canonical(volume.mountpoint))
        a, b = map(PurePosixPath, self._paths)
        if b.name != '_data' or b.parent.name != volume.name or b.parent.parent.name != 'volumes':
            raise ValueError('invalid owned volume mountpoint')
        docker_root = b.parent.parent.parent
        if docker_root == PurePosixPath('/') or a == docker_root or docker_root in a.parents or a in docker_root.parents:
            raise ValueError('alias must be outside Docker root')
        if a == b or a in b.parents or b in a.parents:
            raise ValueError('alias paths must be disjoint')
        self._volume = volume
        self._expected = (uid, gid, mode)
        self._chains = []
        self._closed = False
        try:
            for path in self._paths:
                self._chains.append(self._open_chain(path))
            self._mount_ids = tuple(self._mount_id(chain[-1][1]) for chain in self._chains)
            self.check()
        except BaseException:
            self.close()
            raise

    @property
    def alias(self):
        return self._paths[0]

    @property
    def volume(self):
        return self._volume

    @staticmethod
    def _canonical(value):
        if (type(value) is not str or not value.startswith('/') or value == '/'
                or '\x00' in value or '//' in value or value.endswith('/')
                or any(p in ('.', '..') for p in value.split('/')[1:])):
            raise ValueError('canonical absolute path required')
        return value

    @staticmethod
    def _open_chain(path):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        chain = []
        try:
            fd = os.open('/', flags); chain.append((None, fd))
            for name in path.split('/')[1:]:
                fd = os.open(name, flags, dir_fd=fd); chain.append((name, fd))
            return chain
        except BaseException:
            for _, fd in reversed(chain):
                try: os.close(fd)
                except OSError: pass
            raise

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino

    @staticmethod
    def _mount_id(fd):
        with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as f:
            ids = [line.split(":", 1)[1].strip() for line in f if line.startswith("mnt_id:")]
        if len(ids) != 1 or not ids[0].isdigit():
            raise ValueError("mount identity unavailable")
        return int(ids[0])

    @staticmethod
    def _private_rw(mount_id):
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            entries = [line.split() for line in f if line.split()[0] == str(mount_id)]
        if len(entries) != 1 or "-" not in entries[0]:
            raise ValueError("mount metadata unavailable")
        row = entries[0]; sep = row.index("-")
        if len(row) <= sep + 3 or "rw" not in row[5].split(",") or any(x.startswith(("shared:", "master:", "propagate_from:")) or x == "unbindable" for x in row[6:sep]):
            raise ValueError("private writable alias required")

    def check(self):
        if self._closed or len(self._chains) != 2:
            raise ValueError('alias verifier closed')
        leaves = []
        for index, (path, chain) in enumerate(zip(self._paths, self._chains)):
            if self._mount_id(chain[-1][1]) != self._mount_ids[index]:
                raise ValueError("mount identity changed")
            name, _ = chain[-1]
            fresh = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=chain[-2][1])
            try:
                if self._mount_id(fresh) != self._mount_ids[index]:
                    raise ValueError("named mount identity changed")
            finally:
                os.close(fresh)
            if index == 0:
                self._private_rw(self._mount_ids[index])
            if self._identity(os.stat('/', follow_symlinks=False)) != self._identity(os.fstat(chain[0][1])):
                raise ValueError('root changed')
            for (name, fd), (_, parent) in zip(chain[1:], chain):
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                current = os.fstat(fd)
                if not stat.S_ISDIR(named.st_mode) or self._identity(named) != self._identity(current):
                    raise ValueError('alias path changed')
            leaf = os.fstat(chain[-1][1]); leaves.append(leaf)
            if (leaf.st_uid, leaf.st_gid, stat.S_IMODE(leaf.st_mode)) != self._expected:
                raise ValueError('alias ownership or mode changed')
            self._check_tmpfs(chain[-1][1])
        if self._identity(leaves[0]) != self._identity(leaves[1]):
            raise ValueError('paths do not name same directory')
        return self._identity(leaves[0])

    @staticmethod
    def _check_tmpfs(fd):
        buf = ctypes.create_string_buffer(256)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.fstatfs(fd, ctypes.byref(buf)) != 0 or ctypes.c_long.from_buffer(buf).value != 0x01021994:
            raise ValueError('tmpfs required')
        v = os.fstatvfs(fd)
        if v.f_flag & os.ST_RDONLY:
            raise ValueError('writable tmpfs required')
        if not (0 < v.f_frsize * v.f_blocks <= 512 * 1024 * 1024 and v.f_frsize * v.f_bavail >= 8 * 1024 * 1024
                and 0 < v.f_files <= 4096 and v.f_ffree >= 64):
            raise ValueError('tmpfs bounds unavailable')

    def close(self):
        if not self._closed:
            self._closed = True
            error = None
            for chain in reversed(self._chains):
                for _, fd in reversed(chain):
                    try: os.close(fd)
                    except OSError as exc: error = error or exc
            self._chains.clear()
            if error: raise error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
