"""Durable role coordination. Roles never authorize or dispatch execution."""
import json
import hashlib
import re
import uuid

from agent_message_bus import session_hub_context as context
from .repository import HubError

_ROLE = re.compile(r'[a-z][a-z0-9-]{0,63}')
_NAMESPACE = uuid.UUID('8d82e2b5-5038-4c20-a859-f24d99f882d1')


def _identity(role):
    if type(role) is not str or not _ROLE.fullmatch(role):
        raise HubError('invalid_role', 422)
    return str(uuid.uuid5(_NAMESPACE, 'role:' + role))


def _session(actor):
    operation = context.current.get()
    if operation is None or not context.actor_allowed(actor):
        raise HubError('role_session_required', 403)
    return operation


class RoleRepository:
    def __init__(self, repo):
        self.repo = repo

    def _role(self, db, role):
        item = self.repo._get(db, _identity(role), 'coordination_role')
        if item.get('role_id') != role:
            raise HubError('role_unavailable', 404)
        for project in item['projects']:
            self.repo._project(project, db)
        return item

    def configure(self, actor, role, expected_revision, projects, installations, paths, standing_rules, tool_scope):
        """Human-owned role metadata; scope is descriptive, not a grant."""
        self.repo._actor(actor, human=True)
        key = _identity(role)
        if (type(projects) is not list or not 1 <= len(projects) <= 32
                or any(type(p) is not str for p in projects) or len(set(projects)) != len(projects)
                or type(paths) is not list or len(paths) > 64 or type(tool_scope) is not list or len(tool_scope) > 64):
            raise HubError('invalid_role', 422)
        if any(type(p) is not str or not p or len(p) > 256 or p.startswith('/') or '\\' in p
               or any(part in ('', '.', '..') for part in p.split('/')) for p in paths):
            raise HubError('invalid_role', 422)
        if any(type(t) is not str or not re.fullmatch(r'[a-z][a-z0-9._-]{0,127}', t) for t in tool_scope):
            raise HubError('invalid_role', 422)
        if type(installations) is not list or not 1 <= len(installations) <= 32:
            raise HubError('invalid_role', 422)
        try:
            if any(type(value) is not str or str(uuid.UUID(value)) != value for value in installations) or len(set(installations)) != len(installations):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise HubError('invalid_role', 422) from None
        rules = self.repo._text(standing_rules, 4096)
        with self.repo._transaction() as db:
            for project in projects:
                self.repo._project(project, db)
            row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='coordination_role'", (key,)).fetchone()
            if row is None:
                if type(expected_revision) is not int or expected_revision != 0:
                    raise HubError('revision_conflict', 409)
                item = dict(id=key, role_id=role, projects=projects, installations=installations, paths=paths, standing_rules=rules,
                            tool_scope=tool_scope, handoff={'revision': 0, 'text': ''}, lease=None,
                            generation=0, coordination_only=True, execution_authorization=False)
                return self.repo._save(db, actor, 'role.created', 'coordination_role', item, new=True, publish=False)
            item = json.loads(row['data'])
            self.repo._cas(item, expected_revision)
            # Never alter scope while any lease is present; release or explicit
            # expired takeover must first preserve its state and claims.
            if item.get('lease') is not None:
                raise HubError('role_leased', 409)
            claims = db.execute("SELECT data FROM hub_entities WHERE kind='role_claim' AND json_extract(data,'$.role_id')=? AND json_extract(data,'$.status')='claimed'", (role,)).fetchall()
            if any(json.loads(claim['data'])['project'] not in projects for claim in claims):
                raise HubError('role_claim_scope_conflict', 409)
            item.update(projects=projects, installations=installations, paths=paths, standing_rules=rules, tool_scope=tool_scope)
            return self.repo._save(db, actor, 'role.configured', 'coordination_role', item, publish=False)

    def get(self, actor, role):
        _session(actor)
        with self.repo._connection() as db:
            return self._role(db, role)

    def owner_get(self, actor, role):
        self.repo._actor(actor, human=True)
        with self.repo._connection() as db:
            item = self.repo._get(db, _identity(role), 'coordination_role')
            if item.get('role_id') != role:
                raise HubError('role_unavailable', 404)
            return item

    def owner_list(self, actor, limit, after):
        self.repo._actor(actor, human=True)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if after is not None:
            try:
                if str(uuid.UUID(after)) != after:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise HubError('invalid_cursor', 422) from None
        with self.repo._connection() as db:
            rows = db.execute("SELECT data FROM hub_entities WHERE kind='coordination_role' AND id>? ORDER BY id LIMIT ?",
                              (after or '', limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows]
            return dict(items=items[:limit], next_after=items[limit - 1]['id'] if len(items) > limit else None)

    def list(self, actor, project, limit, after):
        operation = _session(actor)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if after is not None:
            try:
                if str(uuid.UUID(after)) != after:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise HubError('invalid_cursor', 422) from None
        with self.repo._connection() as db:
            self.repo._project(project, db)
            marks = ','.join('?' for _ in operation.projects)
            rows = db.execute("SELECT data FROM hub_entities e WHERE kind='coordination_role' AND id>? "
                "AND EXISTS (SELECT 1 FROM json_each(e.data,'$.projects') p WHERE p.value=?) "
                "AND NOT EXISTS (SELECT 1 FROM json_each(e.data,'$.projects') p LEFT JOIN hub_projects hp ON hp.id=p.value "
                "WHERE p.value NOT IN (" + marks + ") OR hp.id IS NULL OR json_extract(hp.data,'$.archived')=1) "
                "ORDER BY id LIMIT ?", (after or '', project, *operation.projects, limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows]
            for item in items[:limit]:
                for member in item['projects']:
                    self.repo._project(member, db)
            return dict(items=items[:limit], next_after=items[limit - 1]['id'] if len(items) > limit else None,
                        note='Coordination only. Role paths and tool scope are metadata, not additional grants.')

    def owner_clear_expired(self, actor, role, expected_revision, reason, clock):
        """Owner clears an expired lease without claiming a handoff happened."""
        self.repo._actor(actor, human=True)
        reason = self.repo._text(reason, 1000)
        with self.repo._transaction() as db:
            item = self.repo._get(db, _identity(role), 'coordination_role')
            if item.get('role_id') != role:
                raise HubError('role_unavailable', 404)
            self.repo._cas(item, expected_revision)
            if item.get('lease') is None or item['lease']['expires_at'] > clock():
                raise HubError('role_lease_not_expired', 409)
            item['lease'] = None
            item['takeover'] = 'owner_cleared_expired_without_handoff'
            item['last_recovery_reason'] = reason
            return self.repo._save(db, actor, 'role.expired_cleared', 'coordination_role', item, publish=False)

    @staticmethod
    def _guard(operation, expiry):
        # Invoked by the bridge just before its Hub transaction commits.
        def check(db):
            if operation.bridge.sessions._now() >= expiry:
                raise HubError('role_lease_expired', 409)
        operation.commit_checks.append(check)

    def acquire(self, actor, role, expected_revision, lease_seconds):
        operation = _session(actor)
        if type(lease_seconds) is not int or not 10 <= lease_seconds <= 3600:
            raise HubError('invalid_role_lease', 422)
        with self.repo._transaction() as db:
            item = self._role(db, role)
            self.repo._cas(item, expected_revision)
            if operation.principal.installation_id not in item['installations']:
                raise HubError('role_holder_forbidden', 403)
            now = operation.bridge.sessions._now()
            previous = item.get('lease')
            if previous is not None and previous['expires_at'] > now:
                raise HubError('role_leased', 409)
            expiry = min(now + lease_seconds, operation.principal.expires_at)
            if expiry <= now + 1:
                raise HubError('role_lease_expired', 409)
            generation = item['generation'] + 1
            item.update(generation=generation, lease=dict(session_id=operation.principal.session_id,
                installation_id=operation.principal.installation_id, family=operation.principal.family,
                expires_at=expiry, generation=generation),
                takeover='expired_without_handoff' if previous is not None else item.get('takeover', 'available'))
            self._guard(operation, expiry)
            return self.repo._save(db, actor, 'role.acquired', 'coordination_role', item, publish=False)

    def _held(self, db, actor, role, generation):
        operation = _session(actor)
        item = self._role(db, role)
        lease = item.get('lease')
        if (type(generation) is not int or not isinstance(lease, dict) or lease.get('generation') != generation
                or item.get('generation') != generation or lease.get('session_id') != operation.principal.session_id
                or lease.get('installation_id') != operation.principal.installation_id
                or operation.principal.installation_id not in item['installations']
                or lease.get('expires_at', 0) <= operation.bridge.sessions._now()):
            raise HubError('role_lease_lost', 409)
        self._guard(operation, lease['expires_at'])
        return item, operation

    def renew(self, actor, role, expected_revision, generation, lease_seconds):
        if type(lease_seconds) is not int or not 10 <= lease_seconds <= 3600:
            raise HubError('invalid_role_lease', 422)
        with self.repo._transaction() as db:
            item, operation = self._held(db, actor, role, generation)
            self.repo._cas(item, expected_revision)
            now = operation.bridge.sessions._now()
            expiry = min(now + lease_seconds, operation.principal.expires_at)
            if expiry <= now + 1:
                raise HubError('role_lease_expired', 409)
            item['lease']['expires_at'] = expiry
            return self.repo._save(db, actor, 'role.renewed', 'coordination_role', item, publish=False)

    def release(self, actor, role, expected_revision, generation, expected_handoff_revision, handoff):
        text = self.repo._text(handoff, 16384)
        with self.repo._transaction() as db:
            item, _ = self._held(db, actor, role, generation)
            self.repo._cas(item, expected_revision)
            if type(expected_handoff_revision) is not int or item['handoff']['revision'] != expected_handoff_revision:
                raise HubError('handoff_revision_conflict', 409)
            item['handoff'] = dict(revision=expected_handoff_revision + 1, text=text)
            item['lease'] = None
            item['takeover'] = 'handoff_recorded'
            return self.repo._save(db, actor, 'role.released', 'coordination_role', item, publish=False)

    def claim(self, actor, role, generation, task_id, client_request_id):
        """Create a durable coordination claim, distinct from execution ownership."""
        request_id = self.repo._text(client_request_id, 128)
        fingerprint = hashlib.sha256(json.dumps(['role_claim', role, generation, task_id], separators=(',', ':')).encode()).hexdigest()
        with self.repo._transaction() as db:
            item, operation = self._held(db, actor, role, generation)
            row = db.execute("SELECT kind,data FROM hub_entities WHERE id=? AND kind IN ('task','tracking_task')", (task_id,)).fetchone()
            if row is None:
                raise HubError('role_task_unavailable', 404)
            task = json.loads(row['data'])
            # A coordination grant is not a task-reading grant. Verify both
            # against the same live session/grant revision before seeing or
            # attaching a claim to the referenced record.
            try:
                operation.bridge._live(operation.db, operation.secret, operation.principal.authorization_revision,
                    'tracking.read' if row['kind'] == 'tracking_task' else 'task.read',
                    session_id=operation.principal.session_id)
            except HubError:
                raise HubError('role_task_unavailable', 404) from None
            if task['project'] not in item['projects']:
                raise HubError('role_task_unavailable', 404)
            self.repo._project(task['project'], db)
            if task['status'] in ('withdrawn', 'verified', 'completion_verified'):
                raise HubError('role_task_unavailable', 404)
            prior = db.execute('SELECT * FROM hub_requests WHERE actor=? AND key=?', (actor, request_id)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HubError('idempotency_conflict', 409)
                receipt = self.repo._get(db, prior['entity_id'], 'role_claim_receipt')
                current = self.repo._get(db, receipt['claim_id'], 'role_claim')
                if (current.get('role_id') != role or current.get('task_id') != task_id
                        or current.get('status') != 'claimed' or current.get('revision') != receipt['claim_revision']):
                    raise HubError('role_claim_changed', 409)
                return current
            key = str(uuid.uuid5(_NAMESPACE, 'claim:' + task_id))
            existing = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='role_claim'", (key,)).fetchone()
            if existing:
                claim = json.loads(existing['data'])
                if claim.get('status') != 'released' and claim['role_id'] != role:
                    raise HubError('role_task_claimed', 409)
                if claim.get('status') == 'released':
                    claim.update(role_id=role, status='claimed', created_session_id=operation.principal.session_id,
                                 created_installation_id=operation.principal.installation_id)
                    claim = self.repo._save(db, actor, 'role.task_claimed', 'role_claim', claim, publish=False)
            else:
                claim = dict(id=key, task_id=task_id, task_kind=row['kind'], project=task['project'], role_id=role, status='claimed',
                         created_session_id=operation.principal.session_id,
                         created_installation_id=operation.principal.installation_id,
                         coordination_only=True, execution_authorization=False)
                claim = self.repo._save(db, actor, 'role.task_claimed', 'role_claim', claim, new=True, publish=False)
            receipt_id = str(uuid.uuid5(_NAMESPACE, 'receipt:' + actor + ':' + request_id))
            receipt = dict(claim_id=key, claim_revision=claim['revision'], task_id=task_id, role_id=role)
            db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)',
                       (receipt_id, 'role_claim_receipt', json.dumps(receipt, sort_keys=True, separators=(',', ':'))))
            db.execute('INSERT INTO hub_requests VALUES(?,?,?,?)', (actor, request_id, fingerprint, receipt_id))
            return claim

    def claims(self, actor, role, limit, after):
        operation = _session(actor)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if after is not None:
            try:
                if str(uuid.UUID(after)) != after:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise HubError('invalid_cursor', 422) from None
        with self.repo._connection() as db:
            item = self._role(db, role)
            kinds = []
            for kind, capability in (('task', 'task.read'), ('tracking_task', 'tracking.read')):
                try:
                    operation.bridge._live(operation.db, operation.secret, operation.principal.authorization_revision,
                                           capability, session_id=operation.principal.session_id)
                    kinds.append(kind)
                except HubError:
                    pass
            if not kinds:
                return dict(items=[], next_after=None, note='No task read capability; coordination only.')
            marks = ','.join('?' for _ in item['projects'])
            kind_marks = ','.join('?' for _ in kinds)
            rows = db.execute("SELECT data FROM hub_entities WHERE kind='role_claim' AND json_extract(data,'$.role_id')=? AND json_extract(data,'$.status')='claimed' AND json_extract(data,'$.project') IN (" + marks + ") AND json_extract(data,'$.task_kind') IN (" + kind_marks + ") AND id>? ORDER BY id LIMIT ?",
                              (role, *item['projects'], *kinds, after or '', limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows]
            return dict(items=items[:limit], next_after=items[limit - 1]['id'] if len(items) > limit else None,
                        note='Unclosed coordination claims; task state may have changed. No execution authority.')

    def release_claim(self, actor, role, generation, task_id, expected_revision, reason):
        reason = self.repo._text(reason, 1000)
        with self.repo._transaction() as db:
            item, operation = self._held(db, actor, role, generation)
            try:
                key = str(uuid.uuid5(_NAMESPACE, 'claim:' + task_id))
                claim = self.repo._get(db, key, 'role_claim')
            except (HubError, TypeError):
                raise HubError('role_task_unavailable', 404) from None
            self.repo._project(claim['project'], db)
            if claim['role_id'] != role or claim.get('status') != 'claimed' or claim['project'] not in item['projects']:
                raise HubError('role_task_unavailable', 404)
            try:
                if claim.get('task_kind') not in ('task', 'tracking_task'):
                    raise HubError('role_task_unavailable', 404)
                operation.bridge._live(operation.db, operation.secret, operation.principal.authorization_revision,
                    'tracking.read' if claim['task_kind'] == 'tracking_task' else 'task.read',
                    session_id=operation.principal.session_id)
            except HubError:
                raise HubError('role_task_unavailable', 404) from None
            self.repo._cas(claim, expected_revision)
            claim.update(status='released', release_reason=reason)
            return self.repo._save(db, actor, 'role.task_released', 'role_claim', claim, publish=False)
