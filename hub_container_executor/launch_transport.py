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
import array
import math
import os
import re
import secrets
import socket
import stat
import struct
import sys
import threading
import time

from .runtime_identity import MAX_RECEIPT_BYTES, RuntimeIdentityError


REQUEST_PROTOCOL = "engineering-hub.executor-launch-request.v1"
COORDINATOR_REQUEST_PROTOCOL = "engineering-hub.coordinator-launch-request.v1"
CURRENT_REQUEST_PROTOCOL = "engineering-hub.executor-current-observation-request.v1"
GATE_MOUNT_REQUEST_PROTOCOL = "engineering-hub.executor-gate-mount-observation-request.v1"
GATE_LEDGER_REQUEST_PROTOCOL = "engineering-hub.executor-gate-ledger-handoff-request.v1"
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


def _validate_transport_options(socket_path, expected_issuer_uid, expected_issuer_gid):
    _require(sys.platform == 'linux' and hasattr(socket, 'SO_PEERCRED'))
    _require(type(socket_path) is str and socket_path.startswith('/')
             and '\x00' not in socket_path and os.path.normpath(socket_path) == socket_path
             and not socket_path.startswith('//') and len(os.fsencode(socket_path)) < 108)
    for identifier in (expected_issuer_uid, expected_issuer_gid):
        _require(type(identifier) is int and 0 <= identifier < 2**32 - 1)


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
            _validate_transport_options(socket_path, expected_issuer_uid, expected_issuer_gid)
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
        return self._issue_receipt(nonce=nonce, timeout_ms=timeout_ms, protocol=REQUEST_PROTOCOL)

    def _issue_receipt(self, *, nonce, timeout_ms, protocol):
        try:
            _require(protocol in (REQUEST_PROTOCOL, COORDINATOR_REQUEST_PROTOCOL))
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
            request = _canonical({"protocol": protocol, "nonce": nonce})
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

    def issue_pair_enrollment(self, *, role, authority_public_key, own_enrollment, manifest_digest, timeout_ms):
        """Fetch one independently signed, nonce-bound pair enrollment; no retry."""
        from .handoff_identity import PAIR_ENROLLMENT_REQUEST, verify_pair_enrollment
        try:
            _require(os.getpid() == self._pid and role in {'executor', 'coordinator'}
                     and type(timeout_ms) is int and 100 <= timeout_ms <= 10000)
            start, wall = time.monotonic(), int(time.time() * 1000)
            deadline = start + timeout_ms / 1000
            nonce = secrets.token_hex(32)
            with self._lock:
                _require(not self._used and not self._closed and os.getpid() == self._pid)
                self._used = True
            request = _canonical({'protocol': PAIR_ENROLLMENT_REQUEST, 'role': role, 'nonce': nonce})
            _require(len(request) <= MAX_REQUEST_BYTES)
            identity = _socket_leaf(self._path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(_remaining(deadline))
                connection.connect(self._path)
                pid, uid, gid = _CREDENTIALS.unpack(connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, _CREDENTIALS.size))
                _require(pid >= 0 and uid == self._uid and gid == self._gid
                         and _socket_leaf(self._path) == identity)
                connection.settimeout(_remaining(deadline))
                connection.sendall(_LENGTH.pack(len(request)) + request)
                connection.shutdown(socket.SHUT_WR)
                size = _LENGTH.unpack(_read_exact(connection, _LENGTH.size, deadline))[0]
                _require(0 < size <= MAX_RECEIPT_BYTES)
                raw = _receipt_frame(_read_exact(connection, size, deadline))
                connection.settimeout(_remaining(deadline))
                _require(connection.recv(1) == b'' and _socket_leaf(self._path) == identity)
                _remaining(deadline)
                result = verify_pair_enrollment(raw, authority_public_key=authority_public_key, role=role,
                    nonce=nonce, manifest_digest=manifest_digest, own_enrollment=own_enrollment,
                    start_monotonic=start, start_wall_ms=wall)
                _remaining(deadline)
                return result
        except Exception:
            raise RuntimeIdentityError(_ERROR) from None

    def issue_verified_channel(self, *, enrollment, manifest_digest,
                               max_receipt_lifetime_seconds, timeout_ms, role='executor'):
        """One gated role-specific channel exchange; return only a verified owned FD.

        Every received descriptor is closed on failure. A sent challenge is
        never retried, including on lost reply. Caller owns the returned FD.
        The receipt authenticates only this delivered endpoint; it does not
        authenticate its opposite endpoint, its owner, or exclusive custody,
        and is not proof of a paired launch or state isolation.
        """
        from .handoff_identity import (REQUEST_PROTOCOL as HANDOFF_REQUEST,
                                       COORDINATOR_REQUEST_PROTOCOL, verify_channel_receipt, verify_channel_commit)
        fds = []
        try:
            _require(os.getpid() == self._pid and type(timeout_ms) is int and 100 <= timeout_ms <= 10000
                     and role in {'executor', 'coordinator'} and hasattr(socket, 'SCM_RIGHTS') and hasattr(socket, 'MSG_CMSG_CLOEXEC'))
            started = time.monotonic()
            wall_started = int(time.time() * 1000)
            deadline = started + timeout_ms / 1000
            nonce = secrets.token_hex(32)
            _require(type(nonce) is str and _NONCE.fullmatch(nonce))
            with self._lock:
                _require(os.getpid() == self._pid and not self._used and not self._closed)
                self._used = True
            request = _canonical({'protocol': HANDOFF_REQUEST if role == 'executor' else COORDINATOR_REQUEST_PROTOCOL,
                                  'nonce': nonce})
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
                result = bytearray()
                while True:
                    connection.settimeout(_remaining(deadline))
                    part, controls, flags, _ = connection.recvmsg(
                        MAX_RECEIPT_BYTES + _LENGTH.size + 1,
                        socket.CMSG_SPACE(64 * array.array('i').itemsize), socket.MSG_CMSG_CLOEXEC)
                    bad = bool(flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC))
                    for level, kind, data in controls:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            received = array.array('i')
                            received.frombytes(data[:len(data) - len(data) % received.itemsize])
                            if result or not part or fds:
                                bad = True  # Rights belong only to first reply bytes.
                            fds.extend(received)
                            if len(data) % received.itemsize:
                                bad = True
                        else:
                            bad = True
                    _require(not bad and len(fds) <= 1)
                    if not part:
                        break
                    result.extend(part)
                    _require(len(result) <= 2 * (MAX_RECEIPT_BYTES + _LENGTH.size))
                _require(len(fds) == 1 and len(result) >= 2 * _LENGTH.size)
                size = _LENGTH.unpack(result[:_LENGTH.size])[0]
                _require(0 < size <= MAX_RECEIPT_BYTES and len(result) >= size + 2 * _LENGTH.size)
                end = _LENGTH.size + size
                raw = _receipt_frame(bytes(result[_LENGTH.size:end]))
                commit_size = _LENGTH.unpack(result[end:end + _LENGTH.size])[0]
                _require(0 < commit_size <= MAX_RECEIPT_BYTES and len(result) == end + _LENGTH.size + commit_size)
                commit = _receipt_frame(bytes(result[end + _LENGTH.size:]))
                _require(os.getpid() == self._pid and _socket_leaf(self._path) == identity)
                _remaining(deadline)
                launch_id = verify_channel_receipt(raw, fds[0], enrollment=enrollment, nonce=nonce,
                    manifest_digest=manifest_digest, start_monotonic=started,
                    start_wall_ms=wall_started, max_lifetime_seconds=max_receipt_lifetime_seconds, role=role)
                verify_channel_commit(commit, raw, enrollment=enrollment, nonce=nonce, launch_id=launch_id, role=role)
                _remaining(deadline)
                return fds.pop()
        except Exception:
            raise RuntimeIdentityError(_ERROR) from None
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def issue_verified_pair_channel(self, *, role, pair_id, manifest_digest,
                                    executor_launch_id, coordinator_launch_id, executor_enrollment,
                                    coordinator_enrollment, pair_authority_public_key,
                                    max_receipt_lifetime_seconds, timeout_ms, _return_identity=False, _prior_nonce=None):
        """Quarantine the FD until both role receipts and the pair commit verify.

        Pins must be independently enrolled. This API does not establish actual
        process or opposite-FD custody, state isolation, or launch authorization.
        A failed or ambiguous exchange is one-shot and closes all received FDs.
        """
        from .handoff_identity import (PAIR_EXECUTOR_REQUEST_PROTOCOL,
                                       PAIR_COORDINATOR_REQUEST_PROTOCOL, verify_pair_channel_receipts)
        fds = []
        try:
            _require(type(_return_identity) is bool and (not _return_identity or role == 'executor')
                     and os.getpid() == self._pid and type(timeout_ms) is int and 100 <= timeout_ms <= 10000
                     and role in {'executor', 'coordinator'} and hasattr(socket, 'SCM_RIGHTS') and hasattr(socket, 'MSG_CMSG_CLOEXEC'))
            started = time.monotonic()
            wall_started = int(time.time() * 1000)
            deadline = started + timeout_ms / 1000
            nonce = secrets.token_hex(32)
            _require(type(nonce) is str and _NONCE.fullmatch(nonce) and nonce != _prior_nonce)
            with self._lock:
                _require(os.getpid() == self._pid and not self._used and not self._closed)
                self._used = True
            request = _canonical({'protocol': PAIR_EXECUTOR_REQUEST_PROTOCOL if role == 'executor' else PAIR_COORDINATOR_REQUEST_PROTOCOL,
                                  'nonce': nonce})
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
                result = bytearray()
                while True:
                    connection.settimeout(_remaining(deadline))
                    part, controls, flags, _ = connection.recvmsg(
                        MAX_RECEIPT_BYTES + _LENGTH.size + 1,
                        socket.CMSG_SPACE(64 * array.array('i').itemsize), socket.MSG_CMSG_CLOEXEC)
                    bad = bool(flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC))
                    for level, kind, data in controls:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            received = array.array('i')
                            received.frombytes(data[:len(data) - len(data) % received.itemsize])
                            if result or not part or fds:
                                bad = True  # Rights belong only to first reply bytes.
                            fds.extend(received)
                            if len(data) % received.itemsize:
                                bad = True
                        else:
                            bad = True
                    _require(not bad and len(fds) <= 1)
                    if not part:
                        break
                    result.extend(part)
                    _require(len(result) <= 2 * (MAX_RECEIPT_BYTES + _LENGTH.size))
                _require(len(fds) == 1 and len(result) >= 2 * _LENGTH.size)
                size = _LENGTH.unpack(result[:_LENGTH.size])[0]
                _require(0 < size <= MAX_RECEIPT_BYTES and len(result) >= size + 2 * _LENGTH.size)
                end = _LENGTH.size + size
                raw = _receipt_frame(bytes(result[_LENGTH.size:end]))
                commit_size = _LENGTH.unpack(result[end:end + _LENGTH.size])[0]
                _require(0 < commit_size <= MAX_RECEIPT_BYTES and len(result) == end + _LENGTH.size + commit_size)
                commit = _receipt_frame(bytes(result[end + _LENGTH.size:]))
                _require(os.getpid() == self._pid and _socket_leaf(self._path) == identity)
                _remaining(deadline)
                verifier = verify_pair_channel_receipts(raw, commit, fds[0], role=role, nonce=nonce, pair_id=pair_id,
                    manifest_digest=manifest_digest, executor_launch_id=executor_launch_id,
                    coordinator_launch_id=coordinator_launch_id, executor_enrollment=executor_enrollment,
                    coordinator_enrollment=coordinator_enrollment, pair_authority_public_key=pair_authority_public_key,
                    start_monotonic=started, start_wall_ms=wall_started,
                    max_lifetime_seconds=max_receipt_lifetime_seconds)
                _remaining(deadline)
                if _return_identity:
                    return fds.pop(), verifier
                verifier.close()
                return fds.pop()
        except Exception:
            raise RuntimeIdentityError(_ERROR) from None
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def issue_verified_pair_channel_with_identity(self, **kwargs):
        """Return a verified executor FD and its independently signed driver identity."""
        if kwargs.get('role') != 'executor':
            raise RuntimeIdentityError(_ERROR)
        return self.issue_verified_pair_channel(_return_identity=True, **kwargs)

    def issue_enrolled_pair_channel(self, *, enrollment, role, authority_public_key, own_enrollment,
                                    manifest_digest, max_receipt_lifetime_seconds, timeout_ms):
        """Use only a still-current independently verified pair enrollment."""
        from .handoff_identity import VerifiedPairEnrollment, verify_pair_enrollment
        _require(type(enrollment) is VerifiedPairEnrollment
                 and authority_public_key == enrollment.authority_public_key and enrollment.role == role)
        enrollment.check()
        verified = verify_pair_enrollment(enrollment.signed_envelope, authority_public_key=authority_public_key,
            role=role, nonce=enrollment.nonce, manifest_digest=manifest_digest, own_enrollment=own_enrollment,
            start_monotonic=enrollment.start_monotonic, start_wall_ms=enrollment.start_wall_ms)
        _require(verified.pair_id == enrollment.pair_id
                 and verified.executor_launch_id == enrollment.executor_launch_id
                 and verified.coordinator_launch_id == enrollment.coordinator_launch_id
                 and verified.executor_enrollment == enrollment.executor_enrollment
                 and verified.coordinator_enrollment == enrollment.coordinator_enrollment)
        result = self.issue_verified_pair_channel(role=role, pair_id=verified.pair_id,
            manifest_digest=verified.manifest_digest, executor_launch_id=verified.executor_launch_id,
            coordinator_launch_id=verified.coordinator_launch_id, executor_enrollment=verified.executor_enrollment,
            coordinator_enrollment=verified.coordinator_enrollment, pair_authority_public_key=authority_public_key,
            max_receipt_lifetime_seconds=max_receipt_lifetime_seconds, timeout_ms=timeout_ms,
            _return_identity=role == 'executor', _prior_nonce=enrollment.nonce)
        try:
            enrollment.check()
            return result
        except BaseException:
            if role == 'executor':
                fd, verifier = result
                try:
                    verifier.close()
                finally:
                    os.close(fd)
            else:
                os.close(result)
            raise

    def close(self):
        """Revoke future use promptly, without cancelling an admitted exchange.

        Each admitted exchange closes its own FD when it finishes or fails.
        """
        _require(os.getpid() == self._pid)
        with self._lock:
            self._closed = True


