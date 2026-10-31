from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub_container_executor import mount_gate_protocol as gate


def fixture():
    return dict(protocol=gate.PROTOCOL, audience=gate.AUDIENCE, decision='allow',
        nonce='a' * 64, launch_id=str(uuid4()), operation_id=str(uuid4()), run_id=str(uuid4()),
        boot_id=str(uuid4()), container_id='d' * 64, host_pid=42, process_start_ticks=1234,
        mount_target='/var/lib/hub', mount_flags=['nodev', 'rw'],
        runnable_digest='sha256:' + 'e' * 64, platform_manifest_digest='sha256:' + 'f' * 64,
        implementation_commit='a' * 40,
        fence=1, manifest_digest='sha256:' + 'b' * 64, image_config_id='sha256:' + 'c' * 64,
        generation_id='candidate-1', expected_device=42, expected_inode=123,
        observed_device=42, observed_inode=123, issued_at_ms=100000, expires_at_ms=104000)


def verifier(payload, key, consumed, **kwargs):
    raw = gate.issue_decision(payload, key)
    def consume(decision):
        pair = decision['launch_id'], decision['nonce']
        if pair in consumed:
            return False
        consumed.add(pair)
        return True
    return gate.verify_decision(raw, public_key=key.public_key(),
        expected={key: payload[key] for key in gate.IDENTITY_FIELDS}, exchange_wall_ms=100000,
        exchange_monotonic=10.0, consume_challenge=consume,
        wall_clock=kwargs.get('wall_clock', lambda: 101.0),
        monotonic=kwargs.get('monotonic', lambda: 11.0))


def test_valid_decision_consumes_challenge_and_cannot_replay():
    payload, key, consumed = fixture(), Ed25519PrivateKey.generate(), set()
    assert verifier(payload, key, consumed) == payload
    with pytest.raises(gate.MountGateProtocolError):
        verifier(payload, key, consumed)


def test_transaction_receives_full_verified_decision_and_cannot_mutate_result():
    payload, key, received = fixture(), Ed25519PrivateKey.generate(), []
    def consume(value):
        received.append(value.copy())
        value['mount_flags'].clear()
        return True
    result = gate.verify_decision(gate.issue_decision(payload, key), public_key=key.public_key(),
        expected={k: payload[k] for k in gate.IDENTITY_FIELDS}, exchange_wall_ms=100000,
        exchange_monotonic=10.0, consume_challenge=consume,
        wall_clock=lambda: 101.0, monotonic=lambda: 11.0)
    assert set(received[0]) == gate.FIELDS and received[0]['fence'] == payload['fence']
    assert result['mount_flags'] == ['nodev', 'rw']


@pytest.mark.parametrize('field,value', [
    ('observed_inode', 124), ('observed_device', 43), ('fence', True), ('fence', 0),
    ('nonce', 'A' * 64), ('generation_id', '../escape'), ('launch_id', 'bad'),
    ('expires_at_ms', 105001), ('decision', 'deny'), ('audience', 'other'),
    ('mount_target', '/other'), ('mount_flags', ['rw', 'rw']),
])
def test_bad_or_unmatched_measurement_never_signs(field, value):
    payload = fixture(); payload[field] = value
    with pytest.raises(gate.MountGateProtocolError):
        gate.issue_decision(payload, Ed25519PrivateKey.generate())


def test_wrong_pins_key_noncanonical_and_duplicate_denied_without_consumption():
    payload, key, consumed = fixture(), Ed25519PrivateKey.generate(), set()
    raw = gate.issue_decision(payload, key)
    expected = {k: payload[k] for k in gate.IDENTITY_FIELDS}
    args = dict(public_key=key.public_key(), expected=expected, exchange_wall_ms=100000,
        exchange_monotonic=10.0, consume_challenge=lambda x: consumed.add(x['nonce']) or True,
        wall_clock=lambda: 101.0, monotonic=lambda: 11.0)
    bad = dict(expected, expected_inode=124)
    for wire, changes in ((raw, {'expected': bad}), (raw, {'public_key': Ed25519PrivateKey.generate().public_key()}),
                          (raw + b'\n', {}), (raw[:-1] + b',"signature":"x"}', {}),
                          (b' ' * (gate.MAX_BYTES + 1), {})):
        with pytest.raises(gate.MountGateProtocolError):
            gate.verify_decision(wire, **{**args, **changes})
    assert not consumed


@pytest.mark.parametrize('wall,tick', [(99.0, 11.0), (104.0, 11.0), (101.0, 9.0), (101.0, 14.0)])
def test_expiry_or_clock_regression_never_consumes(wall, tick):
    consumed = set()
    with pytest.raises(gate.MountGateProtocolError):
        verifier(fixture(), Ed25519PrivateKey.generate(), consumed,
                 wall_clock=lambda: wall, monotonic=lambda: tick)
    assert not consumed


def test_expiry_after_durable_consumption_does_not_unconsume():
    payload, key, consumed = fixture(), Ed25519PrivateKey.generate(), set()
    ticks = iter((11.0, 14.0))
    with pytest.raises(gate.MountGateProtocolError):
        verifier(payload, key, consumed, monotonic=lambda: next(ticks))
    assert consumed == {(payload['launch_id'], payload['nonce'])}
    with pytest.raises(gate.MountGateProtocolError):
        verifier(payload, key, consumed)


def test_clock_regression_during_consumption_is_denied_and_remains_consumed():
    payload, key, consumed = fixture(), Ed25519PrivateKey.generate(), set()
    walls = iter((101.0, 100.5))
    with pytest.raises(gate.MountGateProtocolError):
        verifier(payload, key, consumed, wall_clock=lambda: next(walls))
    assert consumed == {(payload['launch_id'], payload['nonce'])}
