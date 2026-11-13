"""Owner-admitted, exact-session attention routing. No execution or wake grant."""
import json
import math
import os
import uuid

from agent_message_bus import session_hub_context as context
from agent_message_bus.agent_sessions import AgentSessionError
from .dispatch import DispatchRepository, _canonical_id
from .pilot_ownership import PilotOwner, PilotOwnershipError
from .repository import HubError
from .roles import RoleRepository, _identity
from .stale_service_authority import _boot_clock, _boot_id

_NAMESPACE = uuid.UUID('ffb754ac-2c19-4e0c-a178-a90a4b89d06e')


def _route_id(project):
    return str(uuid.uuid5(_NAMESPACE, 'route:' + project))


class AttentionDelivery:
    def __init__(self, repo, bridge, *, clock=None, boottime=_boot_clock, boot_id=_boot_id):
        if bridge.repo is not repo:
            raise ValueError('attention_bridge_mismatch')
        self.repo, self.bridge = repo, bridge
        self.clock = clock or bridge.sessions._now
        self.boottime, self.boot_id = boottime, boot_id
        self.pid = os.getpid()
        self.owner = repo.pilot_owner
        if type(self.owner) is not PilotOwner:
            raise HubError('attention_delivery_owner_required', 409)
        self.owner.assert_active(repo.path)
        self.epoch = self.owner.epoch

    def _owner(self):
        try:
            if os.getpid() != self.pid or self.repo.pilot_owner is not self.owner or self.owner.epoch != self.epoch:
                raise PilotOwnershipError()
            self.owner.assert_active(self.repo.path)
            self.bridge._owned()
        except PilotOwnershipError:
            raise HubError('attention_delivery_owner_required', 409) from None

    def _now(self):
        now, boot = self.clock(), self.boottime()
        if (type(now) not in (int, float) or not math.isfinite(now) or type(boot) not in (int, float)
                or not math.isfinite(boot) or boot < 0):
            raise HubError('attention_delivery_clock_untrusted', 409)
        return now, boot

    def owner_get(self, actor, project):
        self.repo._actor(actor, human=True)
        self._owner()
        with self.repo._connection() as db:
            self.repo._project(project, db, require_active=False)
            row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='attention_route'", (_route_id(project),)).fetchone()
            return json.loads(row['data']) if row else dict(project=project, revision=0, enabled=False)

    def owner_configure(self, actor, project, expected_revision, installation_id, session_id, generation, lifetime_seconds):
        self.repo._actor(actor, human=True)
        self._owner()
        _canonical_id(installation_id); _canonical_id(session_id)
        if type(expected_revision) is not int or expected_revision < 0 or type(generation) is not int or generation < 1 or type(lifetime_seconds) is not int or not 10 <= lifetime_seconds <= 3600:
            raise HubError('invalid_attention_route', 422)
        with self.bridge.sessions._transaction() as session_db:
            try:
                for capability in ('role.read', 'role.lease', 'dispatch.read'):
                    principal, grant = self.bridge._live(session_db, None, capability=capability, session_id=session_id)
                    if principal.installation_id != installation_id or project not in json.loads(grant['projects']):
                        raise HubError('attention_route_unavailable', 404)
            except (AgentSessionError, HubError):
                raise HubError('attention_route_unavailable', 404) from None
            with self.repo._transaction() as db:
                self._owner(); self.repo._project(project, db)
                if self.repo._paused(db):
                    raise HubError('attention_delivery_paused', 423)
                role = self.repo._get(db, _identity('orchestrator'), 'coordination_role')
                lease = role.get('lease') or {}
                now, boot = self._now()
                if (project not in role['projects'] or installation_id not in role['installations']
                        or role.get('generation') != generation or lease.get('generation') != generation
                        or lease.get('installation_id') != installation_id or lease.get('session_id') != session_id
                        or type(lease.get('expires_at')) not in (int, float) or lease['expires_at'] <= now):
                    raise HubError('attention_route_unavailable', 404)
                identity = _route_id(project)
                row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='attention_route'", (identity,)).fetchone()
                old = json.loads(row['data']) if row else None
                if expected_revision != (old['revision'] if old else 0):
                    raise HubError('revision_conflict', 409)
                deadline = min(now + lifetime_seconds, lease['expires_at'], principal.expires_at, grant['expires_at'])
                if deadline <= now:
                    raise HubError('attention_route_unavailable', 404)
                item = dict(id=identity, revision=expected_revision, project=project, enabled=True,
                            installation_id=installation_id, session_id=session_id, generation=generation,
                            grant_revision=grant['revision'], owner_epoch=self.epoch, boot_id=self.boot_id(),
                            approved_at=now, expires_at=deadline, approved_boottime=boot,
                            expires_boottime=boot + deadline - now, delivery='not_attempted', wake_verified=False)
                self.repo._save(db, actor, 'attention_route.configured', 'attention_route', item, new=old is None, publish=False)
                self._validate_target(session_db, db, item)
                return item

    def _validate_target(self, session_db, db, route):
        self._owner()
        now, boot = self._now()
        if (route.get('enabled') is not True or route.get('owner_epoch') != self.epoch
                or route.get('boot_id') != self.boot_id() or not route['approved_at'] <= now < route['expires_at']
                or not route['approved_boottime'] <= boot < route['expires_boottime']):
            raise HubError('attention_route_unavailable', 404)
        self.repo._project(route['project'], db)
        if self.repo._paused(db):
            raise HubError('attention_route_unavailable', 404)
        try:
            for cap in ('role.read', 'role.lease', 'dispatch.read'):
                principal, grant = self.bridge._live(session_db, None, capability=cap, session_id=route['session_id'])
                if (principal.installation_id != route['installation_id'] or grant['revision'] != route['grant_revision']
                        or route['project'] not in json.loads(grant['projects'])):
                    raise HubError('attention_route_unavailable', 404)
        except (AgentSessionError, HubError):
            raise HubError('attention_route_unavailable', 404) from None
        role = self.repo._get(db, _identity('orchestrator'), 'coordination_role')
        for role_project in role['projects']:
            self.repo._project(role_project, db)
        if not set(role['projects']).issubset(set(json.loads(grant['projects']))):
            raise HubError('attention_route_unavailable', 404)
        lease = role.get('lease') or {}
        if (route['project'] not in role['projects'] or route['installation_id'] not in role['installations']
                or role.get('generation') != route['generation'] or lease.get('generation') != route['generation']
                or lease.get('installation_id') != route['installation_id'] or lease.get('session_id') != route['session_id']
                or type(lease.get('expires_at')) not in (int, float) or lease['expires_at'] <= now):
            raise HubError('attention_route_unavailable', 404)
        return role

    def _route_for(self, db, operation, project, generation):
        route = self.repo._get(db, _route_id(project), 'attention_route')
        if (route.get('project') != project or route.get('generation') != generation
                or route.get('session_id') != operation.principal.session_id
                or route.get('installation_id') != operation.principal.installation_id):
            raise HubError('attention_route_unavailable', 404)
        self._validate_target(operation.db, db, route)
        def recheck(commit_db):
            if commit_db is None:
                with self.repo._connection() as current:
                    self._validate_target(operation.db, current, route)
            else:
                self._validate_target(operation.db, commit_db, route)
        operation.commit_checks.append(recheck)
        return route

    def pending(self, actor, project, generation, limit=50, after=None, *, actionable_only=False):
        operation = context.current.get()
        if operation is None or not context.actor_allowed(actor):
            raise HubError('attention_route_unavailable', 404)
        service = DispatchRepository(self.repo)
        with service._attention_snapshot() as db:
            route = self._route_for(db, operation, project, generation)
            result = service._attention_in(db, actor, generation, project, limit, after, actionable_only=actionable_only)
            self._validate_target(operation.db, db, route)
            return result

    def owner_revoke(self, actor, project, expected_revision):
        self.repo._actor(actor, human=True)
        self._owner()
        if type(expected_revision) is not int or expected_revision < 1:
            raise HubError('invalid_attention_route', 422)
        with self.bridge.sessions._transaction():
            with self.repo._transaction() as db:
                self._owner(); self.repo._project(project, db, require_active=False)
                route = self.repo._get(db, _route_id(project), 'attention_route')
                self.repo._cas(route, expected_revision)
                route['enabled'] = False
                return self.repo._save(db, actor, 'attention_route.revoked', 'attention_route', route, publish=False)

    def acknowledge(self, actor, project, generation, intent_id, expected_revision):
        operation = context.current.get()
        if operation is None or not context.actor_allowed(actor):
            raise HubError('attention_route_unavailable', 404)
        _canonical_id(intent_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise HubError('invalid_attention_receipt', 422)
        service = DispatchRepository(self.repo)
        with self.repo._transaction() as db:
            route = self._route_for(db, operation, project, generation)
            intent = self.repo._get(db, intent_id, 'orchestration_attention')
            item, task, _, _ = service._dispatch(db, actor, intent['dispatch_id'])
            if (intent.get('project') != project or item.get('project') != project
                    or intent.get('recipient_role') != 'orchestrator'
                    or intent.get('state') != 'pending_routing' or item.get('attention_intent_id') != intent_id
                    or not item.get('coordination_stale')
                    or task.get('status') not in ('assigned', 'claimed', 'in_review', 'open', 'completion_claimed')):
                raise HubError('attention_route_unavailable', 404)
            self.repo._cas(intent, expected_revision)
            identity = str(uuid.uuid5(_NAMESPACE, ':'.join(['receipt', intent_id, str(expected_revision),
                route['session_id'], str(generation)])))
            row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='attention_receipt'", (identity,)).fetchone()
            if row:
                return json.loads(row['data'])
            receipt = dict(id=identity, project=project, intent_id=intent_id, intent_revision=expected_revision,
                           route_revision=route['revision'], installation_id=route['installation_id'],
                           session_id=route['session_id'], generation=generation, state='agent_acknowledged',
                           task_completion_verified=False, wake_verified=False)
            receipt = self.repo._save(db, actor, 'attention.acknowledged', 'attention_receipt', receipt,
                                      new=True, publish=False)
            self._validate_target(operation.db, db, route)
            return receipt

    def owner_receipts(self, actor, project, limit=50, after=None):
        self.repo._actor(actor, human=True)
        self._owner()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if after is not None:
            _canonical_id(after)
        with self.repo._connection() as db:
            self.repo._project(project, db, require_active=False)
            rows = db.execute("SELECT id,data FROM hub_entities WHERE kind='attention_receipt' AND json_extract(data,'$.project')=? AND id>? ORDER BY id LIMIT ?",
                              (project, after or '', limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows[:limit]]
            return dict(items=items, next_after=items[-1]['id'] if len(rows) > limit else None,
                        note='Acknowledgment is not verified wake, task completion, or proof of execution.')
