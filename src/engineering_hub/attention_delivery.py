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


def _route_id(project, role='orchestrator'):
    if role == 'orchestrator':
        return str(uuid.uuid5(_NAMESPACE, 'route:' + project))
    _identity(role)
    return str(uuid.uuid5(_NAMESPACE, 'worker-route:' + project + ':' + role))


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

    def owner_get(self, actor, project, role='orchestrator'):
        self.repo._actor(actor, human=True)
        self._owner()
        with self.repo._connection() as db:
            self.repo._project(project, db, require_active=False)
            row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='attention_route'", (_route_id(project, role),)).fetchone()
            return json.loads(row['data']) if row else dict(project=project, revision=0, enabled=False)

    def owner_configure(self, actor, project, expected_revision, installation_id, session_id, generation, lifetime_seconds, role='orchestrator'):
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
                role_record = self.repo._get(db, _identity(role), 'coordination_role')
                lease = role_record.get('lease') or {}
                now, boot = self._now()
                if (role_record.get('role_id') != role or project not in role_record['projects'] or installation_id not in role_record['installations']
                        or role_record.get('generation') != generation or lease.get('generation') != generation
                        or lease.get('installation_id') != installation_id or lease.get('session_id') != session_id
                        or type(lease.get('expires_at')) not in (int, float) or lease['expires_at'] <= now):
                    raise HubError('attention_route_unavailable', 404)
                identity = _route_id(project, role)
                row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='attention_route'", (identity,)).fetchone()
                old = json.loads(row['data']) if row else None
                if expected_revision != (old['revision'] if old else 0):
                    raise HubError('revision_conflict', 409)
                deadline = min(now + lifetime_seconds, lease['expires_at'], principal.expires_at, grant['expires_at'])
                if deadline <= now:
                    raise HubError('attention_route_unavailable', 404)
                item = dict(id=identity, revision=expected_revision, project=project, role_id=role, enabled=True,
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
        role = self.repo._get(db, _identity(route.get('role_id', 'orchestrator')), 'coordination_role')
        for role_project in role['projects']:
            self.repo._project(role_project, db)
        if not set(role['projects']).issubset(set(json.loads(grant['projects']))):
            raise HubError('attention_route_unavailable', 404)
        lease = role.get('lease') or {}
        if (role.get('role_id') != route.get('role_id', 'orchestrator') or route['project'] not in role['projects'] or route['installation_id'] not in role['installations']
                or role.get('generation') != route['generation'] or lease.get('generation') != route['generation']
                or lease.get('installation_id') != route['installation_id'] or lease.get('session_id') != route['session_id']
                or type(lease.get('expires_at')) not in (int, float) or lease['expires_at'] <= now):
            raise HubError('attention_route_unavailable', 404)
        return role

    def _route_for(self, db, operation, project, generation, role='orchestrator'):
        route = self.repo._get(db, _route_id(project, role), 'attention_route')
        if (route.get('project') != project or route.get('role_id', 'orchestrator') != role or route.get('generation') != generation
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

    def owner_revoke(self, actor, project, expected_revision, role='orchestrator'):
        self.repo._actor(actor, human=True)
        self._owner()
        if type(expected_revision) is not int or expected_revision < 1:
            raise HubError('invalid_attention_route', 422)
        with self.bridge.sessions._transaction():
            with self.repo._transaction() as db:
                self._owner(); self.repo._project(project, db, require_active=False)
                route = self.repo._get(db, _route_id(project, role), 'attention_route')
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
                    or task.get('status') not in ('assigned', 'claimed', 'in_review', 'open', 'completion_claimed')):
                raise HubError('attention_route_unavailable', 404)
            identity = str(uuid.uuid5(_NAMESPACE, ':'.join(['receipt', intent_id, str(expected_revision),
                route['session_id'], str(generation)])))
            row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='attention_receipt'", (identity,)).fetchone()
            report_intent = intent.get('intent_type') == 'worker_report'
            if report_intent:
                try:
                    report = self.repo._get(db, intent['report_id'], 'dispatch_report')
                    valid = service._valid_report_attention(intent, item, report)
                except (KeyError, HubError, TypeError, ValueError):
                    valid = False
                if not valid:
                    raise HubError('attention_route_unavailable', 404)
                if row and intent.get('state') == 'agent_acknowledged' and intent.get('ack_receipt_id') == identity:
                    return json.loads(row['data'])
            elif not (intent.get('intent_type') in (None, 'stale')
                      and item.get('attention_intent_id') == intent_id and item.get('coordination_stale')):
                raise HubError('attention_route_unavailable', 404)
            if intent.get('state') != 'pending_routing':
                raise HubError('attention_route_unavailable', 404)
            self.repo._cas(intent, expected_revision)
            if row:
                return json.loads(row['data'])
            receipt = dict(id=identity, project=project, intent_id=intent_id, intent_revision=expected_revision,
                           route_revision=route['revision'], installation_id=route['installation_id'],
                           session_id=route['session_id'], generation=generation, state='agent_acknowledged',
                           task_completion_verified=False, wake_verified=False)
            receipt = self.repo._save(db, actor, 'attention.acknowledged', 'attention_receipt', receipt,
                                      new=True, publish=False)
            if report_intent:
                intent.update(state='agent_acknowledged', ack_receipt_id=identity,
                              delivery='agent_acknowledged', wake_verified=False)
                self.repo._save(db, actor, 'orchestration.report_attention_acknowledged',
                                'orchestration_attention', intent, publish=False)
            self._validate_target(operation.db, db, route)
            return receipt

    def inspect_report(self, actor, project, generation, intent_id, expected_intent_revision,
                       report_id, expected_report_revision):
        """Inspect an immutable worker report under the exact orchestrator route."""
        operation = context.current.get()
        if operation is None or not context.actor_allowed(actor):
            raise HubError('attention_route_unavailable', 404)
        _canonical_id(intent_id); _canonical_id(report_id)
        if (type(expected_intent_revision) is not int or expected_intent_revision < 1
                or type(expected_report_revision) is not int or expected_report_revision < 1):
            raise HubError('invalid_attention_reference', 422)
        service = DispatchRepository(self.repo)
        def read_current(db, route):
            self._validate_target(operation.db, db, route)
            service._grant(operation, 'dispatch.read', 'role.read')
            try:
                intent = self.repo._get(db, intent_id, 'orchestration_attention')
                item, task, _, _ = service._dispatch(db, actor, intent['dispatch_id'])
                report = self.repo._get(db, report_id, 'dispatch_report')
                if (intent.get('id') != intent_id or intent.get('project') != project
                        or intent.get('recipient_role') != 'orchestrator'
                        or intent.get('report_id') != report_id or intent.get('report_revision') != expected_report_revision
                        or type(report.get('revision')) is not int
                        or not service._valid_report_attention(intent, item, report)
                        or task.get('status') not in ('assigned', 'claimed', 'in_review', 'open', 'completion_claimed')):
                    raise HubError('attention_route_unavailable', 404)
                if intent.get('state') == 'pending_routing':
                    if type(intent.get('revision')) is not int or intent['revision'] != expected_intent_revision:
                        raise HubError('attention_route_unavailable', 404)
                elif intent.get('state') == 'agent_acknowledged':
                    identity = str(uuid.uuid5(_NAMESPACE, ':'.join(['receipt', intent_id, str(expected_intent_revision),
                        route['session_id'], str(generation)])))
                    receipt = self.repo._get(db, identity, 'attention_receipt')
                    if (type(intent.get('revision')) is not int or intent['revision'] != expected_intent_revision + 1
                            or intent.get('ack_receipt_id') != identity or receipt.get('id') != identity
                            or receipt.get('project') != project or receipt.get('intent_id') != intent_id
                            or type(receipt.get('intent_revision')) is not int
                            or receipt.get('intent_revision') != expected_intent_revision
                            or receipt.get('session_id') != route['session_id']
                            or receipt.get('installation_id') != route['installation_id']
                            or type(receipt.get('generation')) is not int or receipt.get('generation') != generation
                            or receipt.get('state') != 'agent_acknowledged'):
                        raise HubError('attention_route_unavailable', 404)
                else:
                    raise HubError('attention_route_unavailable', 404)
                decision_id = report.get('decision_id')
                if intent.get('report_kind') == 'ask':
                    _canonical_id(decision_id)
                    service._grant(operation, 'decision.read')
                    decision = self.repo._get(db, decision_id, 'decision')
                    if (decision.get('id') != decision_id or decision.get('project') != project
                            or decision.get('origin_dispatch_id') != item['id']
                            or type(decision.get('origin_dispatch_revision')) is not int
                            or decision.get('origin_dispatch_revision') != expected_report_revision):
                        raise HubError('attention_route_unavailable', 404)
                elif decision_id is not None:
                    raise HubError('attention_route_unavailable', 404)
                note = report.get('note')
                if type(note) is not str or not note.strip() or len(note) > 2000 or len(note.encode('utf-8')) > 8000:
                    raise HubError('attention_route_unavailable', 404)
                return dict(project=project, intent_id=intent_id, intent_revision=expected_intent_revision,
                            dispatch_id=item['id'], report_id=report_id, report_revision=expected_report_revision,
                            report_kind=intent['report_kind'], note=note, decision_id=decision_id,
                            trust='untrusted_hub_data', execution_authorization=False, wake_verified=False)
            except (HubError, KeyError, TypeError, ValueError, UnicodeError):
                raise HubError('attention_route_unavailable', 404) from None
        with service._attention_snapshot() as db:
            try:
                route = self._route_for(db, operation, project, generation)
            except HubError:
                raise HubError('attention_route_unavailable', 404) from None
            service._held(db, actor, 'orchestrator', generation)
            result = read_current(db, route)
        def recheck(_db):
            with service._attention_snapshot() as current:
                if read_current(current, route) != result:
                    raise HubError('attention_route_unavailable', 404)
        operation.commit_checks.append(recheck)
        return result

    def worker_pending(self, actor, project, role, generation, limit=50, after=None):
        """Discover unacknowledged dispatches bound to this exact role session."""
        operation = context.current.get()
        if operation is None or not context.actor_allowed(actor) or role == 'orchestrator':
            raise HubError('attention_route_unavailable', 404)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if after is not None:
            _canonical_id(after)
        service = DispatchRepository(self.repo)
        with service._attention_snapshot() as db:
            route = self._route_for(db, operation, project, generation, role)
            held, _ = service._held(db, actor, role, generation)
            service._grant(operation, 'dispatch.read', 'role.read')
            kinds = []
            for kind, capability in (('task', 'task.read'), ('tracking_task', 'tracking.read')):
                try:
                    service._grant(operation, capability)
                    kinds.append(kind)
                except HubError:
                    pass
            if not kinds:
                self._validate_target(operation.db, db, route)
                return dict(items=[], next_after=None, wake_verified=False, execution_authorization=False)
            try:
                service._grant(operation, 'decision.read')
                decision_read = 1
            except HubError:
                decision_read = 0
            marks = ','.join('?' for _ in kinds)
            rows = db.execute("SELECT d.id FROM hub_entities d "
                "JOIN hub_entities t ON t.id=json_extract(d.data,'$.task_id') "
                "JOIN hub_entities q ON q.id=json_extract(d.data,'$.request_id') AND q.kind='orchestration_request' "
                "LEFT JOIN hub_entities a ON a.id=json_extract(d.data,'$.decision_id') AND a.kind='decision' "
                "WHERE d.kind='role_dispatch' AND json_extract(d.data,'$.role_id')=? "
                "AND json_extract(d.data,'$.project')=? AND json_extract(t.data,'$.project')=? "
                "AND json_extract(q.data,'$.project')=? AND d.id>? "
                "AND ((json_extract(d.data,'$.state')='dispatched' AND json_extract(d.data,'$.decision_id') IS NULL) "
                "OR (?=1 AND json_extract(d.data,'$.state')='asked' AND a.id IS NOT NULL "
                "AND CASE WHEN json_valid(a.data) THEN json_type(a.data)='object' "
                "AND json_extract(a.data,'$.id')=a.id AND json_type(a.data,'$.revision')='integer' "
                "AND json_extract(a.data,'$.revision')>0 AND json_extract(a.data,'$.status')='answered' "
                "AND json_type(a.data,'$.origin_dispatch_revision')='integer' "
                "AND json_extract(a.data,'$.origin_dispatch_revision')>0 "
                "AND json_extract(a.data,'$.origin_dispatch_revision')<=json_extract(d.data,'$.revision') "
                "AND json_extract(a.data,'$.project')=? "
                "AND json_extract(a.data,'$.origin_dispatch_id')=d.id ELSE 0 END)) "
                "AND json_extract(t.data,'$.status') IN ('assigned','claimed','in_review','open','completion_claimed') "
                "AND t.kind IN (" + marks + ") AND t.kind=json_extract(d.data,'$.task_kind') "
                "AND json_extract(d.data,'$.target_binding.session_id')=? "
                "AND json_extract(d.data,'$.target_binding.installation_id')=? "
                "AND json_extract(d.data,'$.target_binding.generation')=? "
                "AND NOT EXISTS (SELECT 1 FROM hub_entities r WHERE r.kind='worker_dispatch_receipt' "
                "AND json_extract(r.data,'$.dispatch_id')=d.id AND json_extract(r.data,'$.session_id')=? "
                "AND json_extract(r.data,'$.generation')=? AND ((json_extract(d.data,'$.state')='dispatched' "
                "AND json_extract(r.data,'$.decision_id') IS NULL) OR (json_extract(d.data,'$.state')='asked' "
                "AND json_extract(r.data,'$.decision_id')=a.id AND json_extract(r.data,'$.decision_revision')="
                "CASE WHEN json_valid(a.data) THEN json_extract(a.data,'$.revision') ELSE NULL END))) "
                "ORDER BY d.id LIMIT ?",
                (role, project, project, project, after or '', decision_read, project, *kinds, route['session_id'],
                 route['installation_id'], generation, route['session_id'], generation, limit + 1)).fetchall()
            items = []
            for row in rows:
                item, task, target, _ = service._dispatch(db, actor, row['id'])
                if (target.get('role_id') != role or item.get('project') != project
                        or service._binding(item.get('target_binding')) != service._binding(held.get('lease'))):
                    raise HubError('attention_route_unavailable', 404)
                view = dict(dispatch_id=item['id'], dispatch_revision=item['revision'],
                            task_id=task['id'], project=project, role=role,
                            execution_authorization=False, wake_verified=False)
                if item['state'] == 'asked':
                    decision = self.repo._get(db, item['decision_id'], 'decision')
                    if (decision.get('project') != project or decision.get('origin_dispatch_id') != item['id']
                            or decision.get('status') != 'answered' or type(decision.get('revision')) is not int):
                        raise HubError('attention_route_unavailable', 404)
                    view.update(event_type='decision_answered', decision_id=decision['id'],
                                decision_revision=decision['revision'])
                items.append(view)
            self._validate_target(operation.db, db, route)
            return dict(items=items[:limit], next_after=items[limit-1]['dispatch_id'] if len(items) > limit else None,
                        wake_verified=False, execution_authorization=False,
                        note='Refresh UUID paging from the beginning for new dispatches; no execution or wake is implied.')

    def worker_acknowledge(self, actor, project, role, generation, dispatch_id, expected_revision,
                           decision_id=None, decision_revision=None):
        operation = context.current.get()
        if operation is None or not context.actor_allowed(actor) or role == 'orchestrator':
            raise HubError('attention_route_unavailable', 404)
        _canonical_id(dispatch_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise HubError('invalid_attention_receipt', 422)
        if decision_id is not None:
            _canonical_id(decision_id)
            if type(decision_revision) is not int or decision_revision < 1:
                raise HubError('invalid_attention_receipt', 422)
        elif decision_revision is not None:
            raise HubError('invalid_attention_receipt', 422)
        service = DispatchRepository(self.repo)
        with self.repo._transaction() as db:
            route = self._route_for(db, operation, project, generation, role)
            held, _ = service._held(db, actor, role, generation)
            service._grant(operation, 'dispatch.read', 'role.read')
            item, task, target, _ = service._dispatch(db, actor, dispatch_id)
            if (item.get('project') != project or target.get('role_id') != role
                    or task.get('status') not in ('assigned', 'claimed', 'in_review', 'open', 'completion_claimed')
                    or service._binding(item.get('target_binding')) != service._binding(held.get('lease'))):
                raise HubError('attention_route_unavailable', 404)
            self.repo._cas(item, expected_revision)
            if decision_id is None:
                if item.get('state') != 'dispatched' or item.get('decision_id') is not None:
                    raise HubError('attention_route_unavailable', 404)
            else:
                service._grant(operation, 'decision.read')
                decision = self.repo._get(db, decision_id, 'decision')
                if (item.get('state') != 'asked' or item.get('decision_id') != decision_id
                        or decision.get('project') != project or decision.get('origin_dispatch_id') != dispatch_id
                        or decision.get('id') != decision_id
                        or type(decision.get('origin_dispatch_revision')) is not int
                        or not 1 <= decision['origin_dispatch_revision'] <= item['revision']
                        or decision.get('status') != 'answered' or decision.get('revision') != decision_revision):
                    raise HubError('attention_route_unavailable', 404)
            parts = ['worker-receipt', dispatch_id, str(expected_revision), route['session_id'], str(generation)]
            if decision_id is not None:
                parts += [decision_id, str(decision_revision)]
            identity = str(uuid.uuid5(_NAMESPACE, ':'.join(parts)))
            row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='worker_dispatch_receipt'", (identity,)).fetchone()
            if row:
                return json.loads(row['data'])
            receipt = dict(id=identity, project=project, role=role, dispatch_id=dispatch_id,
                           dispatch_revision=expected_revision, installation_id=route['installation_id'],
                           session_id=route['session_id'], generation=generation, state='agent_acknowledged',
                           wake_verified=False, execution_authorization=False)
            if decision_id is not None:
                receipt.update(event_type='decision_answered', decision_id=decision_id, decision_revision=decision_revision)
            receipt = self.repo._save(db, actor, 'worker_dispatch.acknowledged', 'worker_dispatch_receipt', receipt,
                                      new=True, publish=False)
            self._validate_target(operation.db, db, route)
            return receipt

    def worker_inspect_decision(self, actor, project, role, generation, dispatch_id,
                                expected_dispatch_revision, decision_id, expected_decision_revision):
        """Read one current answered decision under the exact owner-admitted worker route."""
        operation = context.current.get()
        if operation is None or not context.actor_allowed(actor) or role == 'orchestrator':
            raise HubError('attention_route_unavailable', 404)
        _canonical_id(dispatch_id); _canonical_id(decision_id)
        if (type(expected_dispatch_revision) is not int or expected_dispatch_revision < 1
                or type(expected_decision_revision) is not int or expected_decision_revision < 1):
            raise HubError('invalid_attention_reference', 422)
        service = DispatchRepository(self.repo)
        def read_current(db, route):
            self._validate_target(operation.db, db, route)
            service._grant(operation, 'dispatch.read', 'role.read', 'decision.read')
            try:
                item, task, target, _ = service._dispatch(db, actor, dispatch_id)
                binding = dict(generation=generation, session_id=route['session_id'], installation_id=route['installation_id'])
                if (item.get('project') != project or target.get('role_id') != role
                        or item.get('state') != 'asked' or item.get('decision_id') != decision_id
                        or type(item.get('revision')) is not int or item['revision'] != expected_dispatch_revision
                        or task.get('status') not in ('assigned', 'claimed', 'in_review', 'open', 'completion_claimed')
                        or service._binding(item.get('target_binding')) != binding):
                    raise HubError('attention_route_unavailable', 404)
                decision = self.repo._get(db, decision_id, 'decision')
                answer = decision.get('answer')
                if (decision.get('id') != decision_id or decision.get('project') != project
                        or decision.get('origin_dispatch_id') != dispatch_id
                        or type(decision.get('origin_dispatch_revision')) is not int
                        or not 1 <= decision['origin_dispatch_revision'] <= item['revision']
                        or decision.get('status') != 'answered' or type(decision.get('revision')) is not int
                        or decision['revision'] != expected_decision_revision or type(answer) is not str
                        or not answer.strip() or len(answer) > 4096 or len(answer.encode('utf-8')) > 16384):
                    raise HubError('attention_route_unavailable', 404)
                return answer
            except (HubError, KeyError, TypeError, ValueError, UnicodeError):
                raise HubError('attention_route_unavailable', 404) from None
        with service._attention_snapshot() as db:
            route = self._route_for(db, operation, project, generation, role)
            service._held(db, actor, role, generation)
            answer = read_current(db, route)
        def recheck(_db):
            with service._attention_snapshot() as current:
                if read_current(current, route) != answer:
                    raise HubError('attention_route_unavailable', 404)
        operation.commit_checks.append(recheck)
        return dict(project=project, role=role, generation=generation, dispatch_id=dispatch_id,
                    dispatch_revision=expected_dispatch_revision, decision_id=decision_id,
                    decision_revision=expected_decision_revision, status='answered', answer=answer,
                    trust='untrusted_hub_data', execution_authorization=False, wake_verified=False)

    def owner_worker_receipts(self, actor, project, role, limit=50, after=None):
        self.repo._actor(actor, human=True)
        self._owner()
        _identity(role)
        if role == 'orchestrator' or type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if after is not None:
            _canonical_id(after)
        with self.repo._connection() as db:
            self.repo._project(project, db, require_active=False)
            rows = db.execute("SELECT data FROM hub_entities WHERE kind='worker_dispatch_receipt' "
                              "AND json_extract(data,'$.project')=? AND json_extract(data,'$.role')=? "
                              "AND id>? ORDER BY id LIMIT ?", (project, role, after or '', limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows[:limit]]
            return dict(items=items, next_after=items[-1]['id'] if len(rows) > limit else None,
                        note='Receipt is a client assertion, not proof of wake or execution.')

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
