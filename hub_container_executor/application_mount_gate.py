"""Explicit application gate component; no standalone launch authority.

The trusted supervisor must supply independently enrolled pins, key, connected
channel, token values and an owner-bound durable consumer, and must launch this
process with a safe environment. This module does not establish host isolation,
peer identity, atomic check-to-exec or continuing mount custody. The CLI remains
disabled.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import time

from .mount_gate_protocol import MAX_BYTES, IDENTITY_FIELDS, verify_decision

_TARGET = '/var/lib/hub'
_COMMAND = ('/opt/venv/bin/python', '-I', '-B', '-m', 'engineering_hub', '--host', '0.0.0.0', '--port', '8770')
_TOKEN_NAMES = frozenset({'CODEX_TOKEN', 'HERMES_TOKEN', 'CLAUDE_TOKEN'})


class ApplicationMountGateError(RuntimeError):
    def __init__(self):
        super().__init__('application mount gate unavailable')


def _flags(fd):
    flags = os.fstatvfs(fd).f_flag
    return sorted(name for name, bit in (('nodev', os.ST_NODEV), ('noexec', os.ST_NOEXEC),
        ('nosuid', os.ST_NOSUID)) if flags & bit) + ([] if flags & os.ST_RDONLY else ['rw'])


def run_gate(*, channel, public_key, expected, consume_challenge, application_tokens, timeout_seconds):
    """Measure, exchange once, consume and exec the fixed application command.

    All arguments are supplied by the trusted composition, never discovered
    from candidate environment or wire data. The caller retains channel ownership.
    The consumer must implement the durable transaction required by the protocol.
    A failed exchange or uncertain consumption never starts the application.
    """
    fd = None
    try:
        if type(application_tokens) is not dict:
            raise ValueError()
        tokens = dict(application_tokens)
        if (os.name != 'posix' or not hasattr(socket, 'MSG_CMSG_CLOEXEC') or type(channel) is not socket.socket
                or channel.family != socket.AF_UNIX
                or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
                or type(expected) is not dict or set(expected) != IDENTITY_FIELDS
                or set(tokens) != _TOKEN_NAMES
                or any(type(value) is not str or not value or len(value) > 4096
                       or any(ord(char) < 33 or ord(char) > 126 for char in value)
                       for value in tokens.values())
                or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 5):
            raise ValueError()
        channel.set_inheritable(False)
        # Never inherit a caller-controlled loader, Python path, proxy or runtime
        # setting. Token values are explicit trusted inputs and are never logged.
        environment = {'PATH': '/opt/venv/bin:/usr/local/bin:/usr/bin:/bin',
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
            'OPENBLAS_NUM_THREADS': '1', 'HUB_UI_DIRECTORY': '/app/dashboard',
            'HUB_DB': '/var/lib/hub/hub.sqlite3', 'HUB_BASE_URL': 'http://127.0.0.1:8770',
            **tokens}
        # Snapshot the trusted input so an external mutation cannot change pins.
        pins = json.loads(json.dumps(expected, sort_keys=True, separators=(',', ':')))
        fd = os.open(_TARGET, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        measured = os.fstat(fd)
        named = os.stat(_TARGET, follow_symlinks=False)
        identity = (measured.st_dev, measured.st_ino)
        if (not stat.S_ISDIR(measured.st_mode) or not stat.S_ISDIR(named.st_mode)
                or identity != (named.st_dev, named.st_ino)
                or identity != (pins['expected_device'], pins['expected_inode'])
                or _flags(fd) != pins['mount_flags']):
            raise ValueError()
        started = time.monotonic()
        wall = int(time.time() * 1000)
        deadline = started + timeout_seconds
        observation = json.dumps({'protocol': 'engineering-hub.application-mount-observation.v1',
            'identity': pins, 'observed_device': measured.st_dev, 'observed_inode': measured.st_ino},
            sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
        if len(observation) > MAX_BYTES:
            raise ValueError()
        channel.settimeout(max(0, deadline - time.monotonic()))
        if channel.send(observation) != len(observation):
            raise ValueError()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError()
        channel.settimeout(remaining)
        raw, ancillary, flags, _ = channel.recvmsg(MAX_BYTES + 1, socket.CMSG_SPACE(256), socket.MSG_CMSG_CLOEXEC)
        # No ancillary descriptors are accepted. Close any received rights.
        import array
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                received = array.array('i'); received.frombytes(data[:len(data) - len(data) % received.itemsize])
                for item in received:
                    try: os.close(item)
                    except OSError: pass
        if ancillary or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or not raw or len(raw) > MAX_BYTES:
            raise ValueError()
        named = os.stat(_TARGET, follow_symlinks=False)
        current = os.fstat(fd)
        if (identity != (named.st_dev, named.st_ino) or identity != (current.st_dev, current.st_ino)
                or _flags(fd) != pins['mount_flags'] or time.monotonic() >= deadline):
            raise ValueError()
        def consume_if_current(decision):
            current_name = os.stat(_TARGET, follow_symlinks=False)
            current_fd = os.fstat(fd)
            if (time.monotonic() >= deadline or identity != (current_name.st_dev, current_name.st_ino)
                    or identity != (current_fd.st_dev, current_fd.st_ino) or _flags(fd) != pins['mount_flags']):
                return False
            return consume_challenge(decision)
        verify_decision(raw, public_key=public_key, expected=pins, exchange_wall_ms=wall,
                        exchange_monotonic=started, consume_challenge=consume_if_current)
        named = os.stat(_TARGET, follow_symlinks=False)
        if (time.monotonic() >= deadline or identity != (named.st_dev, named.st_ino)
                or identity != (os.fstat(fd).st_dev, os.fstat(fd).st_ino) or _flags(fd) != pins['mount_flags']):
            raise ValueError()
        # The descriptor is retained through the final check, but the app has
        # no descriptor-based storage interface. CLOEXEC does not prove custody.
        os.execve(_COMMAND[0], _COMMAND, environment)
        raise ValueError()
    except Exception:
        raise ApplicationMountGateError() from None
    finally:
        if fd is not None:
            os.close(fd)


def main():
    # There is no installed trusted supervisor/enrollment composition yet.
    raise SystemExit('application mount gate unavailable')


if __name__ == '__main__':
    main()
