import time
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hub_container_executor.state_custodian_admission import HOLD_DOMAIN, HOLD_OBSERVATION, HOLD_DOMAIN_V2, HOLD_OBSERVATION_V2, _canonical
from hub_container_executor.state_custodian_observation import IDENTITY, HeldObservationError, verify_held_observation


@pytest.mark.parametrize('fault', ['signature', 'nonce', 'identity', 'expired', 'claim', 'clock'])
@pytest.mark.parametrize('version', [1, 2])
def test_signed_hold_observation_rejects_invalid_evidence(monkeypatch, fault, version):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    wall = time.time_ns() // 1_000_000; mono = time.monotonic()
    payload = {'protocol': HOLD_OBSERVATION, 'nonce': 'a' * 64, 'target_id': 'engineering-hub:staging',
        'operation_id': str(uuid4()), 'run_id': str(uuid4()), 'manifest_digest': 'sha256:' + 'b' * 64,
        'admission_digest': 'c' * 64, 'fencing_generation': 1, 'device': 2, 'inode': 3, 'host_uid': 10002,
        'host_gid': 10002, 'owner_policy_digest': 'd' * 64, 'marker_present': True, 'quiescence': 'unknown',
        'issued_at_ms': wall, 'expires_at_ms': wall + 4000}
    if version == 2:
        payload['protocol'] = HOLD_OBSERVATION_V2
        payload['private_preparation_recorded'] = True
    expected = {name: payload[name] for name in IDENTITY}
    if fault == 'expired': payload['expires_at_ms'] = wall
    if fault == 'claim': payload['quiescence'] = 'verified'
    signature = key.sign((HOLD_DOMAIN_V2 if version == 2 else HOLD_DOMAIN) + _canonical(payload)).hex()
    if fault == 'signature': signature = '0' * 128
    if fault == 'identity': expected['fencing_generation'] = True
    if fault == 'clock': monkeypatch.setattr('hub_container_executor.state_custodian_observation.time.time_ns', lambda: (wall - 1) * 1_000_000)
    nonce = 'f' * 64 if fault == 'nonce' else 'a' * 64
    with pytest.raises(HeldObservationError):
        verify_held_observation(_canonical({'payload': payload, 'signature': signature}), provider_public_key=public,
            nonce=nonce, expected=expected, start_wall_ms=wall, start_monotonic=mono)
