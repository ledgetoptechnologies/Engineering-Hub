"""Explicit private queue-to-remote-pilot composition; not an activated service.

The operator supplies the canonical machine/manifest binding and excludes other
host writers. This module supplies neither that host proof nor production
authority. Hub SQLite remains local to its live PilotOwner. Recovery only GETs
original commands; an uncertain effect is never retried under a fresh identity.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import uuid

from .container_pilot import ContainerPilotWorkflow
from .container_pilot_client import ContainerPilotResponse
from .pilot_recovery import ACTIONS, PilotRecovery
from .repository import AGENTS, HubError
from .startup_reconciliation import StartupReconciliation, StartupReconciliationError
from .work_queue import ClosedClaimPolicy, WorkQueue, WorkTarget, WorkWorker


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


class QueuedContainerPilot:
    """One privately enrolled staging action, driven explicitly one job at a time.

    Construction installs closed, bound queue policies and a durable enrollment
    marker. Existing public/legacy pilot entrypoints subsequently fail closed.
    It does not start an event loop, listener, network call or background worker.
    """
    def __init__(self, queue, workflow, *, action_key, machine_id, reconciliation):
        if (type(queue) is not WorkQueue or type(workflow) is not ContainerPilotWorkflow
                or queue.repo is not workflow.repo or workflow.pilot_owner is None
                or workflow.repo.execution_principal != 'executor:container-pilot'):
            raise HubError('queued_pilot_composition_required', 409)
        workflow._assert_pilot_owner()
        action = queue.actions.get(action_key)
        target = workflow.targets.get('engineering-hub:staging')
        if (action is None or action.machine_id != machine_id
                or action.project != 'engineering-hub' or not action.requires_execution_proof
                or action.environments != frozenset({'staging'}) or target is None
                or len(workflow.targets) != 1):
            raise HubError('queued_pilot_binding_required', 409)
        target.validated_manifest()
        if type(reconciliation) is not StartupReconciliation:
            raise HubError('queued_pilot_reconciliation_required', 409)
        try:
            reconciliation.assert_binding(owner=workflow.pilot_owner, target_id=target.target_id,
                                          manifest_digest=target.manifest_digest)
        except StartupReconciliationError:
            raise HubError('queued_pilot_reconciliation_required', 409) from None
        if (any(getattr(queue, name) is not None for name in
                ('completion_verifier', 'reconciliation_verifier'))
                or queue.claim_verifier is not None and type(queue.claim_verifier) is not ClosedClaimPolicy
                or getattr(queue.repo, '_queued_pilot', None) is not None):
            raise HubError('queued_pilot_policy_conflict', 409)
        self.queue, self.workflow, self.repo = queue, workflow, queue.repo
        self.owner, self.target = workflow.pilot_owner, target
        self.reconciliation = reconciliation
        self.action_key, self.machine_id = action_key, machine_id
        self._action = action
        self._additional_target_verifier = queue.target_verifier
        self.worker = WorkWorker('executor:container-pilot', self.owner.epoch)
        self._permit = ContextVar('queued-pilot-private-permit', default=None)
        self._runtime = None
        # Initialize recovery tables before enrollment closes legacy entrypoints.
        self.recovery = PilotRecovery(workflow)
        with self.repo._transaction() as db:
            # Enrollment is not migration/adoption of an unrelated controller.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN "
                          "('hub_dispatch_cursor','hub_dispatch_receipts','hub_dispatch_owners')").fetchone():
                raise HubError('queued_pilot_legacy_dispatch_present', 409)
            db.execute('''CREATE TABLE IF NOT EXISTS hub_queued_pilot_policy (
                id INTEGER PRIMARY KEY CHECK(id=1), action_key TEXT NOT NULL,
                machine_id TEXT NOT NULL, manifest_digest TEXT NOT NULL, target_id TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS hub_queued_pilot_jobs (
                job_id TEXT PRIMARY KEY, fence INTEGER NOT NULL, worker TEXT NOT NULL,
                epoch TEXT NOT NULL, actor TEXT NOT NULL, task_id TEXT NOT NULL,
                task_revision INTEGER NOT NULL, review TEXT NOT NULL, source_commit TEXT NOT NULL,
                artifact_digest TEXT NOT NULL, manifest_digest TEXT NOT NULL,
                target_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
                run_id TEXT UNIQUE, state TEXT NOT NULL)''')
            expected = (action_key, machine_id, target.manifest_digest, target.target_id)
            old = db.execute('SELECT action_key,machine_id,manifest_digest,target_id FROM hub_queued_pilot_policy WHERE id=1').fetchone()
            if old is not None and tuple(old) != expected:
                raise HubError('queued_pilot_policy_changed', 409)
            for row in db.execute("SELECT data FROM hub_entities WHERE kind='run'"):
                run = json.loads(row['data'])
                if run['status'] not in {'pending', 'running', 'held', 'failed'}:
                    continue
                # An interrupted mapping may have committed the original run
                # request, but may not invent a new request on restart.
                bound = db.execute('SELECT 1 FROM hub_queued_pilot_jobs j '
                    'LEFT JOIN hub_run_requests r ON r.actor=j.actor AND r.key=j.request_id '
                    'WHERE j.run_id=? OR (j.run_id IS NULL AND r.run_id=?)', (run['id'], run['id'])).fetchone()
                if bound is None:
                    raise HubError('queued_pilot_unbound_run_present', 409)
            for reservation in db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=? AND job_id IS NOT NULL', (machine_id,)):
                if db.execute('SELECT 1 FROM hub_queued_pilot_jobs WHERE job_id=?', (reservation['job_id'],)).fetchone() is None:
                    raise HubError('queued_pilot_unbound_reservation_present', 409)
            db.execute('INSERT OR IGNORE INTO hub_queued_pilot_policy VALUES(1,?,?,?,?)', expected)
        self.repo._queued_pilot = self
        queue.target_verifier = self.target_verifier
        queue.claim_verifier = self.claim_verifier
        queue.completion_verifier = self.completion_verifier
        queue.reconciliation_verifier = self.reconciliation_verifier

    def _guard(self):
        self.owner.assert_active(self.repo.path)
        if (self.workflow.repo is not self.repo or self.queue.repo is not self.repo
                or self.workflow.pilot_owner is not self.owner
                or getattr(self.repo, '_queued_pilot', None) is not self):
            raise HubError('queued_pilot_ownership_lost', 409)
        if self.queue.actions.get(self.action_key) != self._action:
            raise HubError('queued_pilot_policy_changed', 409)
        if (self.queue.target_verifier != self.target_verifier
                or self.queue.claim_verifier != self.claim_verifier
                or self.queue.completion_verifier != self.completion_verifier
                or self.queue.reconciliation_verifier != self.reconciliation_verifier):
            raise HubError('queued_pilot_policy_changed', 409)

    def _policy(self, db):
        row = db.execute('SELECT action_key,machine_id,manifest_digest,target_id FROM hub_queued_pilot_policy WHERE id=1').fetchone()
        if row is None or tuple(row) != (self.action_key, self.machine_id, self.target.manifest_digest, self.target.target_id):
            raise HubError('queued_pilot_policy_changed', 409)
        if self.workflow.targets.get(self.target.target_id) != self.target:
            raise HubError('queued_pilot_policy_changed', 409)
        self.target.validated_manifest()

    def _runtime_admission(self):
        """An attached native runtime can only veto existing authority."""
        if self._runtime is None:
            self._reconciliation_admission()
            return
        from .queued_pilot_runtime import QueuedPilotRuntime
        if (type(self._runtime) is not QueuedPilotRuntime
                or self._runtime.bridge is not self
                or self._runtime.admission_allowed() is not True):
            raise HubError('queued_pilot_runtime_unavailable', 409)
        self._reconciliation_admission()

    def _reconciliation_admission(self):
        try:
            if type(self.reconciliation) is not StartupReconciliation:
                raise StartupReconciliationError()
            self.reconciliation.assert_current_permitted(owner=self.owner,
                target_id=self.target.target_id, manifest_digest=self.target.manifest_digest)
        except StartupReconciliationError:
            raise HubError('queued_pilot_reconciliation_required', 409) from None

    def close_admission(self):
        """Close only this session's gate; never cancel effects or release ownership."""
        self.reconciliation.close()

    @property
    def reconciliation_state(self):
        """Local gate lifecycle, not an assertion of remote readiness."""
        if type(self.reconciliation) is not StartupReconciliation:
            return 'unavailable'
        try:
            return self.reconciliation.admission_state
        except StartupReconciliationError:
            return 'unavailable'

    def _review(self, db, actor, target, *, original=None):
        task = self.repo._get(db, target.task_id, 'task')
        review = task.get('review')
        if (actor not in AGENTS or task['owner'] != actor or task['project'] != 'engineering-hub'
                or task['status'] != 'verified' or task['candidate_commit'] != target.source_commit
                or target.source_commit != self.target.candidate_commit
                or target.artifact_digest != self.target.artifact_digest
                or type(review) is not dict or review.get('actor') not in AGENTS
                or review.get('actor') == actor or review.get('commit') != target.source_commit
                or review.get('verdict') != 'approve'
                or original is not None and _canonical(review) != original['review']):
            raise HubError('queued_pilot_review_changed', 409)
        return task

    def target_verifier(self, actor, action_key, environment, target):
        try:
            self._guard()
            self._reconciliation_admission()
            if action_key != self.action_key or environment != 'staging' or type(target) is not WorkTarget:
                return False
            if (self._additional_target_verifier is not None
                    and self._additional_target_verifier(actor, action_key, environment, target) is not True):
                return False
            with self.repo._connection() as db:
                self._policy(db)
                self._review(db, actor, target)
            return True
        except Exception:
            return False

    def claim_verifier(self, worker, action_key, environment, machine_id, target):
        try:
            self._guard()
            self._runtime_admission()
            return (worker == self.worker and action_key == self.action_key
                and environment == 'staging' and machine_id == self.machine_id
                and type(target) is WorkTarget and target.source_commit == self.target.candidate_commit
                and target.artifact_digest == self.target.artifact_digest)
        except Exception:
            return False

    @contextmanager
    def _scope(self, job_id, mode):
        token = self._permit.set((job_id, mode))
        try:
            yield
        finally:
            self._permit.reset(token)

    def _bound(self, db, job_id, *, recovery=False):
        self._guard()
        self._policy(db)
        intent = db.execute('SELECT * FROM hub_queued_pilot_jobs WHERE job_id=?', (job_id,)).fetchone()
        job = db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job_id,)).fetchone()
        if intent is None or job is None:
            raise HubError('queued_pilot_mapping_required', 409)
        target = WorkTarget(**json.loads(job['target']))
        if (job['action_key'] != self.action_key or job['environment'] != 'staging'
                or job['machine_id'] != self.machine_id or job['project'] != 'engineering-hub'
                or not job['requires_execution_proof'] or job['actor'] != intent['actor']
                or job['fence'] != intent['fence'] or job['worker'] != intent['worker']
                or target != WorkTarget(intent['task_id'], intent['source_commit'], intent['artifact_digest'])
                or intent['worker'] != 'executor:container-pilot:' + intent['epoch']
                or intent['manifest_digest'] != self.target.manifest_digest
                or intent['target_id'] != self.target.target_id):
            raise HubError('queued_pilot_binding_changed', 409)
        machine = db.execute('SELECT job_id,generation FROM hub_work_machines WHERE machine_id=?', (self.machine_id,)).fetchone()
        if machine is None or tuple(machine) != (job_id, intent['fence']):
            raise HubError('queued_pilot_reservation_lost', 409)
        if recovery:
            if job['state'] not in {'claimed', 'held'}:
                raise HubError('queued_pilot_recovery_not_required', 409)
            if intent['epoch'] != self.owner.epoch:
                self.owner.assert_prior_epoch(intent['epoch'])
            elif job['state'] == 'claimed' and job['lease_until'] > self.queue._now():
                raise HubError('queued_pilot_still_live', 409)
        else:
            if intent['epoch'] != self.owner.epoch:
                raise HubError('queued_pilot_ownership_lost', 409)
            self.queue._owned(db, job_id, self.worker, intent['fence'], self.queue._now())
        if self.repo._paused(db) or db.execute('SELECT paused FROM hub_work_policy WHERE id=1').fetchone()[0]:
            raise HubError('work_queue_paused', 409)
        self._review(db, intent['actor'], target, original=intent)
        if (self._additional_target_verifier is not None
                and self._additional_target_verifier(intent['actor'], self.action_key, 'staging', target) is not True):
            raise HubError('queued_pilot_review_changed', 409)
        if intent['run_id'] is not None:
            run = self.repo._get(db, intent['run_id'], 'run')
            self._run_binding(db, intent, run)
        return intent, job

    def _run_binding(self, db, intent, run):
        request = db.execute('SELECT run_id FROM hub_run_requests WHERE actor=? AND key=?',
                             (intent['actor'], intent['request_id'])).fetchone()
        if (run['task_id'] != intent['task_id'] or run['created_by'] != intent['actor']
                or run['commit'] != intent['source_commit'] or run['artifact_digest'] != intent['artifact_digest']
                or run['execution_manifest_digest'] != intent['manifest_digest']
                or run['target'] != intent['target_id'] or run['execution_actor'] != 'executor:container-pilot'
                or run['policy_revision'] != self.target.policy_revision or _canonical(run['review']) != intent['review']
                or request is None or request['run_id'] != run['id']):
            raise HubError('queued_pilot_run_changed', 409)
        fence = db.execute('SELECT generation FROM hub_target_fences WHERE target=?', (run['target'],)).fetchone()
        if fence is None or fence[0] != run['fencing_generation']:
            raise HubError('stale_fence', 409)

    def authorize(self, operation, *, db=None, **context):
        """Repository/workflow hook; only our task-local private scope may act."""
        self._guard()
        if operation in {'create_run', 'advance', 'command'}:
            self._runtime_admission()
        permit = self._permit.get()
        if permit is None or operation == 'dispatch':
            raise HubError('queued_pilot_admission_required', 409)
        if db is None:
            with self.repo._connection() as connection:
                return self.authorize(operation, db=connection, **context)
        job_id, mode = permit
        recovery = mode == 'recover'
        if operation in {'create_run', 'advance', 'command'} and recovery:
            raise HubError('queued_pilot_no_effect_replay', 409)
        if operation not in {'create_run', 'advance', 'command', 'inspect', 'recover'}:
            raise HubError('queued_pilot_admission_required', 409)
        intent, job = self._bound(db, job_id, recovery=recovery)
        if operation == 'create_run':
            expected = dict(actor=intent['actor'], task_id=intent['task_id'],
                expected_revision=intent['task_revision'], artifact_digest=intent['artifact_digest'],
                target=intent['target_id'], execution_manifest_digest=intent['manifest_digest'],
                client_request_id=intent['request_id'])
            if context != expected:
                raise HubError('queued_pilot_creation_changed', 409)
        elif intent['run_id'] is None or context.get('run_id') != intent['run_id']:
            raise HubError('queued_pilot_mapping_required', 409)
        if operation in {'inspect', 'recover'} and not recovery:
            raise HubError('queued_pilot_admission_required', 409)
        return True

    def _intent(self, job):
        self.queue.live_claim(job_id=job['id'], worker=self.worker, fence=job['fence'])
        with self.repo._transaction() as db:
            self._guard()
            task = self._review(db, job['actor'], WorkTarget(**job['target']))
            if db.execute('SELECT 1 FROM hub_queued_pilot_jobs WHERE job_id=?', (job['id'],)).fetchone():
                raise HubError('queued_pilot_requires_recovery', 409)
            request = str(uuid.uuid5(uuid.UUID(job['id']), 'queued-container-pilot:' + str(job['fence'])))
            db.execute('INSERT INTO hub_queued_pilot_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (job['id'], job['fence'], self.worker.encoded(), self.owner.epoch, job['actor'],
                 task['id'], task['revision'], _canonical(task['review']), job['target']['source_commit'],
                 job['target']['artifact_digest'], self.target.manifest_digest, self.target.target_id,
                 request, None, 'intent'))
            return dict(db.execute('SELECT * FROM hub_queued_pilot_jobs WHERE job_id=?', (job['id'],)).fetchone())

    def _map(self, intent, run):
        with self.repo._transaction() as db:
            self._guard()
            self._run_binding(db, intent, run)
            current = db.execute('SELECT * FROM hub_queued_pilot_jobs WHERE job_id=?', (intent['job_id'],)).fetchone()
            if current is None or dict(current) != intent or current['run_id'] not in {None, run['id']}:
                raise HubError('queued_pilot_mapping_changed', 409)
            db.execute("UPDATE hub_queued_pilot_jobs SET run_id=?,state='mapped' WHERE job_id=?", (run['id'], intent['job_id']))

    def _evidence_id(self, intent):
        return str(uuid.uuid5(uuid.UUID(intent['job_id']), 'queued-pilot-proof:' + str(intent['fence']) + ':' + str(intent['run_id'])))

    def _proof(self, db, intent):
        run = self.repo._get(db, intent['run_id'], 'run')
        self._run_binding(db, intent, run)
        if run['status'] not in {'succeeded', 'rolled_back'}:
            return None
        # An accepted operation must have unwound before its receipt can release
        # physical capacity. Terminal run labels alone are not health evidence.
        if (db.execute('SELECT 1 FROM hub_container_pilot_advances WHERE run_id=?', (run['id'],)).fetchone()
                or db.execute('SELECT 1 FROM hub_container_pilot_advance_epochs WHERE run_id=?', (run['id'],)).fetchone()):
            return None
        attempt = db.execute('SELECT advance_owner,epoch,state FROM hub_container_pilot_attempts WHERE run_id=?', (run['id'],)).fetchone()
        if (attempt is None or attempt['state'] != 'quiescent' or attempt['epoch'] != intent['epoch']
                or str(uuid.UUID(attempt['advance_owner'])) != attempt['advance_owner']):
            return None
        target = self.workflow._target(run, pin=False)
        rows = PilotRecovery._rows(db, run['id'])
        evidence = []
        for saved in rows:
            if saved['action'] not in ACTIONS or saved['response_json'] is None:
                return None
            command = self.workflow._request(run, target, saved['action'])
            if saved['payload'] != _canonical(command.model_dump()):
                return None
            response = self.workflow._validated_response(ContainerPilotResponse.model_validate_json(saved['response_json'], strict=True))
            state = self.workflow._verified_state(response, command, target)
            if state != saved['state']:
                return None
            evidence.append(dict(action=saved['action'], state=state, container_id=response.evidence.container_id))
        derived = PilotRecovery._derive(evidence)
        if derived != run['status']:
            return None
        return 'succeeded' if derived == 'succeeded' else 'failed'

    def _verify_completion(self, job, evidence_id, outcome, *, recovery):
        try:
            self._guard()
            if self._permit.get() != (job['id'], 'recover' if recovery else 'execute'):
                return False
            with self.repo._connection() as db:
                intent, current = self._bound(db, job['id'], recovery=recovery)
                if job['worker'] != intent['worker'] or job['fence'] != intent['fence']:
                    return False
                return evidence_id == self._evidence_id(intent) and self._proof(db, intent) == outcome
        except Exception:
            return False

    def completion_verifier(self, job, evidence_id, outcome):
        return self._verify_completion(job, evidence_id, outcome, recovery=False)

    def reconciliation_verifier(self, job, evidence_id, outcome):
        try:
            self._reconciliation_admission()
        except HubError:
            return False
        return self._verify_completion(job, evidence_id, outcome, recovery=True)

    def _hold(self, job):
        try:
            self.queue.complete(job_id=job['id'], worker=self.worker, fence=job['fence'], outcome='held')
        except Exception:
            # Owner/lease loss is not permission to mutate or release anything.
            pass
        return self.queue.get_job(job['id'])

    async def run_once(self):
        """Claim one eligible staging job; no worker loop or implicit retry."""
        self._guard()
        self._runtime_admission()
        job = self.queue.claim(worker=self.worker, lease_seconds=600, action_keys=(self.action_key,))
        if job is None:
            return None
        with self._scope(job['id'], 'execute'):
            try:
                intent = self._intent(job)
                run = self.workflow.create_run(intent['actor'], intent['task_id'], intent['task_revision'],
                    intent['artifact_digest'], intent['target_id'], client_request_id=intent['request_id'])
                self._map(intent, run)
                self.queue.live_claim(job_id=job['id'], worker=self.worker, fence=job['fence'])
                await self.workflow.advance(run['id'])
                with self.repo._connection() as db:
                    intent, _ = self._bound(db, job['id'])
                    outcome, evidence_id = self._proof(db, intent), self._evidence_id(intent)
                if outcome is None:
                    return self._hold(job)
                return self.queue.complete(job_id=job['id'], worker=self.worker, fence=job['fence'],
                    outcome=outcome, evidence_id=evidence_id)
            except Exception:
                return self._hold(job)

    async def recover(self, job_id):
        """Inspect the original held attempt; never create/replay a host effect."""
        self._guard()
        with self._scope(job_id, 'recover'):
            with self.repo._connection() as db:
                intent, _ = self._bound(db, job_id, recovery=True)
                intent = dict(intent)
            if intent['run_id'] is None:
                run = self.repo.get_run_request(intent['actor'], intent['request_id'])
                if run is None:
                    return self.queue.get_job(job_id)
                self._map(intent, run)
            else:
                run = self.repo.get_run(intent['run_id'])
            try:
                await self.recovery.recover(run['id'])
            except HubError:
                # A creation-only crash has no accepted advance reservation to
                # inspect. It stays held, never converted into permission to POST.
                return self.queue.get_job(job_id)
            with self.repo._connection() as db:
                intent, _ = self._bound(db, job_id, recovery=True)
                outcome, evidence_id = self._proof(db, intent), self._evidence_id(intent)
            if outcome is None:
                return self.queue.get_job(job_id)
            current = self.queue.get_job(job_id)
            if current['state'] == 'claimed' and current['lease_until'] > self.queue._now():
                # Prior-epoch exclusion permits inspection, not adoption of its
                # live queue lease. Reconciliation waits for the durable hold.
                return current
            # Original-command GETs may retain historical evidence while closed,
            # but they cannot prove current-host exclusion or release capacity.
            try:
                self._reconciliation_admission()
            except HubError:
                return current
            return self.queue.reconcile(job_id=job_id, evidence_id=evidence_id, outcome=outcome)