class CoordinatorUnixLaunchReceiptTransport:
    """One-shot direct coordinator request; no executor pair or gate methods."""

    def __init__(self, *, socket_path, expected_issuer_uid, expected_issuer_gid):
        self._transport = UnixLaunchReceiptTransport(socket_path=socket_path,
            expected_issuer_uid=expected_issuer_uid, expected_issuer_gid=expected_issuer_gid)

    def issue_receipt(self, *, nonce, timeout_ms):
        return self._transport._issue_receipt(nonce=nonce, timeout_ms=timeout_ms,
                                             protocol=COORDINATOR_REQUEST_PROTOCOL)

    def close(self):
        self._transport.close()


class UnixCurrentObservationTransport:
    """Fresh read-only exchanges over the SAME independently supplied issuer UDS.

    This is not the one-shot receipt transport. Each call has a new connection,
    no retry and no waiting admission queue. Peer metadata authenticates only
    the local transport; the Hub must verify the original independently pinned
    signing authority and launch. Closing denies future admission and does not
    abandon an admitted exchange. No socket, thread or service starts at init.
    """

    def __init__(self, *, socket_path, expected_issuer_uid, expected_issuer_gid):
        try:
            _validate_transport_options(socket_path, expected_issuer_uid, expected_issuer_gid)
            self._path, self._uid, self._gid = socket_path, expected_issuer_uid, expected_issuer_gid
            self._pid = os.getpid()
            self._lock = threading.Lock()
            self._closed = False
            self._busy = False
            self._ledger_used = False
        except Exception:
            raise RuntimeIdentityError('Current launch issuer exchange unavailable') from None

    def observe_current_launch(self, *, nonce, timeout_ms):
        return self._observe(nonce=nonce, timeout_ms=timeout_ms, protocol=CURRENT_REQUEST_PROTOCOL)

    def observe_gate_mount(self, *, nonce, timeout_ms):
        return self._observe(nonce=nonce, timeout_ms=timeout_ms, protocol=GATE_MOUNT_REQUEST_PROTOCOL)

    def issue_ledger_listener(self, *, verifier, manifest_digest, source_directory, timeout_ms):
        """Receive one signed listener; caller owns the returned descriptor.

        This one-shot exchange must be direct. Lost replies are uncertain and
        must not be retried; all unexpected received rights are closed.
        """
        from .gate_ledger_handoff import verify_handoff
        from engineering_hub.executor_launch_profile import MAX_PROFILE_BYTES, verify_profile_bytes
        fds, admitted = [], False
        try:
            _require(os.getpid() == self._pid and type(timeout_ms) is int and 100 <= timeout_ms <= 10000
                     and hasattr(socket, 'MSG_CMSG_CLOEXEC'))
            started, wall = time.monotonic(), int(time.time() * 1000)
            deadline = started + timeout_ms / 1000
            nonce = secrets.token_hex(32)
            _require(self._lock.acquire(blocking=False))
            try:
                _require(not self._closed and not self._busy and not self._ledger_used)
                self._ledger_used = True
                self._busy = admitted = True
            finally:
                self._lock.release()
            identity = _socket_leaf(self._path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(_remaining(deadline)); connection.connect(self._path)
                peer_pid, uid, gid = _CREDENTIALS.unpack(connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _CREDENTIALS.size))
                _require(peer_pid >= 0 and (uid, gid) == (self._uid, self._gid) and _socket_leaf(self._path) == identity)
                request = _canonical({'protocol': GATE_LEDGER_REQUEST_PROTOCOL, 'nonce': nonce})
                connection.sendall(_LENGTH.pack(len(request)) + request); connection.shutdown(socket.SHUT_WR)
                result = bytearray()
                while True:
                    connection.settimeout(_remaining(deadline))
                    part, controls, flags, _ = connection.recvmsg(65536,
                        socket.CMSG_SPACE(64 * array.array('i').itemsize), socket.MSG_CMSG_CLOEXEC)
                    bad = bool(flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC))
                    for level, kind, data in controls:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            received = array.array('i'); received.frombytes(data[:len(data) - len(data) % received.itemsize])
                            if result or not part or fds or len(data) % received.itemsize: bad = True
                            fds.extend(received)
                        else: bad = True
                    _require(not bad and len(fds) <= 1)
                    if not part: break
                    result.extend(part); _require(len(result) <= MAX_RECEIPT_BYTES + MAX_PROFILE_BYTES + 8)
                _require(len(fds) == 1 and len(result) >= 8)
                size = _LENGTH.unpack(result[:4])[0]
                end = size + 4
                _require(0 < size <= MAX_RECEIPT_BYTES and len(result) >= end + 4
                         and _socket_leaf(self._path) == identity)
                profile_size = _LENGTH.unpack(result[end:end + 4])[0]
                _require(0 < profile_size <= MAX_PROFILE_BYTES and len(result) == end + 4 + profile_size)
                raw = _receipt_frame(bytes(result[4:end]))
                profile_bytes = bytes(result[end + 4:])
                verify_handoff(raw, fds[0], verifier=verifier, nonce=nonce, manifest_digest=manifest_digest,
                               source_directory=source_directory, start_wall_ms=wall, start_monotonic=started)
                profile = verify_profile_bytes(profile_bytes, verifier.launch_profile_digest())
                _require(profile.gate_ledger_channel == 'supervisor-listener-v1' and profile.gate_completion is not None)
                mount = next((m for m in profile.mounts if m.role == 'gate_channel_directory'), None)
                _require(mount is not None and mount.source == mount.target == source_directory and mount.read_only)
                _remaining(deadline)
                return fds.pop(), raw, nonce, wall, started, profile_bytes
        except Exception:
            raise RuntimeIdentityError(_ERROR) from None
        finally:
            for fd in fds:
                try: os.close(fd)
                except OSError: pass
            if admitted:
                with self._lock: self._busy = False

    def _observe(self, *, nonce, timeout_ms, protocol):
        admitted = False
        try:
            _require(os.getpid() == self._pid)
            _require(type(nonce) is str and _NONCE.fullmatch(nonce)
                     and type(timeout_ms) is int and 1 <= timeout_ms <= 10000)
            started = time.monotonic()
            _require(type(started) in (int, float) and math.isfinite(started))
            deadline = started + timeout_ms / 1000
            # Never queue behind an exchange or inherit a locked parent mutex.
            _require(self._lock.acquire(blocking=False))
            try:
                _require(not self._closed and not self._busy)
                self._busy = admitted = True
            finally:
                self._lock.release()
            request = _canonical({'protocol': protocol, 'nonce': nonce})
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
                from engineering_hub.current_executor_observation import MAX_OBSERVATION_BYTES
                _require(0 < size <= MAX_OBSERVATION_BYTES)
                response = _receipt_frame(_read_exact(connection, size, deadline))
                connection.settimeout(_remaining(deadline))
                _require(connection.recv(1) == b'')
                _require(os.getpid() == self._pid and _socket_leaf(self._path) == identity)
                _remaining(deadline)
                return response
        except Exception:
            raise RuntimeIdentityError('Current launch issuer exchange unavailable') from None
        finally:
            if admitted:
                with self._lock:
                    self._busy = False

    def close(self):
        _require(os.getpid() == self._pid)
        with self._lock:
            self._closed = True
