# Vendored TEST FIXTURE: verbatim copy of src/engineering_hub/supervisor_reservation_verifier.py
# from Codex commit 819d5aa12d1b4fb1798a60bff66f55d0c95f3367 (codex Hub side).
# Used only by interop tests; not part of the supervisor module. Do not edit below.
#
"""Pinned verifier for supervisor reservation-v1 receipts, never dispatches.

Pin and epoch must come from independently enrolled operator authority, not the
receipt or a task caller. This verifies signed reservation bytes only; no host
trust establishment, dispatch permission, or outcome verification is supplied.
"""
import base64
import json
import re
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PROTOCOL='engineering-hub.supervisor-route-reservation.v1'
AUDIENCE='engineering-hub.hub-supervisor-route-reservation'
DOMAIN=b'engineering-hub-supervisor-route-reserve-v1\x00'
_FIELDS=frozenset({'protocol','audience','key_id','command_id','body_sha256','method','path','pin','epoch','fence','counter','reserved_at_ms'})
_PIN=frozenset({'container_id','host_pid','process_start_ticks','uid','gid','engine_id','engine_pid','engine_start_ticks'})
_EPOCH=frozenset({'launch_id','launch_counter','boot_id'})

def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',', ':'),ensure_ascii=False,allow_nan=False).encode('utf-8')

def _pairs(items):
    result={}
    for key,value in items:
        if key in result: raise ValueError('duplicate')
        result[key]=value
    return result

def _integer(value, *, zero=False):
    return type(value) is int and (0 if zero else 1)<=value<2**63

class PinnedReservationVerifier:
    """Explicit trusted composition only; invalid or absent enrollment refuses."""
    def __init__(self, *, public_key, key_id, pin, epoch):
        import uuid
        if (type(public_key) is not bytes or len(public_key)!=32 or type(key_id) is not str
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',key_id) is None
                or type(pin) is not dict or set(pin)!=_PIN or type(epoch) is not dict or set(epoch)!=_EPOCH
                or type(pin['container_id']) is not str or re.fullmatch('[0-9a-f]{64}',pin['container_id']) is None
                or type(pin['engine_id']) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',pin['engine_id']) is None
                or not all(_integer(pin[k]) for k in ('host_pid','process_start_ticks','engine_pid','engine_start_ticks'))
                or not all(_integer(pin[k],zero=True) and pin[k]<2**32-1 for k in ('uid','gid'))
                or not _integer(epoch['launch_counter'])):
            raise ValueError('reservation enrollment invalid')
        for key in ('launch_id','boot_id'):
            try:
                if type(epoch[key]) is not str or str(uuid.UUID(epoch[key]))!=epoch[key]: raise ValueError()
            except Exception: raise ValueError('reservation enrollment invalid') from None
        if uuid.UUID(epoch['launch_id']).version != 4: raise ValueError('reservation enrollment invalid')
        self._public=public_key;self._key_id=key_id
        self._pin=_canonical(pin);self._epoch=_canonical(epoch)

    def reservation(self, raw):
        try:
            if type(raw) is not bytes or not 1<=len(raw)<=65536: return None
            envelope=json.loads(raw,object_pairs_hook=_pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if type(envelope) is not dict or set(envelope)!={'payload','signature'} or _canonical(envelope)!=raw: return None
            payload=envelope['payload']; signature=envelope['signature']
            if (type(payload) is not dict or set(payload)!=_FIELDS or type(signature) is not str
                or re.fullmatch('[A-Za-z0-9_-]{86}',signature) is None): return None
            decoded=base64.urlsafe_b64decode(signature+'==')
            if len(decoded)!=64 or base64.urlsafe_b64encode(decoded).rstrip(b'=').decode()!=signature: return None
            Ed25519PublicKey.from_public_bytes(self._public).verify(decoded,DOMAIN+_canonical(payload))
            if (payload['protocol']!=PROTOCOL or payload['audience']!=AUDIENCE or payload['key_id']!=self._key_id
                or _canonical(payload['pin'])!=self._pin or _canonical(payload['epoch'])!=self._epoch
                or payload['method']!='POST' or payload['path']!='/v1/container-pilot/commands'
                or type(payload['body_sha256']) is not str or re.fullmatch('[0-9a-f]{64}',payload['body_sha256']) is None
                or not _integer(payload['fence']) or not _integer(payload['counter']) or not _integer(payload['reserved_at_ms'])): return None
            import uuid
            if type(payload['command_id']) is not str or str(uuid.UUID(payload['command_id']))!=payload['command_id']: return None
            return dict(command_id=payload['command_id'],command_sha256=payload['body_sha256'],method=payload['method'],path=payload['path'],
                        supervisor_pin=self._pin.decode('ascii'),launch_epoch=self._epoch.decode('ascii'),
                        command_fence=payload['fence'],dispatch_counter=payload['counter'])
        except Exception:
            return None

    def outcome(self, receipt, outcome, claim):
        return None  # Signed inspection outcomes are not integrated.

