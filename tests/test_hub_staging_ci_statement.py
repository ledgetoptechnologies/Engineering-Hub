import base64
import json
import sqlite3
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
import pytest

from hub_staging_service.ci_statement import (
    AUDIENCE, DOMAIN, EXPECTED, MAX_STATEMENT_BYTES, PROTOCOL, PinnedStatementVerifier,
    RefusingStatementVerifier, SqliteStatementLedger, StatementSigner, _canonical, accept,
    manifest_pin_matches,
)


KEY_ID = 'fixture-controller-1'


def keypair():
    key = Ed25519PrivateKey.generate()
    return (key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()),
            key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


PRIVATE, PUBLIC = keypair()
OTHER_PRIVATE, OTHER_PUBLIC = keypair()


def ident():
    return str(uuid.uuid4())


def expected():
    return dict(attempt_id=ident(), job_id=ident(), fence=3, epoch=ident(), task_id=ident(), task_revision=7,
                review_digest='sha256:' + 'a' * 64, actor='codex', manifest_digest='sha256:' + 'b' * 64,
                image_id='sha256:' + 'c' * 64, engine_id='engine-ci', controller_digest='sha256:' + 'd' * 64,
                profile='all', source_commit='e' * 40, source_digest='f' * 64)


def result(**changes):
    return dict(dict(receipt_id='receipt-1', container_id='1' * 64, boot_id=ident(), exit_code=0,
                     terminal_verified=True, descendants_empty=True), **changes)


def signer(private=PRIVATE, key_id=KEY_ID):
    return StatementSigner(private_key=private, key_id=key_id)


def verifier():
    return PinnedStatementVerifier(public_key=PUBLIC, key_id=KEY_ID)


def raw_signed(payload, private=PRIVATE, domain=DOMAIN):
    """Sign bytes directly, so tests can build statements the real signer refuses."""
    signature = Ed25519PrivateKey.from_private_bytes(private).sign(domain + _canonical(payload))
    return _canonical({'payload': payload, 'signature': base64.urlsafe_b64encode(signature).rstrip(b'=').decode()})


def good():
    want = expected()
    raw = signer().sign({**want, **result()})
    return want, raw, json.loads(raw)['payload']


def test_valid_statement_is_accepted_with_every_binding():
    want, raw, _ = good()
    payload = verifier().verify(raw, expected=want)
    assert payload is not None
    assert {name: payload[name] for name in EXPECTED} == want
    assert (payload['protocol'], payload['audience'], payload['key_id']) == (PROTOCOL, AUDIENCE, KEY_ID)


def test_failed_exit_code_is_authentic_not_a_pass():
    want = expected()
    payload = verifier().verify(signer().sign({**want, **result(exit_code=1)}), expected=want)
    assert payload['exit_code'] == 1


@pytest.mark.parametrize('name', sorted(EXPECTED))
def test_every_expected_field_is_bound(name):
    want, raw, _ = good()
    different = {'fence': 4, 'task_revision': 8, 'actor': 'claude', 'profile': 'python', 'engine_id': 'engine-other',
                 'source_commit': '0' * 40, 'source_digest': '0' * 64}
    value = different.get(name, ident() if want[name].count('-') == 4 else 'sha256:' + '0' * 64) \
        if type(want[name]) is str else different[name]
    assert value != want[name]
    assert verifier().verify(raw, expected=dict(want, **{name: value})) is None


@pytest.mark.parametrize('name', ['manifest_digest', 'image_id', 'profile', 'source_commit', 'source_digest'])
def test_resigned_statement_for_other_values_still_fails_expectation(name):
    want = expected()
    changed = dict(want, **{name: {'profile': 'python', 'source_commit': '0' * 40, 'source_digest': '0' * 64}.get(
        name, 'sha256:' + '0' * 64)})
    assert verifier().verify(signer().sign({**changed, **result()}), expected=want) is None


def test_replay_to_another_attempt_stays_held():
    want, raw, _ = good()
    assert verifier().verify(raw, expected=dict(want, attempt_id=ident())) is None
    assert verifier().verify(raw, expected=dict(want, job_id=ident(), fence=4)) is None


def test_forged_key_unsigned_and_wrong_domain_stay_held():
    want, raw, payload = good()
    assert verifier().verify(raw_signed(payload, OTHER_PRIVATE), expected=want) is None
    assert verifier().verify(raw_signed(payload, domain=b'other\x00'), expected=want) is None
    assert verifier().verify(_canonical({'payload': payload}), expected=want) is None
    assert verifier().verify(_canonical({'payload': payload, 'signature': ''}), expected=want) is None
    assert verifier().verify(_canonical(payload), expected=want) is None


def test_wrong_key_id_stays_held_even_when_correctly_signed():
    want = expected()
    assert verifier().verify(signer(key_id='fixture-other').sign({**want, **result()}), expected=want) is None


@pytest.mark.parametrize('change', [
    lambda p: p.update(exit_code=1),
    lambda p: p.update(receipt_id='receipt-2'),
    lambda p: p.update(container_id='2' * 64),
    lambda p: p.update(boot_id=str(uuid.uuid4())),
])
def test_tampered_result_fails_the_signature(change):
    want, raw, _ = good()
    statement = json.loads(raw)
    change(statement['payload'])
    assert verifier().verify(_canonical(statement), expected=want) is None


