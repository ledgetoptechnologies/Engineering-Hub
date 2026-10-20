"""Direct, one-shot Linux Unix-socket launch-receipt exchange.

Trusted composition supplies the socket path and issuer UID/GID as visible in
the CONSUMER's user namespace. An issuer outside its PID namespace can have peer
PID zero; this client neither equates that PID with a host PID nor relays through
another process. Peer credentials and stable leaf metadata are useful checks,
not release approval or filesystem isolation. The independent Ed25519 verifier
still authenticates receipt claims; the issuer must authenticate this consumer.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import stat
import struct
import sys
import threading
import time

from .runtime_identity import MAX_RECEIPT_BYTES, RuntimeIdentityError


REQUEST_PROTOCOL = "engineering-hub.executor-launch-request.v1"
MAX_REQUEST_BYTES = 256
_NONCE = re.compile(r"[0-9a-f]{64}\Z")
_LENGTH = struct.Struct("!I")
_CREDENTIALS = struct.Struct("=iII")
_ERROR = "Direct launch issuer exchange unavailable"


def _require(condition):
    if not condition:
        raise RuntimeIdentityError(_ERROR)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _receipt_frame(raw):
    # Bound framing and canonical syntax here; signed identity policy belongs to
    # LaunchReceiptIdentityVerifier, not the transport's socket credential check.
    def unique(pairs):
        value = {}
        for name, item in pairs:
            _require(name not in value)
            value[name] = item
        return value

    def reject(_):
        raise RuntimeIdentityError(_ERROR)

    value = json.loads(raw, object_pairs_hook=unique, parse_float=reject,
                       parse_constant=reject)
    _require(type(value) is dict and set(value) == {"payload", "signature"}
             and type(value["payload"]) is dict and type(value["signature"]) is str
             and _canonical(value) == raw)
    return raw


def _remaining(deadline):
    now = time.monotonic()
    _require(type(now) in (int, float) and math.isfinite(now) and now < deadline)
    return deadline - now


def _socket_leaf(path):
    info = os.lstat(path)
    _require(stat.S_ISSOCK(info.st_mode))  # Reject a symlink, not just its target.
    return info.st_dev, info.st_ino


def _read_exact(connection, size, deadline):
    result = bytearray()
    while len(result) < size:
        connection.settimeout(_remaining(deadline))
        fragment = connection.recv(size - len(result))
        _require(bool(fragment))
        result.extend(fragment)
    return bytes(result)


class UnixLaunchReceiptTransport:
    """One connection, owned and closed by this constructing consumer process.

    There is no environment discovery, retry or key/algorithm negotiation. Socket
    and parsing operations share one monotonic deadline. Blocking filesystem or
    scheduling delays cannot be interrupted here, but overruns are rejected.
    Each direction contains exactly one length-prefixed frame and then EOF: the
    client half-closes after its request and requires issuer EOF after its reply.
    Parent directories, mount identity and enrollment remain trusted composition
    responsibilities. The launcher can pin its newly created socket's inode;
    this client only compares the observed leaf before and after connection.
    """

    def __init__(self, *, socket_path, expected_issuer_uid, expected_issuer_gid):
        try:
            _require(sys.platform == "linux" and hasattr(socket, "SO_PEERCRED"))
            _require(type(socket_path) is str and socket_path.startswith("/")
                     and "\x00" not in socket_path and os.path.normpath(socket_path) == socket_path
                     and not socket_path.startswith("//") and len(os.fsencode(socket_path)) < 108)
            for identifier in (expected_issuer_uid, expected_issuer_gid):
                _require(type(identifier) is int and 0 <= identifier < 2**32 - 1)
            self._path = socket_path
            self._uid = expected_issuer_uid
            self._gid = expected_issuer_gid
            self._pid = os.getpid()
            self._lock = threading.Lock()
            self._used = False
            self._closed = False
        except Exception:
            raise RuntimeIdentityError(_ERROR) from None

    def issue_receipt(self, *, nonce, timeout_ms):
        try:
            # A child cannot wait on a lock inherited from another parent thread.
            _require(os.getpid() == self._pid)
            _require(type(nonce) is str and _NONCE.fullmatch(nonce)
                     and type(timeout_ms) is int and 100 <= timeout_ms <= 10000)
            started = time.monotonic()
            _require(type(started) in (int, float) and math.isfinite(started))
            deadline = started + timeout_ms / 1000
            with self._lock:
                _require(os.getpid() == self._pid and not self._used and not self._closed)
                self._used = True  # Unknown outcomes must never trigger replay.
            # Only admission is serialized. A duplicate call or close() must not
            # wait behind filesystem/socket I/O belonging to the admitted call.
            request = _canonical({"protocol": REQUEST_PROTOCOL, "nonce": nonce})
            _require(0 < len(request) <= MAX_REQUEST_BYTES)
            identity = _socket_leaf(self._path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(_remaining(deadline))
                connection.connect(self._path)
                peer_pid, uid, gid = _CREDENTIALS.unpack(connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, _CREDENTIALS.size))
                _require(peer_pid >= 0 and uid == self._uid and gid == self._gid
                         and _socket_leaf(self._path) == identity)
                connection.settimeout(_remaining(deadline))
                connection.sendall(_LENGTH.pack(len(request)) + request)
                connection.shutdown(socket.SHUT_WR)
                size = _LENGTH.unpack(_read_exact(connection, _LENGTH.size, deadline))[0]
                _require(0 < size <= MAX_RECEIPT_BYTES)
                response = _receipt_frame(_read_exact(connection, size, deadline))
                connection.settimeout(_remaining(deadline))
                _require(connection.recv(1) == b"")  # A second frame is not a valid reply.
                _require(os.getpid() == self._pid and _socket_leaf(self._path) == identity)
                _remaining(deadline)  # Parsing/metadata checks do not renew the deadline.
                return response
        except Exception:
            raise RuntimeIdentityError(_ERROR) from None

    def close(self):
        """Revoke future use promptly, without cancelling an admitted exchange.

        Each admitted exchange closes its own FD when it finishes or fails.
        """
        _require(os.getpid() == self._pid)
        with self._lock:
            self._closed = True
