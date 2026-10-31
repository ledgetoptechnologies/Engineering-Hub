"""Verify a provider's held-state report; never grant custody or release."""
import json
import math
import re
import time
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .state_custodian_admission import (HOLD_DOMAIN, HOLD_OBSERVATION, HOLD_DOMAIN_V2, HOLD_OBSERVATION_V2,
                                        HOLD_DOMAIN_V3, HOLD_OBSERVATION_V3, _canonical)

IDENTITY = frozenset({'target_id', 'operation_id', 'run_id', 'manifest_digest', 'admission_digest',
                      'fencing_generation', 'device', 'inode', 'host_uid', 'host_gid', 'owner_policy_digest'})
FIELDS = IDENTITY | {'protocol', 'nonce', 'marker_present', 'quiescence', 'issued_at_ms', 'expires_at_ms'}
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')


class HeldObservationError(RuntimeError):
    def __init__(self):
        super().__init__('custodian held observation unavailable')


def verify_held_observation(raw, *, provider_public_key, nonce, expected, start_wall_ms, start_monotonic):
    """Verify one response against independently enrolled pins and request start.

    The caller must establish the key and expected identity independently of the
    received report, generate a fresh nonce and track challenge consumption if
    one-shot handling is required. Result describes a held record only; it proves neither
    current file contents nor writer quiescence and never authorizes release.
    """
    try:
        if (type(raw) is not bytes or not 0 < len(raw) <= 8192
                or type(provider_public_key) is not bytes or len(provider_public_key) != 32
                or type(nonce) is not str or not _HEX.fullmatch(nonce)
                or type(expected) is not dict or set(expected) != IDENTITY
                or type(start_wall_ms) is not int or start_wall_ms <= 0
                or type(start_monotonic) not in (int, float) or not math.isfinite(start_monotonic)):
            raise ValueError()
        envelope = json.loads(raw)
        if (type(envelope) is not dict or set(envelope) != {'payload', 'signature'} or _canonical(envelope) != raw
                or type(envelope['payload']) is not dict
                or type(envelope['signature']) is not str or not re.fullmatch(r'[0-9a-f]{128}', envelope['signature'])):
            raise ValueError()
        p = envelope['payload']
        v3 = p.get('protocol') == HOLD_OBSERVATION_V3
        v2 = p.get('protocol') == HOLD_OBSERVATION_V2
        fields = FIELDS | ({'private_preparation_recorded', 'intent_state', 'preparation_state'} if v3 else
                           {'private_preparation_recorded'} if v2 else set())
        if (set(p) != fields or p['protocol'] not in (HOLD_OBSERVATION, HOLD_OBSERVATION_V2, HOLD_OBSERVATION_V3)
                or ((v2 or v3) and type(p['private_preparation_recorded']) is not bool)
                or (v3 and (p['intent_state'] not in ('absent', 'incomplete', 'valid')
                    or p['preparation_state'] not in ('absent', 'incomplete', 'valid')
                    or p['marker_present'] != (p['intent_state'] != 'absent')
                    or p['private_preparation_recorded'] != (p['preparation_state'] == 'valid')
                    or (p['intent_state'] != 'valid' and p['preparation_state'] != 'absent')))
                or p['nonce'] != nonce or p['quiescence'] != 'unknown'
                or type(p['marker_present']) is not bool or p['target_id'] != 'engineering-hub:staging'
                or any(type(p[k]) is not type(expected[k]) or p[k] != expected[k] for k in IDENTITY)
                or any(type(p[k]) is not int or p[k] < 0 or p[k] >= 2**63
                       for k in ('fencing_generation', 'device', 'inode', 'host_uid', 'host_gid'))
                or p['fencing_generation'] == 0 or p['inode'] == 0 or p['host_uid'] == 0 or p['host_gid'] == 0
                or any(type(p[k]) is not str or not _HEX.fullmatch(p[k]) for k in ('admission_digest', 'owner_policy_digest'))
                or type(p['manifest_digest']) is not str or not _SHA.fullmatch(p['manifest_digest'])
                or any(type(p[k]) is not str or str(UUID(p[k])) != p[k] for k in ('operation_id', 'run_id'))
                or type(p['issued_at_ms']) is not int or type(p['expires_at_ms']) is not int):
            raise ValueError()
        wall = time.time_ns() // 1_000_000; tick = time.monotonic()
        if (not start_wall_ms <= wall or not p['issued_at_ms'] <= wall < p['expires_at_ms']
                or not start_wall_ms <= p['issued_at_ms'] < p['expires_at_ms'] <= p['issued_at_ms'] + 5000
                or not start_monotonic <= tick or (tick - start_monotonic) * 1000 >= p['expires_at_ms'] - start_wall_ms):
            raise ValueError()
        Ed25519PublicKey.from_public_bytes(provider_public_key).verify(bytes.fromhex(envelope['signature']),
            (HOLD_DOMAIN_V3 if v3 else HOLD_DOMAIN_V2 if v2 else HOLD_DOMAIN) + _canonical(p))
        final_wall = time.time_ns() // 1_000_000; final_tick = time.monotonic()
        if (not wall <= final_wall < p['expires_at_ms'] or not tick <= final_tick
                or (final_tick - start_monotonic) * 1000 >= p['expires_at_ms'] - start_wall_ms):
            raise ValueError()
        return p.copy()
    except Exception:
        raise HeldObservationError() from None
