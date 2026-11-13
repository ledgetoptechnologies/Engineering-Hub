"""Versioned coordination progress, separate from task execution state."""
import json
import math
import uuid
import hashlib

from agent_message_bus import session_hub_context as context
from .repository import HubError
from .roles import RoleRepository, _NAMESPACE

_PROGRESS_NS = uuid.UUID('97181b1c-a3ca-4615-9b34-ef9cba79e6c4')


def _key(task_id):
    try:
        if type(task_id) is not str or str(uuid.UUID(task_id)) != task_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise HubError('progress_unavailable', 404) from None
    return str(uuid.uuid5(_PROGRESS_NS, task_id))


class TaskProgress:
    def __init__(self, repo):
        self.repo = repo

    def _task(self, db, actor, task_id):
        operation = context.current.get()
        if operation is None or not context.actor_allowed(actor):
            raise HubError('progress_unavailable', 404)
        _key(task_id)
        row = db.execute("SELECT kind,data FROM hub_entities WHERE id=? AND kind IN ('task','tracking_task')", (task_id,)).fetchone()
        if row is None:
            raise HubError('progress_unavailable', 404)
        task = json.loads(row['data'])
        try:
            self.repo._project(task['project'], db)
            operation.bridge._live(operation.db, operation.secret, operation.principal.authorization_revision,
                'tracking.read' if row['kind'] == 'tracking_task' else 'task.read', session_id=operation.principal.session_id)
        except HubError:
            raise HubError('progress_unavailable', 404) from None
        return task, row['kind'], operation

    @staticmethod
    def _read(db, task_id, task):
        row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='task_progress'", (_key(task_id),)).fetchone()
        return json.loads(row['data']) if row else dict(task_id=task_id, project=task['project'], revision=0,
            checklist=[], percent=None, eta_seconds=None, agent_eta_guess=None, status_line='', blocked_on='',
            coordination_only=True, execution_authorization=False)

    def get(self, actor, task_id):
        with self.repo._connection() as db:
            task, _, _ = self._task(db, actor, task_id)
            return self._read(db, task_id, task)

    def owner_get(self, actor, task_id):
        self.repo._actor(actor, human=True)
        _key(task_id)
        with self.repo._connection() as db:
            row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind IN ('task','tracking_task')", (task_id,)).fetchone()
            if row is None:
                raise HubError('progress_unavailable', 404)
            task = json.loads(row['data'])
            try:
                self.repo._project(task['project'], db)
            except HubError:
                raise HubError('progress_unavailable', 404) from None
            return self._read(db, task_id, task)

    def update(self, actor, task_id, expected_revision, client_request_id, checklist, status_line, blocked_on,
               agent_eta_guess=None, role=None, generation=None):
        request_id = self.repo._text(client_request_id, 128)
        if (type(checklist) is not list or len(checklist) > 50 or
                type(status_line) is not str or len(status_line) > 1000 or
                type(blocked_on) is not str or len(blocked_on) > 1000 or
                (agent_eta_guess is not None and (type(agent_eta_guess) is not int or not 0 <= agent_eta_guess <= 31_536_000))):
            raise HubError('invalid_progress', 422)
        for entry in checklist:
            if (type(entry) is not dict or set(entry) != {'id', 'text', 'done'} or
                    type(entry['id']) is not str or not 1 <= len(entry['id']) <= 64 or
                    type(entry['text']) is not str or not entry['text'].strip() or len(entry['text']) > 500 or
                    type(entry['done']) is not bool):
                raise HubError('invalid_progress', 422)
        if len({e['id'] for e in checklist}) != len(checklist):
            raise HubError('invalid_progress', 422)
        fingerprint = hashlib.sha256(json.dumps(['task_progress', task_id, expected_revision, checklist,
            status_line, blocked_on, agent_eta_guess, role, generation], sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        with self.repo._transaction() as db:
            task, kind, operation = self._task(db, actor, task_id)
            if task.get('status') in ('withdrawn', 'verified', 'completion_verified'):
                raise HubError('progress_unavailable', 404)
            if role is not None:
                try:
                    for capability in ('role.lease', 'role.claim'):
                        operation.bridge._live(operation.db, operation.secret, operation.principal.authorization_revision,
                                               capability, session_id=operation.principal.session_id)
                    role_item, _ = RoleRepository(self.repo)._held(db, actor, role, generation)
                    claim = self.repo._get(db, str(uuid.uuid5(_NAMESPACE, 'claim:' + task_id)), 'role_claim')
                    if claim.get('role_id') != role or claim.get('task_id') != task_id or claim.get('status') != 'claimed' or task['project'] not in role_item['projects']:
                        raise HubError('progress_unavailable', 404)
                except (HubError, TypeError, ValueError):
                    raise HubError('progress_unavailable', 404) from None
            elif (generation is not None or kind != 'task' or task.get('owner') != actor or
                  task.get('owner_session_id') != operation.principal.session_id):
                raise HubError('progress_unavailable', 404)
            prior = db.execute('SELECT * FROM hub_requests WHERE actor=? AND key=?', (actor, request_id)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HubError('idempotency_conflict', 409)
                receipt = self.repo._get(db, prior['entity_id'], 'task_progress_receipt')
                current = self.repo._get(db, receipt['progress_id'], 'task_progress')
                if current['revision'] != receipt['revision']:
                    raise HubError('progress_changed', 409)
                return current
            key = _key(task_id)
            row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='task_progress'", (key,)).fetchone()
            previous = json.loads(row['data']) if row else None
            if type(expected_revision) is not int or expected_revision != (previous['revision'] if previous else 0):
                raise HubError('revision_conflict', 409)
            now = operation.bridge.sessions._now()
            if not math.isfinite(now):
                raise HubError('invalid_progress_clock', 409)
            done = sum(entry['done'] for entry in checklist)
            total = len(checklist)
            percent = round(100 * done / total, 2) if total else None
            eta = None
            baseline = {'time': now, 'done': done}
            if previous:
                old = previous['checklist']
                same = len(old) == total and all(a['id'] == b['id'] and a['text'] == b['text'] for a, b in zip(old, checklist))
                monotonic = same and all(not a['done'] or b['done'] for a, b in zip(old, checklist))
                old_time = previous.get('_observed_at', now)
                if monotonic and now > old_time and now - old_time <= 86400:
                    anchor = previous.get('_rate_anchor') or {'time': old_time, 'done': sum(e['done'] for e in old)}
                    if (type(anchor) is dict and type(anchor.get('time')) in (int, float)
                            and type(anchor.get('done')) is int and anchor['time'] < now
                            and now - anchor['time'] <= 86400 and done >= anchor['done']):
                        baseline = anchor
                        if done > anchor['done']:
                            eta = min(31_536_000, max(0, round((now - anchor['time']) * (total - done) / (done - anchor['done']))))
            item = dict(id=key, task_id=task_id, task_kind=kind, project=task['project'], checklist=checklist,
                        percent=percent, eta_seconds=eta, agent_eta_guess=agent_eta_guess,
                        status_line=status_line, blocked_on=blocked_on, _observed_at=now,
                        _rate_anchor=baseline, coordination_only=True, execution_authorization=False)
            if previous:
                item['revision'] = previous['revision']
            result = self.repo._save(db, actor, 'task.progress_updated', 'task_progress', item,
                                     new=previous is None, publish=False)
            receipt_id = str(uuid.uuid5(_PROGRESS_NS, 'receipt:' + actor + ':' + request_id))
            db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)',
                       (receipt_id, 'task_progress_receipt', json.dumps({'progress_id': key, 'revision': result['revision']}, sort_keys=True, separators=(',', ':'))))
            db.execute('INSERT INTO hub_requests VALUES(?,?,?,?)', (actor, request_id, fingerprint, receipt_id))
            return result
