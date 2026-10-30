"""Strict observer identity and descriptor-anchored credential checks."""
import os
from pathlib import Path
import stat

from .runtime_inventory import _secure_directory


def restricted_identity():
    try:
        if os.name != 'posix' or os.geteuid() <= 0:
            return False
        gid = os.getegid()
        return gid > 0 and all(group == gid for group in os.getgroups())
    except (AttributeError, OSError):
        return False


def read_observer_credential(path, owner_uid):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or not path.name:
        raise ValueError('credential_reference')
    directory = _secure_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) & 0o077
                    or info.st_size > 4096):
                raise ValueError('credential_protection')
            raw = os.read(fd, 4097)
            if len(raw) > 4096:
                raise ValueError('credential_invalid')
            token = raw.decode('utf-8').strip()
            if not token.isascii() or not 16 <= len(token) <= 4096 or '\n' in token or '\r' in token:
                raise ValueError('credential_invalid')
            return token
        finally:
            os.close(fd)
    finally:
        os.close(directory)
