"""Verify separately signed baseline gate enrollment; no host custody is implied."""
import json
import re
from pathlib import PurePosixPath
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

DOMAIN = b'engineering-hub-baseline-gate-enrollment-v1\x00'
FIELDS = {'schema', 'manifest_digest', 'image_config_id', 'container_id', 'generation_id',
          'runtime_profile_digest', 'gate_socket_directory'}

def verify_baseline_gate_enrollment(envelope, public_key):
    try:
        if type(envelope) is not dict or set(envelope) != {'payload', 'signature'} or type(public_key) is not bytes or len(public_key) != 32:
            raise ValueError()
        p, sig = envelope['payload'], envelope['signature']
        if type(p) is not dict or set(p) != FIELDS or p['schema'] != 'hub_baseline_gate_v1': raise ValueError()
        for name in ('manifest_digest', 'image_config_id', 'runtime_profile_digest'):
            if type(p[name]) is not str or not re.fullmatch(r'sha256:[a-f0-9]{64}', p[name]): raise ValueError()
        if (type(p['container_id']) is not str or not re.fullmatch(r'[a-f0-9]{64}', p['container_id'])
                or type(p['generation_id']) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', p['generation_id'])
                or type(p['gate_socket_directory']) is not str or not p['gate_socket_directory'].startswith('/')
                or p['gate_socket_directory'] == '/' or p['gate_socket_directory'].startswith('//')
                or any(ord(c) < 32 or ord(c) == 127 for c in p['gate_socket_directory'])
                or str(PurePosixPath(p['gate_socket_directory'])) != p['gate_socket_directory']
                or '..' in PurePosixPath(p['gate_socket_directory']).parts
                or type(sig) is not str or not re.fullmatch(r'[a-f0-9]{128}', sig)):
            raise ValueError()
        raw = json.dumps(p, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
        Ed25519PublicKey.from_public_bytes(public_key).verify(bytes.fromhex(sig), DOMAIN + raw)
        return json.loads(raw)
    except Exception:
        raise ValueError('invalid baseline gate enrollment') from None
