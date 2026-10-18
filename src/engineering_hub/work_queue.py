"""Durable, explicitly driven cross-environment admission queue.

This is not an executor or an approval service. Registry entries are supplied by
the operator; no command, endpoint, path or secret is accepted in a job. Existing
staging dispatch remains responsible for its workflow/review checks. An expired
reservation holds the physical machine until authoritative reconciliation.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import re
import time
import uuid

from .repository import AGENTS, PROJECTS, HubError


def _key(value):
    if type(value) is not str or re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}', value) is None:
        raise HubError('invalid_work_key')
    return value


@dataclass(frozen=True)
class WorkAction:
    machine_id: str
    environments: frozenset[str] = frozenset({'staging'})
    project: str = field(kw_only=True)
    requires_execution_proof: bool = True

    def __post_init__(self):
        _key(self.machine_id)
        if type(self.project) is not str or self.project not in PROJECTS:
            raise HubError('invalid_work_project')
        if type(self.requires_execution_proof) is not bool:
            raise HubError('invalid_work_execution_proof_policy')
        if type(self.environments) is not frozenset or not self.environments or not self.environments <= {'production', 'staging'}:
            raise HubError('invalid_work_environments')


@dataclass(frozen=True)
class WorkTarget:
    task_id: str
    source_commit: str
    artifact_digest: str

    def __post_init__(self):
        try:
            valid = type(self.task_id) is str and str(uuid.UUID(self.task_id)) == self.task_id
        except ValueError:
            valid = False
        if not valid or type(self.source_commit) is not str or re.fullmatch(r'[a-f0-9]{40}', self.source_commit) is None or type(self.artifact_digest) is not str or re.fullmatch(r'sha256:[a-f0-9]{64}', self.artifact_digest) is None:
            raise HubError('invalid_work_target')


@dataclass(frozen=True)
class WorkWorker:
    agent: str
    instance_id: str

    def __post_init__(self):
        try:
            valid = type(self.instance_id) is str and str(uuid.UUID(self.instance_id)) == self.instance_id
        except ValueError:
            valid = False
        if type(self.agent) is not str or self.agent not in AGENTS or not valid:
            raise HubError('invalid_work_worker')

    def encoded(self):
        return self.agent + ':' + self.instance_id


class WorkQueue:
    def __init__(self, repo, actions, *, approval_verifier=None,
                 reconciliation_verifier=None, completion_verifier=None, target_verifier=None, clock=time.time, max_jobs=10000):
        if type(actions) is not dict or not 1 <= len(actions) <= 256:
            raise HubError('invalid_work_registry')
        self.actions = dict(actions)
        for key, action in self.actions.items():
            _key(key)
            if type(action) is not WorkAction:
                raise HubError('invalid_work_registry')
        if type(max_jobs) is not int or not 1 <= max_jobs <= 100000:
            raise HubError('invalid_work_capacity')
        if not callable(clock) or any(v is not None and not callable(v) for v in (approval_verifier, reconciliation_verifier, completion_verifier, target_verifier)):
            raise HubError('invalid_work_verifier')
        self.repo, self.clock, self.max_jobs = repo, clock, max_jobs
        self.approval_verifier = approval_verifier
        self.reconciliation_verifier = reconciliation_verifier
        self.completion_verifier = completion_verifier
        self.target_verifier = target_verifier
        with repo._transaction() as db:
            for statement in '''
                CREATE TABLE IF NOT EXISTS hub_work_jobs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE, actor TEXT NOT NULL, request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, action_key TEXT NOT NULL,
                    environment TEXT NOT NULL, machine_id TEXT NOT NULL, project TEXT NOT NULL,
                    approval_id TEXT, target TEXT NOT NULL, requires_execution_proof INTEGER NOT NULL,
                    state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, worker TEXT,
                    fence INTEGER, lease_until REAL, created_at REAL NOT NULL,
                    UNIQUE(actor,request_id));
                CREATE TABLE IF NOT EXISTS hub_work_machines (
                    machine_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                    job_id TEXT);
                CREATE TABLE IF NOT EXISTS hub_work_policy (
                    id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO hub_work_policy VALUES(1,0,0);
            '''.split(';'):
                if statement.strip():
                    db.execute(statement)

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise HubError('invalid_work_clock')
        return value

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_work_limit')

    @staticmethod
    def _lease(seconds):
        if type(seconds) is not int or not 1 <= seconds <= 600:
            raise HubError('invalid_work_lease')

    @staticmethod
    def _public(row):
        result = {k: row[k] for k in row.keys() if k != 'fingerprint'}
        result['target'] = json.loads(result['target'])
        result['requires_execution_proof'] = bool(result['requires_execution_proof'])
        result['coordination_only'] = not result['requires_execution_proof']
        return result

    def _approved(self, row):
        if row['environment'] == 'staging':
            return True
        if self.approval_verifier is None or row['approval_id'] is None:
            return False
        try:
            return self.approval_verifier(row['actor'], row['action_key'], row['environment'], row['approval_id'], WorkTarget(**json.loads(row['target']))) is True
        except Exception:
            return False

    def action_project(self, action_key):
        _key(action_key)
        action = self.actions.get(action_key)
        if action is None:
            raise HubError('work_action_not_registered', 403)
        return action.project

    def _target_verified(self, actor, action_key, environment, target):
        if self.target_verifier is None:
            return False
        try:
            return self.target_verifier(actor, action_key, environment, target) is True
        except Exception:
            return False

    def _event(self, db, action, job_id, actor):
        db.execute('UPDATE hub_work_jobs SET revision=revision+1 WHERE id=?', (job_id,))
        revision = db.execute('SELECT revision FROM hub_work_jobs WHERE id=?', (job_id,)).fetchone()[0]
        self._emit(db, action, job_id, revision, actor)

    def _emit(self, db, action, entity_id, revision, actor):
        stamp = datetime.now(timezone.utc).isoformat()
        db.execute('INSERT INTO hub_audit(actor,action,entity_id,revision,created_at) VALUES(?,?,?,?,?)', (actor, action, entity_id, revision, stamp))
        self.repo._emit(db, action, {'id': entity_id, 'revision': revision, 'updated_at': stamp})

    def _expire(self, db, now):
        # Keep machine ownership: lease expiry cannot prove an external effect
        # stopped. Only authoritative reconciliation can release this hold.
        expired = db.execute("SELECT id FROM hub_work_jobs WHERE state='claimed' AND lease_until<=?", (now,)).fetchall()
        for row in expired:
            db.execute("UPDATE hub_work_jobs SET state='held' WHERE id=?", (row['id'],))
            self._event(db, 'work.held', row['id'], 'system:work-queue')

    def submit(self, *, actor, request_id, action_key, environment, target, approval_id=None):
        if type(actor) is not str or actor not in AGENTS:
            raise HubError('invalid_work_actor')
        _key(request_id)
        _key(action_key)
        if type(environment) is not str or environment not in {'staging', 'production'}:
            raise HubError('invalid_work_environment')
        if approval_id is not None:
            _key(approval_id)
        action = self.actions.get(action_key)
        if action is None or environment not in action.environments:
            raise HubError('work_action_not_registered', 403)
        if type(target) is not WorkTarget:
            raise HubError('invalid_work_target')
        if not self._target_verified(actor, action_key, environment, target):
            raise HubError('work_target_not_verified', 403)
        target_json = json.dumps(dict(task_id=target.task_id, source_commit=target.source_commit, artifact_digest=target.artifact_digest), sort_keys=True, separators=(',', ':'))
        fingerprint = json.dumps([action_key, environment, action.machine_id, action.project, action.requires_execution_proof, approval_id, target_json], separators=(',', ':'))
        with self.repo._transaction() as db:
            old = db.execute('SELECT * FROM hub_work_jobs WHERE actor=? AND request_id=?', (actor, request_id)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise HubError('work_request_conflict', 409)
                return self._public(old)
            if db.execute('SELECT COUNT(*) FROM hub_work_jobs').fetchone()[0] >= self.max_jobs:
                raise HubError('work_queue_capacity', 503)
            job_id = str(uuid.uuid4())
            db.execute('''INSERT INTO hub_work_jobs
                (id,actor,request_id,fingerprint,action_key,environment,machine_id,project,approval_id,target,requires_execution_proof,state,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued',?)''',
                (job_id, actor, request_id, fingerprint, action_key, environment, action.machine_id, action.project, approval_id, target_json, int(action.requires_execution_proof), self._now()))
            self._event(db, 'work.submitted', job_id, actor)
            return self._public(db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job_id,)).fetchone())

    def claim(self, *, worker, lease_seconds=60):
        if type(worker) is not WorkWorker:
            raise HubError('invalid_work_worker')
        self._lease(lease_seconds)
        with self.repo._transaction() as db:
            now = self._now()
            self._expire(db, now)
            if db.execute('SELECT paused FROM hub_work_policy WHERE id=1').fetchone()[0] or self.repo._paused(db):
                return None
            # Entire durable queue is bounded. Approval is rechecked at claim,
            # not cached from submission. Production priority is non-preemptive.
            rows = db.execute("SELECT j.* FROM hub_work_jobs j LEFT JOIN hub_work_machines m ON m.machine_id=j.machine_id WHERE j.state='queued' AND m.job_id IS NULL ORDER BY CASE j.environment WHEN 'production' THEN 0 ELSE 1 END,j.sequence").fetchall()
            for row in rows:
                action = self.actions.get(row['action_key'])
                if action is None or action.machine_id != row['machine_id'] or action.project != row['project'] or action.requires_execution_proof != bool(row['requires_execution_proof']) or row['environment'] not in action.environments or not self._approved(row) or not self._target_verified(row['actor'], row['action_key'], row['environment'], WorkTarget(**json.loads(row['target']))):
                    continue
                db.execute('INSERT OR IGNORE INTO hub_work_machines VALUES(?,0,NULL)', (row['machine_id'],))
                db.execute('UPDATE hub_work_machines SET generation=generation+1,job_id=? WHERE machine_id=?', (row['id'], row['machine_id']))
                fence = db.execute('SELECT generation FROM hub_work_machines WHERE machine_id=?', (row['machine_id'],)).fetchone()[0]
                db.execute("UPDATE hub_work_jobs SET state='claimed',worker=?,fence=?,lease_until=? WHERE id=?", (worker.encoded(), fence, now + lease_seconds, row['id']))
                self._event(db, 'work.claimed', row['id'], worker.agent)
                return self._public(db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (row['id'],)).fetchone())
            return None

    def _owned(self, db, job_id, worker, fence, now):
        _key(job_id)
        if type(worker) is not WorkWorker:
            raise HubError('invalid_work_worker')
        if type(fence) is not int or fence < 1:
            raise HubError('invalid_work_fence')
        row = db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None or row['state'] != 'claimed' or row['worker'] != worker.encoded() or row['fence'] != fence or row['lease_until'] <= now:
            raise HubError('work_ownership_lost', 409)
        machine = db.execute('SELECT generation,job_id FROM hub_work_machines WHERE machine_id=?', (row['machine_id'],)).fetchone()
        if machine is None or tuple(machine) != (fence, job_id):
            raise HubError('work_ownership_lost', 409)
        return row

    def live_claim(self, *, job_id, worker, fence):
        """Revalidate admission immediately before an explicitly owned effect.

        This does not extend the lease or supply an executor capability. A
        worker must still persist intent and never replay uncertain effects.
        """
        with self.repo._transaction() as db:
            row = self._owned(db, job_id, worker, fence, self._now())
            if db.execute('SELECT paused FROM hub_work_policy WHERE id=1').fetchone()[0] or self.repo._paused(db):
                raise HubError('work_queue_paused', 409)
            action = self.actions.get(row['action_key'])
            if (action is None or action.machine_id != row['machine_id']
                    or action.project != row['project']
                    or action.requires_execution_proof != bool(row['requires_execution_proof'])
                    or row['environment'] not in action.environments
                    or not self._approved(row)
                    or not self._target_verified(row['actor'], row['action_key'], row['environment'],
                                                 WorkTarget(**json.loads(row['target'])))):
                raise HubError('work_admission_changed', 409)
            return self._public(row)

    def renew(self, *, job_id, worker, fence, lease_seconds=60):
        self._lease(lease_seconds)
        with self.repo._transaction() as db:
            now = self._now()
            row = self._owned(db, job_id, worker, fence, now)
            if row['lease_until'] != now + lease_seconds:
                db.execute('UPDATE hub_work_jobs SET lease_until=? WHERE id=?', (now + lease_seconds, job_id))
                self._event(db, 'work.renewed', job_id, worker.agent)
            return self._public(db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job_id,)).fetchone())

    def complete(self, *, job_id, worker, fence, outcome, evidence_id=None):
        if type(outcome) is not str or outcome not in {'succeeded', 'failed', 'held'}:
            raise HubError('invalid_work_outcome')
        if evidence_id is not None:
            _key(evidence_id)
        with self.repo._transaction() as db:
            row = self._owned(db, job_id, worker, fence, self._now())
            if outcome != 'held' and row['requires_execution_proof']:
                verified = False
                if evidence_id is not None and self.completion_verifier is not None:
                    try:
                        verified = self.completion_verifier(self._public(row), evidence_id, outcome) is True
                    except Exception:
                        pass
                if not verified:
                    raise HubError('work_completion_proof_required', 409)
            db.execute('UPDATE hub_work_jobs SET state=? WHERE id=?', (outcome, job_id))
            if outcome != 'held':
                db.execute('UPDATE hub_work_machines SET job_id=NULL WHERE machine_id=?', (row['machine_id'],))
            self._event(db, 'work.' + outcome, job_id, worker.agent)
            return self._public(db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job_id,)).fetchone())

    def reconcile(self, *, job_id, evidence_id, outcome):
        _key(job_id)
        _key(evidence_id)
        if type(outcome) is not str or outcome not in {'succeeded', 'failed', 'cancelled'}:
            raise HubError('invalid_work_outcome')
        with self.repo._transaction() as db:
            self._expire(db, self._now())
            row = db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row['state'] != 'held':
                raise HubError('work_not_held', 409)
            verified = False
            if self.reconciliation_verifier is not None:
                try:
                    verified = self.reconciliation_verifier(self._public(row), evidence_id, outcome) is True
                except Exception:
                    pass
            if not verified:
                raise HubError('work_reconciliation_required', 403)
            db.execute('UPDATE hub_work_jobs SET state=? WHERE id=?', (outcome, job_id))
            db.execute('UPDATE hub_work_machines SET job_id=NULL WHERE machine_id=? AND job_id=?', (row['machine_id'], job_id))
            self._event(db, 'work.reconciled', job_id, 'system:work-queue')
            return self._public(db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job_id,)).fetchone())

    def set_paused(self, paused):
        if type(paused) is not bool:
            raise HubError('invalid_work_pause')
        with self.repo._transaction() as db:
            current = db.execute('SELECT paused,revision FROM hub_work_policy WHERE id=1').fetchone()
            if bool(current['paused']) != paused:
                db.execute('UPDATE hub_work_policy SET paused=?,revision=revision+1 WHERE id=1', (int(paused),))
                self._emit(db, 'work.paused', 'work-queue-policy', current['revision'] + 1, 'system:work-queue')

    def status(self, *, limit=50):
        self._limit(limit)
        # A status read never mutates expired jobs; expose ambiguity explicitly.
        now = self._now()
        with self.repo._connection() as db:
            rows = db.execute('SELECT * FROM hub_work_jobs ORDER BY sequence DESC LIMIT ?', (limit,)).fetchall()
            queue_paused = bool(db.execute('SELECT paused FROM hub_work_policy WHERE id=1').fetchone()[0])
            master_paused = bool(self.repo._paused(db))
            paused = queue_paused or master_paused
            jobs = []
            for row in rows:
                item = self._public(row)
                reconciliation = row['state'] == 'held' or (row['state'] == 'claimed' and row['lease_until'] <= now)
                reason = 'requires_reconciliation' if reconciliation else None
                if row['state'] == 'queued':
                    action = self.actions.get(row['action_key'])
                    if paused:
                        reason = 'paused'
                    elif action is None or action.machine_id != row['machine_id'] or action.project != row['project'] or action.requires_execution_proof != bool(row['requires_execution_proof']) or row['environment'] not in action.environments:
                        reason = 'registry_changed'
                    elif not self._approved(row):
                        reason = 'production_approval_required'
                    elif not self._target_verified(row['actor'], row['action_key'], row['environment'], WorkTarget(**json.loads(row['target']))):
                        reason = 'target_not_verified'
                    elif db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=? AND job_id IS NOT NULL', (row['machine_id'],)).fetchone():
                        reason = 'machine_reserved'
                item.update(requires_reconciliation=reconciliation,
                            eligible=row['state'] == 'queued' and reason is None,
                            blocked_reason=reason)
                jobs.append(item)
            return {'paused': paused, 'queue_paused': queue_paused, 'master_paused': master_paused,
                    'actions': {key: {'machine_id': action.machine_id, 'project': action.project,
                                     'environments': sorted(action.environments),
                                     'requires_execution_proof': action.requires_execution_proof,
                                     'coordination_only': not action.requires_execution_proof}
                                for key, action in self.actions.items()},
                    'counts': dict(db.execute('SELECT state,COUNT(*) FROM hub_work_jobs GROUP BY state').fetchall()),
                    'jobs': jobs}
