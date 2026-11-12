"""Durable, explicitly driven cross-environment admission queue.

This is not an executor or an approval service. Registry entries are supplied by
the operator; no command, endpoint, path or secret is accepted in a job. Existing
staging dispatch remains responsible for its workflow/review checks. An expired
reservation holds the physical machine until authoritative reconciliation.

Production priority is machine-wide and non-preemptive, not narrowed by a
claiming worker or action filter. Valid approved production conservatively
blocks new same-machine staging even when global executor readiness is unknown.
Only a trusted local readiness verifier can establish global unavailability;
agent presence or one worker's denial is not that evidence.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import hashlib
import inspect
import math
import os
import re
import sqlite3
import time
import uuid

from .repository import AGENTS, PROJECTS, HubError


# A fixed private control-plane worker role, never an agent bearer identity.
# Public REST/MCP derive their worker from authenticated AGENTS and cannot select it.
SOFTWARE_VALIDATION_EXECUTOR = 'executor:software-validation'
CONTAINER_PILOT_EXECUTOR = 'executor:container-pilot'
STAGING_SERVICE_EXECUTOR = 'executor:staging-service'


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
        if type(self.project) is not str or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', self.project) is None:
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
    review_revision: int | None = None
    submission_id: str | None = None
    ci_attempt_job_id: str | None = None
    build_receipt_digest: str | None = None
    execution_release_digest: str | None = None

    def __post_init__(self):
        try:
            valid = type(self.task_id) is str and str(uuid.UUID(self.task_id)) == self.task_id
        except ValueError:
            valid = False
        if (not valid or type(self.source_commit) is not str
                or re.fullmatch(r'[a-f0-9]{40}', self.source_commit) is None
                or type(self.artifact_digest) is not str
                or re.fullmatch(r'sha256:[a-f0-9]{64}', self.artifact_digest) is None
                or self.ci_attempt_job_id is not None
                and (type(self.ci_attempt_job_id) is not str
                or re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', self.ci_attempt_job_id) is None)
                or self.build_receipt_digest is not None
                and (type(self.build_receipt_digest) is not str
                or re.fullmatch(r'sha256:[a-f0-9]{64}', self.build_receipt_digest) is None)
                or self.execution_release_digest is not None
                and (type(self.execution_release_digest) is not str
                or re.fullmatch(r'sha256:[a-f0-9]{64}', self.execution_release_digest) is None)
                or self.review_revision is not None
                and (type(self.review_revision) is not int or self.review_revision < 1)
                or self.submission_id is not None and (type(self.submission_id) is not str
                or re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', self.submission_id) is None)
                or (self.review_revision is None) != (self.submission_id is None)):
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
        if (type(self.agent) is not str
                or self.agent not in {*AGENTS, SOFTWARE_VALIDATION_EXECUTOR, CONTAINER_PILOT_EXECUTOR, STAGING_SERVICE_EXECUTOR} or not valid):
            raise HubError('invalid_work_worker')

    def encoded(self):
        return self.agent + ':' + self.instance_id


class ClosedClaimPolicy:
    """Configured coordination without an enrolled executor cannot reserve work."""
    def __call__(self, worker, action_key, environment, machine_id, target):
        return False


@dataclass(frozen=True)
class WorkClaimPlan:
    """Read-only selection snapshot, NOT a reservation or admission capability.

    Even a reconstructed matching plan must pass the queue's actual claim
    callback again. Full raw row and machine generation are rechecked inside
    claim_exact's transaction; a plan never authorizes a different candidate.
    """
    queue: object = field(repr=False)
    queue_identity: str
    process_id: int
    worker: WorkWorker
    action_keys: tuple | None
    job_snapshot: tuple
    machine_generation: int | None

    @property
    def raw_job(self):
        return dict(self.job_snapshot)

    @property
    def job(self):
        return WorkQueue._public(self.raw_job)

    @property
    def job_id(self):
        return self.raw_job['id']

    @property
    def binding_digest(self):
        value = [self.queue_identity, self.process_id, self.worker.encoded(),
                 self.action_keys, self.job_snapshot, self.machine_generation]
        return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class WorkQueue:
    def __init__(self, repo, actions, *, approval_verifier=None,
                 reconciliation_verifier=None, completion_verifier=None, target_verifier=None,
                 claim_verifier=None, production_readiness_verifier=None,
                 clock=time.time, max_jobs=10000):
        if type(actions) is not dict or not 1 <= len(actions) <= 256:
            raise HubError('invalid_work_registry')
        self.actions = dict(actions)
        for key, action in self.actions.items():
            _key(key)
            if type(action) is not WorkAction:
                raise HubError('invalid_work_registry')
            # Retain historical profiles at startup; archival denies new
            # admission/claims below without taking the whole Hub offline.
            repo._project(action.project, require_active=False)
        if type(max_jobs) is not int or not 1 <= max_jobs <= 100000:
            raise HubError('invalid_work_capacity')
        if not callable(clock) or any(v is not None and not callable(v) for v in (approval_verifier, reconciliation_verifier, completion_verifier, target_verifier, claim_verifier, production_readiness_verifier)):
            raise HubError('invalid_work_verifier')
        self.repo, self.clock, self.max_jobs = repo, clock, max_jobs
        self._claim_plan_identity = str(uuid.uuid4())
        self.approval_verifier = approval_verifier
        self.reconciliation_verifier = reconciliation_verifier
        self.completion_verifier = completion_verifier
        self.target_verifier = target_verifier
        # None is retained only for explicit trusted local composition. The
        # environment-enabled factory installs ClosedClaimPolicy by default.
        self.claim_verifier = claim_verifier
        # Trusted local composition only, never a public worker flag. Exact
        # True means globally ready; False proves globally unavailable; anything
        # else is unknown. Unknown intentionally sacrifices staging liveness.
        self.production_readiness_verifier = production_readiness_verifier
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

    def _shared_policy_present(self):
        return (hasattr(self, '_shared_execution_policies')
                or getattr(self, '_shared_execution_required', False) is not False)

    def _shared_policy_valid(self, action_key=None):
        """No legacy None-callback admission after shared composition installs."""
        if not self._shared_policy_present():
            return True
        try:
            from .execution_policy import SharedExecutionPolicies
            policy = getattr(self, '_shared_execution_policies', None)
            if (type(policy) is not SharedExecutionPolicies or policy.queue is not self
                    or policy.repo is not self.repo):
                return False
            policy.assert_integrity()
            return action_key is None or policy.action_bound(action_key) is True
        except Exception:
            return False

    def _shared_approval(self, row):
        """Only an explicit trusted False releases shared production priority."""
        if self.approval_verifier is None or row['approval_id'] is None:
            return None
        try:
            value = self.approval_verifier(row['actor'], row['action_key'], row['environment'],
                row['approval_id'], WorkTarget(**json.loads(row['target'])))
            if type(value) is bool:
                return value
            if inspect.iscoroutine(value) and inspect.getcoroutinestate(value) == inspect.CORO_CREATED:
                value.close()
        except Exception:
            pass
        return None

    def _shared_row_bound(self, row):
        if not self._shared_policy_valid(row['action_key']):
            return False
        action = self.actions.get(row['action_key'])
        return (type(action) is WorkAction and action.machine_id == row['machine_id']
                and action.project == row['project']
                and action.requires_execution_proof == bool(row['requires_execution_proof'])
                and row['environment'] in action.environments)

    def action_project(self, action_key):
        _key(action_key)
        action = self.actions.get(action_key)
        if action is None:
            raise HubError('work_action_not_registered', 403)
        return action.project

    def _target_verified(self, actor, action_key, environment, target):
        if not self._shared_policy_valid(action_key):
            return False
        if not self.repo.task_allows_environment(target.task_id, environment):
            return False
        if self.target_verifier is None:
            return False
        try:
            return self.target_verifier(actor, action_key, environment, target) is True
        except Exception:
            return False

    def _modular_target_in_transaction(self, db, actor, action_key, environment, target):
        """Bind modular source and independent review to this SQLite snapshot."""
        # The retired namespace can contain legacy jobs that never passed this
        # transaction gate. Preserve them, but do not claim or replay them.
        from .staging_action_key import legacy_staging_action
        if legacy_staging_action(action_key, target):
            return False
        if not action_key.startswith('staging-service.'):
            return True
        if (environment != 'staging' or target.submission_id is None or target.review_revision is None
                or target.ci_attempt_job_id is not None or target.build_receipt_digest is not None
                or target.execution_release_digest is not None):
            return False
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_staging_submissions'").fetchone():
            return False
        row = db.execute('SELECT data FROM hub_staging_submissions WHERE id=?', (target.submission_id,)).fetchone()
        if row is None:
            return False
        try:
            item = json.loads(row['data'])
            from .staging_source_binding import UNBOUND_HOLD, valid_source_binding
            if item.get('hold_reason') == UNBOUND_HOLD or not valid_source_binding(item):
                return False
            task = self.repo._get(db, target.task_id, 'task')
            self.repo._no_active_run(db, target.task_id)
            from .staging_action_key import staging_action_key
            key = staging_action_key(item['project'], item['recipe'])
            return (key == action_key and item['actor'] == actor and item['phase'] in ('deposited', 'queued')
                    and item['review_task_id'] == target.task_id
                    and item['review_task_revision'] == target.review_revision
                    and item['source_commit'] == target.source_commit
                    and target.artifact_digest == 'sha256:' + item['source_digest']
                    and task['project'] == item['project']
                    and self.repo.reviewed_task_matches(target.task_id, actor, target.review_revision,
                                                        target.source_commit, db=db))
        except (KeyError, TypeError, ValueError, HubError):
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
        self.repo._project(action.project)
        if type(target) is not WorkTarget:
            raise HubError('invalid_work_target')
        # Capture task state before the external target verifier; taskless
        # trusted coordination actions retain their existing semantics.
        with self.repo._connection() as db:
            before = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='task'", (target.task_id,)).fetchone()
            task_snapshot = before['data'] if before is not None else None
        if not self._target_verified(actor, action_key, environment, target):
            raise HubError('work_target_not_verified', 403)
        target_fields = {name: getattr(target, name) for name in target.__dataclass_fields__ if getattr(target, name) is not None}
        target_json = json.dumps(target_fields, sort_keys=True, separators=(',', ':'))
        fingerprint = json.dumps([action_key, environment, action.machine_id, action.project, action.requires_execution_proof, approval_id, target_json], separators=(',', ':'))
        with self.repo._transaction() as db:
            if not self.repo.task_allows_environment(target.task_id, environment, db=db):
                raise HubError('work_target_not_verified', 403)
            if not self._modular_target_in_transaction(db, actor, action_key, environment, target):
                raise HubError('work_target_not_verified', 403)
            old = db.execute('SELECT * FROM hub_work_jobs WHERE actor=? AND request_id=?', (actor, request_id)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise HubError('work_request_conflict', 409)
                return self._public(old)
            current = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='task'", (target.task_id,)).fetchone()
            if (current['data'] if current is not None else None) != task_snapshot:
                raise HubError('work_review_changed', 409)
            if current is not None:
                task = json.loads(current['data'])
                if not (task.get('project') == action.project and
                        self.repo._reviewed_task_data_matches(task, actor, task.get('revision'),
                                                              target.source_commit, db=db)):
                    raise HubError('work_review_changed', 409)
            if self.repo._open_work_job(db, target.task_id):
                raise HubError('work_task_already_queued', 409)
            if db.execute('SELECT COUNT(*) FROM hub_work_jobs').fetchone()[0] >= self.max_jobs:
                raise HubError('work_queue_capacity', 503)
            job_id = str(uuid.uuid4())
            db.execute('''INSERT INTO hub_work_jobs
                (id,actor,request_id,fingerprint,action_key,environment,machine_id,project,approval_id,target,requires_execution_proof,state,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued',?)''',
                (job_id, actor, request_id, fingerprint, action_key, environment, action.machine_id, action.project, approval_id, target_json, int(action.requires_execution_proof), self._now()))
            self._event(db, 'work.submitted', job_id, actor)
            return self._public(db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job_id,)).fetchone())

    def cancel_unclaimed(self, *, actor, job_id):
        """Cancel an eligible never-claimed queue entry; controller resources are separate."""
        if type(actor) is not str or actor not in AGENTS:
            raise HubError('invalid_work_actor')
        _key(job_id)
        with self.repo._transaction() as db:
            row = db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise HubError('work_job_not_found', 404)
            if row['actor'] != actor:
                raise HubError('forbidden', 403)
            # Check the durable job project inside the cancellation transaction.
            # Legacy agents may clear an archived project's unclaimed job;
            # installation principals are not supported by this queue yet.
            self.repo._project(row['project'], db, require_active=False)
            # Modular source deposits and resource registration straddle queue
            # insertion; only the controller can safely cancel that lifecycle.
            # A durable submission precedes queue admission in both schemas.
            try:
                target = json.loads(row['target'])
                if type(target) is not dict:
                    raise ValueError('invalid target')
                staging_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_staging_submissions'").fetchone()
                controller_owned = (row['action_key'].startswith(('modular.', 'staging-service.'))
                    or row['request_id'].startswith('staging:')
                    or staging_table and db.execute(
                        "SELECT 1 FROM hub_staging_submissions WHERE id IN (?,?) OR json_extract(data,'$.job_id')=? OR (actor=? AND request_id=?) LIMIT 1",
                        (target.get('task_id'), target.get('submission_id'), job_id, actor,
                         row['request_id'][len('staging:'):] if row['request_id'].startswith('staging:') else None)).fetchone())
            except (ValueError, TypeError, sqlite3.DatabaseError):
                raise HubError('work_controller_cancel_required', 409) from None
            if controller_owned:
                raise HubError('work_controller_cancel_required', 409)
            if row['state'] == 'cancelled':
                return self._public(row)
            # A retained pilot intent is authoritative evidence that this job
            # entered an owned lifecycle, even if another row looks unclaimed.
            # A corrupt or partially recovered mapping must never be cancelled.
            pilot_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_queued_pilot_jobs'").fetchone()
            if pilot_table and db.execute('SELECT 1 FROM hub_queued_pilot_jobs WHERE job_id=? LIMIT 1',
                                          (job_id,)).fetchone():
                raise HubError('work_not_unclaimed', 409)
            if (row['state'] != 'queued' or row['revision'] != 1
                    or row['worker'] is not None or row['fence'] is not None
                    or row['lease_until'] is not None
                    or db.execute('SELECT 1 FROM hub_work_machines WHERE job_id=? LIMIT 1', (job_id,)).fetchone()
                    or db.execute("SELECT 1 FROM hub_audit WHERE entity_id=? AND action='work.claimed' LIMIT 1", (job_id,)).fetchone()):
                raise HubError('work_not_unclaimed', 409)
            updated = db.execute("UPDATE hub_work_jobs SET state='cancelled' WHERE id=? AND state='queued' AND revision=1 AND worker IS NULL AND fence IS NULL AND lease_until IS NULL", (job_id,)).rowcount
            if updated != 1:
                raise HubError('work_not_unclaimed', 409)
            self._event(db, 'work.cancelled', job_id, actor)
            return self._public(db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job_id,)).fetchone())

    def _admitted(self, worker, row):
        if not self._shared_policy_valid(row['action_key']):
            return False
        if self.claim_verifier is None:
            return True
        try:
            return self.claim_verifier(worker, row['action_key'], row['environment'], row['machine_id'],
                                       WorkTarget(**json.loads(row['target']))) is True
        except Exception:
            return False

    def _eligible_target(self, row):
        """Current registry, approval and target policy, independent of worker."""
        try:
            if not self._shared_policy_valid(row['action_key']):
                return False
            self.repo._project(row['project'])
            action = self.actions.get(row['action_key'])
            return (type(action) is WorkAction and action.machine_id == row['machine_id']
                and action.project == row['project']
                and action.requires_execution_proof == bool(row['requires_execution_proof'])
                and row['environment'] in action.environments and self._approved(row)
                and self._target_verified(row['actor'], row['action_key'], row['environment'],
                                          WorkTarget(**json.loads(row['target']))))
        except Exception:
            return False

    @staticmethod
    def _production_readiness(row, verifier):
        if verifier is None:
            return None
        try:
            result = verifier(row['actor'], row['action_key'], row['environment'],
                              row['machine_id'], WorkTarget(**json.loads(row['target'])))
            if type(result) is bool:
                return result
            if (inspect.iscoroutine(result)
                    and inspect.getcoroutinestate(result) == inspect.CORO_CREATED):
                result.close()
            # Never cancel/close caller-owned started coroutines, Tasks or
            # Futures. None, errors and every non-Boolean remain unknown.
        except Exception:
            pass
        return None

    def claim(self, *, worker, lease_seconds=60, action_keys=None):
        if type(worker) is not WorkWorker:
            raise HubError('invalid_work_worker')
        self._lease(lease_seconds)
        if action_keys is not None:
            if type(action_keys) is not tuple or not 1 <= len(action_keys) <= 32:
                raise HubError('invalid_work_action_filter')
            for key in action_keys:
                _key(key)
                if key not in self.actions:
                    raise HubError('work_action_not_registered', 403)
            if len(set(action_keys)) != len(action_keys):
                raise HubError('invalid_work_action_filter')
        with self.repo._transaction() as db:
            now = self._now()
            self._expire(db, now)
            if db.execute('SELECT paused FROM hub_work_policy WHERE id=1').fetchone()[0] or self.repo._paused(db):
                return None
            row = self._select_claimable(db, worker, action_keys)
            return None if row is None else self._reserve_claim(db, row, worker, now, lease_seconds)

    def _select_claimable(self, db, worker, action_keys):
        # Entire durable queue is bounded. Approval is rechecked at claim,
        # not cached from submission. Production priority is non-preemptive.
        rows = db.execute("SELECT j.* FROM hub_work_jobs j LEFT JOIN hub_work_machines m ON m.machine_id=j.machine_id WHERE j.state='queued' AND m.job_id IS NULL ORDER BY CASE j.environment WHEN 'production' THEN 0 ELSE 1 END,j.sequence").fetchall()
        # Establish physical-machine precedence before applying the caller's
        # filters or capability. A different enrolled production worker may
        # exist; no fabricated identity or agent-presence inference is used.
        # The SQL order visits production before any staging. Select a
        # claimable production row immediately, without running callbacks
        # for later jobs between its admission check and reservation.
        # Callbacks are point-in-time observations under the SQLite job
        # transaction, not an atomic snapshot of external policy/readiness.
        readiness_verifier = self.production_readiness_verifier
        shared = self._shared_policy_present()
        if shared and not self._shared_policy_valid():
            return None
        production_machines = set()
        for row in rows:
            if row['environment'] == 'production':
                if shared:
                    # A routed target denial is not evidence of global
                    # unavailability. Unbound production retains its machine
                    # barrier even when another family's worker filters it out.
                    bound = self._shared_row_bound(row)
                    approval = self._shared_approval(row) if bound else None
                    if bound and approval is False:
                        continue
                    readiness = (self._production_readiness(row, readiness_verifier)
                                 if bound and approval is True else None)
                else:
                    if not self._eligible_target(row):
                        continue
                    readiness = self._production_readiness(row, readiness_verifier)
                if readiness is not False:
                    production_machines.add(row['machine_id'])
            elif row['machine_id'] in production_machines:
                continue
            if action_keys is not None and row['action_key'] not in action_keys:
                continue
            if row['environment'] == 'production':
                if readiness is False:
                    continue
                if (shared or readiness_verifier is not None) and readiness is not True:
                    continue
                if shared and not self._eligible_target(row):
                    continue
            elif not self._eligible_target(row):
                continue
            if not self._modular_target_in_transaction(db, row['actor'], row['action_key'], row['environment'],
                                                         WorkTarget(**json.loads(row['target']))):
                continue
            if not self._admitted(worker, row):
                continue
            return row
        return None

    def _reserve_claim(self, db, row, worker, now, lease_seconds):
        if not self._modular_target_in_transaction(db, row['actor'], row['action_key'], row['environment'],
                                                   WorkTarget(**json.loads(row['target']))):
            return None
        db.execute('INSERT OR IGNORE INTO hub_work_machines VALUES(?,0,NULL)', (row['machine_id'],))
        db.execute('UPDATE hub_work_machines SET generation=generation+1,job_id=? WHERE machine_id=?', (row['id'], row['machine_id']))
        fence = db.execute('SELECT generation FROM hub_work_machines WHERE machine_id=?', (row['machine_id'],)).fetchone()[0]
        db.execute("UPDATE hub_work_jobs SET state='claimed',worker=?,fence=?,lease_until=? WHERE id=?", (worker.encoded(), fence, now + lease_seconds, row['id']))
        self._event(db, 'work.claimed', row['id'], worker.agent)
        return self._public(db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (row['id'],)).fetchone())

    def _claim_plan_options(self, worker, action_keys):
        if type(worker) is not WorkWorker:
            raise HubError('invalid_work_worker')
        if action_keys is not None:
            if type(action_keys) is not tuple or not 1 <= len(action_keys) <= 32:
                raise HubError('invalid_work_action_filter')
            for key in action_keys:
                _key(key)
                if key not in self.actions:
                    raise HubError('work_action_not_registered', 403)
            if len(set(action_keys)) != len(action_keys):
                raise HubError('invalid_work_action_filter')

    @staticmethod
    def _machine_state(db, machine_id):
        row = db.execute('SELECT generation,job_id FROM hub_work_machines WHERE machine_id=?', (machine_id,)).fetchone()
        if row is None:
            return None, None
        return row['generation'], row['job_id']

    def plan_claim(self, *, worker, action_keys=None):
        """Read exact metadata selection only: no expire, reservation or writes.

        Verifiers retain their existing policy. The trusted pilot caller supplies
        its own private metadata-preview scope; there is no queue bypass flag.
        """
        self._claim_plan_options(worker, action_keys)
        with self.repo._connection() as db:
            if db.execute('SELECT paused FROM hub_work_policy WHERE id=1').fetchone()[0] or self.repo._paused(db):
                return None
            row = self._select_claimable(db, worker, action_keys)
            if row is None:
                return None
            generation, reserved = self._machine_state(db, row['machine_id'])
            if reserved is not None:
                return None  # Another selector won this read-only race.
            return WorkClaimPlan(self, self._claim_plan_identity, os.getpid(), worker, action_keys,
                tuple(sorted(dict(row).items())), generation)

    def claim_exact(self, plan, *, worker, lease_seconds=60):
        """Revalidate the SAME selection and callbacks; mismatch claims nothing.

        A plan is public metadata, not proof of a remote observation. Any
        configured actual claim verifier must authorize this transaction again.
        """
        self._lease(lease_seconds)
        if (type(plan) is not WorkClaimPlan or plan.queue is not self
                or type(plan.queue_identity) is not str or plan.queue_identity != self._claim_plan_identity
                or type(plan.process_id) is not int or plan.process_id != os.getpid()
                or type(plan.worker) is not WorkWorker or type(worker) is not WorkWorker
                or plan.worker != worker or type(plan.job_snapshot) is not tuple
                or plan.machine_generation is not None and (
                    type(plan.machine_generation) is not int or plan.machine_generation < 0)):
            raise HubError('invalid_work_claim_plan', 409)
        self._claim_plan_options(worker, plan.action_keys)
        with self.repo._transaction() as db:
            now = self._now()
            self._expire(db, now)
            if db.execute('SELECT paused FROM hub_work_policy WHERE id=1').fetchone()[0] or self.repo._paused(db):
                return None
            row = self._select_claimable(db, worker, plan.action_keys)
            if row is None:
                return None
            current = tuple(sorted(dict(row).items()))
            if (current != plan.job_snapshot or any(type(value) is not type(saved)
                    for (_, value), (_, saved) in zip(current, plan.job_snapshot))):
                return None
            generation, reserved = self._machine_state(db, row['machine_id'])
            if reserved is not None or generation != plan.machine_generation:
                return None
            # Snapshot/precedence reads may consume a prepared pilot scope's
            # deadline. Recheck its local claim veto at reservation time.
            if not self._admitted(worker, row):
                return None
            return self._reserve_claim(db, row, worker, now, lease_seconds)

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
                    or not self._modular_target_in_transaction(db, row['actor'], row['action_key'],
                        row['environment'], WorkTarget(**json.loads(row['target'])))
                    or not self._admitted(worker, row)
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
            if outcome != 'held' and not self._shared_policy_valid(row['action_key']):
                raise HubError('work_completion_proof_required', 409)
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
            if not self._shared_policy_valid(row['action_key']):
                raise HubError('work_reconciliation_required', 403)
            verified = False
            if self.reconciliation_verifier is not None:
                try:
                    verified = self.reconciliation_verifier(self._public(row), evidence_id, outcome) is True
                except Exception:
                    pass
            if not verified:
                raise HubError('work_reconciliation_required', 403)
            updated = db.execute(
                "UPDATE hub_work_jobs SET state=? WHERE id=? AND state='held' AND revision=? "
                'AND machine_id=? AND worker IS ? AND fence IS ?',
                (outcome, job_id, row['revision'], row['machine_id'], row['worker'], row['fence'])).rowcount
            released = db.execute(
                'UPDATE hub_work_machines SET job_id=NULL WHERE machine_id=? AND job_id=? AND generation=?',
                (row['machine_id'], job_id, row['fence'])).rowcount
            if updated != 1 or released != 1:
                raise HubError('work_reservation_changed', 409)
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

    def get_job(self, job_id):
        """Fetch one durable job without claiming it or replaying an effect."""
        _key(job_id)
        with self.repo._connection() as db:
            row = db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise HubError('work_job_not_found', 404)
            result = self._public(row)
        result['requires_reconciliation'] = (row['state'] == 'held'
            or row['state'] == 'claimed' and row['lease_until'] <= self._now())
        return result

    def status(self, *, limit=50):
        self._limit(limit)
        # A status read never mutates expired jobs; expose ambiguity explicitly.
        now = self._now()
        with self.repo._connection() as db:
            # Keep the rows, totals and pause flags in one read snapshot. End
            # it before calling external target verification below.
            db.execute('BEGIN')
            # Read each visible job and the machine reservation in one SQLite
            # statement so a concurrent release/reclaim cannot splice snapshots.
            rows = db.execute('SELECT j.*, m.job_id AS _reservation_holder '
                              'FROM hub_work_jobs AS j LEFT JOIN hub_work_machines AS m '
                              'ON m.machine_id=j.machine_id ORDER BY j.sequence DESC LIMIT ?', (limit,)).fetchall()
            visible = {row['id']: row for row in rows}
            queue_paused = bool(db.execute('SELECT paused FROM hub_work_policy WHERE id=1').fetchone()[0])
            master_paused = bool(self.repo._paused(db))
            counts = dict(db.execute('SELECT state,COUNT(*) FROM hub_work_jobs GROUP BY state').fetchall())
            db.commit()
            paused = queue_paused or master_paused
            jobs = []
            for row in rows:
                item = self._public(row)
                holder_id = item.pop('_reservation_holder')
                visible_holder = visible.get(holder_id)
                disclosed_holder = None
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
                    elif type(self.claim_verifier) is ClosedClaimPolicy:
                        reason = 'admission_not_configured'
                    elif not self._target_verified(row['actor'], row['action_key'], row['environment'], WorkTarget(**json.loads(row['target']))):
                        reason = 'target_not_verified'
                    elif holder_id is not None:
                        reason = 'machine_reserved'
                        # Never add an identity outside the existing result scope.
                        if (visible_holder is not None and visible_holder['machine_id'] == row['machine_id']
                                and visible_holder['state'] in ('claimed', 'held')):
                            disclosed_holder = holder_id
                item.update(requires_reconciliation=reconciliation,
                            eligible=row['state'] == 'queued' and reason is None,
                            eligible_requires_worker_admission=self.claim_verifier is not None and type(self.claim_verifier) is not ClosedClaimPolicy,
                            blocked_reason=reason, reservation_holder_job_id=disclosed_holder)
                jobs.append(item)
            return {'paused': paused, 'queue_paused': queue_paused, 'master_paused': master_paused,
                    'claim_admission_state': ('unconfigured' if type(self.claim_verifier) is ClosedClaimPolicy
                                              else 'trusted_composition' if self.claim_verifier is None
                                              else 'worker_policy'),
                    'actions': {key: {'machine_id': action.machine_id, 'project': action.project,
                                     'environments': sorted(action.environments),
                                     'requires_execution_proof': action.requires_execution_proof,
                                     'coordination_only': not action.requires_execution_proof}
                                for key, action in self.actions.items()},
                    'counts': counts,
                    'jobs': jobs}
