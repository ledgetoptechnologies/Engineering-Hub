"""Explicit staging-only installation grants for a finite set of Hub tools.

Lock order: session writer, Hub writer, knowledge writer. No network/await is
permitted inside admission. An OS lock refuses a second bridge process; the
session SQLite writer also serializes revocation from independent connections.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import inspect
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from fastapi import HTTPException, Request, Query
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from agent_message_bus.agent_sessions import AgentSessionError, AgentSessionRepository, SessionPrincipal, _iso
from agent_message_bus.agent_sessions_api import Closed, OWNER_CONNECTION_PREFIX
from agent_message_bus.agent_sessions_transport import bearer
from agent_message_bus import session_hub_context as context
from .repository import HubError


TOOLS = {
    'hub_project_list': 'project.read', 'hub_project_get': 'project.read',
    'hub_component_list': 'component.read', 'hub_component_get': 'component.read',
    'hub_component_create': 'component.write', 'hub_component_update': 'component.write',
    'hub_component_retire': 'component.write', 'hub_task_get': 'task.read',
    'hub_staging_task_propose': 'task.propose', 'hub_task_claim': 'task.claim',
    'hub_task_candidate': 'task.candidate', 'hub_task_review': 'task.review',
    'hub_planning_list': 'planning.read', 'hub_planning_get': 'planning.read',
    'hub_planning_put': 'planning.write', 'hub_decision_request': 'decision.request',
    'hub_entity_withdraw': 'entity.withdraw',
}
CAPABILITIES = tuple(sorted(set(TOOLS.values())))


class GrantWrite(Closed):
    expected_revision: int = Field(ge=0)
    projects: list[str] = Field(min_length=1, max_length=32)
    capabilities: list[str] = Field(min_length=1, max_length=len(CAPABILITIES))
    lifetime_seconds: int = Field(default=86400, ge=60, le=604800)


class GrantRevoke(Closed):
    expected_revision: int = Field(ge=1)


class ClaimRecovery(Closed):
    expected_revision: int = Field(ge=1)
    target_session_id: str = Field(min_length=36, max_length=36)
    reason: str = Field(min_length=1, max_length=500)
    client_request_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')


@dataclass
class _Operation:
    bridge: object
    principal: context.HubSessionPrincipal
    db: object
    secret: str
    capability: str
    projects: tuple
    used_projects: set = None

    def project(self, project):
        if project not in self.projects:
            raise HubError('hub_project_forbidden', 403)
        if self.bridge.projects.get_project(project)['archived']:
            raise HubError('project_archived', 409)
        if self.used_projects is None:
            self.used_projects = set()
        self.used_projects.add(project)

    def before_commit(self, storage_path, db, **metadata):
        if Path(storage_path).resolve() not in self.bridge.paths:
            raise HubError('hub_repository_binding_required', 403)
        self.bridge._live(self.db, self.secret, self.principal.authorization_revision,
                          self.capability, session_id=self.principal.session_id)
        for project in tuple(self.used_projects or ()):
            self.project(project)
        if Path(storage_path).resolve() == self.bridge.knowledge_path:
            # Planning is committed in the separate shared knowledge database.
            # Record exact session attribution in that same transaction.
            p = self.principal
            db.execute('INSERT INTO knowledge_session_provenance '
                       '(installation_id,session_id,family,grant_revision,capability,project,path,version) VALUES(?,?,?,?,?,?,?,?)',
                       (p.installation_id, p.session_id, p.family,
                        p.authorization_revision, self.capability, metadata['project'], metadata['path'], metadata['version']))


class SessionHubBridge:
    def __init__(self, sessions, repo, projects, components, knowledge):
        if type(sessions) is not AgentSessionRepository:
            raise ValueError('exact_agent_session_repository_required')
        self.sessions, self.repo, self.projects, self.components = sessions, repo, projects, components
        self.pid = os.getpid()
        self.knowledge_path = Path(knowledge.db_path).resolve()
        self.paths = {Path(repo.path).resolve(), self.knowledge_path}
        self._owner_file = open(str(sessions.path) + '.hub-bridge.lock', 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self._owner_file.seek(0)
                self._owner_file.write(b'0'); self._owner_file.flush(); self._owner_file.seek(0)
                msvcrt.locking(self._owner_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._owner_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with sessions._transaction() as db:
                db.execute('CREATE TABLE IF NOT EXISTS session_hub_grants '
                    '(installation_id TEXT PRIMARY KEY REFERENCES session_installations(id), '
                    'revision INTEGER NOT NULL, projects TEXT NOT NULL, capabilities TEXT NOT NULL, '
                    'expires_at REAL NOT NULL, approved_by TEXT NOT NULL, approved_at REAL NOT NULL, '
                    'revoked_at REAL)')
            with repo._transaction() as db:
                db.execute('CREATE TABLE IF NOT EXISTS hub_session_provenance '
                    '(audit_sequence INTEGER PRIMARY KEY REFERENCES hub_audit(sequence), '
                    'installation_id TEXT NOT NULL, session_id TEXT NOT NULL, family TEXT NOT NULL, '
                    'grant_revision INTEGER NOT NULL, capability TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS hub_claim_recoveries '
                    '(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, actor TEXT NOT NULL, request_key TEXT NOT NULL, '
                    'fingerprint TEXT NOT NULL, receipt TEXT NOT NULL, UNIQUE(actor,request_key))')
            with knowledge._connection() as db:
                db.execute('CREATE TABLE IF NOT EXISTS knowledge_session_provenance '
                    '(sequence INTEGER PRIMARY KEY AUTOINCREMENT, installation_id TEXT NOT NULL, '
                    'session_id TEXT NOT NULL, family TEXT NOT NULL, grant_revision INTEGER NOT NULL, '
                    'capability TEXT NOT NULL, project TEXT NOT NULL, path TEXT NOT NULL, version INTEGER NOT NULL)')
            sessions._hub_bridge_tools = tuple(TOOLS)
            sessions._hub_bridge_owner = self
        except BaseException:
            self.close()
            raise

    def close(self):
        if getattr(self.sessions, '_hub_bridge_owner', None) is self:
            self.sessions._hub_bridge_tools = ()
            self.sessions._hub_bridge_owner = None
        self._owner_file.close()

    def _owned(self):
        if os.getpid() != self.pid or self._owner_file.closed:
            raise HubError('hub_bridge_process_owner_required', 503)

    def _grant(self, db, installation_id):
        return db.execute('SELECT * FROM session_hub_grants WHERE installation_id=?', (installation_id,)).fetchone()

    def _public(self, installation, row, now):
        inactive = self.sessions._state(installation, now)[0] != 'active'
        if row is None:
            return dict(installation_id=installation['id'], revision=0, state='inactive' if inactive else 'absent',
                        projects=[], capabilities=[], expires_at=None, approved_by=None, approved_at=None,
                        effective=dict(state='inactive' if inactive else 'absent',
                            reason='connection_inactive' if inactive else 'grant_absent',
                            projects=[], capabilities=[], expires_at=None))
        state = ('inactive' if inactive else 'revoked' if row['revoked_at'] is not None else
                 'expired' if row['expires_at'] <= now else 'active')
        active_projects = set(p['id'] for p in self.projects.list_projects())
        projects = [p for p in json.loads(row['projects']) if p in active_projects] if state == 'active' else []
        effective_state = 'inactive' if state == 'active' and not projects else state
        reason = {'inactive': 'connection_inactive', 'revoked': 'owner_revoked',
                  'expired': 'grant_expired', 'active': 'authorized'}[state]
        if state == 'active' and not projects:
            reason = 'no_active_projects'
        return dict(installation_id=installation['id'], revision=row['revision'], state=state,
                    projects=json.loads(row['projects']), capabilities=json.loads(row['capabilities']),
                    expires_at=_iso(row['expires_at']), approved_by=row['approved_by'],
                    approved_at=_iso(row['approved_at']), effective=dict(state=effective_state, reason=reason,
                        projects=projects, capabilities=json.loads(row['capabilities']) if projects else [],
                        expires_at=_iso(row['expires_at']) if projects else None))

    def grant_get(self, actor, installation_id):
        self._owned(); self.sessions._owner(actor)
        with self.sessions._transaction() as db:
            installation = self.sessions._installation(db, installation_id)
            return self._public(installation, self._grant(db, installation_id), self.sessions._now())

    def grant_put(self, actor, installation_id, expected_revision, projects, capabilities, lifetime_seconds=86400):
        self._owned(); self.sessions._owner(actor)
        if (type(expected_revision) is not int or expected_revision < 0
                or type(lifetime_seconds) is not int or not 60 <= lifetime_seconds <= 604800
                or type(projects) is not list or not 1 <= len(projects) <= 32
                or any(type(p) is not str for p in projects) or len(set(projects)) != len(projects)
                or type(capabilities) is not list or not capabilities
                or any(type(c) is not str or c not in CAPABILITIES for c in capabilities)
                or len(set(capabilities)) != len(capabilities)):
            raise HubError('invalid_hub_grant', 422)
        with self.sessions._transaction() as db:
            now = self.sessions._now()
            installation = self.sessions._installation(db, installation_id)
            self.sessions._active_installation(installation, now)
            row = self._grant(db, installation_id)
            if expected_revision != (row['revision'] if row is not None else 0):
                raise HubError('grant_revision_conflict', 409)
            with self.repo._transaction() as hub:
                for project in projects:
                    self.repo._project(project, hub)
                db.execute('INSERT INTO session_hub_grants VALUES(?,?,?,?,?,?,?,NULL) '
                    'ON CONFLICT(installation_id) DO UPDATE SET revision=excluded.revision, '
                    'projects=excluded.projects,capabilities=excluded.capabilities,expires_at=excluded.expires_at, '
                    'approved_by=excluded.approved_by,approved_at=excluded.approved_at,revoked_at=NULL',
                    (installation_id, expected_revision + 1, json.dumps(sorted(projects)),
                     json.dumps(sorted(capabilities)), min(now + lifetime_seconds, installation['expires_at']), actor, now))
            self.sessions._audit(db, 'hub_grant.updated', actor, now, installation_id)
            return self._public(installation, self._grant(db, installation_id), now)

    def grant_revoke(self, actor, installation_id, expected_revision):
        self._owned(); self.sessions._owner(actor)
        with self.sessions._transaction() as db:
            now = self.sessions._now(); installation = self.sessions._installation(db, installation_id)
            row = self._grant(db, installation_id)
            if row is None or type(expected_revision) is not int or row['revision'] != expected_revision:
                raise HubError('grant_revision_conflict', 409)
            db.execute('UPDATE session_hub_grants SET revision=revision+1,revoked_at=? WHERE installation_id=?', (now, installation_id))
            self.sessions._audit(db, 'hub_grant.revoked', actor, now, installation_id)
            return self._public(installation, self._grant(db, installation_id), now)

    def _live(self, db, secret, revision=None, capability=None, *, session_id=None):
        self._owned(); now = self.sessions._now()
        if secret is not None:
            principal = self.sessions._principal(db, secret, now)
        else:
            row = db.execute('SELECT * FROM agent_sessions WHERE id=?', (session_id,)).fetchone()
            if row is None or row['revoked_at'] is not None or row['expires_at'] <= now:
                raise AgentSessionError('invalid_session', 401)
            installation = self.sessions._installation(db, row['installation_id'])
            self.sessions._active_installation(installation, now)
            principal = SessionPrincipal(installation['id'], row['id'], installation['family'],
                min(row['expires_at'], installation['expires_at']))
        grant = self._grant(db, principal.installation_id)
        if grant is None or grant['revoked_at'] is not None or grant['expires_at'] <= now:
            raise HubError('hub_grant_required', 403)
        if revision is not None and grant['revision'] != revision:
            raise HubError('hub_grant_required', 403)
        if capability not in json.loads(grant['capabilities']):
            raise HubError('hub_capability_forbidden', 403)
        return principal, grant

    def recover_claim(self, actor, task_id, expected_revision, target_session_id, reason, client_request_id):
        self._owned(); self.sessions._owner(actor)
        from .entity_context import bounded_text
        try:
            reason = bounded_text(reason, 500)
        except ValueError:
            raise HubError('invalid_recovery_target', 422) from None
        if (type(expected_revision) is not int or expected_revision < 1
                or type(client_request_id) is not str
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', client_request_id) is None):
            raise HubError('invalid_recovery_target', 422)
        fingerprint = hashlib.sha256(json.dumps([task_id, expected_revision, target_session_id, reason],
            separators=(',', ':')).encode()).hexdigest()
        with self.sessions._transaction() as db:
            principal, grant = self._live(db, None, capability='task.claim', session_id=target_session_id)
            p = context.HubSessionPrincipal(principal.installation_id, principal.session_id, principal.family,
                min(principal.expires_at, grant['expires_at']), grant['revision'])
            operation = _Operation(self, p, db, None, 'task.claim', tuple(json.loads(grant['projects'])))
            token = context.current.set(operation)
            try:
                with self.repo._transaction() as hub:
                    task = self.repo._get(hub, task_id, 'task')
                    operation.project(task['project']); self.repo._project(task['project'], hub)
                    if task['owner'] != p.actor or task.get('owner_family') != p.family:
                        raise HubError('invalid_recovery_target', 422)
                    prior = hub.execute('SELECT * FROM hub_claim_recoveries WHERE actor=? AND request_key=?',
                                        (actor, client_request_id)).fetchone()
                    if prior is not None:
                        if prior['fingerprint'] != fingerprint:
                            raise HubError('idempotency_conflict', 409)
                        return dict(task=task, recovery=json.loads(prior['receipt']), replayed=True)
                    self.repo._cas(task, expected_revision)
                    if task['status'] not in {'claimed', 'in_review', 'verified'}:
                        raise HubError('invalid_recovery_target', 422)
                    old = db.execute('SELECT * FROM agent_sessions WHERE id=?', (task.get('owner_session_id'),)).fetchone()
                    if old is None or old['installation_id'] != p.installation_id:
                        raise HubError('invalid_recovery_target', 422)
                    provenance = hub.execute('SELECT p.* FROM hub_session_provenance p JOIN hub_audit a '
                        'ON a.sequence=p.audit_sequence WHERE a.entity_id=? AND a.action IN '
                        "('task.claimed','task.claim_recovered') ORDER BY a.revision DESC LIMIT 1", (task_id,)).fetchone()
                    if (provenance is None or provenance['installation_id'] != p.installation_id
                            or provenance['session_id'] != old['id'] or provenance['family'] != p.family
                            or task.get('candidate_family', p.family) != p.family):
                        raise HubError('invalid_recovery_target', 422)
                    if old['revoked_at'] is None and old['expires_at'] > self.sessions._now():
                        raise HubError('claim_session_active', 409)
                    if hub.execute("SELECT 1 FROM sqlite_master WHERE name='hub_work_jobs'").fetchone():
                        if hub.execute("SELECT 1 FROM hub_work_jobs WHERE json_extract(target,'$.task_id')=? LIMIT 1", (task_id,)).fetchone():
                            raise HubError('task_execution_present', 409)
                    for row in hub.execute("SELECT data FROM hub_entities WHERE kind='run'"):
                        run = json.loads(row['data'])
                        if run['task_id'] == task_id and run['status'] in {'pending', 'running', 'held', 'failed'}:
                            raise HubError('task_execution_present', 409)
                    if hub.execute('SELECT count(*) FROM hub_claim_recoveries WHERE task_id=?', (task_id,)).fetchone()[0] >= 128:
                        raise HubError('recovery_history_full', 409)
                    receipt = dict(id=str(uuid.uuid4()), task_id=task_id, installation_id=p.installation_id,
                        family=p.family, prior_session_id=old['id'], target_session_id=p.session_id,
                        prior_revision=task['revision'], resulting_revision=task['revision'] + 1,
                        actor=actor, reason=reason, at=_iso(self.sessions._now()),
                        prior_candidate_commit=task.get('candidate_commit'), prior_review=task.get('review'))
                    task.update(owner_session_id=p.session_id, review=None, status='claimed')
                    task = self.repo._save(hub, actor, 'task.claim_recovered', 'task', task)
                    hub.execute('INSERT INTO hub_claim_recoveries VALUES(?,?,?,?,?,?)',
                        (receipt['id'], task_id, actor, client_request_id, fingerprint, json.dumps(receipt, sort_keys=True)))
                    return dict(task=task, recovery=receipt, replayed=False)
            finally:
                context.current.reset(token)

    def claim_history(self, actor, task_id):
        self._owned(); self.sessions._owner(actor)
        with self.repo._connection() as db:
            task = self.repo._get(db, task_id, 'task')
            rows = db.execute('SELECT receipt FROM hub_claim_recoveries WHERE task_id=? ORDER BY rowid LIMIT 128', (task_id,))
            return dict(task=task, recoveries=[json.loads(row['receipt']) for row in rows])

    def task_claims(self, actor, installation_id, after=None, limit=50):
        self._owned(); self.sessions._owner(actor)
        if type(limit) is not int or not 1 <= limit <= 100 or (after is not None and (type(after) is not str or len(after) != 36)):
            raise HubError('invalid_input', 422)
        with self.sessions._transaction() as db:
            installation = self.sessions._installation(db, installation_id)
            now = self.sessions._now()
            grant = self._public(installation, self._grant(db, installation_id), now)
            live = [r['id'] for r in db.execute('SELECT id FROM agent_sessions WHERE installation_id=? '
                'AND revoked_at IS NULL AND expires_at>? ORDER BY id', (installation_id, now))]
            with self.repo._connection() as hub:
                rows = hub.execute("SELECT data FROM hub_entities WHERE kind='task' AND "
                    "json_extract(data,'$.owner')=? AND id>? ORDER BY id LIMIT ?",
                    ('installation:' + installation_id, after or '', limit + 1)).fetchall()
                items = []
                for row in rows[:limit]:
                    task = json.loads(row['data'])
                    old = db.execute('SELECT * FROM agent_sessions WHERE id=?', (task.get('owner_session_id'),)).fetchone()
                    claim_state = ('unknown' if old is None else 'revoked' if old['revoked_at'] is not None
                        or installation['revoked_at'] is not None else 'expired' if min(old['expires_at'], installation['expires_at']) <= now else 'active')
                    targets = live if (grant['effective']['state'] == 'active' and task['project'] in grant['effective']['projects']
                        and 'task.claim' in grant['effective']['capabilities']) else []
                    execution = False
                    if hub.execute("SELECT 1 FROM sqlite_master WHERE name='hub_work_jobs'").fetchone():
                        execution = hub.execute("SELECT 1 FROM hub_work_jobs WHERE json_extract(target,'$.task_id')=? LIMIT 1", (task['id'],)).fetchone() is not None
                    execution = execution or any(json.loads(r['data']).get('task_id') == task['id'] and
                        json.loads(r['data']).get('status') in {'pending','running','held','failed'}
                        for r in hub.execute("SELECT data FROM hub_entities WHERE kind='run'"))
                    count = hub.execute('SELECT count(*) FROM hub_claim_recoveries WHERE task_id=?', (task['id'],)).fetchone()[0]
                    reason = ('claim_session_active' if claim_state == 'active' else 'invalid_claim_provenance' if claim_state == 'unknown'
                        or old['installation_id'] != installation_id or task.get('owner_family') != installation['family'] else
                        'task_state_ineligible' if task['status'] not in {'claimed','in_review','verified'} else
                        'task_execution_present' if execution else 'recovery_history_full' if count >= 128 else
                        'no_authorized_target' if not targets else 'ready')
                    items.append(dict(task=task, claim_session_id=task.get('owner_session_id'), claim_state=claim_state,
                        recovery=dict(state='recoverable' if reason == 'ready' else 'blocked', reason=reason,
                                      target_session_ids=targets if reason == 'ready' else [])))
                return dict(installation_id=installation_id, items=items,
                    next_after=items[-1]['task']['id'] if len(rows) > limit else None)

    @contextmanager
    def admit(self, secret, tool):
        if tool not in TOOLS:
            raise HubError('hub_capability_forbidden', 403)
        # Held through every downstream commit. Revokes use this same writer;
        # a revoke that wins first denies, otherwise it follows the committed op.
        with self.sessions._transaction() as db:
            principal, grant = self._live(db, secret, capability=TOOLS[tool])
            p = context.HubSessionPrincipal(principal.installation_id, principal.session_id,
                principal.family, min(principal.expires_at, grant['expires_at']), grant['revision'])
            operation = _Operation(self, p, db, secret, TOOLS[tool], tuple(json.loads(grant['projects'])))
            token = context.current.set(operation)
            try:
                yield operation
            finally:
                context.current.reset(token)

    def _scope(self, tool, arguments, operation):
        payload = arguments.get('payload')
        data = payload.model_dump() if hasattr(payload, 'model_dump') else payload or {}
        project = arguments.get('project', data.get('project'))
        if tool == 'hub_project_get':
            project = arguments['project_id']
        elif tool in {'hub_task_get', 'hub_task_claim', 'hub_task_candidate', 'hub_task_review'}:
            project = self.repo.get_task(arguments['task_id'])['project']
        elif tool in {'hub_component_get', 'hub_component_update', 'hub_component_retire'}:
            identity = arguments.get('component_id', data.get('component_id'))
            project = self.components.get_component(identity)['project']
        elif tool == 'hub_entity_withdraw':
            project = self.repo._read(arguments['entity_id'], arguments['kind'])['project']
        if project is not None:
            operation.project(project)

    def registration(self, mcp):
        bridge = self
        class Registration:
            def tool(self, *decorator_args, **decorator_kwargs):
                def decorate(fn):
                    if fn.__name__ not in TOOLS:
                        return mcp.tool(*decorator_args, **decorator_kwargs)(fn)
                    if inspect.iscoroutinefunction(fn):
                        raise ValueError('synchronous_hub_bridge_tool_required')
                    signature = inspect.signature(fn)
                    @wraps(fn)
                    def wrapped(*args, **kwargs):
                        arguments = signature.bind(*args, **kwargs).arguments
                        request = arguments['ctx'].request_context.request
                        values = request.headers.getlist('authorization')
                        if not any(value.lower().startswith('bearer ehs_') for value in values):
                            return fn(*args, **kwargs)
                        try:
                            with bridge.admit(bearer(request), fn.__name__) as operation:
                                bridge._scope(fn.__name__, arguments, operation)
                                if fn.__name__ == 'hub_project_list':
                                    from .projects_api import project_view
                                    items = [bridge.projects.get_project(p) for p in operation.projects]
                                    return {'items': [project_view(p) for p in items if not p['archived']]}
                                result = fn(*args, **kwargs)
                                if operation.capability.endswith('.read'):
                                    operation.before_commit(bridge.repo.path, None)
                                return result
                        except (HubError, AgentSessionError) as error:
                            raise ToolError(error.code) from None
                    return mcp.tool(*decorator_args, **decorator_kwargs)(wrapped)
                return decorate
        return Registration()

    def attach_routes(self, app, human):
        def owner(request, mutation=False):
            if request.headers.getlist('authorization'):
                raise HTTPException(403, detail={'code': 'human_session_required'})
            actor = human(request, mutation=mutation)
            self.sessions._owner(actor)
            return actor
        def owner_call(operation, *args, **kwargs):
            # Owner authentication has already succeeded. Connection/target
            # state must not masquerade as a lost human browser session.
            try:
                return operation(*args, **kwargs)
            except AgentSessionError as error:
                if error.code == 'installation_inactive':
                    raise HTTPException(409, detail={'code': 'installation_inactive'}) from None
                if error.code == 'invalid_session':
                    raise HTTPException(422, detail={'code': 'invalid_recovery_target'}) from None
                raise
        @app.get(OWNER_CONNECTION_PREFIX + '/hub-capabilities')
        def capabilities(request: Request):
            owner(request)
            return dict(enabled=True, capabilities=list(CAPABILITIES), features=dict(task_recovery=True))
        path = OWNER_CONNECTION_PREFIX + '/installations/{installation_id}/hub-grant'
        @app.get(path)
        def get(installation_id: str, request: Request):
            return owner_call(self.grant_get, owner(request), installation_id)
        @app.put(path)
        def put(installation_id: str, data: GrantWrite, request: Request):
            return owner_call(self.grant_put, owner(request, True), installation_id, **data.model_dump())
        @app.post(path + '/revoke')
        def revoke(installation_id: str, data: GrantRevoke, request: Request):
            return owner_call(self.grant_revoke, owner(request, True), installation_id, data.expected_revision)
        @app.get(OWNER_CONNECTION_PREFIX + '/tasks/{task_id}/claim-recoveries')
        def history(task_id: str, request: Request):
            return owner_call(self.claim_history, owner(request), task_id)
        @app.post(OWNER_CONNECTION_PREFIX + '/tasks/{task_id}/recover-claim')
        def recover(task_id: str, data: ClaimRecovery, request: Request):
            return owner_call(self.recover_claim, owner(request, True), task_id, **data.model_dump())
        @app.get(OWNER_CONNECTION_PREFIX + '/installations/{installation_id}/task-claims')
        def claims(installation_id: str, request: Request, after: str | None = None,
                   limit: int = Query(default=50, ge=1, le=100)):
            return owner_call(self.task_claims, owner(request), installation_id, after, limit)
