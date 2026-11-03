"""Draft persistence primitive for a bound OAuth flow; not wired to any route.

The caller must independently validate the Access assertion and enrollment on
EVERY call. These rows are not authentication proof and must not be used as an
identity provider without a reviewed ingress integration.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _binding(issuer, audience, subject, github_id):
    if (any(type(v) is not str or not v or len(v) > 2048 for v in (issuer, audience, subject))
            or type(github_id) is not int or github_id <= 0):
        raise ValueError('invalid enrolled identity binding')
    import json
    return _digest(json.dumps([issuer, audience, subject, github_id], ensure_ascii=True, separators=(',', ':')))


class BoundOAuthDraftStore:
    """Use existing SessionStore transaction/owner; no separate database."""
    def __init__(self, store, *, initialize=True):
        self.store = store
        if not initialize:
            return
        with store._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS auth_bound_oauth_states (state_hash TEXT PRIMARY KEY, verifier TEXT NOT NULL, binding TEXT NOT NULL, expires REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS auth_bound_oauth_sessions (token_hash TEXT PRIMARY KEY, binding TEXT NOT NULL, github_id INTEGER NOT NULL, csrf_token TEXT NOT NULL, expires REAL NOT NULL)')

    def create_state(self, issuer, audience, subject, github_id):
        binding = _binding(issuer, audience, subject, github_id)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        import base64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        with self.store._db() as db:
            db.execute('DELETE FROM auth_bound_oauth_states WHERE expires<=?', (self.store.clock(),))
            db.execute('INSERT INTO auth_bound_oauth_states VALUES (?,?,?,?)', (_digest(state), verifier, binding, self.store.clock() + self.store.state_ttl))
        return state, challenge

    def consume_state(self, state, issuer, audience, subject, github_id):
        binding = _binding(issuer, audience, subject, github_id)
        with self.store._db() as db:
            row = db.execute('SELECT verifier,binding,expires FROM auth_bound_oauth_states WHERE state_hash=?', (_digest(state),)).fetchone()
            if row is None or row[2] <= self.store.clock() or not hmac.compare_digest(row[1], binding):
                return None
            db.execute('DELETE FROM auth_bound_oauth_states WHERE state_hash=?', (_digest(state),))
            return row[0]

    def create_session(self, issuer, audience, subject, enrolled_github_id, verified_github_id):
        binding = _binding(issuer, audience, subject, enrolled_github_id)
        if type(verified_github_id) is not int or verified_github_id != enrolled_github_id:
            raise ValueError('identity not enrolled')
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        with self.store._db() as db:
            db.execute('INSERT INTO auth_bound_oauth_sessions VALUES (?,?,?,?,?)', (_digest(token), binding, verified_github_id, csrf, self.store.clock() + self.store.session_ttl))
        return token, csrf

    def session_for(self, token, issuer, audience, subject, github_id):
        if type(token) is not str or not token:
            return None
        binding = _binding(issuer, audience, subject, github_id)
        with self.store._read_db() as db:
            row = db.execute('SELECT binding,github_id,csrf_token,expires FROM auth_bound_oauth_sessions WHERE token_hash=?', (_digest(token),)).fetchone()
        if row is None or row[3] <= self.store.clock() or row[1] != github_id or not hmac.compare_digest(row[0], binding):
            return None
        return {'actor':f'human:{github_id}', 'github_id':github_id, 'csrf_token':row[2]}

    def delete_session(self, token):
        if token:
            with self.store._db() as db:
                db.execute('DELETE FROM auth_bound_oauth_sessions WHERE token_hash=?', (_digest(token),))


def require_enrolled_access(request, config, expected_subject):
    """Read the ingress-only proof; user-supplied state/headers are not proof.

    Draft helper, not wired into the OAuth router. A verified assertion at the
    Cloudflare front door and independently enrolled subject are mandatory.
    """
    from .cloudflare_access import CloudflareAccessConfig, _verified_access
    if type(config) is not CloudflareAccessConfig or type(expected_subject) is not str or not expected_subject:
        raise ValueError('invalid access enrollment')
    identity = _verified_access(request.scope, config)
    if identity is None or not hmac.compare_digest(identity.subject.encode(), expected_subject.encode()):
        raise PermissionError('enrolled Access identity required')
    return identity
