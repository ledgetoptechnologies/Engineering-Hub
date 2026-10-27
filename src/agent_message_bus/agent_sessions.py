"""Optional staging identity/mailbox repository; never maps a bearer to a legacy actor.

The containing server explicitly owns this separate database and this repository
instance. Listener registrations are process-local, owned by that same instance.
Installation digests are SHA256 of the complete canonical ehi_ secret string.
Only exchange and active claim responses return their newly issued raw secrets.
"""
from __future__ import annotations

import base64
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid


FAMILIES = frozenset({'codex', 'claude', 'hermes'})
MAX_INSTALLATIONS = 256
MAX_PENDING = 32
MAX_ENROLLMENTS_PER_MINUTE = 20
MAX_ACTIVE_SESSIONS = 16
MAX_SESSION_HISTORY = 256
MAX_ALL_SESSIONS = 4096
MAX_MESSAGES = 10000
MAX_MAILBOX = 1000
MAX_SUBSCRIPTIONS = 32
MAX_BODY_BYTES = 16384
SESSION_SECONDS = 900
PAIRING_SECONDS = 600
_OWNER = re.compile(r'human:(?:[1-9][0-9]{0,19}|cf:[0-9a-f]{64})\Z')
_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')


class AgentSessionError(Exception):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class SessionPrincipal:
    installation_id: str
    session_id: str
    family: str
    expires_at: float


def _hash(value):
    return hashlib.sha256(value.encode('ascii')).hexdigest()


def _secret(value, prefix):
    if type(value) is not str or not re.fullmatch(re.escape(prefix) + r'[A-Za-z0-9_-]{43}', value):
        raise AgentSessionError('invalid_session' if prefix == 'ehs_' else 'invalid_credential', 401)
    raw = base64.urlsafe_b64decode(value[len(prefix):] + '=')
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).decode().rstrip('=') != value[len(prefix):]:
        raise AgentSessionError('invalid_session' if prefix == 'ehs_' else 'invalid_credential', 401)
    return _hash(value)


