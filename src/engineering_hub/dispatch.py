"""Durable, inert role dispatch records. No execution or wake authority."""
import hashlib
import json
import math
import uuid
from contextlib import contextmanager

from agent_message_bus import session_hub_context as context
from agent_message_bus.agent_sessions import AgentSessionError
from .repository import HubError
from .roles import RoleRepository
from .task_progress import TaskProgress, role_activity_id, _key as progress_key

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

    @contextmanager
    def _attention_snapshot(self):
        # One read snapshot for project, task, role, dispatch and intent.
        with self.repo._connection() as db:
            db.execute('BEGIN')
            try:
                yield db
            finally:
                db.rollback()

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

    @staticmethod
    def _binding(lease):
        if (type(lease) is not dict or type(lease.get('generation')) is not int
                or type(lease.get('session_id')) is not str or type(lease.get('installation_id')) is not str):
            return None
        return dict(generation=lease['generation'], session_id=lease['session_id'], installation_id=lease['installation_id'])

    def _target_live(self, operation, item, target, binding, now):
        """Check recorded target binding; this never proves a worker stopped."""
        lease = target.get('lease')
        if (self._binding(lease) != binding or target.get('generation') != binding['generation']
                or type(lease.get('expires_at')) not in (int, float) or not math.isfinite(lease['expires_at'])
                or lease['expires_at'] <= now):
            return False
        if binding['installation_id'] not in target['installations']:
            return False
        try:
            capabilities = ['role.read', 'role.lease', 'role.claim', 'dispatch.report',
                            'tracking.read' if item['task_kind'] == 'tracking_task' else 'task.read']
            if item.get('decision_id') is not None:
                capabilities.append('decision.read')
            for capability in capabilities:
                principal, grant = operation.bridge._live(operation.db, None, capability=capability,
                                                          session_id=binding['session_id'])
                if principal.installation_id != binding['installation_id']:
                    return False
                if not set(target['projects']).issubset(set(json.loads(grant['projects']))):
                    return False
            # A session or lease can expire while the Hub transaction is open.
            # Fail the transaction and let a fresh checkpoint mark it stale.
            RoleRepository._guard(operation, min(lease['expires_at'], principal.expires_at, grant['expires_at']))
            return True
        except AgentSessionError as error:
            if error.code in ('invalid_session', 'installation_inactive', 'installation_not_found'):
                return False
            raise
        except HubError as error:
            if error.code in ('hub_grant_required', 'hub_capability_forbidden'):
                return False
            raise

    def _activity_at(self, db, item, binding, now):
        """Use only the current role claim's own committed progress event."""
        if type(item.get('created_at_epoch')) not in (int, float):
            return None  # Legacy dispatch: no trustworthy start time.
        baseline_id = str(uuid.uuid5(_NAMESPACE, 'progress-baseline:' + item['id']))
        try:
            baseline = self.repo._get(db, baseline_id, 'dispatch_progress_baseline')
        except HubError:
            return None  # Legacy dispatch: no trustworthy pre-dispatch revision.
        if (baseline.get('dispatch_id') != item['id'] or baseline.get('task_id') != item['task_id']
                or type(baseline.get('progress_revision')) is not int or baseline['progress_revision'] < 0):
            return None
        from .roles import _NAMESPACE as role_namespace
        try:
            claim = self.repo._get(db, str(uuid.uuid5(role_namespace, 'claim:' + item['task_id'])), 'role_claim')
        except HubError:
            return None
        if claim.get('status') != 'claimed' or claim.get('role_id') != item['role_id']:
            return None
        row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='role_progress_activity'",
                         (role_activity_id(item['task_id'], item['role_id']),)).fetchone()
        if not row:
            return None
        activity = json.loads(row['data'])
        if (activity.get('task_id') != item['task_id'] or activity.get('task_kind') != item['task_kind']
                or activity.get('project') != item['project'] or activity.get('role_id') != item['role_id']
                or activity.get('claim_id') != claim['id'] or activity.get('claim_revision') != claim['revision']
                or activity.get('generation') != binding['generation']
                or activity.get('session_id') != binding['session_id']
                or activity.get('installation_id') != binding['installation_id']
                or type(activity.get('progress_revision')) is not int
                or activity['progress_revision'] <= baseline['progress_revision']
                or type(activity.get('observed_at')) not in (int, float)
                or not math.isfinite(activity['observed_at'])
                or activity['observed_at'] < item['created_at_epoch']):
            return None
        if activity['observed_at'] > now:
            raise HubError('dispatch_clock_untrusted', 409)
        return activity['observed_at']

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

    def _resolve_attention(self, db, actor, item, state):
        """Retain an unattempted intent when a stale condition changes."""
        identity = item.get('attention_intent_id')
        if identity is None:
            return
        try:
            intent = self.repo._get(db, identity, 'orchestration_attention')
            if intent.get('dispatch_id') != item['id'] or intent.get('project') != item['project']:
                raise HubError('attention_corrupt', 409)
        except HubError:
            raise HubError('attention_corrupt', 409) from None
        if intent.get('state') == 'pending_routing':
            intent.update(state=state, delivery='not_attempted', wake_verified=False)
            self.repo._save(db, actor, 'orchestration.attention_resolved', 'orchestration_attention', intent, publish=False)
        item['attention_intent_id'] = None

    def _stale_attention(self, db, actor, item, basis):
        self._resolve_attention(db, actor, item, 'superseded_without_delivery')
        stale_revision = item['revision'] + 1
        identity = str(uuid.uuid5(_NAMESPACE, 'stale-attention:' + item['id'] + ':' + str(stale_revision)))
        item['attention_intent_id'] = identity
        return dict(id=identity, project=item['project'], request_id=item['request_id'], dispatch_id=item['id'],
                    dispatch_revision=stale_revision, recipient_role='orchestrator', reason=basis,
                    state='pending_routing', delivery='not_attempted', wake_verified=False,
                    execution_authorization=False)

    def _report_attention(self, db, item, report_id, report_revision, report_kind):
        """A distinct durable attention for each committed meaningful report."""
        identity = str(uuid.uuid5(_NAMESPACE, 'report-attention:' + item['id'] + ':' + str(report_revision)))
        return dict(id=identity, project=item['project'], request_id=item['request_id'], dispatch_id=item['id'],
                    dispatch_revision=report_revision, report_id=report_id, report_revision=report_revision,
                    report_kind=report_kind, intent_type='worker_report', recipient_role='orchestrator',
                    reason='worker_report', state='pending_routing', delivery='not_attempted', wake_verified=False,
                    execution_authorization=False)

    @staticmethod
    def _valid_report_attention(intent, dispatch, report):
        if type(intent) is not dict or type(dispatch) is not dict or type(report) is not dict:
            return False
        kind = intent.get('report_kind')
        expected_state = {'ask': 'asked', 'resume': 'working', 'blocked': 'blocked',
                          'done': 'done', 'failed': 'failed'}.get(kind)
        return bool(expected_state and intent.get('intent_type') == 'worker_report'
            and intent.get('project') == dispatch.get('project') == report.get('project')
            and intent.get('request_id') == dispatch.get('request_id')
            and intent.get('dispatch_id') == dispatch.get('id') == report.get('dispatch_id')
            and intent.get('report_id') == report.get('id')
            and type(intent.get('report_revision')) is int and intent['report_revision'] > 0
            and intent.get('dispatch_revision') == intent['report_revision'] == report.get('revision')
            and report.get('state') == expected_state)

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
            if not math.isfinite(now):
                raise HubError('dispatch_clock_untrusted', 409)
            item = dict(id=str(uuid.uuid4()), request_id=request['id'], project=task['project'], task_id=task_id,
                        task_kind=kind, role_id=role, state='dispatched', report='', window_seconds=window_seconds,
                        created_at_epoch=now, last_report_at=now, target_binding=self._binding(target.get('lease')),
                        stale_basis=None, coordination_stale=False, decision_id=None, ask_count=0, coordination_only=True,
                        execution_authorization=False, completion_verified=False, wake_delivery='not_attempted')
            existing_progress = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='task_progress'", (progress_key(task_id),)).fetchone()
            try:
                progress_revision = json.loads(existing_progress['data'])['revision'] if existing_progress else 0
            except (ValueError, KeyError, TypeError):
                raise HubError('dispatch_unavailable', 404) from None
            if type(progress_revision) is not int or progress_revision < 0:
                raise HubError('dispatch_unavailable', 404)
            baseline_id = str(uuid.uuid5(_NAMESPACE, 'progress-baseline:' + item['id']))
            db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)',
                       (baseline_id, 'dispatch_progress_baseline', json.dumps(dict(dispatch_id=item['id'], task_id=task_id,
                        progress_revision=progress_revision), sort_keys=True, separators=(',', ':'))))
            item = self.repo._save(db, actor, 'orchestration.dispatched', 'role_dispatch', item, new=True, publish=False)
            self._save_receipt(db, actor, key, fingerprint, item)
        broker = getattr(self.repo, '_attention_hint_broker', None)
        if broker is not None and item['target_binding'] is not None:
            broker.signal(('worker', item['project'], item['role_id']))
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

    def owner_attention(self, actor, project, limit, after):
        self.repo._actor(actor, human=True)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if after is not None:
            _canonical_id(after)
        with self._attention_snapshot() as db:
            self.repo._project(project, db, require_active=False)
            projects_exist = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_projects'").fetchone()
            project_row = db.execute('SELECT data FROM hub_projects WHERE id=?', (project,)).fetchone() if projects_exist else None
            archived = bool(json.loads(project_row['data']).get('archived', False)) if project_row else False
            rows = db.execute("SELECT a.data,t.data AS task_data,d.data AS dispatch_data,p.data AS report_data FROM hub_entities a "
                "LEFT JOIN hub_entities p ON p.id=json_extract(a.data,'$.report_id') AND p.kind='dispatch_report' "
                "JOIN hub_entities d ON d.id=json_extract(a.data,'$.dispatch_id') AND d.kind='role_dispatch' "
                "JOIN hub_entities t ON t.id=json_extract(d.data,'$.task_id') AND t.kind IN ('task','tracking_task') "
                "WHERE a.kind='orchestration_attention' AND json_extract(a.data,'$.project')=? "
                "AND json_extract(d.data,'$.project')=? AND json_extract(t.data,'$.project')=? "
                "AND json_extract(a.data,'$.state')='pending_routing' AND a.id>? ORDER BY a.id LIMIT ?",
                (project, project, project, after or '', limit + 1)).fetchall()
            items = [self._attention_view(row, project_archived=archived) for row in rows]
            return dict(items=items[:limit], next_after=items[limit-1]['id'] if len(items) > limit else None,
                        note='Pending routing only; no delivery or wake is implied. Restart UUID paging to find new items.')

    def attention(self, actor, generation, project, limit, after):
        with self._attention_snapshot() as db:
            return self._attention_in(db, actor, generation, project, limit, after)

    def _attention_in(self, db, actor, generation, project, limit, after, *, actionable_only=False):
        operation = self._operation(actor)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if after is not None:
            _canonical_id(after)
        role, operation = self._held(db, actor, 'orchestrator', generation)
        self._grant(operation, 'dispatch.read')
        self._grant(operation, 'role.read')
        self.repo._project(project, db)
        if project not in role['projects']:
            raise HubError('dispatch_unavailable', 404)
        kinds = []
        for kind, capability in (('task', 'task.read'), ('tracking_task', 'tracking.read')):
            try:
                operation.bridge._live(operation.db, operation.secret, operation.principal.authorization_revision,
                                       capability, session_id=operation.principal.session_id)
                kinds.append(kind)
            except HubError:
                pass
        if not kinds:
            return dict(items=[], next_after=None, note='Pending routing only; no delivery or wake is implied.')
        try:
            self._grant(operation, 'decision.read')
            decision_read = 1
        except HubError:
            decision_read = 0
        project_marks = ','.join('?' for _ in operation.projects)
        kind_marks = ','.join('?' for _ in kinds)
        active_filter = ("AND json_extract(t.data,'$.status') IN ('assigned','claimed','in_review','open','completion_claimed') "
                         "AND ((COALESCE(json_extract(a.data,'$.intent_type'),'stale')='stale' "
                         "AND json_extract(d.data,'$.attention_intent_id')=a.id AND json_extract(d.data,'$.coordination_stale')=1) "
                         "OR (json_extract(a.data,'$.intent_type')='worker_report' AND p.id IS NOT NULL "
                         "AND json_extract(p.data,'$.id')=p.id AND json_extract(p.data,'$.project')=? "
                         "AND json_extract(p.data,'$.dispatch_id')=d.id AND json_extract(a.data,'$.request_id')=q.id "
                         "AND json_type(a.data,'$.report_revision')='integer' AND json_extract(a.data,'$.report_revision')>0 "
                         "AND json_extract(p.data,'$.revision')=json_extract(a.data,'$.report_revision') "
                         "AND json_extract(a.data,'$.dispatch_revision')=json_extract(a.data,'$.report_revision') "
                         "AND ((json_extract(a.data,'$.report_kind')='ask' AND json_extract(p.data,'$.state')='asked') "
                         "OR (json_extract(a.data,'$.report_kind')='resume' AND json_extract(p.data,'$.state')='working') "
                         "OR (json_extract(a.data,'$.report_kind') IN ('blocked','done','failed') "
                         "AND json_extract(p.data,'$.state')=json_extract(a.data,'$.report_kind'))))) " if actionable_only else '')
        rows = db.execute("SELECT a.data,t.data AS task_data,d.data AS dispatch_data,p.data AS report_data FROM hub_entities a LEFT JOIN hub_entities p ON p.id=json_extract(a.data,'$.report_id') AND p.kind='dispatch_report' JOIN hub_entities d ON d.id=json_extract(a.data,'$.dispatch_id') AND d.kind='role_dispatch' "
            "JOIN hub_entities q ON q.id=json_extract(d.data,'$.request_id') AND q.kind='orchestration_request' "
            "JOIN hub_entities t ON t.id=json_extract(d.data,'$.task_id') "
            "JOIN hub_entities r ON r.kind='coordination_role' AND json_extract(r.data,'$.role_id')=json_extract(d.data,'$.role_id') "
            "WHERE a.kind='orchestration_attention' AND json_extract(a.data,'$.project')=? "
            "AND json_extract(a.data,'$.recipient_role')='orchestrator' AND json_extract(a.data,'$.state')='pending_routing' AND a.id>? "
            "AND json_extract(d.data,'$.project')=? AND json_extract(q.data,'$.project')=? AND json_extract(t.data,'$.project')=? "
            "AND (json_extract(d.data,'$.decision_id') IS NULL OR ?=1) "
            "AND t.kind IN (" + kind_marks + ") AND t.kind=json_extract(d.data,'$.task_kind') "
            "AND EXISTS (SELECT 1 FROM json_each(r.data,'$.projects') rp WHERE rp.value=?) "
            "AND NOT EXISTS (SELECT 1 FROM json_each(r.data,'$.projects') rp LEFT JOIN hub_projects hp ON hp.id=rp.value "
            "WHERE rp.value NOT IN (" + project_marks + ") OR hp.id IS NULL OR json_extract(hp.data,'$.archived')=1) "
            + active_filter + "ORDER BY a.id LIMIT ?", (project, after or '', project, project, project, decision_read, *kinds, project, *operation.projects, *((project,) if actionable_only else ()), limit + 1)).fetchall()
        items = [self._attention_view(row) for row in rows]
        for item in items:
            self._dispatch(db, actor, item['dispatch_id'])
        return dict(items=items[:limit], next_after=items[limit-1]['id'] if len(items) > limit else None,
                    note='Pending routing only; no delivery or wake is implied. Restart UUID paging to find new items.')


    @staticmethod
    def _attention_view(row, *, project_archived=False):
        intent, task, dispatch = json.loads(row['data']), json.loads(row['task_data']), json.loads(row['dispatch_data'])
        status = task.get('status')
        open_status = status in ('assigned', 'claimed', 'in_review', 'open', 'completion_claimed')
        if intent.get('intent_type') == 'worker_report':
            try:
                report = json.loads(row['report_data']) if row['report_data'] else None
            except (ValueError, TypeError):
                report = None
            current = DispatchRepository._valid_report_attention(intent, dispatch, report)
        elif intent.get('intent_type') in (None, 'stale'):
            current = dispatch.get('attention_intent_id') == intent['id'] and dispatch.get('coordination_stale') is True
        else:
            current = False
        return {**intent, 'task_status': status, 'project_archived': project_archived,
                'actionable': open_status and current and not project_archived}

    def reconcile_closed_attention(self, actor, generation, intent_id, expected_revision,
                                   expected_dispatch_revision, client_request_id):
        key = self.repo._text(client_request_id, 128)
        fingerprint = self._fingerprint(['attention_closed', generation, intent_id, expected_revision, expected_dispatch_revision])
        with self.repo._transaction() as db:
            role, operation = self._held(db, actor, 'orchestrator', generation)
            self._grant(operation, 'dispatch.read')
            try:
                intent = self.repo._get(db, _canonical_id(intent_id), 'orchestration_attention')
                item, task, _, _ = self._dispatch(db, actor, intent['dispatch_id'])
                if intent['project'] != item['project'] or item['project'] not in role['projects']:
                    raise HubError('dispatch_unavailable', 404)
            except (HubError, KeyError):
                raise HubError('dispatch_unavailable', 404) from None
            prior = self._receipt(db, actor, key, fingerprint, 'orchestration_attention')
            if prior:
                return prior
            self.repo._cas(intent, expected_revision)
            self.repo._cas(item, expected_dispatch_revision)
            report_intent = intent.get('intent_type') == 'worker_report'
            if report_intent:
                try:
                    report = self.repo._get(db, intent['report_id'], 'dispatch_report')
                    valid = self._valid_report_attention(intent, item, report)
                except (KeyError, HubError, TypeError, ValueError):
                    valid = False
            else:
                valid = intent.get('intent_type') in (None, 'stale') and item.get('attention_intent_id') == intent['id']
            if (intent.get('state') != 'pending_routing' or not valid
                    or task.get('status') not in ('withdrawn', 'verified', 'completion_verified')):
                raise HubError('attention_not_closed', 409)
            intent.update(state='resolved_task_closed_without_delivery', delivery='not_attempted', wake_verified=False)
            intent = self.repo._save(db, actor, 'orchestration.attention_task_closed', 'orchestration_attention', intent, publish=False)
            if not report_intent:
                item['attention_intent_id'] = None
                self.repo._save(db, actor, 'orchestration.attention_reconciled', 'role_dispatch', item, publish=False)
            self._save_receipt(db, actor, key, fingerprint, intent)
            return intent

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

    def role_inbox(self, actor, role, generation, project, limit, after):
        """Discover a held role's open dispatches; this never assigns execution."""
        operation = self._operation(actor)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_limit', 422)
        if after is not None:
            _canonical_id(after)
        with self._attention_snapshot() as db:
            held, operation = self._held(db, actor, role, generation)
            self._grant(operation, 'dispatch.read', 'role.read')
            self.repo._project(project, db)
            if project not in held['projects']:
                raise HubError('dispatch_unavailable', 404)
            # Ensure even an empty response rechecks the exact lease.
            def recheck(_db):
                with self.repo._connection() as current_db:
                    self._held(current_db, actor, role, generation)
                    self.repo._project(project, current_db)
            operation.commit_checks.append(recheck)
            kinds = []
            for kind, capability in (('task', 'task.read'), ('tracking_task', 'tracking.read')):
                try:
                    self._grant(operation, capability)
                    kinds.append(kind)
                except HubError:
                    pass
            if not kinds:
                return dict(items=[], next_after=None, wake_delivery='not_attempted', execution_authorization=False)
            try:
                self._grant(operation, 'decision.read')
                decision_read = 1
            except HubError:
                decision_read = 0
            marks = ','.join('?' for _ in kinds)
            rows = db.execute("SELECT d.id FROM hub_entities d "
                "JOIN hub_entities t ON t.id=json_extract(d.data,'$.task_id') "
                "JOIN hub_entities q ON q.id=json_extract(d.data,'$.request_id') AND q.kind='orchestration_request' "
                "WHERE d.kind='role_dispatch' AND json_extract(d.data,'$.role_id')=? "
                "AND json_extract(d.data,'$.project')=? AND json_extract(q.data,'$.project')=? "
                "AND json_extract(t.data,'$.project')=? AND d.id>? "
                "AND json_extract(d.data,'$.state') IN ('dispatched','working','blocked','asked','stale') "
                "AND json_extract(t.data,'$.status') IN ('assigned','claimed','in_review','open','completion_claimed') "
                "AND (json_extract(d.data,'$.decision_id') IS NULL OR ?=1) "
                "AND t.kind IN (" + marks + ") AND t.kind=json_extract(d.data,'$.task_kind') "
                "ORDER BY d.id LIMIT ?", (role, project, project, project, after or '', decision_read, *kinds, limit + 1)).fetchall()
            values = []
            for row in rows:
                item, task, target, _ = self._dispatch(db, actor, row['id'])
                if target.get('role_id') != role or item.get('project') != project:
                    raise HubError('dispatch_unavailable', 404)
                binding = item.get('target_binding')
                current = self._binding(held.get('lease'))
                if binding is None:
                    binding_state = 'unbound'
                elif self._binding(binding) is None:
                    binding_state = 'unknown'
                elif self._binding(binding) == current:
                    binding_state = 'current_session'
                else:
                    binding_state = 'prior_or_other_session'
                from .roles import _NAMESPACE as role_namespace
                claim_id = str(uuid.uuid5(role_namespace, 'claim:' + item['task_id']))
                claim_row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='role_claim'", (claim_id,)).fetchone()
                claim_state = 'unavailable'
                if claim_row is not None:
                    try:
                        claim = json.loads(claim_row['data'])
                        if (type(claim) is dict and claim.get('task_id') == item['task_id']
                                and claim.get('project') == project and claim.get('role_id') == role):
                            if claim.get('status') == 'released':
                                claim_state = 'released'
                            elif claim.get('status') == 'claimed':
                                if (claim.get('created_session_id') == operation.principal.session_id
                                        and claim.get('created_installation_id') == operation.principal.installation_id):
                                    claim_state = 'current_session'
                                else:
                                    claim_state = 'prior_or_other_session'
                    except (ValueError, TypeError):
                        pass  # Missing, foreign and malformed claims are indistinguishable.
                values.append(dict(dispatch=item, task_status=task['status'], target_binding_state=binding_state,
                                   coordination_claim_state=claim_state, report_text_untrusted=True, execution_authorization=False))
            return dict(items=values[:limit],
                        next_after=values[limit-1]['dispatch']['id'] if len(values) > limit else None,
                        wake_delivery='not_attempted', execution_authorization=False,
                        note='UUID order; refresh from the beginning for new dispatches. Prior bindings do not imply takeover.')

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
            now = operation.bridge.sessions._now()
            if not math.isfinite(now) or now < item['last_report_at']:
                raise HubError('dispatch_clock_untrusted', 409)
            self._resolve_attention(db, actor, item, 'resolved_without_delivery')
            item.update(state='asked' if state == 'ask' else 'working' if state == 'resume' else state, report=note,
                        last_report_at=now, stale_basis=None, coordination_stale=False,
                        target_binding=dict(generation=generation, session_id=operation.principal.session_id,
                                            installation_id=operation.principal.installation_id))
            report_revision = item['revision'] + 1
            report = dict(dispatch_id=item['id'], project=item['project'], revision=report_revision,
                          state=item['state'], note=note, reported_at=item['last_report_at'],
                          installation_id=operation.principal.installation_id, session_id=operation.principal.session_id,
                          decision_id=item['decision_id'] if state == 'ask' else None)
            report_id = str(uuid.uuid5(_NAMESPACE, 'report:' + item['id'] + ':' + str(report_revision)))
            report['id'] = report_id
            db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)',
                       (report_id, 'dispatch_report', json.dumps(report, sort_keys=True, separators=(',', ':'))))
            item = self.repo._save(db, actor, 'orchestration.reported', 'role_dispatch', item, publish=False)
            notify = state in ('blocked', 'ask', 'resume', 'done', 'failed')
            if notify:
                intent = self._report_attention(db, item, report_id, report_revision, state)
                self.repo._save(db, actor, 'orchestration.report_attention_pending', 'orchestration_attention',
                                intent, new=True, publish=False)
            self._save_receipt(db, actor, key, fingerprint, item)
        if notify:
            broker = getattr(self.repo, '_attention_hint_broker', None)
            if broker is not None:
                broker.signal(item['project'])
        return item

    def _evaluate_checkpoint(self, db, actor, item, target, operation, *, timeout_grace=0, allow_timeout=True, allow_target_loss=True, loss_observed=None):
        """Apply the shared advisory liveness transition within a held writer."""
        now = operation.bridge.sessions._now()
        if (not math.isfinite(now) or type(item.get('last_report_at')) not in (int, float)
                or not math.isfinite(item['last_report_at']) or now < item['last_report_at']):
            raise HubError('dispatch_clock_untrusted', 409)
        created = item.get('created_at_epoch')
        if created is not None and (type(created) not in (int, float) or not math.isfinite(created) or now < created):
            raise HubError('dispatch_clock_untrusted', 409)
        binding = item.get('target_binding')
        basis = None
        activity_at = item['last_report_at']
        bound_by_activity = False
        if item['state'] in ('dispatched', 'working', 'blocked', 'asked'):
            if binding is None:
                # A dispatch may predate its first role lease. A committed,
                # attributable progress update can bind the current lease;
                # merely observing a lease cannot imply delivery or work.
                candidate = self._binding(target.get('lease'))
                if candidate is not None and self._activity_at(db, item, candidate, now) is not None:
                    if self._target_live(operation, item, target, candidate, now):
                        binding = candidate
                        item['target_binding'] = candidate
                        bound_by_activity = True
            if binding is not None and (type(binding) is not dict or not self._target_live(operation, item, target, binding, now)):
                basis = 'target_lease_or_authorization_lost'
            elif binding is not None:
                observed = self._activity_at(db, item, binding, now)
                if observed is not None:
                    activity_at = max(activity_at, observed)
            if basis == 'target_lease_or_authorization_lost':
                if loss_observed is not None:
                    loss_observed.append(True)
                if not allow_target_loss:
                    return item
            if basis is None and allow_timeout and item['state'] in ('dispatched', 'working') and now >= activity_at + item['window_seconds'] + timeout_grace:
                basis = 'report_and_attributed_progress_timeout'
        if basis is not None:
            # Preserve blocked/asked state and its outstanding decision;
            # expiry is an advisory stale flag, not loss of that context.
            state = item['state'] if item['state'] in ('blocked', 'asked') else 'stale'
            if item.get('state') != state or item.get('stale_basis') != basis or not item.get('coordination_stale'):
                intent = self._stale_attention(db, actor, item, basis)
                item.update(state=state, stale_basis=basis, coordination_stale=True, wake_delivery='not_attempted')
                item = self.repo._save(db, actor, 'orchestration.stale', 'role_dispatch', item, publish=False)
                self.repo._save(db, actor, 'orchestration.attention_pending', 'orchestration_attention', intent,
                                new=True, publish=False)
        elif bound_by_activity:
            item = self.repo._save(db, actor, 'orchestration.activity_bound', 'role_dispatch', item, publish=False)
        return item

    def checkpoint(self, actor, dispatch_id, expected_revision, generation, client_request_id):
        key = self.repo._text(client_request_id, 128)
        fingerprint = self._fingerprint(['dispatch_checkpoint', dispatch_id, expected_revision, generation])
        with self.repo._transaction() as db:
            role, operation = self._held(db, actor, 'orchestrator', generation)
            item, task, target, _ = self._dispatch(db, actor, dispatch_id)
            if task.get('status') in ('withdrawn', 'verified', 'completion_verified'):
                raise HubError('dispatch_task_closed', 409)
            if item['project'] not in role['projects']:
                raise HubError('dispatch_unavailable', 404)
            prior = self._receipt(db, actor, key, fingerprint, 'role_dispatch')
            if prior:
                return prior
            self.repo._cas(item, expected_revision)
            previous_intent = item.get('attention_intent_id')
            item = self._evaluate_checkpoint(db, actor, item, target, operation)
            self._save_receipt(db, actor, key, fingerprint, item)
            notify = bool(item.get('attention_intent_id') and item['attention_intent_id'] != previous_intent)
        if notify:
            broker = getattr(self.repo, '_attention_hint_broker', None)
            if broker is not None:
                broker.signal(item['project'])
        return item
