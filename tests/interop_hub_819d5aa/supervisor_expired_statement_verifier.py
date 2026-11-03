# Vendored TEST FIXTURE: verbatim copy of src/engineering_hub/supervisor_expired_statement_verifier.py
# from Codex commit 819d5aa12d1b4fb1798a60bff66f55d0c95f3367 (codex Hub side).
# Used only by interop tests; not part of the supervisor module. Do not edit below.
#
"""Offline verifier for supervisor expired-unconsumed v1 statements.

Authenticates only a statement about one reservation. This does not release a
barrier, prevent replay at a caller, or establish that the backend has stopped.
Expectations must come from independently persisted, verified authority.
"""
import base64
import json
import re

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .supervisor_reservation_verifier import _canonical, _pairs
from .supervisor_challenge_verifier import _nonce, _uuid

PROTOCOL = 'engineering-hub.supervisor-route-challenge-expired.v1'
AUDIENCE = 'engineering-hub.hub-dispatch-in-flight'
DOMAIN = b'engineering-hub-supervisor-route-challenge-expired-v1\x00'
_FIELDS = frozenset({'protocol', 'audience', 'key_id', 'nonce', 'command_id',
                     'counter', 'status', 'reservation_status', 'boot_id'})
_EXPECTED = frozenset({'nonce', 'command_id', 'counter', 'boot_id'})


class RefusingExpiredStatementVerifier:
    def verify(self, raw, *, expected):
        return None


class PinnedExpiredStatementVerifier:
    def __init__(self, *, public_key, key_id):
        if (type(public_key) is not bytes or len(public_key) != 32
                or type(key_id) is not str
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', key_id) is None):
            raise ValueError('expired statement enrollment invalid')
        self._public = public_key
        self._key_id = key_id

    def verify(self, raw, *, expected):
        try:
            if (type(raw) is not bytes or not 1 <= len(raw) <= 65536
                    or type(expected) is not dict or set(expected) != _EXPECTED):
                return None
            obj = json.loads(raw, object_pairs_hook=_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                             parse_float=lambda _: (_ for _ in ()).throw(ValueError()))
            if type(obj) is not dict or set(obj) != {'payload', 'signature'} or _canonical(obj) != raw:
                return None
            payload, signature = obj['payload'], obj['signature']
            if (type(payload) is not dict or set(payload) != _FIELDS or type(signature) is not str
                    or re.fullmatch('[A-Za-z0-9_-]{86}', signature) is None):
                return None
            decoded = base64.urlsafe_b64decode(signature + '==')
            if len(decoded) != 64 or base64.urlsafe_b64encode(decoded).rstrip(b'=').decode() != signature:
                return None
            Ed25519PublicKey.from_public_bytes(self._public).verify(decoded, DOMAIN + _canonical(payload))
            if (payload['protocol'] != PROTOCOL or payload['audience'] != AUDIENCE
                    or payload['key_id'] != self._key_id or not _nonce(payload['nonce'])
                    or not _uuid(payload['command_id']) or not _uuid(payload['boot_id'])
                    or type(payload['counter']) is not int or not 1 <= payload['counter'] < 2**63
                    or payload['status'] != 'expired_unconsumed' or payload['reservation_status'] != 'retired'
                    or any(type(expected[k]) is not type(payload[k]) or expected[k] != payload[k] for k in _EXPECTED)):
                return None
            return payload
        except Exception:
            return None
