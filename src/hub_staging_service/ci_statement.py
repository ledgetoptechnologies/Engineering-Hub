"""Signed terminal statement for one development CI attempt.

The worker's terminal receipt is evidence only: its checksum proves framing, not
who wrote it. This statement adds a signature from a controller-held key, bound
to the exact attempt, manifest, image, profile and source. It proves that the
enrolled controller said so. The controller and its host stay trusted, so this
is not independent proof of what ran.

Nothing here is wired into a job path. No key is enrolled, and an unenrolled
caller uses RefusingStatementVerifier, which leaves every attempt held. A failed
exit code is an authentic result; callers decide what it means.
"""
import base64
import hashlib
import json
import re

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PROTOCOL = 'engineering-hub.staging-ci-terminal.v1'
AUDIENCE = 'engineering-hub.development-ci-admission'
DOMAIN = b'engineering-hub-staging-ci-terminal-v1\x00'
MAX_STATEMENT_BYTES = 8192
MAX_INT = 2**63 - 1
AGENTS = ('codex', 'claude', 'hermes')
PROFILES = ('python', 'ui', 'all')

_UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
_SHA256 = r'sha256:[0-9a-f]{64}'
_NAME = r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}'
_TEXT = {
    'key_id': _NAME, 'attempt_id': _UUID, 'job_id': _UUID, 'epoch': _UUID, 'task_id': _UUID,
    'review_digest': _SHA256, 'manifest_digest': _SHA256, 'image_id': _SHA256,
    'controller_digest': _SHA256, 'engine_id': _NAME, 'receipt_id': _NAME,
    'source_commit': r'[0-9a-f]{40}', 'source_digest': r'[0-9a-f]{64}',
    'container_id': r'[0-9a-f]{64}', 'boot_id': _UUID,
}
_COUNTS = ('fence', 'task_revision')
# Everything the Hub knows before the worker reports. The rest is the result.
EXPECTED = frozenset({
    'attempt_id', 'job_id', 'fence', 'epoch', 'task_id', 'task_revision', 'review_digest',
    'actor', 'manifest_digest', 'image_id', 'engine_id', 'controller_digest', 'profile',
    'source_commit', 'source_digest',
})
_RESULT = frozenset({'receipt_id', 'container_id', 'boot_id', 'exit_code',
                     'terminal_verified', 'descendants_empty'})
_FIELDS = EXPECTED | _RESULT | {'protocol', 'audience', 'key_id'}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _refuse(_value):
    raise ValueError('non-integer number')


def _valid(payload):
    if type(payload) is not dict or set(payload) != _FIELDS:
        return False
    return (payload['protocol'] == PROTOCOL and payload['audience'] == AUDIENCE
            and all(type(payload[name]) is str and re.fullmatch(pattern, payload[name]) is not None
                    for name, pattern in _TEXT.items())
            and all(type(payload[name]) is int and 1 <= payload[name] <= MAX_INT for name in _COUNTS)
            and type(payload['actor']) is str and payload['actor'] in AGENTS
            and type(payload['profile']) is str and payload['profile'] in PROFILES
            and type(payload['exit_code']) is int and 0 <= payload['exit_code'] <= 255
            and payload['terminal_verified'] is True and payload['descendants_empty'] is True)


class StatementSigner:
    """Controller-side signer. Takes key bytes only; reading a key is an enrollment step."""

    def __init__(self, *, private_key, key_id):
        if (type(private_key) is not bytes or len(private_key) != 32 or type(key_id) is not str
                or re.fullmatch(_NAME, key_id) is None):
            raise ValueError('statement signer enrollment invalid')
        self._key = Ed25519PrivateKey.from_private_bytes(private_key)
        self._key_id = key_id

    def __repr__(self):
        return f'StatementSigner(key_id={self._key_id!r})'

    def sign(self, fields):
        """Sign one terminal result; refuses anything a verifier would refuse."""
        payload = dict(fields, protocol=PROTOCOL, audience=AUDIENCE, key_id=self._key_id)
        if not _valid(payload):
            raise ValueError('statement fields invalid')
        signature = self._key.sign(DOMAIN + _canonical(payload))
        return _canonical({'payload': payload,
                           'signature': base64.urlsafe_b64encode(signature).rstrip(b'=').decode()})


class RefusingStatementVerifier:
    """The unenrolled default: no statement is accepted, so attempts stay held."""

    def verify(self, raw, *, expected):
        return None


class PinnedStatementVerifier:
    def __init__(self, *, public_key, key_id):
        if (type(public_key) is not bytes or len(public_key) != 32 or type(key_id) is not str
                or re.fullmatch(_NAME, key_id) is None):
            raise ValueError('statement verifier enrollment invalid')
        self._public = public_key
        self._key_id = key_id

    def verify(self, raw, *, expected):
        """Return the payload for an exact statement, or None so the attempt stays held."""
        try:
            if (type(raw) is not bytes or not 1 <= len(raw) <= MAX_STATEMENT_BYTES
                    or type(expected) is not dict or set(expected) != EXPECTED):
                return None
            statement = json.loads(raw, object_pairs_hook=_pairs, parse_float=_refuse,
                                   parse_constant=_refuse)
            if (type(statement) is not dict or set(statement) != {'payload', 'signature'}
                    or _canonical(statement) != raw):
                return None
            payload, signature = statement['payload'], statement['signature']
            if (not _valid(payload) or payload['key_id'] != self._key_id or type(signature) is not str
                    or re.fullmatch('[A-Za-z0-9_-]{86}', signature) is None):
                return None
            decoded = base64.urlsafe_b64decode(signature + '==')
            if len(decoded) != 64 or base64.urlsafe_b64encode(decoded).rstrip(b'=').decode() != signature:
                return None
            Ed25519PublicKey.from_public_bytes(self._public).verify(decoded, DOMAIN + _canonical(payload))
            if any(type(expected[name]) is not type(payload[name]) or expected[name] != payload[name]
                   for name in EXPECTED):
                return None
            return payload
        except Exception:
            return None


class SqliteStatementLedger:
    """First statement per attempt wins. Runs in the caller's transaction, which commits it.

    The attempt id is generated by the Hub for each attempt, so a statement can't
    be prepared for, or moved to, a different attempt. This covers the remaining
    case: a second, different signed statement for one attempt.
    """

    def __init__(self, db):
        self._db = db
        db.execute('CREATE TABLE IF NOT EXISTS ci_terminal_statements ('
                   'attempt_id TEXT PRIMARY KEY, statement_sha256 TEXT NOT NULL)')

    def record(self, attempt_id, raw):
        digest = hashlib.sha256(raw).hexdigest()
        self._db.execute('INSERT OR IGNORE INTO ci_terminal_statements VALUES (?,?)', (attempt_id, digest))
        row = self._db.execute('SELECT statement_sha256 FROM ci_terminal_statements WHERE attempt_id=?',
                               (attempt_id,)).fetchone()
        return row is not None and row[0] == digest


def accept(verifier, ledger, raw, *, expected):
    """Verify, then record. A repeat of the same statement is fine; a conflict stays held."""
    payload = verifier.verify(raw, expected=expected)
    if payload is None or not ledger.record(payload['attempt_id'], raw):
        return None
    return payload


def manifest_pin_matches(recipe, attempt):
    """A CI recipe runs only the manifest it was installed for; a missing pin is a refusal."""
    if type(recipe) is not dict or type(attempt) is not dict:
        return False
    pin, digest = recipe.get('ci_manifest_digest'), attempt.get('manifest_digest')
    return (type(pin) is str and re.fullmatch(_SHA256, pin) is not None
            and type(digest) is str and pin == digest)
