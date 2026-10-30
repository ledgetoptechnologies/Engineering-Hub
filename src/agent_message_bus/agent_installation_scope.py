"""Owner-maintained installation classification, separate from project grants.

No family, name, hostname or project count confers internal access. Existing
installations remain unclassified. A classification never keeps a revoked or
expired installation/session alive. This module does not attach routes or tools.
"""

from .agent_sessions import AgentSessionError, AgentSessionRepository, _iso


KINDS = frozenset({'internal', 'project_only', 'unclassified'})


class InstallationScopeRepository:
    def __init__(self, sessions):
        if type(sessions) is not AgentSessionRepository:
            raise TypeError('explicit_session_repository_required')
        self.sessions = sessions
        with sessions._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS session_installation_scopes (
                installation_id TEXT PRIMARY KEY REFERENCES session_installations(id),
                kind TEXT NOT NULL CHECK(kind IN ('internal','project_only','unclassified')),
                revision INTEGER NOT NULL CHECK(revision>0),
                updated_by TEXT NOT NULL, updated_at REAL NOT NULL)''')

    @staticmethod
    def _row(db, installation_id):
        return db.execute('SELECT * FROM session_installation_scopes WHERE installation_id=?',
                          (installation_id,)).fetchone()

    def _public(self, installation, row, now):
        kind = row['kind'] if row is not None else 'unclassified'
        return {'installation_id': installation['id'], 'kind': kind,
                'revision': row['revision'] if row is not None else 0,
                'updated_by': row['updated_by'] if row is not None else None,
                'updated_at': _iso(row['updated_at']) if row is not None else None,
                'internal_effective': kind == 'internal'
                    and self.sessions._state(installation, now)[0] == 'active'}

    def get(self, actor, installation_id):
        self.sessions._owner(actor)
        with self.sessions._transaction() as db:
            installation = self.sessions._installation(db, installation_id)
            return self._public(installation, self._row(db, installation_id), self.sessions._now())

    def list(self, actor):
        self.sessions._owner(actor)
        with self.sessions._transaction() as db:
            now = self.sessions._now()
            installations = db.execute('SELECT * FROM session_installations WHERE approved_at IS NOT NULL '
                'ORDER BY CASE WHEN revoked_at IS NULL AND (expires_at=0 OR expires_at>?) '
                'THEN 0 ELSE 1 END,created_at DESC,id LIMIT 257', (now,)).fetchall()
            truncated = len(installations) > 256
            installations = installations[:256]
            ids = [row['id'] for row in installations]
            scopes = db.execute('SELECT * FROM session_installation_scopes WHERE installation_id IN ('
                + ','.join('?' for _ in ids) + ')', ids).fetchall() if ids else []
            by_id = {row['installation_id']: row for row in scopes}
            return {'installations': [self._public(row, by_id.get(row['id']), now) for row in installations],
                    'truncated': truncated}

    def set(self, actor, installation_id, expected_revision, kind):
        return self.set_many(actor, [{'installation_id': installation_id,
            'expected_revision': expected_revision}], kind)['installations'][0]

    def set_many(self, actor, installations, kind):
        self.sessions._owner(actor)
        if (type(kind) is not str or kind not in KINDS or type(installations) is not list
                or not 1 <= len(installations) <= 256):
            raise AgentSessionError('invalid_installation_scope', 422)
        seen = set()
        for item in installations:
            if (type(item) is not dict or set(item) != {'installation_id', 'expected_revision'}
                    or type(item['installation_id']) is not str
                    or item['installation_id'] in seen
                    or type(item['expected_revision']) is not int or item['expected_revision'] < 0):
                raise AgentSessionError('invalid_installation_scope', 422)
            seen.add(item['installation_id'])
        with self.sessions._transaction() as db:
            now = self.sessions._now()
            result = []
            for item in installations:
                installation_id, expected_revision = item['installation_id'], item['expected_revision']
                installation = self.sessions._installation(db, installation_id)
                # Pending enrollments can be pruned by the base repository.
                if installation['approved_at'] is None:
                    raise AgentSessionError('installation_not_approved', 409)
                if kind == 'internal':
                    self.sessions._active_installation(installation, now)
                row = self._row(db, installation_id)
                if expected_revision != (row['revision'] if row is not None else 0):
                    raise AgentSessionError('scope_revision_conflict', 409)
                db.execute("""INSERT INTO session_installation_scopes VALUES(?,?,?,?,?)
                    ON CONFLICT(installation_id) DO UPDATE SET kind=excluded.kind,
                    revision=excluded.revision,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                    (installation_id, kind, expected_revision + 1, actor, now))
                self.sessions._audit(db, 'installation.scope.' + kind, actor, now, installation_id)
                result.append(self._public(installation, self._row(db, installation_id), now))
            # Any conflict/denial rolls back every row and audit in this batch.
            return {'installations': result}

    def require_internal(self, session_secret):
        """Authenticate the actual bearer and classification in one transaction.

        Call again after upstream I/O; do not cache this result as a grant. The
        caller must already enforce the private transport's Host and source IP.
        """
        with self.sessions._transaction() as db:
            now = self.sessions._now()
            principal = self.sessions._principal(db, session_secret, now)
            row = self._row(db, principal.installation_id)
            if row is None or row['kind'] != 'internal':
                raise AgentSessionError('internal_installation_required', 403)
            return principal


def attach_scope_routes(app, scopes, human):
    """Owner browser routes; scope changes use the existing CSRF boundary."""
    from typing import Literal
    from fastapi import HTTPException, Request
    from pydantic import BaseModel, ConfigDict, Field
    from .agent_sessions_api import OWNER_CONNECTION_PREFIX

    class ScopeWrite(BaseModel):
        model_config = ConfigDict(extra='forbid', strict=True)
        expected_revision: int = Field(ge=0, le=2**53-1)
        kind: Literal['internal', 'project_only', 'unclassified']

    class ScopeTarget(BaseModel):
        model_config = ConfigDict(extra='forbid', strict=True)
        installation_id: str = Field(pattern=r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
        expected_revision: int = Field(ge=0, le=2**53-1)

    class ScopeBatch(BaseModel):
        model_config = ConfigDict(extra='forbid', strict=True)
        installations: list[ScopeTarget] = Field(min_length=1, max_length=256)
        kind: Literal['internal', 'project_only', 'unclassified']

    def owner(request, mutation=False):
        if request.headers.getlist('authorization'):
            raise HTTPException(403, detail={'code': 'human_session_required'})
        actor = human(request, mutation=mutation)
        scopes.sessions._owner(actor)
        return actor

    path = OWNER_CONNECTION_PREFIX + '/installations/{installation_id}/scope'

    @app.get(path)
    def read_scope(installation_id: str, request: Request):
        return scopes.get(owner(request), installation_id)

    @app.put(path)
    def write_scope(installation_id: str, payload: ScopeWrite, request: Request):
        return scopes.set(owner(request, True), installation_id, **payload.model_dump())

    @app.put(OWNER_CONNECTION_PREFIX + '/installation-scopes')
    def write_scopes(payload: ScopeBatch, request: Request):
        return scopes.set_many(owner(request, True), **payload.model_dump())

    @app.get(OWNER_CONNECTION_PREFIX + '/installation-scopes')
    def read_scopes(request: Request):
        return scopes.list(owner(request))
