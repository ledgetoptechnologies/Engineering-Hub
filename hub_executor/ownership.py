"""Linux fixture ownership; kernel locks survive neither crash nor fd close.

These locks protect one local SQLite executor, not distributed host leases.
Lock paths are operator-configured private state, never request parameters.
"""
import fcntl
import os
from contextlib import contextmanager
from functools import wraps
from pathlib import Path


class OwnerBusy(RuntimeError):
    def __init__(self):
        super().__init__('fixture executor owner is active')


@contextmanager
def exclusive_owner(path):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OwnerBusy() from None
        yield
    finally:
        os.close(descriptor)  # Release only this open file description's lock.


def guard_initialization(function):
    @wraps(function)
    def initialize(self, database_path, *args, **kwargs):
        path = Path(database_path)
        with exclusive_owner(path.with_name(path.name + '.execution.lock')):
            return function(self, database_path, *args, **kwargs)
    return initialize
