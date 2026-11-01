"""One-shot durable decision-consumption acknowledgment; no service is enabled.

The caller must independently own the enrolled peer, current host/custodian and
ledger. A signed acknowledgment proves only what the trusted caller attests;
it does not establish storage anti-rollback or continuing mount custody.
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
from .mount_gate_protocol import IDENTITY_FIELDS, MAX_BYTES, _canonical, _payload, verify_decision

REQUEST = 'engineering-hub.application-gate-consume-request.v1'
PROTOCOL = 'engineering-hub.application-gate-consume-ack.v1'
AUDIENCE = 'engineering-hub.static-application-bootstrap'
DOMAIN = b'engineering-hub-application-gate-consume-ack-v1\x00'


class GateConsumeError(RuntimeError):
    def __init__(self):
        super().__init__('application gate consumption unavailable')


def _request(pins, digest):
    return {'protocol': REQUEST, 'nonce': pins['nonce'], 'launch_id': pins['launch_id'],
            'operation_id': pins['operation_id'], 'fence': pins['fence'], 'decision_sha256': digest}


class MountGateConsumeController:
    """Consume the exact decision once and send a signed, unconfirmed ack.

    Callbacks are trusted synchronous bounded owner operations. A callback that
    blocks cannot be cancelled here. Hold the operation until it has exited.
    """
    def __init__(self, *, channel, peer, expected, decision_wire, signing_key,
                 enrolled_public_key, consume_challenge, verify_live, hold_uncertain,
                 exchange_wall_ms, exchange_monotonic, timeout_seconds):
        try:
            if (not hasattr(socket, 'MSG_CMSG_CLOEXEC') or type(channel) is not socket.socket
                    or channel.family != socket.AF_UNIX
                    or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
                    or channel.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED) != 1
                    or type(peer) is not PeerPin or type(expected) is not dict or set(expected) != IDENTITY_FIELDS
                    or type(decision_wire) is not bytes or not 0 < len(decision_wire) <= MAX_BYTES
                    or not isinstance(signing_key, Ed25519PrivateKey)
                    or type(enrolled_public_key) is not bytes or len(enrolled_public_key) != 32
                    or any(not callable(x) or inspect.iscoroutinefunction(x)
                           or inspect.iscoroutinefunction(getattr(x, '__call__', None))
                           for x in (consume_challenge, verify_live, hold_uncertain))
                    or type(exchange_wall_ms) is not int or exchange_wall_ms <= 0
                    or type(exchange_monotonic) not in (int, float) or not math.isfinite(exchange_monotonic)
                    or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                    or not 0 < timeout_seconds <= 5):
                raise ValueError()
            public = signing_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            if public != enrolled_public_key:
                raise ValueError()
            pins = json.loads(_canonical(expected))
            envelope = _parse(decision_wire)
            if set(envelope) != {'payload', 'signature'}:
                raise ValueError()
            payload = _payload(envelope['payload'])
            if (any(type(pins[k]) is not type(payload[k]) or pins[k] != payload[k] for k in IDENTITY_FIELDS)
                    or pins['host_pid'] != peer.pid or pins['process_start_ticks'] != peer.start):
                raise ValueError()
            self._channel, self._peer, self._key = channel, peer, signing_key
            self._pins, self._wire, self._payload = pins, decision_wire, payload
            self._digest = hashlib.sha256(decision_wire).hexdigest()
            self._consume, self._live, self._hold = consume_challenge, verify_live, hold_uncertain
            self._wall, self._tick, self._timeout = exchange_wall_ms, exchange_monotonic, timeout_seconds
            self._pid, self._used, self._lock = os.getpid(), False, threading.Lock()
        except Exception:
            raise GateConsumeError() from None

    def serve_once(self):
        if os.getpid() != self._pid:
            raise GateConsumeError()
        with self._lock:
            if self._used:
                raise GateConsumeError()
            self._used = True
        deadline = time.monotonic() + self._timeout
        attempted = sent = False
        def scope():
            if os.getpid() != self._pid or time.monotonic() >= deadline:
                raise ValueError()
            self._peer.check()
        try:
            scope(); self._channel.settimeout(deadline - time.monotonic())
            raw, credentials = _read_observation(self._channel)
            request = _parse(raw)
            wanted = _request(self._pins, self._digest)
            if (credentials != [(self._peer.pid, self._peer.uid, self._peer.gid)]
                    or request.keys() != wanted.keys()
                    or any(type(request[k]) is not type(v) or request[k] != v
                           for k, v in wanted.items())):
                raise ValueError()
            scope()
            if self._live(json.loads(_canonical(self._pins))) is not True:
                raise ValueError()
            scope()
            def consume(decision):
                nonlocal attempted
                scope()
                if self._live(json.loads(_canonical(self._pins))) is not True:
                    return False
                scope(); attempted = True
                return self._consume(decision)
            verify_decision(self._wire, public_key=self._key.public_key(), expected=self._pins,
                            exchange_wall_ms=self._wall, exchange_monotonic=self._tick,
                            consume_challenge=consume)
            scope()
            if self._live(json.loads(_canonical(self._pins))) is not True:
                raise ValueError()
            scope()
            now = int(time.time() * 1000)
            if not self._payload['issued_at_ms'] <= now < self._payload['expires_at_ms']:
                raise ValueError()
            payload = dict(_request(self._pins, self._digest), protocol=PROTOCOL, audience=AUDIENCE,
                           expires_at_ms=self._payload['expires_at_ms'])
            wire = _canonical({'payload': payload, 'signature': self._key.sign(DOMAIN + _canonical(payload)).hex()})
            if len(wire) > MAX_BYTES:
                raise ValueError()
            self._channel.settimeout(deadline - time.monotonic())
            if self._channel.send(wire) != len(wire):
                raise ValueError()
            sent = True
            return 'sent_unconfirmed'
        except Exception:
            raise GateConsumeError() from None
        finally:
            if attempted and not sent:
                try: self._hold(json.loads(_canonical(self._pins)))
                except Exception: pass


def verify_consume_ack(raw, *, public_key, expected, decision_wire, exchange_wall_ms,
                       exchange_monotonic, wall_clock=time.time, monotonic=time.monotonic):
    """Verify one ack; stateless and not itself a replay or custody boundary."""
    try:
        if (type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES
                or not isinstance(public_key, Ed25519PublicKey) or type(expected) is not dict
                or set(expected) != IDENTITY_FIELDS or type(decision_wire) is not bytes
                or not 0 < len(decision_wire) <= MAX_BYTES or type(exchange_wall_ms) is not int
                or exchange_wall_ms <= 0 or type(exchange_monotonic) not in (int, float)
                or not math.isfinite(exchange_monotonic)):
            raise ValueError()
        original = _parse(decision_wire)
        if set(original) != {'payload', 'signature'}:
            raise ValueError()
        decision = _payload(original['payload'])
        # Verify both the original decision and the separate ack under the pinned key.
        sig = original['signature']
        if type(sig) is not str or not re.fullmatch(r'[0-9a-f]{128}', sig):
            raise ValueError()
        from .mount_gate_protocol import DOMAIN as DECISION_DOMAIN
        public_key.verify(bytes.fromhex(sig), DECISION_DOMAIN + _canonical(decision))
        if any(type(expected[k]) is not type(decision[k]) or expected[k] != decision[k] for k in IDENTITY_FIELDS):
            raise ValueError()
        envelope = _parse(raw)
        if set(envelope) != {'payload', 'signature'}:
            raise ValueError()
        payload, signature = envelope['payload'], envelope['signature']
        if (type(payload) is not dict or set(payload) != {'protocol', 'audience', 'nonce', 'launch_id',
                'operation_id', 'fence', 'decision_sha256', 'expires_at_ms'}
                or type(signature) is not str or not re.fullmatch(r'[0-9a-f]{128}', signature)):
            raise ValueError()
        public_key.verify(bytes.fromhex(signature), DOMAIN + _canonical(payload))
        wanted = dict(_request(expected, hashlib.sha256(decision_wire).hexdigest()), protocol=PROTOCOL,
                      audience=AUDIENCE, expires_at_ms=decision['expires_at_ms'])
        wall, tick = wall_clock(), monotonic()
        if (any(type(payload[k]) is not type(wanted[k]) or payload[k] != wanted[k] for k in wanted)
                or type(wall) not in (int, float) or type(tick) not in (int, float)
                or not math.isfinite(wall) or not math.isfinite(tick) or tick < exchange_monotonic
                or wall * 1000 < exchange_wall_ms or not decision['issued_at_ms'] <= wall * 1000 < decision['expires_at_ms']
                or (tick - exchange_monotonic) * 1000 >= decision['expires_at_ms'] - exchange_wall_ms):
            raise ValueError()
        return True
    except Exception:
        raise GateConsumeError() from None
