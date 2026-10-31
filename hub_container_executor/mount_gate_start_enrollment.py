"""One-shot signed static-gate start enrollment; no launch or host authority.

The signing key must be independently enrolled and its public key baked into the
verified bootstrap. The caller must independently measure the bootstrap digest,
peer, live mount/custodian and ledger owner. This protocol does not prove them.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import re
import socket
import threading
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .broker_seqpacket import PeerPin, _parse
from .mount_gate_controller import _read_observation
from .mount_gate_protocol import (AUDIENCE as DECISION_AUDIENCE, IDENTITY_FIELDS, MAX_BYTES,
                                  PROTOCOL as DECISION_PROTOCOL, _canonical, _payload)

REQUEST = 'engineering-hub.application-gate-start-request.v1'
PROTOCOL = 'engineering-hub.application-gate-start-enrollment.v1'
AUDIENCE = 'engineering-hub.static-application-bootstrap'
DOMAIN = b'engineering-hub-application-gate-start-enrollment-v1\x00'
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
_NONCE = re.compile(r'[0-9a-f]{64}\Z')


class StartEnrollmentError(RuntimeError):
    def __init__(self):
        super().__init__('application gate start enrollment unavailable')


def _identity(value):
    if type(value) is not dict or set(value) != IDENTITY_FIELDS:
        raise ValueError()
    # Reuse the closed decision schema; the signed decision is a later phase.
    _payload(dict(value, protocol=DECISION_PROTOCOL, audience=DECISION_AUDIENCE,
                  decision='allow', observed_device=value['expected_device'],
                  observed_inode=value['expected_inode'], issued_at_ms=1, expires_at_ms=2))


class MountGateStartEnrollmentController:
    """One signed enrollment on the retained peer channel; delivery is uncertain.

    `verify_live` must independently establish all launch and custody facts.
    `reserve_start` must durably reserve the nonce and exact signed bytes before
    return. `hold_uncertain` must hold any possibly committed reservation.
    They are trusted, bounded synchronous callbacks, not verified by this class.
    """
    def __init__(self, *, channel, peer, expected_without_nonce, bootstrap_digest,
                 signing_key, enrolled_public_key, verify_live, reserve_start,
                 hold_uncertain, timeout_seconds):
        try:
            if (not hasattr(socket, 'MSG_CMSG_CLOEXEC') or type(channel) is not socket.socket
                    or channel.family != socket.AF_UNIX
                    or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
                    or channel.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED) != 1
                    or type(peer) is not PeerPin or type(expected_without_nonce) is not dict
                    or set(expected_without_nonce) != IDENTITY_FIELDS - {'nonce'}
                    or type(bootstrap_digest) is not str or not _SHA.fullmatch(bootstrap_digest)
                    or not isinstance(signing_key, Ed25519PrivateKey)
                    or type(enrolled_public_key) is not bytes or len(enrolled_public_key) != 32
                    or any(not callable(v) or inspect.iscoroutinefunction(v)
                           or inspect.iscoroutinefunction(getattr(v, '__call__', None))
                           for v in (verify_live, reserve_start, hold_uncertain))
                    or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                    or not 0 < timeout_seconds <= 5):
                raise ValueError()
            public = signing_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            if public != enrolled_public_key:
                raise ValueError()
            self._base = json.loads(_canonical(expected_without_nonce))
            _identity(dict(self._base, nonce='0' * 64))
            if self._base['host_pid'] != peer.pid or self._base['process_start_ticks'] != peer.start:
                raise ValueError()
            self._channel, self._peer, self._key, self._digest = channel, peer, signing_key, bootstrap_digest
            self._live, self._reserve, self._hold, self._timeout = verify_live, reserve_start, hold_uncertain, timeout_seconds
            self._pid, self._used, self._lock = os.getpid(), False, threading.Lock()
        except Exception:
            raise StartEnrollmentError() from None

    def serve_once(self):
        if os.getpid() != self._pid:
            raise StartEnrollmentError()
        with self._lock:
            if self._used:
                raise StartEnrollmentError()
            self._used = True
        deadline = time.monotonic() + self._timeout
        attempted = sent = False
        pins = None
        def scope():
            if os.getpid() != self._pid or time.monotonic() >= deadline:
                raise ValueError()
            self._peer.check()
        try:
            scope(); self._channel.settimeout(deadline - time.monotonic())
            raw, credentials = _read_observation(self._channel)
            request = _parse(raw)
            if (credentials != [(self._peer.pid, self._peer.uid, self._peer.gid)]
                    or set(request) != {'protocol', 'nonce'} or request['protocol'] != REQUEST
                    or type(request['nonce']) is not str or not _NONCE.fullmatch(request['nonce'])):
                raise ValueError()
            pins = dict(self._base, nonce=request['nonce']); _identity(pins)
            scope()
            if self._live(json.loads(_canonical(pins))) is not True:
                raise ValueError()
            scope(); now = int(time.time() * 1000)
            ttl = min(5000, int((deadline - time.monotonic()) * 1000))
            if now <= 0 or ttl <= 0:
                raise ValueError()
            payload = {'protocol': PROTOCOL, 'audience': AUDIENCE, 'identity': pins,
                       'bootstrap_digest': self._digest, 'issued_at_ms': now, 'expires_at_ms': now + ttl}
            raw_payload = _canonical(payload)
            wire = _canonical({'payload': payload, 'signature': self._key.sign(DOMAIN + raw_payload).hex()})
            if len(wire) > MAX_BYTES:
                raise ValueError()
            attempted = True
            if self._reserve(json.loads(_canonical(payload)), hashlib.sha256(wire).hexdigest()) is not True:
                raise ValueError()
            scope()
            if self._live(json.loads(_canonical(pins))) is not True:
                raise ValueError()
            scope()
            if not now <= int(time.time() * 1000) < payload['expires_at_ms']:
                raise ValueError()
            self._channel.settimeout(deadline - time.monotonic())
            if self._channel.send(wire) != len(wire):
                raise ValueError()
            sent = True
            return {'identity': dict(pins), 'delivery': 'sent_unconfirmed'}
        except Exception:
            raise StartEnrollmentError() from None
        finally:
            if attempted and not sent:
                try: self._hold(pins)
                except Exception: pass


def verify_start_enrollment(raw, *, public_key, nonce, measured_bootstrap_digest,
                            exchange_wall_ms, exchange_monotonic,
                            wall_clock=time.time, monotonic=time.monotonic):
    """Verify a signed enrollment, not mount custody or permission to exec.

    The public key must be independently embedded in the measured executable;
    the digest must be measured from that executable, not supplied by the peer.
    This verifier is stateless: the bootstrap must generate an unpredictable
    fresh CSPRNG nonce per attempt and enforce a restart-safe one-use transition
    through independently owned durable authority. Successful verification alone
    is not replay prevention, decision consumption or permission to exec.
    """
    try:
        if (type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES
                or not isinstance(public_key, Ed25519PublicKey)
                or type(nonce) is not str or not _NONCE.fullmatch(nonce)
                or type(measured_bootstrap_digest) is not str or not _SHA.fullmatch(measured_bootstrap_digest)
                or type(exchange_wall_ms) is not int or exchange_wall_ms <= 0
                or type(exchange_monotonic) not in (int, float) or not math.isfinite(exchange_monotonic)):
            raise ValueError()
        envelope = _parse(raw)
        if set(envelope) != {'payload', 'signature'}:
            raise ValueError()
        value, signature = envelope['payload'], envelope['signature']
        if (type(value) is not dict or set(value) != {'protocol', 'audience', 'identity',
                'bootstrap_digest', 'issued_at_ms', 'expires_at_ms'}
                or type(signature) is not str or not re.fullmatch(r'[0-9a-f]{128}', signature)):
            raise ValueError()
        public_key.verify(bytes.fromhex(signature), DOMAIN + _canonical(value))
        pins = value['identity']; _identity(pins)
        issued, expires = value['issued_at_ms'], value['expires_at_ms']
        wall, tick = wall_clock(), monotonic()
        if (value['protocol'] != PROTOCOL or value['audience'] != AUDIENCE or pins['nonce'] != nonce
                or value['bootstrap_digest'] != measured_bootstrap_digest
                or type(issued) is not int or type(expires) is not int or not 0 < expires - issued <= 5000
                or type(wall) not in (int, float) or type(tick) not in (int, float)
                or not math.isfinite(wall) or not math.isfinite(tick)
                or wall * 1000 < exchange_wall_ms or tick < exchange_monotonic
                or not exchange_wall_ms - 5000 <= issued <= wall * 1000 < expires
                or (tick - exchange_monotonic) * 1000 >= expires - exchange_wall_ms):
            raise ValueError()
        return json.loads(_canonical(pins))
    except Exception:
        raise StartEnrollmentError() from None
