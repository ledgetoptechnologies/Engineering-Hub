"""Durable, inert role dispatch records. No execution or wake authority."""
import hashlib
import json
import uuid

from agent_message_bus import session_hub_context as context
from .repository import HubError
from .roles import RoleRepository
from .task_progress import TaskProgress

_NAMESPACE = uuid.UUID('4ebbc0ea-4407-4779-a6a5-bd505d8dffad')


def _canonical_id(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise HubError('dispatch_unavailable', 404) from None
    return value


class DispatchRepository:
    def __init__(self, repo):
        self.repo = repo

    def _operation(self, actor):
        operation = context.current.get()
        if operation is None or not context.actor_allowed(actor):
            raise HubError('dispatch_unavailable', 404)
        return operation

    def _grant(self, operation, *capabilities):
        try:
            for capability in capabilities:
                operation.bridge._live(operation.db, operation.secret, operation.principal.authorization_revision,
                                       capability, session_id=operation.principal.session_id)
        except HubError:
            raise HubError('dispatch_unavailable', 404) from None

    def _held(self, db, actor, role, generation):
        operation = self._operation(actor)
        self._grant(operation, 'role.lease')
        try:
            return RoleRepository(self.repo)._held(db, actor, role, generation)[0], operation
        except (HubError, ValueError, TypeError):
            raise HubError('dispatch_unavailable', 404) from None

    def _request(self, db, request_id):
        try:
            item = self.repo._get(db, _canonical_id(request_id), 'orchestration_request')
            self.repo._project(item['project'], db)
            return item
        except HubError:
            raise HubError('dispatch_unavailable', 404) from None

    def _dispatch(self, db, actor, dispatch_id):
        try:
            item = self.repo._get(db, _canonical_id(dispatch_id), 'role_dispatch')
            request = self._request(db, item['request_id'])
            task, kind, operation = TaskProgress(self.repo)._task(db, actor, item['task_id'])
            self._grant(operation, 'role.read')
            if item.get('decision_id') is not None:
                self._grant(operation, 'decision.read')
            target = RoleRepository(self.repo)._role(db, item['role_id'])
            if (task['project'] != request['project'] or item.get('project') != task['project']
                    or task['project'] not in target['projects'] or kind != item['task_kind']):
                raise HubError('dispatch_unavailable', 404)
            return item, task, target, operation
        except (HubError, ValueError, TypeError):
            raise HubError('dispatch_unavailable', 404) from None

    def _receipt(self, db, actor, key, fingerprint, kind):
        row = db.execute('SELECT * FROM hub_requests WHERE actor=? AND key=?', (actor, key)).fetchone()
        if not row:
            return None
        if row['fingerprint'] != fingerprint:
            raise HubError('idempotency_conflict', 409)
        receipt = self.repo._get(db, row['entity_id'], 'dispatch_receipt')
        current = self.repo._get(db, receipt['entity_id'], kind)
        if current['revision'] != receipt['revision']:
            raise HubError('dispatch_changed', 409)
        return current

    def _save_receipt(self, db, actor, key, fingerprint, item):
        identity = str(uuid.uuid5(_NAMESPACE, 'receipt:' + actor + ':' + key))
        db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)',
                   (identity, 'dispatch_receipt', json.dumps({'entity_id': item['id'], 'revision': item['revision']}, sort_keys=True, separators=(',', ':'))))
        db.execute('INSERT INTO hub_requests VALUES(?,?,?,?)', (actor, key, fingerprint, identity))

    @staticmethod
    def _fingerprint(parts):
        return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def create_request(self, actor, generation, project, title, client_request_id):
        key, title = self.repo._text(client_request_id, 128), self.repo._text(title, 1000)
        fingerprint = self._fingerprint(['orchestration_request', generation, project, title])
        with self.repo._transaction() as db:
            role, operation = self._held(db, actor, 'orchestrator', generation)
            self.repo._project(project, db)
            if project not in role['projects']:
                raise HubError('dispatch_unavailable', 404)
            prior = self._receipt(db, actor, key, fingerprint, 'orchestration_request')
            if prior:
                return prior
            item = dict(id=str(uuid.uuid4()), project=project, title=title,
                        created_installation_id=operation.principal.installation_id,
                        created_session_id=operation.principal.session_id,
                        coordination_only=True, execution_authorization=False)
            item = self.repo._save(db, actor, 'orchestration.requested', 'orchestration_request', item, new=True, publish=False)
            self._save_receipt(db, actor, key, fingerprint, item)
            return item

    def create_dispatch(self, actor, generation, request_id, task_id, role, window_seconds, client_request_id):
        if type(window_seconds) is not int or not 10 <= window_seconds <= 86400:
            raise HubError('invalid_dispatch_window', 422)
        key = self.repo._text(client_request_id, 128)
        fingerprint = self._fingerprint(['role_dispatch', generation, request_id, task_id, role, window_seconds])
        with self.repo._transaction() as db:
            orchestrator, operation = self._held(db, actor, 'orchestrator', generation)
            self._grant(operation, 'role.read')
            request = self._request(db, request_id)
            task, kind, _ = TaskProgress(self.repo)._task(db, actor, task_id)
            try:
                target = RoleRepository(self.repo)._role(db, role)
            except HubError:
                raise HubError('dispatch_unavailable', 404) from None
            if (request['project'] != task['project'] or task['project'] not in target['projects']
                    or task['project'] not in orchestrator['projects'] or task.get('status') in ('withdrawn', 'verified', 'completion_verified')):
                raise HubError('dispatch_unavailable', 404)
            prior = self._receipt(db, actor, key, fingerprint, 'role_dispatch')
            if prior:
                return prior
            existing = db.execute("SELECT 1 FROM hub_entities WHERE kind='role_dispatch' "
                "AND json_extract(data,'$.task_id')=? AND json_extract(data,'$.role_id')=? "
                "AND json_extract(data,'$.state') IN ('dispatched','working','blocked','asked','stale') LIMIT 1", (task_id, role)).fetchone()
            if existing:
                raise HubError('dispatch_already_open', 409)
            now = operation.bridge.sessions._now()
            item = dict(id=str(uuid.uuid4()), request_id=request['id'], project=task['project'], task_id=task_id,
                        task_kind=kind, role_id=role, state='dispatched', report='', window_seconds=window_seconds,
                        last_report_at=now, stale_basis=None, decision_id=None, ask_count=0, coordination_only=True,
                        execution_authorization=False, completion_verified=False, wake_delivery='not_attempted')
            item = self.repo._save(db, actor, 'orchestration.dispatched', 'role_dispatch', item, new=True, publish=False)
            self._save_receipt(db, actor, key, fingerprint, item)
            return item

    def get_request(self, actor, request_id):
        self._operation(actor)
        with self.repo._connection() as db:
            return self._request(db, request_id)

    def list_requests(self, actor, project, limit, after):
        self._operation(actor)
        return self._list_requests(project, limit, after)

    def owner_list_requests(self, actor, project, limit, after):
        self.repo._actor(actor, human=True)
        return self._list_requests(project, limit, after)

    def _list_requests(self, project, limit, after):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if after is not None:
            _canonical_id(after)
        with self.repo._connection() as db:
            self.repo._project(project, db)
            rows = db.execute("SELECT data FROM hub_entities WHERE kind='orchestration_request' AND json_extract(data,'$.project')=? "
                "AND id>? ORDER BY id LIMIT ?", (project, after or '', limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows]
            return dict(items=items[:limit], next_after=items[limit-1]['id'] if len(items) > limit else None,
                        note='UUID order; restart from the beginning to discover new requests.')

    def owner_get_request(self, actor, request_id):
        self.repo._actor(actor, human=True)
        with self.repo._connection() as db:
            return self._request(db, request_id)

    def owner_list_dispatches(self, actor, request_id, limit, after):
        self.repo._actor(actor, human=True)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if after is not None:
            _canonical_id(after)
        with self.repo._connection() as db:
            request = self._request(db, request_id)
            rows = db.execute("SELECT data FROM hub_entities WHERE kind='role_dispatch' AND json_extract(data,'$.request_id')=? AND id>? ORDER BY id LIMIT ?",
                              (request['id'], after or '', limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows]
            if any(item.get('project') != request['project'] for item in items):
                raise HubError('dispatch_unavailable', 404)
            return dict(items=items[:limit], next_after=items[limit-1]['id'] if len(items) > limit else None,
                        wake_delivery='not_attempted')

    def owner_records(self, actor, dispatch_id, kind, limit, after_revision):
        self.repo._actor(actor, human=True)
        if kind not in ('dispatch_report', 'decision') or type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if type(after_revision) is not int or after_revision < 0:
            raise HubError('invalid_cursor', 422)
        with self.repo._connection() as db:
            item = self.repo._get(db, _canonical_id(dispatch_id), 'role_dispatch')
            request = self._request(db, item['request_id'])
            if item.get('project') != request['project']:
                raise HubError('dispatch_unavailable', 404)
            field = 'revision' if kind == 'dispatch_report' else 'origin_dispatch_revision'
            foreign = 'dispatch_id' if kind == 'dispatch_report' else 'origin_dispatch_id'
            rows = db.execute("SELECT data FROM hub_entities WHERE kind=? AND json_extract(data,'$." + foreign + "')=? "
                "AND json_extract(data,'$.project')=? AND json_extract(data,'$." + field + "')>? "
                "ORDER BY json_extract(data,'$." + field + "') LIMIT ?", (kind, item['id'], item['project'], after_revision, limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows]
            return dict(items=items[:limit], next_after_revision=items[limit-1][field] if len(items) > limit else None)

    def get_dispatch(self, actor, dispatch_id):
        self._operation(actor)
        with self.repo._connection() as db:
            return self._dispatch(db, actor, dispatch_id)[0]

    def decisions(self, actor, dispatch_id, limit, after_revision):
        operation = self._operation(actor)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if type(after_revision) is not int or after_revision < 0:
            raise HubError('invalid_cursor', 422)
        with self.repo._connection() as db:
            item, _, _, _ = self._dispatch(db, actor, dispatch_id)
            self._grant(operation, 'decision.read')
            rows = db.execute("SELECT data FROM hub_entities WHERE kind='decision' AND json_extract(data,'$.origin_dispatch_id')=? "
                "AND json_extract(data,'$.project')=? AND json_extract(data,'$.origin_dispatch_revision')>? "
                "ORDER BY json_extract(data,'$.origin_dispatch_revision') LIMIT ?", (item['id'], item['project'], after_revision, limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows]
            return dict(items=items[:limit], next_after_revision=items[limit-1]['origin_dispatch_revision'] if len(items) > limit else None)

    def reports(self, actor, dispatch_id, limit, after_revision):
        self._operation(actor)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if type(after_revision) is not int or after_revision < 0:
            raise HubError('invalid_cursor', 422)
        with self.repo._connection() as db:
            item, _, _, _ = self._dispatch(db, actor, dispatch_id)
            rows = db.execute("SELECT data FROM hub_entities WHERE kind='dispatch_report' AND json_extract(data,'$.dispatch_id')=? "
                "AND json_extract(data,'$.revision')>? ORDER BY json_extract(data,'$.revision') LIMIT ?",
                (item['id'], after_revision, limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows]
            return dict(items=items[:limit], next_after_revision=items[limit-1]['revision'] if len(items) > limit else None)

    def list_dispatches(self, actor, request_id, limit, after):
        operation = self._operation(actor)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if after is not None:
            _canonical_id(after)
        with self.repo._connection() as db:
            request = self._request(db, request_id)
            self._grant(operation, 'role.read')
            kinds = []
            for kind, capability in (('task', 'task.read'), ('tracking_task', 'tracking.read')):
                try:
                    operation.bridge._live(operation.db, operation.secret, operation.principal.authorization_revision,
                                           capability, session_id=operation.principal.session_id)
                    kinds.append(kind)
                except HubError:
                    pass
            if not kinds:
                return dict(items=[], next_after=None, wake_delivery='not_attempted')
            try:
                self._grant(operation, 'decision.read')
                decision_read = 1
            except HubError:
                decision_read = 0
            project_marks = ','.join('?' for _ in operation.projects)
            kind_marks = ','.join('?' for _ in kinds)
            # Scope, active role projects and underlying task kind are filtered
            # in SQLite before pagination; invisible rows cannot starve pages.
            rows = db.execute("SELECT d.id FROM hub_entities d JOIN hub_entities t ON t.id=json_extract(d.data,'$.task_id') "
                "JOIN hub_entities r ON r.kind='coordination_role' AND json_extract(r.data,'$.role_id')=json_extract(d.data,'$.role_id') "
                "WHERE d.kind='role_dispatch' AND json_extract(d.data,'$.request_id')=? AND d.id>? "
                "AND (json_extract(d.data,'$.decision_id') IS NULL OR ?=1) "
                "AND t.kind IN (" + kind_marks + ") AND t.kind=json_extract(d.data,'$.task_kind') "
                "AND json_extract(t.data,'$.project')=? "
                "AND EXISTS (SELECT 1 FROM json_each(r.data,'$.projects') rp WHERE rp.value=?) "
                "AND NOT EXISTS (SELECT 1 FROM json_each(r.data,'$.projects') rp LEFT JOIN hub_projects hp ON hp.id=rp.value "
                "WHERE rp.value NOT IN (" + project_marks + ") OR hp.id IS NULL OR json_extract(hp.data,'$.archived')=1) "
                "ORDER BY d.id LIMIT ?", (request['id'], after or '', decision_read, *kinds, request['project'], request['project'], *operation.projects, limit + 1)).fetchall()
            visible = []
            for row in rows:
                item, _, _, _ = self._dispatch(db, actor, row['id'])
                visible.append(item)
            return dict(items=visible[:limit], next_after=visible[limit-1]['id'] if len(visible) > limit else None,
                        wake_delivery='not_attempted')

    def report(self, actor, dispatch_id, expected_revision, generation, state, note, client_request_id,
               ask_title=None, ask_options=None):
        if state not in ('working', 'blocked', 'ask', 'resume', 'done', 'failed'):
            raise HubError('invalid_dispatch_report', 422)
        note, key = self.repo._text(note, 2000), self.repo._text(client_request_id, 128)
        if state == 'ask':
            ask_title = self.repo._text(ask_title, 200)
            if type(ask_options) is not list or not 2 <= len(ask_options) <= 5:
                raise HubError('invalid_options', 422)
            ask_options = [self.repo._text(option, 200) for option in ask_options]
            if len(set(ask_options)) != len(ask_options):
                raise HubError('invalid_options', 422)
        elif ask_title is not None or ask_options is not None:
            raise HubError('invalid_dispatch_report', 422)
        fingerprint = self._fingerprint(['dispatch_report', dispatch_id, expected_revision, generation, state, note, ask_title, ask_options])
        with self.repo._transaction() as db:
            item, task, _, operation = self._dispatch(db, actor, dispatch_id)
            if task.get('status') in ('withdrawn', 'verified', 'completion_verified'):
                raise HubError('dispatch_task_closed', 409)
            self._grant(operation, 'role.lease', 'role.claim')
            try:
                RoleRepository(self.repo)._held(db, actor, item['role_id'], generation)
                from .roles import _NAMESPACE as role_namespace
                claim = self.repo._get(db, str(uuid.uuid5(role_namespace, 'claim:' + item['task_id'])), 'role_claim')
                if claim.get('role_id') != item['role_id'] or claim.get('status') != 'claimed':
                    raise HubError('dispatch_unavailable', 404)
            except HubError:
                raise HubError('dispatch_unavailable', 404) from None
            prior = self._receipt(db, actor, key, fingerprint, 'role_dispatch')
            if prior:
                return prior
            self.repo._cas(item, expected_revision)
            if item['state'] in ('done', 'failed') or (item['state'] == 'asked' and state != 'resume') or (item['state'] != 'asked' and state == 'resume'):
                raise HubError('dispatch_terminal', 409)
            if state == 'ask':
                self._grant(operation, 'decision.request', 'decision.read')
                decision = dict(id=str(uuid.uuid4()), project=item['project'], title=ask_title, options=ask_options,
                    protected=False, status='open', answer=None, execution_authorization=False,
                    details=note, origin_dispatch_id=item['id'], origin_dispatch_revision=item['revision'] + 1)
                decision = self.repo._save(db, actor, 'decision.created', 'decision', decision, new=True, publish=False)
                item['decision_id'] = decision['id']
                item['ask_count'] = item.get('ask_count', 0) + 1
            if state == 'resume':
                self._grant(operation, 'decision.read')
                try:
                    decision = self.repo._get(db, item['decision_id'], 'decision')
                    if decision.get('project') != item['project'] or decision.get('origin_dispatch_id') != item['id'] or decision.get('status') != 'answered':
                        raise HubError('dispatch_decision_pending', 409)
                except (KeyError, TypeError):
                    raise HubError('dispatch_decision_pending', 409) from None
            item.update(state='asked' if state == 'ask' else 'working' if state == 'resume' else state, report=note,
                        last_report_at=operation.bridge.sessions._now(), stale_basis=None)
            report_revision = item['revision'] + 1
            report = dict(dispatch_id=item['id'], project=item['project'], revision=report_revision,
                          state=item['state'], note=note, reported_at=item['last_report_at'],
                          installation_id=operation.principal.installation_id, session_id=operation.principal.session_id,
                          decision_id=item['decision_id'] if state == 'ask' else None)
            report_id = str(uuid.uuid5(_NAMESPACE, 'report:' + item['id'] + ':' + str(report_revision)))
            db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)',
                       (report_id, 'dispatch_report', json.dumps(report, sort_keys=True, separators=(',', ':'))))
            item = self.repo._save(db, actor, 'orchestration.reported', 'role_dispatch', item, publish=False)
            self._save_receipt(db, actor, key, fingerprint, item)
            return item

    def checkpoint(self, actor, dispatch_id, expected_revision, generation, client_request_id):
        key = self.repo._text(client_request_id, 128)
        fingerprint = self._fingerprint(['dispatch_checkpoint', dispatch_id, expected_revision, generation])
        with self.repo._transaction() as db:
            role, operation = self._held(db, actor, 'orchestrator', generation)
            item, task, _, _ = self._dispatch(db, actor, dispatch_id)
            if task.get('status') in ('withdrawn', 'verified', 'completion_verified'):
                raise HubError('dispatch_task_closed', 409)
            if item['project'] not in role['projects']:
                raise HubError('dispatch_unavailable', 404)
            prior = self._receipt(db, actor, key, fingerprint, 'role_dispatch')
            if prior:
                return prior
            self.repo._cas(item, expected_revision)
            now = operation.bridge.sessions._now()
            if item['state'] in ('dispatched', 'working') and now >= item['last_report_at'] + item['window_seconds']:
                item.update(state='stale', stale_basis='report_timeout_only', wake_delivery='not_attempted')
                item = self.repo._save(db, actor, 'orchestration.stale', 'role_dispatch', item, publish=False)
            self._save_receipt(db, actor, key, fingerprint, item)
            return item