def _text(value, maximum, *, empty=False):
    if (type(value) is not str or len(value) > maximum or (not empty and not value.strip())
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise AgentSessionError('invalid_input', 422)
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise AgentSessionError('invalid_input', 422) from None
    return value.strip()


def _number(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise AgentSessionError('invalid_input', 422)
    return value


def _iso(value):
    return None if value is None else datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


class AgentSessionRepository:
    def __init__(self, path, owner, clock=time.time):
        if type(owner) is not str or _OWNER.fullmatch(owner) is None or not callable(clock):
            raise ValueError('explicit_session_owner_required')
        self.path, self.owner, self.clock = Path(path), owner, clock
        self._lock = threading.RLock()
        self._listeners = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            # This feature must never silently share a legacy bus/Hub database.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('messages','hub_entities')").fetchone():
                raise ValueError('separate_session_database_required')
            statements = (
                'CREATE TABLE IF NOT EXISTS session_owner (singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner TEXT NOT NULL)',
                '''CREATE TABLE IF NOT EXISTS session_installations (
                    id TEXT PRIMARY KEY, family TEXT NOT NULL, name TEXT NOT NULL, host_label TEXT NOT NULL,
                    credential_hash TEXT UNIQUE NOT NULL, pairing_code TEXT NOT NULL,
                    created_at REAL NOT NULL, approved_at REAL, approved_by TEXT,
                    revoked_at REAL, revoked_by TEXT, expires_at REAL NOT NULL, revision INTEGER NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS agent_sessions (
                    id TEXT PRIMARY KEY, installation_id TEXT NOT NULL REFERENCES session_installations(id),
                    token_hash TEXT UNIQUE NOT NULL, label TEXT NOT NULL, issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL, last_used_at REAL, revoked_at REAL, revision INTEGER NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS session_messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    sender TEXT NOT NULL REFERENCES session_installations(id),
                    recipient TEXT NOT NULL REFERENCES session_installations(id), body TEXT NOT NULL,
                    created_at REAL NOT NULL, status TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
                    claim_session TEXT, claim_hash TEXT, claim_expires_at REAL, acknowledged_at REAL)''',
                '''CREATE TABLE IF NOT EXISTS session_send_requests (
                    installation_id TEXT NOT NULL, request_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, message_id TEXT NOT NULL REFERENCES session_messages(id),
                    PRIMARY KEY(installation_id,request_key))''',
                '''CREATE TABLE IF NOT EXISTS agent_session_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, actor TEXT NOT NULL,
                    installation_id TEXT, session_id TEXT, message_id TEXT, created_at REAL NOT NULL)''',
                'CREATE INDEX IF NOT EXISTS session_inbox ON session_messages(recipient,seq)',
                'CREATE INDEX IF NOT EXISTS session_installation_sessions ON agent_sessions(installation_id)',
            )
            for statement in statements:
                db.execute(statement)
            previous = db.execute('SELECT owner FROM session_owner WHERE singleton=1').fetchone()
            if previous is not None and previous['owner'] != owner:
                raise ValueError('session_owner_mismatch')
            db.execute('INSERT OR IGNORE INTO session_owner VALUES(1,?)', (owner,))

    @contextmanager
    def _transaction(self):
        with self._lock, closing(sqlite3.connect(self.path, timeout=10, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA busy_timeout=10000')
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253402300000:
            raise AgentSessionError('session_clock_unavailable', 503)
        return float(value)

    def _owner(self, actor):
        if type(actor) is not str or actor != self.owner:
            raise AgentSessionError('session_owner_required', 403)

    @staticmethod
    def _cas(row, revision):
        if type(revision) is not int or revision < 1 or row['revision'] != revision:
            raise AgentSessionError('revision_conflict', 409)

    @staticmethod
    def _installation(db, identity):
        if type(identity) is not str:
            raise AgentSessionError('installation_not_found', 404)
        row = db.execute('SELECT * FROM session_installations WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise AgentSessionError('installation_not_found', 404)
        return row

    @staticmethod
    def _state(row, now):
        if row['revoked_at'] is not None:
            return 'revoked', 'owner_revoked'
        if now >= row['expires_at']:
            return 'expired', 'pairing_expired' if row['approved_at'] is None else 'installation_expired'
        return ('pending', 'awaiting_owner_approval') if row['approved_at'] is None else ('active', None)

    def _public_installation(self, row, now):
        state, reason = self._state(row, now)
        return dict(id=row['id'], family=row['family'], name=row['name'], host_label=row['host_label'],
                    state=state, reason=reason, created_at=_iso(row['created_at']),
                    approved_at=_iso(row['approved_at']), approved_by=row['approved_by'],
                    revoked_at=_iso(row['revoked_at']), revoked_by=row['revoked_by'],
                    expires_at=_iso(row['expires_at']), revision=row['revision'],
                    pairing_code=row['pairing_code'] if state == 'pending' else None,
                    key_fingerprint=row['credential_hash'][:16])

    def _public_session(self, row, installation, now):
        state = ('revoked' if row['revoked_at'] is not None or installation['revoked_at'] is not None
                 else 'expired' if now >= min(row['expires_at'], installation['expires_at']) else 'active')
        return dict(id=row['id'], installation_id=row['installation_id'], label=row['label'],
                    issued_at=_iso(row['issued_at']), expires_at=_iso(row['expires_at']),
                    last_used_at=_iso(row['last_used_at']), state=state, revision=row['revision'])

    def _active_installation(self, row, now):
        if self._state(row, now)[0] != 'active':
            raise AgentSessionError('installation_inactive', 401)

    def _principal(self, db, secret, now):
        digest = _secret(secret, 'ehs_')
        row = db.execute('SELECT * FROM agent_sessions WHERE token_hash=?', (digest,)).fetchone()
        if row is None or row['revoked_at'] is not None or now >= row['expires_at']:
            raise AgentSessionError('invalid_session', 401)
        installation = self._installation(db, row['installation_id'])
        self._active_installation(installation, now)
        db.execute('UPDATE agent_sessions SET last_used_at=? WHERE id=?', (now, row['id']))
        return SessionPrincipal(installation['id'], row['id'], installation['family'],
                                min(row['expires_at'], installation['expires_at']))

    @staticmethod
    def _audit(db, action, actor, now, installation_id=None, session_id=None, message_id=None):
        db.execute('INSERT INTO agent_session_audit(action,actor,installation_id,session_id,message_id,created_at) VALUES(?,?,?,?,?,?)',
                   (action, actor, installation_id, session_id, message_id, now))

    def request_enrollment(self, family, name, host_label, credential_sha256):
        if type(family) is not str or family not in FAMILIES:
            raise AgentSessionError('invalid_input', 422)
        name, host_label = _text(name, 100), _text(host_label, 100)
        if type(credential_sha256) is not str or re.fullmatch(r'[a-f0-9]{64}', credential_sha256) is None:
            raise AgentSessionError('invalid_input', 422)
        with self._transaction() as db:
            now = self._now()
            # Pairing records have no authority until approved. Reclaim only
            # expired, never-approved requests, including owner-declined ones.
            # Their original ten-minute deadline is older than the rate window;
            # approved identity/history and durable audit are never pruned here.
            db.execute('DELETE FROM session_installations WHERE approved_at IS NULL AND expires_at<=?', (now,))
            previous = db.execute('SELECT * FROM session_installations WHERE credential_hash=?', (credential_sha256,)).fetchone()
            if previous is not None:
                if (self._state(previous, now)[0] == 'pending' and
                        (previous['family'], previous['name'], previous['host_label']) == (family, name, host_label)):
                    return self._public_installation(previous, now)
                raise AgentSessionError('enrollment_conflict', 409)
            if (db.execute('SELECT count(*) FROM session_installations').fetchone()[0] >= MAX_INSTALLATIONS
                    or db.execute('SELECT count(*) FROM session_installations WHERE created_at>?', (now - 60,)).fetchone()[0] >= MAX_ENROLLMENTS_PER_MINUTE
                    or db.execute('SELECT count(*) FROM session_installations WHERE approved_at IS NULL AND revoked_at IS NULL AND expires_at>?', (now,)).fetchone()[0] >= MAX_PENDING):
                raise AgentSessionError('enrollment_limit', 429)
            identity = str(uuid.uuid4())
            for _ in range(8):
                code = f'{secrets.randbelow(1000000):06d}'
                if db.execute('SELECT 1 FROM session_installations WHERE pairing_code=? AND approved_at IS NULL AND revoked_at IS NULL AND expires_at>?', (code, now)).fetchone() is None:
                    break
            else:
                raise AgentSessionError('enrollment_limit', 429)
            db.execute('INSERT INTO session_installations VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,1)',
                       (identity, family, name, host_label, credential_sha256, code, now, now + PAIRING_SECONDS))
            self._audit(db, 'enrollment.requested', identity, now, identity)
            return self._public_installation(self._installation(db, identity), now)

    def list_installations(self, actor):
        self._owner(actor)
        with self._transaction() as db:
            now = self._now()
            return {'installations': [self._public_installation(row, now) for row in
                    db.execute('SELECT * FROM session_installations ORDER BY created_at DESC,id')]}

    def approve(self, actor, id, expected_revision, lifetime_seconds=86400):
        self._owner(actor)
        _number(lifetime_seconds, 1, 7 * 86400)
        with self._transaction() as db:
            now = self._now(); row = self._installation(db, id); self._cas(row, expected_revision)
            if self._state(row, now)[1] == 'pairing_expired':
                raise AgentSessionError('pairing_expired', 409)
            if self._state(row, now)[0] != 'pending':
                raise AgentSessionError('enrollment_not_pending', 409)
            db.execute('UPDATE session_installations SET approved_at=?,approved_by=?,expires_at=?,revision=revision+1 WHERE id=?',
                       (now, actor, now + lifetime_seconds, id))
            self._audit(db, 'installation.approved', actor, now, id)
            return self._public_installation(self._installation(db, id), now)

    def rename(self, actor, id, expected_revision, name):
        self._owner(actor); name = _text(name, 100)
        with self._transaction() as db:
            now = self._now(); row = self._installation(db, id); self._cas(row, expected_revision)
            if name != row['name']:
                db.execute('UPDATE session_installations SET name=?,revision=revision+1 WHERE id=?', (name, id))
                self._audit(db, 'installation.renamed', actor, now, id)
            return self._public_installation(self._installation(db, id), now)

    def _invalidate(self, session_ids):
        with self._lock:
            callbacks = [callback for sid in session_ids for callback in self._listeners.pop(sid, {}).values()]
        for callback in callbacks:
            try:
                callback()
            except BaseException:
                # Committed revocation remains successful and every listener gets its notification.
                pass

    def revoke_installation(self, actor, id, expected_revision):
        self._owner(actor)
        with self._transaction() as db:
            now = self._now(); row = self._installation(db, id); self._cas(row, expected_revision)
            if row['revoked_at'] is None:
                db.execute('UPDATE session_installations SET revoked_at=?,revoked_by=?,revision=revision+1 WHERE id=?', (now, actor, id))
                db.execute('UPDATE agent_sessions SET revoked_at=?,revision=revision+1 WHERE installation_id=? AND revoked_at IS NULL', (now, id))
                self._audit(db, 'installation.revoked', actor, now, id)
            sessions = [r['id'] for r in db.execute('SELECT id FROM agent_sessions WHERE installation_id=?', (id,))]
            result = self._public_installation(self._installation(db, id), now)
        self._invalidate(sessions)
        return result

    def exchange(self, installation_secret, label=''):
        digest = _secret(installation_secret, 'ehi_'); label = _text(label, 100, empty=True)
        with self._transaction() as db:
            now = self._now()
            installation = db.execute('SELECT * FROM session_installations WHERE credential_hash=?', (digest,)).fetchone()
            if installation is None:
                raise AgentSessionError('invalid_credential', 401)
            self._active_installation(installation, now)
            if (db.execute('SELECT count(*) FROM agent_sessions WHERE installation_id=? AND revoked_at IS NULL AND expires_at>?', (installation['id'], now)).fetchone()[0] >= MAX_ACTIVE_SESSIONS
                    or db.execute('SELECT count(*) FROM agent_sessions WHERE installation_id=?', (installation['id'],)).fetchone()[0] >= MAX_SESSION_HISTORY
                    or db.execute('SELECT count(*) FROM agent_sessions').fetchone()[0] >= MAX_ALL_SESSIONS):
                raise AgentSessionError('session_limit', 429)
            token, identity = 'ehs_' + secrets.token_urlsafe(32), str(uuid.uuid4())
            db.execute('INSERT INTO agent_sessions VALUES(?,?,?,?,?,?,NULL,NULL,1)',
                       (identity, installation['id'], _hash(token), label, now, min(now + SESSION_SECONDS, installation['expires_at'])))
            self._audit(db, 'session.issued', installation['id'], now, installation['id'], identity)
            row = db.execute('SELECT * FROM agent_sessions WHERE id=?', (identity,)).fetchone()
            return {'session': self._public_session(row, installation, now), 'token': token,
                    'installation': self._public_installation(installation, now)}

    def authenticate_session(self, session_secret):
        with self._transaction() as db:
            return self._principal(db, session_secret, self._now())

    def list_sessions(self, actor, installation_id):
        self._owner(actor)
        with self._transaction() as db:
            now = self._now(); installation = self._installation(db, installation_id)
            return {'sessions': [self._public_session(row, installation, now) for row in db.execute(
                'SELECT * FROM agent_sessions WHERE installation_id=? ORDER BY issued_at DESC,id', (installation_id,))]}

    def revoke_session(self, actor, id, expected_revision):
        self._owner(actor)
        with self._transaction() as db:
            now = self._now()
            row = db.execute('SELECT * FROM agent_sessions WHERE id=?', (id,)).fetchone()
            if row is None:
                raise AgentSessionError('session_not_found', 404)
            self._cas(row, expected_revision)
            if row['revoked_at'] is None:
                db.execute('UPDATE agent_sessions SET revoked_at=?,revision=revision+1 WHERE id=?', (now, id))
                self._audit(db, 'session.revoked', actor, now, row['installation_id'], id)
            result = self._public_session(db.execute('SELECT * FROM agent_sessions WHERE id=?', (id,)).fetchone(),
                                          self._installation(db, row['installation_id']), now)
        self._invalidate([id])
        return result

    def subscribe_session(self, secret, callback):
        if not callable(callback):
            raise AgentSessionError('invalid_input', 422)
        with self._lock:
            with self._transaction() as db:
                principal = self._principal(db, secret, self._now())
                if sum(map(len, self._listeners.values())) >= MAX_SUBSCRIPTIONS:
                    raise AgentSessionError('subscription_limit', 429)
            key = uuid.uuid4().hex
            self._listeners.setdefault(principal.session_id, {})[key] = callback
        def unsubscribe():
            with self._lock:
                listeners = self._listeners.get(principal.session_id)
                if listeners is not None:
                    listeners.pop(key, None)
                    if not listeners:
                        self._listeners.pop(principal.session_id, None)
        return principal, unsubscribe

    def peers(self, session_secret):
        with self._transaction() as db:
            now = self._now(); principal = self._principal(db, session_secret, now)
            return {'installations': [self._public_installation(row, now) for row in db.execute(
                'SELECT * FROM session_installations WHERE id!=? AND approved_at IS NOT NULL AND revoked_at IS NULL AND expires_at>? ORDER BY name,id',
                (principal.installation_id, now))]}

    @staticmethod
    def _public_message(row):
        return dict(id=row['id'], seq=row['seq'], sender=row['sender'], recipient=row['recipient'], body=row['body'],
                    status=row['status'], created_at=_iso(row['created_at']), generation=row['generation'],
                    claim_expires_at=_iso(row['claim_expires_at']), acknowledged_at=_iso(row['acknowledged_at']))

    @staticmethod
    def _received(db, principal, identity):
        if type(identity) is not str:
            raise AgentSessionError('message_not_found', 404)
        row = db.execute('SELECT * FROM session_messages WHERE id=? AND recipient=?', (identity, principal.installation_id)).fetchone()
        if row is None:
            raise AgentSessionError('message_not_found', 404)
        return row

    def send(self, session_secret, recipient, body, client_request_id):
        if (type(body) is not str or any(ord(c) < 32 and c not in '\t\r\n' or ord(c) == 127 for c in body)
                or type(client_request_id) is not str or _KEY.fullmatch(client_request_id) is None):
            raise AgentSessionError('invalid_input', 422)
        try:
            body_bytes = body.encode('utf-8')
        except UnicodeEncodeError:
            raise AgentSessionError('invalid_input', 422) from None
        if len(body_bytes) > MAX_BODY_BYTES:
            raise AgentSessionError('message_too_large', 413)
        if type(recipient) is not str:
            raise AgentSessionError('invalid_input', 422)
        fingerprint = hashlib.sha256(json.dumps([recipient, body], ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
        with self._transaction() as db:
            now = self._now(); p = self._principal(db, session_secret, now)
            previous = db.execute('SELECT * FROM session_send_requests WHERE installation_id=? AND request_key=?', (p.installation_id, client_request_id)).fetchone()
            if previous is not None:
                if previous['fingerprint'] != fingerprint:
                    raise AgentSessionError('idempotency_conflict', 409)
                return self._public_message(db.execute('SELECT * FROM session_messages WHERE id=?', (previous['message_id'],)).fetchone())
            target = self._installation(db, recipient)
            if self._state(target, now)[0] != 'active' or recipient == p.installation_id:
                raise AgentSessionError('recipient_unavailable', 404)
            if (db.execute('SELECT count(*) FROM session_messages').fetchone()[0] >= MAX_MESSAGES
                    or db.execute('SELECT count(*) FROM session_messages WHERE recipient=?', (recipient,)).fetchone()[0] >= MAX_MAILBOX):
                raise AgentSessionError('mailbox_limit', 429)
            identity = str(uuid.uuid4())
            db.execute("INSERT INTO session_messages(id,sender,recipient,body,created_at,status) VALUES(?,?,?,?,?,'pending')", (identity, p.installation_id, recipient, body, now))
            db.execute('INSERT INTO session_send_requests VALUES(?,?,?,?)', (p.installation_id, client_request_id, fingerprint, identity))
            self._audit(db, 'message.sent', p.installation_id, now, p.installation_id, p.session_id, identity)
            return self._public_message(db.execute('SELECT * FROM session_messages WHERE id=?', (identity,)).fetchone())

    def messages(self, session_secret, after=0, limit=50):
        _number(after, 0, (1 << 63) - 1); _number(limit, 1, 100)
        with self._transaction() as db:
            p = self._principal(db, session_secret, self._now())
            rows = db.execute('SELECT * FROM session_messages WHERE recipient=? AND seq>? ORDER BY seq LIMIT ?', (p.installation_id, after, limit)).fetchall()
            return {'messages': [self._public_message(row) for row in rows], 'next_cursor': rows[-1]['seq'] if rows else after}

    def get(self, session_secret, id):
        with self._transaction() as db:
            p = self._principal(db, session_secret, self._now())
            return self._public_message(self._received(db, p, id))

    def claim(self, session_secret, id, lease_seconds=30):
        _number(lease_seconds, 1, 300)
        with self._transaction() as db:
            now = self._now(); p = self._principal(db, session_secret, now); row = self._received(db, p, id)
            if row['status'] == 'acknowledged' or row['status'] == 'claimed' and row['claim_expires_at'] > now:
                raise AgentSessionError('message_not_claimable', 409)
            handle = 'ehc_' + secrets.token_urlsafe(32)
            expiry = min(now + lease_seconds, p.expires_at)
            db.execute("UPDATE session_messages SET status='claimed',claim_session=?,claim_hash=?,claim_expires_at=?,generation=generation+1 WHERE id=?", (p.session_id, _hash(handle), expiry, id))
            self._audit(db, 'message.claimed', p.installation_id, now, p.installation_id, p.session_id, id)
            row = self._received(db, p, id)
            return {'message': self._public_message(row), 'claim_handle': handle, 'generation': row['generation'], 'claim_expires_at': _iso(expiry)}

    def _claimed(self, db, p, id, handle, generation, now):
        _number(generation, 1, (1 << 63) - 1)
        try:
            digest = _secret(handle, 'ehc_')
        except AgentSessionError:
            raise AgentSessionError('claim_not_owned', 409) from None
        row = self._received(db, p, id)
        if (row['status'] != 'claimed' or row['claim_session'] != p.session_id or row['claim_hash'] != digest
                or row['generation'] != generation or row['claim_expires_at'] <= now):
            raise AgentSessionError('claim_not_owned', 409)
        return row

    def renew(self, session_secret, id, claim_handle, generation, lease_seconds=30):
        _number(lease_seconds, 1, 300)
        with self._transaction() as db:
            now = self._now(); p = self._principal(db, session_secret, now)
            row = self._claimed(db, p, id, claim_handle, generation, now)
            expiry = min(max(row['claim_expires_at'], now + lease_seconds), p.expires_at)
            db.execute('UPDATE session_messages SET claim_expires_at=? WHERE id=?', (expiry, id))
            self._audit(db, 'message.claim_renewed', p.installation_id, now, p.installation_id, p.session_id, id)
            return {'message': self._public_message(self._received(db, p, id)), 'claim_handle': claim_handle,
                    'generation': generation, 'claim_expires_at': _iso(expiry)}

    def acknowledge(self, session_secret, id, claim_handle, generation):
        with self._transaction() as db:
            now = self._now(); p = self._principal(db, session_secret, now)
            self._claimed(db, p, id, claim_handle, generation, now)
            db.execute("UPDATE session_messages SET status='acknowledged',acknowledged_at=?,claim_hash=NULL,claim_expires_at=NULL WHERE id=?", (now, id))
            self._audit(db, 'message.acknowledged', p.installation_id, now, p.installation_id, p.session_id, id)
            return self._public_message(self._received(db, p, id))

    def audit(self, actor, limit=100):
        self._owner(actor); _number(limit, 1, 100)
        with self._transaction() as db:
            rows = db.execute('SELECT * FROM agent_session_audit ORDER BY sequence DESC LIMIT ?', (limit,))
            return {'events': [dict(id=row['sequence'], at=_iso(row['created_at']), actor=row['actor'],
                                   action=row['action'], installation_id=row['installation_id'],
                                   session_id=row['session_id'], message_id=row['message_id']) for row in rows]}
