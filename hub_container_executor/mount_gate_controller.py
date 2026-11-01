"""One-attempt mount-gate controller, disabled until trusted composition exists.

The caller owns independent enrollment, live host/custodian verification and a
durable issuance journal. This module does not manufacture those authorities or
prove exclusive descriptor custody. A successful send is not a delivery receipt.
"""
from __future__ import annotations

import hashlib
import inspect
import array
import json
import math
import os
import socket
import threading
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from .broker_seqpacket import PeerPin, _parse, _CREDS
from .mount_gate_protocol import (AUDIENCE, IDENTITY_FIELDS, MAX_BYTES, PROTOCOL,
                                  _canonical, issue_decision)


class MountGateControllerError(RuntimeError):
    def __init__(self):
        super().__init__('mount gate controller unavailable')


def _read_observation(channel):
    """Quarantine and close passed rights with close-on-exec set atomically."""
    raw, ancillary, flags, _ = channel.recvmsg(MAX_BYTES + 1,
        socket.CMSG_SPACE(_CREDS.size) + socket.CMSG_SPACE(64 * array.array('i').itemsize),
        socket.MSG_CMSG_CLOEXEC)
    credentials, invalid = [], False
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            rights = array.array('i')
            rights.frombytes(data[:len(data) - len(data) % rights.itemsize])
            invalid = True
            for fd in rights:
                try: os.close(fd)
                except OSError: pass
        elif level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(data) == _CREDS.size:
            credentials.append(_CREDS.unpack(data))
        else:
            invalid = True
    if invalid or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or not 0 < len(raw) <= MAX_BYTES:
        raise MountGateControllerError()
    return raw, credentials


class MountGateController:
    """One request on an already enrolled direct channel; caller owns resources.

    ``verify_live`` must independently verify full process, mount, exclusive
    custodian, owner and fence against the supplied pins, and return exactly
    True. ``reserve_issuance`` must durably and atomically recheck the owner,
    fence, identities and nonterminal launch while reserving the signed digest
    and challenge once. It returns True only after commit. ``hold_uncertain``
    durably holds any possibly committed reservation. The callbacks are trusted
    bounded synchronous operations; this class cannot cancel a blocked callback.
    The owner must keep the operation held until an uncertain callback exits.
    The caller must provision the key and peer independently and enforce direct
    channel custody; per-packet credentials cannot prevent later FD delegation.
    """

    def __init__(self, *, channel, peer, expected, signing_key, verify_live,
                 reserve_issuance, hold_uncertain, timeout_seconds):
        try:
            if (not hasattr(socket, 'MSG_CMSG_CLOEXEC') or type(channel) is not socket.socket or channel.family != socket.AF_UNIX
                    or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
                    or channel.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED) != 1
                    or type(peer) is not PeerPin or type(expected) is not dict
                    or set(expected) != IDENTITY_FIELDS or not isinstance(signing_key, Ed25519PrivateKey)
                    or any(not callable(x) or inspect.iscoroutinefunction(x)
                           or inspect.iscoroutinefunction(getattr(x, '__call__', None))
                           for x in (verify_live, reserve_issuance, hold_uncertain))
                    or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                    or not 0 < timeout_seconds <= 5):
                raise ValueError()
            self._expected = json.loads(_canonical(expected))
            if self._expected['host_pid'] != peer.pid or self._expected['process_start_ticks'] != peer.start:
                raise ValueError()
            self._channel, self._peer, self._key = channel, peer, signing_key
            self._verify_live, self._reserve, self._hold = verify_live, reserve_issuance, hold_uncertain
            self._timeout = timeout_seconds
            self._pid, self._used, self._lock = os.getpid(), False, threading.Lock()
        except Exception:
            raise MountGateControllerError() from None

    def serve_once(self):
        """Return ``sent_unconfirmed`` only after a durable reservation and send."""
        if os.getpid() != self._pid:
            raise MountGateControllerError()
        with self._lock:
            if self._used:
                raise MountGateControllerError()
            self._used = True
        attempted = delivered = False
        deadline = time.monotonic() + self._timeout
        def scope():
            if os.getpid() != self._pid or time.monotonic() >= deadline:
                raise ValueError()
            self._peer.check()
        try:
            scope()
            self._channel.settimeout(deadline - time.monotonic())
            raw, credentials = _read_observation(self._channel)
            if len(raw) > MAX_BYTES or credentials != [(self._peer.pid, self._peer.uid, self._peer.gid)]:
                raise ValueError()
            observation = _parse(raw)
            if (set(observation) != {'protocol', 'identity', 'observed_device', 'observed_inode'}
                    or observation['protocol'] != 'engineering-hub.application-mount-observation.v1'
                    or _canonical(observation['identity']) != _canonical(self._expected)
                    or type(observation['observed_device']) is not int
                    or type(observation['observed_inode']) is not int
                    or observation['observed_device'] != self._expected['expected_device']
                    or observation['observed_inode'] != self._expected['expected_inode']):
                raise ValueError()
            scope()
            if self._verify_live(json.loads(_canonical(self._expected))) is not True:
                raise ValueError()
            scope()
            wall = time.time()
            if not math.isfinite(wall) or wall <= 0:
                raise ValueError()
            issued = int(wall * 1000)
            ttl = min(5000, int((deadline - time.monotonic()) * 1000))
            if ttl <= 0:
                raise ValueError()
            decision = dict(self._expected, protocol=PROTOCOL, audience=AUDIENCE, decision='allow',
                observed_device=observation['observed_device'], observed_inode=observation['observed_inode'],
                issued_at_ms=issued, expires_at_ms=issued + ttl)
            wire = issue_decision(decision, self._key)
            attempted = True  # Callback failure may be a committed transaction.
            if self._reserve(json.loads(_canonical(decision)), hashlib.sha256(wire).hexdigest()) is not True:
                raise ValueError()
            scope()
            if self._verify_live(json.loads(_canonical(self._expected))) is not True:
                raise ValueError()
            scope()
            current_wall = time.time() * 1000
            if not math.isfinite(current_wall) or not issued <= current_wall < decision['expires_at_ms']:
                raise ValueError()
            self._channel.settimeout(deadline - time.monotonic())
            if self._channel.send(wire) != len(wire):
                raise ValueError()
            delivered = True
            return 'sent_unconfirmed'
        except Exception:
            raise MountGateControllerError() from None
        finally:
            if attempted and not delivered:
                try:
                    self._hold(json.loads(_canonical(self._expected)))
                except Exception:
                    # Reservation remains uncertain; never manufacture success.
                    pass
