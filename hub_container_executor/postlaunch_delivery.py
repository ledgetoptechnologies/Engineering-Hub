"""Bounded native wait and revocable protected post-launch approval lease.

The operator publishes a separately signed record by atomic rename into the
preconfigured private, read-only-to-runner directory. No timer polling or
self-approval occurs. A missing, malformed, changed or revoked record fails
closed. An accepted Engine call cannot be cancelled by revoking the file.
"""
import ctypes
import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import struct
import sys
import time

from engineering_hub.unified_pilot_startup import read_protected_file
from .postlaunch_approval import MAX_BYTES, PostLaunchApprovalError, verify_postlaunch_approval

_EVENT = struct.Struct('iIII')
_MOVED_TO = 0x80
_CREATE = 0x100
_CLOSE_WRITE = 0x8
_INVALID = 0x400 | 0x800 | 0x4000 | 0x8000


@dataclass(frozen=True)
class ProtectedApprovalLease:
    path: str
    digest: bytes
    approval: object
    verifier: object
    release: object
    approver_public_key: bytes
    expected_engine_id: str

    def check(self):
        raw = read_protected_file(self.path, maximum_bytes=MAX_BYTES, private=True, require_readonly=True)
        if hashlib.sha256(raw).digest() != self.digest:
            raise PostLaunchApprovalError('Independent launch approval unavailable')
        accepted = verify_postlaunch_approval(raw, verifier=self.verifier, release=self.release,
            approver_public_key=self.approver_public_key, expected_engine_id=self.expected_engine_id)
        if accepted != self.approval:
            raise PostLaunchApprovalError('Independent launch approval unavailable')
        return accepted


async def wait_for_postlaunch_approval(path, *, verifier, release, approver_public_key, expected_engine_id, timeout_ms):
    """Subscribe before checking existence; wait only for the exact leaf event.

    The deadline begins after watch setup. Synchronous filesystem or signature
    work cannot be interrupted here; an overrun is rejected after it returns.
    """
    fd = None
    registered = False
    loop = asyncio.get_running_loop()
    try:
        if (sys.platform != 'linux' or type(path) is not str or not path.startswith('/')
                or os.path.normpath(path) != path or type(timeout_ms) is not int or not 100 <= timeout_ms <= 3600000):
            raise ValueError
        target = Path(path)
        if target.parent.resolve(strict=True) != target.parent:
            raise ValueError
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.inotify_init1
        init.argtypes, init.restype = [ctypes.c_int], ctypes.c_int
        add = libc.inotify_add_watch
        add.argtypes, add.restype = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32], ctypes.c_int
        fd = init(os.O_CLOEXEC | os.O_NONBLOCK)
        if fd < 0 or add(fd, os.fsencode(target.parent), _MOVED_TO | _CREATE | _CLOSE_WRITE | _INVALID) < 0:
            raise OSError('native approval watch unavailable')
        deadline = time.monotonic() + timeout_ms / 1000
        # Receipt expiry is independently enforced while waiting and while read.
        verifier.accepted_launch_receipt()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError
            try:
                os.lstat(target)
            except FileNotFoundError:
                pass
            else:
                raw = read_protected_file(path, maximum_bytes=MAX_BYTES, private=True, require_readonly=True)
                if time.monotonic() >= deadline:
                    raise TimeoutError
                approval = verify_postlaunch_approval(raw, verifier=verifier, release=release,
                    approver_public_key=approver_public_key, expected_engine_id=expected_engine_id)
                lease = ProtectedApprovalLease(path, hashlib.sha256(raw).digest(), approval,
                    verifier, release, approver_public_key, expected_engine_id)
                lease.check()
                if time.monotonic() >= deadline:
                    raise TimeoutError
                return lease
            remaining = min(deadline, verifier._deadline) - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            ready = loop.create_future()
            def notify():
                if not ready.done(): ready.set_result(None)
            loop.add_reader(fd, notify)
            registered = True
            try:
                await asyncio.wait_for(ready, timeout=remaining)
            finally:
                loop.remove_reader(fd)
                registered = False
            data = os.read(fd, 65536)
            offset = 0
            matched = False
            while offset < len(data):
                if len(data) - offset < _EVENT.size:
                    raise ValueError
                _wd, mask, _cookie, size = _EVENT.unpack_from(data, offset)
                offset += _EVENT.size
                if offset + size > len(data) or mask & _INVALID:
                    raise ValueError
                name = data[offset:offset + size].split(b'\0', 1)[0]
                offset += size
                if name == os.fsencode(target.name) and mask & (_MOVED_TO | _CREATE | _CLOSE_WRITE):
                    matched = True
            if not matched:
                verifier.accepted_launch_receipt()
    except Exception:
        raise PostLaunchApprovalError('Independent launch approval unavailable') from None
    finally:
        if registered:
            loop.remove_reader(fd)
        if fd is not None and fd >= 0:
            os.close(fd)
