"""Closed, versioned input for a supervised trusted-local coordinator.

Parsing and normalization grant no authority. Root-owned launch policy must
independently pin the exact raw bytes as well as the normalized commitment.
"""
from dataclasses import dataclass
import hashlib
import json
import re

from engineering_hub.container_pilot_manifest import Platform

SCHEMA = 'engineering-hub.local-coordinator-admission.v1'
SCHEMA_V2 = 'engineering-hub.local-coordinator-admission.v2'
DOMAIN = b'engineering-hub.local-coordinator-admission-commitment.v1\x00'
DOMAIN_V2 = b'engineering-hub.local-coordinator-admission-commitment.v2\x00'
OAUTH_SOCKET = '/run/engineering-hub/github-oauth.sock'
SELF_REFERENCE = 'engineering-hub.local-coordinator-profile.self-reference.v1'
MAX_BYTES = 16384
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')


@dataclass(frozen=True)
class CoordinatorAdmission:
    release: object
    issuer_uid: int
    issuer_gid: int
    config_digest: str
    session_digest: str
    lifetime_seconds: int
    timeout_ms: int
    oauth_broker: object = None


@dataclass(frozen=True)
class GitHubBrokerEnrollment:
    socket_path: str
    issuer_uid: int
    issuer_gid: int
    client_id: str
    origin: str
    secret_digest: str


def parse_admission(raw):
    """Accept only canonical bounded input, independently of any receipt."""
    from .runtime_identity import CoordinatorReleaseEnrollment
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES: raise ValueError()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError()
                result[key] = value
            return result
        def reject(_): raise ValueError()
        value = json.loads(raw, object_pairs_hook=unique, parse_float=reject, parse_constant=reject)
        fields={'schema', 'release', 'issuer_uid', 'issuer_gid','config_digest', 'session_digest', 'lifetime_seconds', 'timeout_ms'}
        if (type(value) is not dict or not ((value.get('schema')==SCHEMA and set(value)==fields)
                or (value.get('schema')==SCHEMA_V2 and set(value)==fields|{'oauth_broker'}))
                or json.dumps(value, sort_keys=True, separators=(',', ':'),
                    ensure_ascii=False, allow_nan=False).encode() != raw): raise ValueError()
        release = value['release']
        if type(release) is not dict or set(release) != (set(CoordinatorReleaseEnrollment.__dataclass_fields__) - {'public_key'}) | {'public_key_hex'}:
            raise ValueError()
        release = dict(release); key = release.pop('public_key_hex')
        if type(key) is not str or re.fullmatch(r'[0-9a-f]{64}', key) is None: raise ValueError()
        release['public_key'] = bytes.fromhex(key)
        release['platform'] = Platform.model_validate(release['platform'])
        release = CoordinatorReleaseEnrollment(**release)
        for name in ('issuer_uid', 'issuer_gid'):
            if type(value[name]) is not int or not 0 <= value[name] < 2**32 - 1: raise ValueError()
        for name in ('config_digest', 'session_digest'):
            if type(value[name]) is not str or not _SHA.fullmatch(value[name]): raise ValueError()
        if (type(value['lifetime_seconds']) is not int or not 1 <= value['lifetime_seconds'] <= 3600
                or type(value['timeout_ms']) is not int or not 100 <= value['timeout_ms'] <= 10000): raise ValueError()
        oauth=None
        if value['schema']==SCHEMA_V2:
            from engineering_hub.preview_protocol import _origin
            item=value['oauth_broker']
            if (type(item) is not dict or set(item)!={'socket_path','issuer_uid','issuer_gid','client_id','origin','secret_digest'}
                or item['socket_path']!=OAUTH_SOCKET or any(type(item[k]) is not int or not 0<=item[k]<2**32-1 for k in ('issuer_uid','issuer_gid'))
                or type(item['client_id']) is not str or not 1<=len(item['client_id'])<=256
                or any(not 33<=ord(c)<=126 for c in item['client_id']) or _origin(item['origin'])!=item['origin']
                or type(item['secret_digest']) is not str or not _SHA.fullmatch(item['secret_digest'])):raise ValueError()
            oauth=GitHubBrokerEnrollment(**item)
        return CoordinatorAdmission(release, value['issuer_uid'], value['issuer_gid'],
            value['config_digest'], value['session_digest'], value['lifetime_seconds'], value['timeout_ms'],oauth)
    except Exception:
        raise ValueError('coordinator admission invalid') from None


def admission_commitment(raw):
    """Normalize only the profile self-reference; also return the raw digest."""
    parse_admission(raw)
    value = json.loads(raw)
    value['release']['launch_profile_digest'] = SELF_REFERENCE
    normalized = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                            allow_nan=False).encode()
    return ('sha256:' + hashlib.sha256((DOMAIN_V2 if value['schema']==SCHEMA_V2 else DOMAIN) + normalized).hexdigest(),
            'sha256:' + hashlib.sha256(raw).hexdigest())
