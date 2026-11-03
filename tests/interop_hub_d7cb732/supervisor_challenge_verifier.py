# VENDORED TEST FIXTURE - DO NOT EDIT. Verbatim copy of
# src/engineering_hub/supervisor_challenge_verifier.py at commit d7cb732 (Codex's Hub-side
# verifier), used only by tests/test_hub_container_executor_supervisor_hub_interop.py
# to check that the supervisor's real signed bytes verify on the Hub side.
"""Pinned supervisor challenge verifier for the no-send Hub foundation.

Implements the signed challenge envelope from supervisor-route 42ab5b5. This
module provides no barrier, claim proof signer, release, transport, or dispatch.
Callers must obtain expectations from independently enrolled authority and the
already verified, committed reservation, never from the challenge itself.
"""
import base64
import json
import re
import uuid
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .supervisor_reservation_verifier import PinnedReservationVerifier, _canonical, _pairs

PROTOCOL='engineering-hub.supervisor-route-dispatch-challenge.v1'
AUDIENCE='engineering-hub.hub-dispatch-challenge'
DOMAIN=b'engineering-hub-supervisor-route-dispatch-challenge-v1\x00'
_FIELDS=frozenset({'protocol','audience','key_id','nonce','command_id','counter',
    'reservation_receipt_sha256','executor','pin','epoch','supervisor_boot_id','proof_window_ms'})
_EXPECTED=frozenset({'command_id','counter','reservation_receipt_sha256','executor','pin','epoch'})


def _uuid(value):
    try: return type(value) is str and str(uuid.UUID(value))==value
    except (ValueError,TypeError,AttributeError): return False


def _nonce(value):
    try:
        return (type(value) is str and re.fullmatch('[A-Za-z0-9_-]{43}',value) is not None
                and base64.urlsafe_b64encode(base64.urlsafe_b64decode(value+'=')).rstrip(b'=').decode()==value)
    except (ValueError,TypeError): return False


class RefusingChallengeVerifier:
    def verify(self, raw, *, expected): return None


class PinnedChallengeVerifier:
    """Read-only authentication; no proof or dispatch authority is granted."""
    def __init__(self, *, public_key, key_id, pin, epoch):
        # Reuse the existing stricter structural checks for operator enrollment.
        PinnedReservationVerifier(public_key=public_key,key_id=key_id,pin=pin,epoch=epoch)
        self._public=public_key; self._key_id=key_id
        self._pin=_canonical(pin); self._epoch=_canonical(epoch)

    def verify(self, raw, *, expected):
        try:
            if (type(raw) is not bytes or not 1<=len(raw)<=65536
                    or type(expected) is not dict or set(expected)!=_EXPECTED): return None
            obj=json.loads(raw,object_pairs_hook=_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                parse_float=lambda _: (_ for _ in ()).throw(ValueError()))
            if type(obj) is not dict or set(obj)!={'payload','signature'} or _canonical(obj)!=raw: return None
            payload=obj['payload']; signature=obj['signature']
            if (type(payload) is not dict or set(payload)!=_FIELDS or type(signature) is not str
                    or re.fullmatch('[A-Za-z0-9_-]{86}',signature) is None): return None
            decoded=base64.urlsafe_b64decode(signature+'==')
            if len(decoded)!=64 or base64.urlsafe_b64encode(decoded).rstrip(b'=').decode()!=signature: return None
            Ed25519PublicKey.from_public_bytes(self._public).verify(decoded,DOMAIN+_canonical(payload))
            if (payload['protocol']!=PROTOCOL or payload['audience']!=AUDIENCE or payload['key_id']!=self._key_id
                    or not _nonce(payload['nonce']) or not _uuid(payload['command_id'])
                    or type(payload['counter']) is not int or not 1<=payload['counter']<2**63
                    or type(payload['reservation_receipt_sha256']) is not str
                    or re.fullmatch('[0-9a-f]{64}',payload['reservation_receipt_sha256']) is None
                    or type(payload['executor']) is not str or re.fullmatch('[0-9a-f]{64}',payload['executor']) is None
                    or _canonical(payload['pin'])!=self._pin or _canonical(payload['epoch'])!=self._epoch
                    or payload['pin']['container_id']!=payload['executor'] or not _uuid(payload['supervisor_boot_id'])
                    or type(payload['proof_window_ms']) is not int or not 1000<=payload['proof_window_ms']<=30000
                    or any(_canonical(payload[k])!=_canonical(v) for k,v in expected.items())): return None
            return payload
        except Exception:
            return None