@pytest.mark.parametrize('change', [
    lambda p: p.update(terminal_verified=False),
    lambda p: p.update(descendants_empty=False),
    lambda p: p.update(terminal_verified=1),
    lambda p: p.update(exit_code=256),
    lambda p: p.update(exit_code=True),
    lambda p: p.update(fence=True),
    lambda p: p.update(fence=0),
    lambda p: p.update(task_revision=2**63),
    lambda p: p.update(actor='root'),
    lambda p: p.update(profile='some'),
    lambda p: p.update(extra=1),
    lambda p: p.pop('receipt_id'),
    lambda p: p.update(protocol='other'),
    lambda p: p.update(audience='other'),
    lambda p: p.update(source_commit='E' * 40),
])
def test_validly_signed_but_malformed_payload_stays_held(change):
    want, _, payload = good()
    change(payload)
    assert verifier().verify(raw_signed(payload), expected=want) is None
    if payload.get('protocol') == PROTOCOL and payload.get('audience') == AUDIENCE:  # The signer sets these two.
        with pytest.raises(ValueError):
            signer().sign({key: value for key, value in payload.items()
                           if key not in {'protocol', 'audience', 'key_id'}})


@pytest.mark.parametrize('make', [
    lambda raw: raw + b' ',
    lambda raw: b' ' + raw,
    lambda raw: raw.replace(b'"payload":', b'"payload": '),
    lambda raw: raw.replace(b'{"payload":', b'{"signature":"x","payload":'),
    lambda raw: json.dumps(json.loads(raw), indent=1).encode(),
    lambda raw: raw[:-1],
    lambda raw: b'',
    lambda raw: b'null',
    lambda raw: b'[]',
    lambda raw: b'x' * (MAX_STATEMENT_BYTES + 1),
    lambda raw: raw.decode(),
    lambda raw: None,
])
def test_non_canonical_or_malformed_bytes_stay_held(make):
    want, raw, _ = good()
    assert verifier().verify(make(raw), expected=want) is None


def test_floats_and_nan_are_refused():
    want, raw, _ = good()
    assert verifier().verify(raw.replace(b'"exit_code":0', b'"exit_code":0.0'), expected=want) is None
    assert verifier().verify(raw.replace(b'"exit_code":0', b'"exit_code":NaN'), expected=want) is None


def test_non_canonical_signature_encoding_stays_held():
    want, raw, _ = good()
    statement = json.loads(raw)
    statement['signature'] = statement['signature'][:-1] + ('B' if statement['signature'][-1] == 'A' else 'A')
    assert verifier().verify(_canonical(statement), expected=want) is None


@pytest.mark.parametrize('change', [lambda e: e.pop('profile'), lambda e: e.update(extra=1)])
def test_incomplete_expectation_stays_held(change):
    want, raw, _ = good()
    change(want)
    assert verifier().verify(raw, expected=want) is None


def test_expectation_type_must_match():
    want, raw, _ = good()
    assert verifier().verify(raw, expected=dict(want, fence=3.0)) is None


def test_refusing_verifier_accepts_nothing():
    want, raw, _ = good()
    assert RefusingStatementVerifier().verify(raw, expected=want) is None


def test_ledger_allows_same_statement_and_refuses_a_conflict():
    db = sqlite3.connect(':memory:')
    ledger = SqliteStatementLedger(db)
    want = expected()
    first = signer().sign({**want, **result()})
    conflict = signer().sign({**want, **result(exit_code=1)})
    assert accept(verifier(), ledger, first, expected=want)['exit_code'] == 0
    assert accept(verifier(), ledger, first, expected=want)['exit_code'] == 0
    assert accept(verifier(), ledger, conflict, expected=want) is None


def test_ledger_survives_a_restart(tmp_path):
    path = tmp_path / 'statements.sqlite3'
    want = expected()
    with sqlite3.connect(path) as db:
        assert accept(verifier(), SqliteStatementLedger(db), signer().sign({**want, **result()}),
                      expected=want) is not None
    with sqlite3.connect(path) as db:
        assert accept(verifier(), SqliteStatementLedger(db), signer().sign({**want, **result(exit_code=1)}),
                      expected=want) is None


def test_rejected_statement_is_not_recorded():
    db = sqlite3.connect(':memory:')
    ledger = SqliteStatementLedger(db)
    want, _, payload = good()
    assert accept(verifier(), ledger, raw_signed(payload, OTHER_PRIVATE), expected=want) is None
    assert db.execute('SELECT COUNT(*) FROM ci_terminal_statements').fetchone()[0] == 0


def test_manifest_pin():
    digest = 'sha256:' + 'b' * 64
    assert manifest_pin_matches({'ci_manifest_digest': digest}, {'manifest_digest': digest})
    assert not manifest_pin_matches({'ci_manifest_digest': digest}, {'manifest_digest': 'sha256:' + 'c' * 64})
    assert not manifest_pin_matches({}, {'manifest_digest': digest})
    assert not manifest_pin_matches({'ci_manifest_digest': 'b' * 64}, {'manifest_digest': 'b' * 64})
    assert not manifest_pin_matches({'ci_manifest_digest': None}, {'manifest_digest': None})
    assert not manifest_pin_matches(None, {'manifest_digest': digest})


def test_enrollment_input_is_checked_and_key_is_not_shown():
    for bad in (dict(private_key=b'short', key_id=KEY_ID), dict(private_key=PRIVATE, key_id='bad key')):
        with pytest.raises(ValueError):
            StatementSigner(**bad)
    with pytest.raises(ValueError):
        PinnedStatementVerifier(public_key=b'short', key_id=KEY_ID)
    assert PRIVATE.hex() not in repr(signer()) and KEY_ID in repr(signer())


def test_worker_core_does_not_depend_on_this_module():
    import hub_staging_service.core as core
    assert 'ci_statement' not in open(core.__file__, encoding='utf-8').read()
