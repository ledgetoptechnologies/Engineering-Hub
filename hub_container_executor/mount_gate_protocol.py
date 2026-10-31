"""Closed mount-gate decision wire format, not enrollment or a launcher.

The issuer must independently verify the live mount and custodian. The receiver
must independently pin every identity and supply a durable atomic challenge
consumer. Neither a signature nor this module proves peer or host isolation.
"""
from __future__ import annotations

import json
import math
import re
import time
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

DOMAIN = b'engineering-hub.mount-gate-decision.v1\x00'
PROTOCOL = 'engineering-hub.mount-gate-decision.v1'
AUDIENCE = 'engineering-hub.application-bootstrap'
MAX_BYTES = 8192
MAX_LIFETIME_MS = 5000
IDENTITY_FIELDS = frozenset({'nonce', 'launch_id', 'operation_id', 'run_id', 'fence',
    'manifest_digest', 'image_config_id', 'generation_id', 'expected_device', 'expected_inode',
    'boot_id', 'container_id', 'host_pid', 'process_start_ticks', 'mount_target', 'mount_flags',
    'runnable_digest', 'platform_manifest_digest', 'implementation_commit'})
FIELDS = IDENTITY_FIELDS | {'protocol', 'audience', 'observed_device', 'observed_inode',
                            'issued_at_ms', 'expires_at_ms', 'decision'}
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
_GENERATION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')


class MountGateProtocolError(ValueError):
    def __init__(self):
        super().__init__('mount gate decision unavailable')


def _require(condition):
    if not condition:
        raise MountGateProtocolError()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _payload(value):
    _require(type(value) is dict and set(value) == FIELDS)
    _require(value['protocol'] == PROTOCOL and value['audience'] == AUDIENCE
             and value['decision'] == 'allow')
    _require(type(value['nonce']) is str and _HEX.fullmatch(value['nonce']) is not None)
    _require(all(_uuid(value[key]) for key in ('launch_id', 'operation_id', 'run_id', 'boot_id')))
    _require(type(value['container_id']) is str and _HEX.fullmatch(value['container_id']) is not None)
    _require(all(type(value[key]) is int and 1 <= value[key] < 2**63
                 for key in ('host_pid', 'process_start_ticks')))
    _require(all(type(value[key]) is str and _DIGEST.fullmatch(value[key]) is not None
                 for key in ('manifest_digest', 'image_config_id', 'runnable_digest', 'platform_manifest_digest')))
    _require(type(value['implementation_commit']) is str
             and re.fullmatch(r'[0-9a-f]{40}', value['implementation_commit']) is not None)
    _require(type(value['generation_id']) is str and _GENERATION.fullmatch(value['generation_id']) is not None)
    _require(value['mount_target'] == '/var/lib/hub' and type(value['mount_flags']) is list
             and all(type(flag) is str and flag in {'rw', 'nodev', 'nosuid', 'noexec'}
                     for flag in value['mount_flags'])
             and value['mount_flags'] == sorted(set(value['mount_flags'])) and 'rw' in value['mount_flags'])
    _require(type(value['fence']) is int and 1 <= value['fence'] < 2**63)
    for key in ('expected_device', 'observed_device'):
        _require(type(value[key]) is int and 0 <= value[key] < 2**64)
    for key in ('expected_inode', 'observed_inode'):
        _require(type(value[key]) is int and 1 <= value[key] < 2**64)
    _require(value['expected_device'] == value['observed_device']
             and value['expected_inode'] == value['observed_inode'])
    for key in ('issued_at_ms', 'expires_at_ms'):
        _require(type(value[key]) is int and 0 <= value[key] < 2**53)
    _require(0 < value['expires_at_ms'] - value['issued_at_ms'] <= MAX_LIFETIME_MS)
    return value


def issue_decision(payload, private_key):
    """Serialize one signed decision; the caller owns authority and journaling."""
    try:
        _require(type(private_key) is Ed25519PrivateKey or isinstance(private_key, Ed25519PrivateKey))
        _payload(payload)
        signature = private_key.sign(DOMAIN + _canonical(payload)).hex()
        raw = _canonical({'payload': payload, 'signature': signature})
        _require(len(raw) <= MAX_BYTES)
        return raw
    except Exception:
        raise MountGateProtocolError() from None


def verify_decision(raw, *, public_key, expected, exchange_wall_ms, exchange_monotonic,
                    consume_challenge, wall_clock=time.time, monotonic=time.monotonic):
    """Verify independent pins and atomically consume the challenge before return.

    ``consume_challenge`` is a trusted, bounded durable transaction supplied by
    the caller. It receives the entire verified decision and must atomically
    recheck the live owner, original fence and all enrolled identities, reject
    terminal/uncertain launches, and consume this exact challenge once. Return
    exactly True only after commit. A timeout or unknown commit is held, never
    retried. This synchronous function cannot interrupt a blocked transaction;
    the caller must bound and own it, and keep admission closed until it exits.
    Never roll consumption back if the final freshness check fails. The signed
    response is not proof of peer identity or current mount custody.
    """
    try:
        _require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES
                 and isinstance(public_key, Ed25519PublicKey)
                 and type(expected) is dict and set(expected) == IDENTITY_FIELDS
                 and type(exchange_wall_ms) is int and 0 <= exchange_wall_ms < 2**53
                 and type(exchange_monotonic) in (int, float) and math.isfinite(exchange_monotonic)
                 and callable(consume_challenge) and callable(wall_clock) and callable(monotonic))
        def unique(pairs):
            result = {}
            for key, value in pairs:
                _require(key not in result)
                result[key] = value
            return result
        parsed = json.loads(raw, object_pairs_hook=unique,
                            parse_constant=lambda _: _require(False))
        _require(type(parsed) is dict and set(parsed) == {'payload', 'signature'}
                 and _canonical(parsed) == raw)
        payload = _payload(parsed['payload'])
        signature = parsed['signature']
        _require(type(signature) is str and re.fullmatch(r'[0-9a-f]{128}', signature) is not None)
        public_key.verify(bytes.fromhex(signature), DOMAIN + _canonical(payload))
        _require(all(type(expected[k]) is type(payload[k]) and expected[k] == payload[k]
                     for k in IDENTITY_FIELDS))
        _require(payload['issued_at_ms'] >= exchange_wall_ms - MAX_LIFETIME_MS)
        previous = [exchange_wall_ms, exchange_monotonic]
        def fresh():
            wall, tick = wall_clock(), monotonic()
            _require(type(wall) in (int, float) and type(tick) in (int, float)
                     and math.isfinite(wall) and math.isfinite(tick) and tick >= exchange_monotonic)
            now = wall * 1000
            _require(now >= previous[0] and tick >= previous[1]
                     and payload['issued_at_ms'] <= now < payload['expires_at_ms']
                     and (tick - exchange_monotonic) * 1000 < payload['expires_at_ms'] - exchange_wall_ms)
            previous[:] = [now, tick]
        fresh()
        _require(consume_challenge(json.loads(_canonical(payload))) is True)
        fresh()
        return dict(payload)
    except Exception:
        raise MountGateProtocolError() from None
