"""Explicit canonical-queue adapter for the separately deployed staging worker.

Only this controller's registered actions use its verifiers. Existing actions,
production priority, machine reservations and proofs retain their verifiers.
Dispatch intent is durable before an HTTP effect; uncertainty is never retried.
"""
import asyncio
from contextlib import asynccontextmanager
import base64
import hashlib
import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

from .repository import AGENTS, HubError
from .staging_resources import KINDS
from .staging_client import _FILE_TYPES, StagingNotAccepted
from .work_queue import WorkAction, WorkQueue, WorkTarget, WorkWorker, STAGING_SERVICE_EXECUTOR

_PROOF_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='staging-proof')
_PROOF_SLOT = BoundedSemaphore(1)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _require(value, code='invalid_staging_request', status=400):
    if not value:
        raise HubError(code, status)


def _id(value):
    try:
        _require(type(value) is str and str(uuid.UUID(value)) == value)
    except (ValueError, TypeError):
        raise HubError('invalid_staging_request') from None
    return value


def compose_queue(repo, existing_queue, projects, machine_id):
    """Register configured project/recipe actions on the SAME physical queue."""
    actions, mapping = {}, {}
    for project, recipes in projects.items():
        repo._project(project)
        for recipe in recipes:
            _require(type(recipe) is str and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', recipe))
            key = 'staging-service.' + hashlib.sha256(_json([project, recipe]).encode()).hexdigest()[:24]
            mapping[(project, recipe)] = key
            actions[key] = WorkAction(machine_id, project=project)
    _require(bool(actions), 'invalid_staging_configuration')
    if existing_queue is None:
        existing_queue = WorkQueue(repo, actions)
    else:
        _require(existing_queue.repo is repo, 'invalid_staging_configuration')
        for key, action in actions.items():
            _require(key not in existing_queue.actions or existing_queue.actions[key] == action,
                     'staging_action_conflict', 409)
        existing_queue.actions.update(actions)
    return existing_queue, mapping


class StagingController:
    def __init__(self, repo, queue, client, *, resources, control_principal,
                 actions, worker=None, poll_interval=1, max_poll_seconds=2700,
                 execution_policies=None):
        _require(queue.repo is repo and resources.repo is repo and resources._control is control_principal,
                 'invalid_staging_configuration')
        _require(type(actions) is dict and bool(actions) and 0 < poll_interval <= 30 and
                 0 < max_poll_seconds <= 2700, 'invalid_staging_configuration')
        self.repo, self.queue, self.client, self.resources = repo, queue, client, resources
        self.principal, self.actions = control_principal, dict(actions)
        self.worker = worker or WorkWorker(STAGING_SERVICE_EXECUTOR, str(uuid.uuid4()))
        _require(self.worker.agent == STAGING_SERVICE_EXECUTOR, 'invalid_staging_worker')
        self.execution_policies = execution_policies
        self._execution_policies = execution_policies
        self._policy_binding = None
        self._policy_actions = dict(self.actions)
        self._policy_context = (repo, queue, client, resources, control_principal, self.worker)
        if execution_policies is not None:
            from .execution_policy import SharedExecutionPolicies
            _require(type(execution_policies) is SharedExecutionPolicies and
                     execution_policies.queue is queue and execution_policies.repo is repo,
                     'invalid_staging_configuration')
        self.poll_interval, self.max_poll_seconds = poll_interval, max_poll_seconds
        self.lock, self.wake = asyncio.Lock(), asyncio.Event()
        self.background, self.stopping = None, False
        self.inflight = set()
        self.recheck_backoff = {}
        for (project, recipe), key in self.actions.items():
            repo._project(project)
            _require(key in queue.actions and queue.actions[key].project == project and
                     queue.actions[key].environments == frozenset({'staging'}) and
                     queue.actions[key].requires_execution_proof, 'invalid_staging_action')
        with repo._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS hub_staging_submissions (id TEXT PRIMARY KEY,actor TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,data TEXT NOT NULL,UNIQUE(actor,request_id))')
            db.execute("CREATE INDEX IF NOT EXISTS hub_staging_submission_job ON hub_staging_submissions(json_extract(data,'$.job_id'))")
            db.execute('CREATE TABLE IF NOT EXISTS hub_staging_dispatch_cleanup (id TEXT PRIMARY KEY,data TEXT NOT NULL)')
        self._install_verifiers()

    def _shared_guard(self, *, admission=False):
        if self._execution_policies is not None:
            _require(self.execution_policies is self._execution_policies and
                     self.actions == self._policy_actions and
                     all(current is original for current, original in zip(
                         (self.repo, self.queue, self.client, self.resources, self.principal, self.worker),
                         self._policy_context)), 'staging_policy_changed', 409)
            self._execution_policies.assert_binding('modular', self._policy_binding, admission=admission)

    def _read(self, identity=None, *, job_id=None):
        with self.repo._connection() as db:
            if identity is not None:
                row = db.execute('SELECT data FROM hub_staging_submissions WHERE id=?', (identity,)).fetchone()
                if row:
                    return json.loads(row[0])
            else:
                row = db.execute("SELECT data FROM hub_staging_submissions WHERE json_extract(data,'$.job_id')=?", (job_id,)).fetchone()
                if row:
                    return json.loads(row[0])
        raise HubError('staging_job_not_found', 404)

    def _save(self, item):
        with self.repo._transaction() as db:
            db.execute('UPDATE hub_staging_submissions SET data=? WHERE id=?', (_json(item), item['id']))

    @asynccontextmanager
    async def _short_lock(self):
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=0.25)
        except TimeoutError:
            raise HubError('staging_busy', 409) from None
        try:
            yield
        finally:
            self.lock.release()

    def _grants(self, actor, project):
        self.resources.authorize(actor, project, 'build')
        self.resources.authorize(actor, project, 'test')

    def _install_verifiers(self):
        queue = self.queue
        old_target, old_claim = queue.target_verifier, queue.claim_verifier
        old_complete, old_reconcile = queue.completion_verifier, queue.reconciliation_verifier
        owned = set(self.actions.values())

        def target(actor, action, environment, value):
            if action not in owned:
                return old_target(actor, action, environment, value) if old_target else False
            try:
                self._shared_guard(admission=True)
                item = self._read(value.submission_id)
                self._grants(item['actor'], item['project'])
                return (actor == item['actor'] and environment == 'staging' and item['phase'] != 'depositing'
                        and action == self.actions[(item['project'], item['recipe'])]
                        and value.task_id == item['review_task_id']
                        and value.review_revision == item['review_task_revision']
                        and self.repo.reviewed_task_matches(value.task_id, actor, value.review_revision, value.source_commit)
                        and value.source_commit == item['source_commit']
                        and value.artifact_digest == 'sha256:' + item['source_digest'])
            except Exception:
                return False

        def claim(worker, action, environment, machine, value):
            if action not in owned:
                return old_claim(worker, action, environment, machine, value) if old_claim else True
            self._shared_guard(admission=True)
            if self._unknown_production(machine, old_target):
                return False
            item = self._read(value.submission_id)
            return (worker == self.worker and item['phase'] == 'queued'
                    and set(item['resource_ids']) == KINDS
                    and target(item['actor'], action, environment, value))

        def proof(job, evidence_id, outcome):
            if job['action_key'] not in owned:
                return old_complete(job, evidence_id, outcome) if old_complete else False
            try:
                self._shared_guard()
                item = self._read(job_id=job['id'])
                return item['phase'] == 'verified' and item['evidence_id'] == evidence_id and item['outcome'] == outcome
            except Exception:
                return False

        def reconciled(job, evidence_id, outcome):
            if job['action_key'] not in owned:
                return old_reconcile(job, evidence_id, outcome) if old_reconcile else False
            return proof(job, evidence_id, outcome)
        if self.execution_policies is None:
            queue.target_verifier, queue.claim_verifier = target, claim
            queue.completion_verifier, queue.reconciliation_verifier = proof, reconciled
        else:
            self._policy_binding = self.execution_policies.bind('modular',
                {key: queue.actions[key] for key in owned}, worker=self.worker,
                target=target, claim=claim, completion=proof, reconciliation=reconciled)

    def _unknown_production(self, machine, previous_target):
        # Called from claim admission as well as preflight. Unknown persisted
        # production cannot disappear merely because a registry was reloaded.
        with self.repo._connection() as db:
            rows = db.execute("SELECT action_key FROM hub_work_jobs WHERE state='queued' AND environment='production' AND machine_id=? AND approval_id IS NOT NULL", (machine,)).fetchall()
        return any(row['action_key'] not in self.queue.actions or
                   row['action_key'] not in self.actions.values() and previous_target is None for row in rows)

    async def submit(self, actor, payload):
        self._shared_guard(admission=True)
        _require(actor in AGENTS, 'staging_agent_required', 403)
        fields = {'request_id', 'review_task_id', 'review_task_revision', 'project', 'recipe',
                  'source_commit', 'source_digest', 'source_zip_base64', 'source_commit_object_base64'}
        _require(type(payload) is dict and set(payload) in (fields, fields | {'topic'}))
        _id(payload['request_id'])
        _id(payload['review_task_id'])
        _require(type(payload['review_task_revision']) is int and payload['review_task_revision'] >= 1,
                 'staging_review_required', 409)
        project, recipe = payload['project'], payload['recipe']
        _require((project, recipe) in self.actions, 'staging_profile_not_registered', 403)
        self._grants(actor, project)
        _require(type(payload['source_commit']) is str and re.fullmatch('[a-f0-9]{40}', payload['source_commit']))
        _require(type(payload['source_digest']) is str and re.fullmatch('[a-f0-9]{64}', payload['source_digest']))
        encoded = payload['source_zip_base64']
        _require(type(encoded) is str and len(encoded) <= 11184812)
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise HubError('invalid_staging_source') from None
        _require(len(raw) <= 8 * 1024**2 and hashlib.sha256(raw).hexdigest() == payload['source_digest'], 'staging_source_digest_mismatch')
        _require(type(payload['source_commit_object_base64']) is str
                 and 0 < len(payload['source_commit_object_base64']) <= 87384, 'staging_source_binding_invalid')
        with self.repo._connection() as db:
            prior = db.execute('SELECT data FROM hub_staging_submissions WHERE actor=? AND request_id=?',
                               (actor, payload['request_id'])).fetchone()
        if prior is not None:
            previous = json.loads(prior['data'])
            _require(all(previous.get(key) == payload.get(key) for key in (
                'review_task_id', 'review_task_revision', 'project', 'recipe', 'source_commit', 'source_digest'))
                and previous.get('topic') == payload.get('topic'), 'staging_request_conflict', 409)
        _require(_PROOF_SLOT.acquire(blocking=False), 'staging_source_verification_busy', 503)
        from hub_staging_service.source_provenance import verify
        try:
            future = _PROOF_EXECUTOR.submit(verify, raw, payload['source_commit_object_base64'], payload['source_commit'])
        except BaseException:
            _PROOF_SLOT.release()
            raise
        future.add_done_callback(lambda _: _PROOF_SLOT.release())
        try:
            binding = await asyncio.shield(asyncio.wrap_future(future))
        except ValueError:
            raise HubError('staging_source_binding_invalid') from None
        topic = payload.get('topic')
        _require(topic is None or type(topic) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', topic))
        public = {key: value for key, value in payload.items()
                  if key not in {'source_zip_base64', 'source_commit_object_base64'}}
        public['source_binding'] = binding
        public['topic'] = topic  # Omitted and explicit null have one request identity.
        fingerprint = hashlib.sha256(_json(public).encode()).hexdigest()
        async with self._short_lock():
            with self.repo._transaction() as db:
                _require(self.repo.reviewed_task_matches(payload['review_task_id'], actor,
                    payload['review_task_revision'], payload['source_commit'], db=db)
                    and self.repo._get(db, payload['review_task_id'], 'task')['project'] == project,
                    'staging_review_required', 409)
                old = db.execute('SELECT * FROM hub_staging_submissions WHERE actor=? AND request_id=?', (actor, payload['request_id'])).fetchone()
                if old:
                    _require(old['fingerprint'] == fingerprint, 'staging_request_conflict', 409)
                    item = json.loads(old['data'])
                else:
                    item = dict(public, id=str(uuid.uuid4()), actor=actor, source_id=str(uuid.uuid4()),
                                phase='depositing', job_id=None, resource_ids={}, command_id=str(uuid.uuid4()), topic=topic)
                    db.execute('INSERT INTO hub_staging_submissions VALUES(?,?,?,?,?)',
                               (item['id'], actor, payload['request_id'], fingerprint, _json(item)))
            if item['phase'] == 'depositing':
                self._shared_guard(admission=True)
                receipt = await self.client.put_source({'source_id': item['source_id'], 'project': project,
                    'source_digest': item['source_digest'], 'source_zip_base64': encoded,
                    'source_commit': item['source_commit'],
                    'source_commit_object_base64': payload['source_commit_object_base64']})
                self._shared_guard()
                _require(all(receipt.get(key) == item[key] for key in ('source_id', 'source_digest', 'project')),
                         'staging_source_receipt_mismatch', 502)
                _require(receipt.get('source_binding') == binding, 'staging_source_binding_receipt_mismatch', 502)
                item['phase'] = 'deposited'; self._save(item)
            if item['job_id'] is None:
                self._shared_guard(admission=True)
                target = WorkTarget(item['review_task_id'], item['source_commit'], 'sha256:' + item['source_digest'],
                                    item['review_task_revision'], item['id'])
                job = self.queue.submit(actor=actor, request_id='staging:' + item['request_id'],
                    action_key=self.actions[(project, recipe)], environment='staging', target=target)
                item['job_id'] = job['id']; self._save(item)
            self.resources.bind_job(self.principal, item['job_id'], project=project, created_by=actor,
                                    source_commit=item['source_commit'], artifact_digest=None)
            # Recover a registration interrupted before submission-journal update.
            with self.repo._connection() as db:
                registered = [json.loads(row[0]) for row in db.execute("SELECT data FROM hub_entities WHERE kind='staging_resource'")]
            for kind in sorted(KINDS):
                if kind not in item['resource_ids']:
                    old = next((resource for resource in registered if resource['job_id'] == item['job_id'] and resource['kind'] == kind), None)
                    resource = old or self.resources.register_resource(self.principal, item['job_id'], kind, topic=topic)
                    item['resource_ids'][kind] = resource['id']; self._save(item)
            if item['phase'] == 'deposited':
                item['phase'] = 'queued'; self._save(item)
            self.wake.set()
            return self.get_job(actor, item['job_id'])

    def get_job(self, actor, identity):
        item = self._read(job_id=_id(identity))
        self.resources.authorize(actor, item['project'], 'read')
        return self._view(item)

    def _view(self, item):
        result = {'job': self.queue.get_job(item['job_id']), 'source_id': item['source_id'],
                  'resource_ids': item['resource_ids'], 'phase': item['phase']}
        if 'source_binding' in item:
            result['source_binding'] = item['source_binding']
            result['source_commit_verification'] = 'git-sha1-tree-v1'
        return result

    async def runtime(self, actor):
        self.repo._actor(actor, human=True)
        _require(actor in self.resources.admin_actors, 'staging_admin_required', 403)
        try:
            snapshot = await self.client.runtime()
        except Exception:
            return {'configured': True, 'state': 'unavailable', 'snapshot': None}
        return {'configured': True, 'state': 'ready', 'snapshot': snapshot}

    async def status(self, actor=None):
        result = {'configured': True, 'running': self.background is not None and not self.background.done(),
                  'reachable': False, 'profiles': {}, 'held_jobs': [],
                  'can_reconcile': actor in self.resources.admin_actors}
        try:
            remote = await self.client.status()
            result['reachable'] = True
        except Exception:
            remote = {'projects': {}}
        if actor is not None:
            for project in {project for project, _ in self.actions}:
                try:
                    self.resources.authorize(actor, project, 'read')
                except HubError:
                    continue
                result['profiles'][project] = [recipe for recipe in remote.get('projects', {}).get(project, [])
                                               if (project, recipe) in self.actions]
            with self.repo._connection() as db:
                projects = tuple(result['profiles'])
                entries = [] if not projects else [json.loads(row[0]) for row in db.execute(
                    "SELECT s.data FROM hub_staging_submissions s JOIN hub_work_jobs j ON j.id=json_extract(s.data,'$.job_id') "
                    "WHERE j.state IN ('claimed','held') AND j.project IN (" + ','.join('?' for _ in projects) + ') ORDER BY j.sequence LIMIT 50', projects)]
            for item in entries:
                if item['project'] not in result['profiles'] or item.get('job_id') is None:
                    continue
                job = self.queue.get_job(item['job_id'])
                if job['state'] not in ('claimed', 'held'):
                    continue
                if (job['state'] == 'claimed' and not job.get('requires_reconciliation')
                        and item['phase'] != 'held' and job.get('worker') == self.worker.encoded()):
                    continue  # Normal in-flight work is not a held outcome.
                result['held_jobs'].append({'id': job['id'], 'project': item['project'],
                    'recipe': item['recipe'], 'state': job['state'], 'phase': item['phase'],
                    'source_commit': item['source_commit']})
                if len(result['held_jobs']) == 50:
                    break
        return result

    async def reconcile_for(self, actor, identity):
        self.repo._actor(actor, human=True)
        _require(actor in self.resources.admin_actors, 'staging_admin_required', 403)
        item = self._read(job_id=_id(identity))
        self.resources.authorize(actor, item['project'], 'read')
        return await self.reconcile(identity)

    def _finish_no_effect(self, item, claim, proof, reason):
        """Local intent or authenticated rejection proves THIS command did not run."""
        self._shared_guard()
        item.update(phase='verified', outcome='failed', dispatch_proof=proof,
                    rejection_reason=reason, evidence_id=item.get('evidence_id', str(uuid.uuid4())))
        if claim is not None:
            item['claim'] = claim
        self._save(item)
        self.resources.abandon_not_created(self.principal, item['job_id'])
        current = self.queue.get_job(item['job_id'])
        if current['state'] == 'failed':
            return self._view(item)
        if current.get('requires_reconciliation') or current['state'] == 'held':
            self.queue.reconcile(job_id=item['job_id'], evidence_id=item['evidence_id'], outcome='failed')
        else:
            bound = claim or item.get('claim')
            _require(bound is not None, 'staging_reconciliation_pending', 409)
            self.queue.complete(job_id=item['job_id'], worker=self.worker, fence=bound['fence'],
                                outcome='failed', evidence_id=item['evidence_id'])
        return self._view(item)

    def _hold(self, item, claim=None):
        self._shared_guard()
        if item['phase'] != 'held':
            item['phase'] = 'held'; self._save(item)
        job = self.resources.inspect_job(self.principal, item['job_id'])
        protected = list(job['protected'])
        hold = {'reason': 'held_campaign', 'ref': item['job_id']}
        if hold not in protected:
            protected.append(hold)
        if protected != job['protected']:
            self.resources.update_job(self.principal, item['job_id'], job['revision'], terminal=job['terminal'],
                protected=protected)
        if claim and self.queue.get_job(item['job_id'])['state'] != 'held':
            try:
                self.queue.complete(job_id=item['job_id'], worker=self.worker, fence=claim['fence'], outcome='held')
            except HubError:
                pass  # Expired claim already keeps the physical machine reserved.

    async def run_once(self):
        _require(not self.stopping, 'staging_controller_closing', 503)
        # Shield a tracked execution from an HTTP caller's cancellation. close()
        # drains this same task; cancellation never starts a replacement POST.
        task = asyncio.create_task(self._run_once())
        self.inflight.add(task)
        def finished(done):
            self.inflight.discard(done)
            if not done.cancelled():
                done.exception()  # Retrieve detached failures; awaiting still raises them.
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _run_once(self):
        async with self.lock:
            self._shared_guard(admission=True)
            claim = self.queue.claim(worker=self.worker, action_keys=tuple(sorted(self.actions.values())), lease_seconds=600)
            if claim is None:
                return None
            item = self._read(job_id=claim['id'])
            try:
                _require(item['phase'] == 'queued', 'staging_dispatch_already_attempted', 409)
                self._grants(item['actor'], item['project'])
                self.queue.live_claim(job_id=claim['id'], worker=self.worker, fence=claim['fence'])
                self._shared_guard(admission=True)
                with self.repo._transaction() as db:
                    row = self.queue._owned(db, claim['id'], self.worker, claim['fence'], self.queue._now())
                    _require(not self.repo._paused(db) and not db.execute(
                        'SELECT paused FROM hub_work_policy WHERE id=1').fetchone()[0], 'staging_admission_changed', 409)
                    _require(self.queue._modular_target_in_transaction(db, row['actor'], row['action_key'],
                        row['environment'], WorkTarget(**json.loads(row['target']))), 'staging_review_required', 409)
                    current = db.execute('SELECT data FROM hub_staging_submissions WHERE id=?', (item['id'],)).fetchone()
                    _require(current is not None and json.loads(current['data']) == item and item['phase'] == 'queued',
                             'staging_admission_changed', 409)
                    self.resources._authorize(db, item['actor'], item['project'], 'build')
                    self.resources._authorize(db, item['actor'], item['project'], 'test')
                    item['phase'], item['claim'] = 'dispatching', claim
                    db.execute('UPDATE hub_staging_submissions SET data=? WHERE id=?', (_json(item), item['id']))
                receipt = await self.client.submit({'job_id': item['job_id'], 'command_id': item['command_id'],
                    'idempotency_key': item['command_id'], 'project': item['project'], 'recipe': item['recipe'],
                    'source_commit': item['source_commit'], 'source_digest': item['source_digest'],
                    'source_id': item['source_id'], 'resource_ids': item['resource_ids'],
                    'require_source_binding': True, 'expected_source_binding': item['source_binding']})
                self._shared_guard()
                item['phase'] = 'accepted'; self._save(item)
                return await self._follow(item, receipt, claim)
            except Exception as error:
                if item['phase'] in ('queued', 'deposited'):
                    return self._finish_no_effect(item, claim, 'never_sent', 'admission_changed')
                if isinstance(error, StagingNotAccepted) and error.reason not in ('worker_busy', 'worker_busy_or_held'):
                    return self._finish_no_effect(item, claim, 'definitively_rejected', error.reason)
                if isinstance(error, StagingNotAccepted):
                    # A different busy/held operation is not proof that the
                    # physical machine is idle. Preserve its conservative hold.
                    item['rejection_reason'] = error.reason
                self._hold(item, claim)
                return self._view(item)

    async def _follow(self, item, receipt, claim):
        deadline = time.monotonic() + self.max_poll_seconds
        while True:
            self._shared_guard()
            _require(receipt.get('job_id') == item['job_id'] and receipt.get('command_id') == item['command_id'],
                     'staging_worker_identity_mismatch', 502)
            if receipt['state'] in ('succeeded', 'failed'):
                return await self._finish(item, receipt, claim)
            _require(receipt['state'] in ('accepted', 'running') and time.monotonic() < deadline,
                     'staging_worker_held', 409)
            if claim and self.queue.get_job(item['job_id'])['state'] == 'claimed':
                self.queue.renew(job_id=item['job_id'], worker=self.worker, fence=claim['fence'], lease_seconds=600)
            await asyncio.sleep(self.poll_interval)
            receipt = await self.client.job(item['job_id'])
            self._shared_guard()

    async def _finish(self, item, receipt, claim):
        self._shared_guard()
        outcome = receipt['state']
        result = receipt['result']
        _require(result.get('source_commit') == item['source_commit']
                 and result.get('source_digest') == item['source_digest']
                 and result.get('source_commit_verification') == 'git-sha1-tree-v1'
                 and result.get('source_binding') == item['source_binding'],
                 'staging_completion_proof_invalid', 502)
        if outcome == 'succeeded':
            _require(all(result.get(key) == item[key] for key in ('source_commit', 'source_digest', 'resource_ids'))
                     and result.get('source_commit_verification') == 'git-sha1-tree-v1'
                     and result.get('source_binding') == item['source_binding'] and result.get('preview_ready') is True
                     and set(result.get('stages', {})) == {'build', 'test'}
                     and all(stage.get('exit_code') == 0 for stage in result['stages'].values()),
                     'staging_completion_proof_invalid', 502)
        metadata = {}
        for kind, identity in item['resource_ids'].items():
            if outcome == 'succeeded':
                fact = await self.client.resource(identity)
                self._shared_guard()
                _require(fact.get('id') == identity and fact.get('kind') == kind and
                         fact.get('job_id') == item['job_id'] and fact.get('state') == 'ready',
                         'staging_resource_proof_invalid', 502)
                metadata[kind] = fact
        if outcome == 'failed' and 'diagnostic' in receipt['result']:
            diagnostic = receipt['result']['diagnostic']
            identity = item['resource_ids']['test_run']
            _require(type(diagnostic) is dict and set(diagnostic) ==
                     {'resource_id', 'manifest_sha256', 'bytes', 'stage', 'scope'}
                     and diagnostic['resource_id'] == identity and diagnostic['stage'] in ('build', 'test')
                     and diagnostic['scope'] == 'original_engine_owned_writers_exporters_volumes_absent'
                     and receipt['result'].get('error') == 'stage_' + diagnostic['stage'] + '_failed',
                     'staging_diagnostic_proof_invalid', 502)
            fact = await self.client.resource(identity)
            self._shared_guard()
            _require(fact.get('id') == identity and fact.get('kind') == 'test_run'
                     and fact.get('job_id') == item['job_id'] and fact.get('state') == 'ready'
                     and fact.get('bytes') == diagnostic['bytes'] and bool(fact.get('manifest'))
                     and hashlib.sha256(_json(fact['manifest']).encode()).hexdigest() == diagnostic['manifest_sha256'],
                     'staging_diagnostic_proof_invalid', 502)
            metadata['test_run'] = fact
        for kind, identity in item['resource_ids'].items():
            resource = self.resources.inspect_resource(self.principal, identity)
            if resource['state'] != 'provisioning':
                continue  # Previously recorded fact from interrupted read-only reconciliation.
            fact = metadata.get(kind)
            digest = 'sha256:' + hashlib.sha256(_json(fact['manifest']).encode()).hexdigest() if fact else None
            self.resources.mark_resource_ready(self.principal, identity, resource['revision'],
                artifact_digest=digest, size_bytes=fact['bytes'] if fact else None,
                preview_path='/staging/previews/' + identity if kind == 'preview' and fact else None,
                failed=outcome == 'failed')
        binding = self.resources.inspect_job(self.principal, item['job_id'])
        protected = [p for p in binding['protected'] if not(p['reason'] == 'held_campaign' and p['ref'] == item['job_id'])]
        if outcome == 'failed' and {'reason': 'latest_failure', 'ref': item['job_id']} not in protected:
            protected.append({'reason': 'latest_failure', 'ref': item['job_id']})
        self.resources.update_job(self.principal, item['job_id'], binding['revision'], terminal=True,
            protected=protected)
        item.update(phase='verified', outcome=outcome, evidence_id=item.get('evidence_id', str(uuid.uuid4())))
        self._save(item)
        self._shared_guard()
        current = self.queue.get_job(item['job_id'])
        if current['state'] == 'held' or current.get('requires_reconciliation'):
            self.queue.reconcile(job_id=item['job_id'], evidence_id=item['evidence_id'], outcome=outcome)
        else:
            self.queue.complete(job_id=item['job_id'], worker=self.worker, fence=claim['fence'],
                                outcome=outcome, evidence_id=item['evidence_id'])
        if outcome == 'failed':
            self._supersede_failures(item)
        return self._view(item)

    def _supersede_failures(self, newest):
        with self.repo._connection() as db:
            previous = [json.loads(row[0]) for row in db.execute('SELECT data FROM hub_staging_submissions')]
        for prior in previous:
            if (prior['id'] == newest['id'] or prior['project'] != newest['project'] or
                    prior['recipe'] != newest['recipe'] or prior.get('phase') != 'verified' or prior.get('outcome') != 'failed'):
                continue
            self.resources.supersede_latest_failure(self.principal, prior['job_id'])

    async def reconcile(self, identity):
        async with self._short_lock():
            self._shared_guard()
            item = self._read(job_id=_id(identity))
            current = self.queue.get_job(identity)
            if (item['phase'] in ('queued', 'deposited') and
                    (current['state'] == 'held' or current.get('requires_reconciliation'))):
                return self._finish_no_effect(item, None, 'never_sent', 'restart_before_dispatch')
            if item.get('dispatch_proof') in ('never_sent', 'definitively_rejected'):
                return self._finish_no_effect(item, None, item['dispatch_proof'], item['rejection_reason'])
            if item['phase'] == 'verified' and current['state'] == item['outcome']:
                return self._view(item)  # Public owner authorization was checked separately.
            _require(item['phase'] in ('held', 'dispatching', 'accepted', 'verified'), 'staging_reconcile_not_needed', 409)
            try:
                return await self._follow(item, await self.client.job(identity), item.get('claim'))
            except Exception:
                self._hold(item, item.get('claim'))
                return {'job': self.queue.get_job(identity), 'resource_ids': item['resource_ids'], 'phase': 'held'}

    async def cleanup_apply(self, actor, plan_id, digest):
        async with self._short_lock():
            self._shared_guard()
            begun = self.resources.cleanup_begin(actor, plan_id, digest)
            result = {'removed': [], 'failed': [], 'skipped_changed': [entry['id'] for entry in begun['skipped']]}
            for operation in begun['operations']:
                identity, command = operation['id'], operation['operation_id']
                with self.repo._connection() as db:
                    old = db.execute('SELECT data FROM hub_staging_dispatch_cleanup WHERE id=?', (command,)).fetchone()
                if old:
                    saved = json.loads(old[0])
                    if saved['state'] == 'verified':
                        result['removed'].append(identity)
                    elif saved['state'] in ('not_dispatched', 'not_accepted'):
                        result['failed'].append({'id': identity, 'code': saved['state']})
                    elif saved['state'] == 'rejected':
                        code = self._cleanup_not_accepted(plan_id, operation, saved.get('reason'))
                        result['failed'].append({'id': identity, 'code': code})
                    elif saved['state'] == 'preflight':
                        code = self._cleanup_not_dispatched(plan_id, operation, 'preflight_interrupted')
                        result['failed'].append({'id': identity, 'code': code})
                    else:
                        try:
                            receipt = await self.client.operation(command)
                            await self._cleanup_receipt(plan_id, operation, saved['payload'], receipt)
                            saved['state'] = 'verified'
                            with self.repo._transaction() as db:
                                db.execute('UPDATE hub_staging_dispatch_cleanup SET data=? WHERE id=?', (_json(saved), command))
                            result['removed'].append(identity)
                        except Exception:
                            result['failed'].append({'id': identity, 'code': 'reconciliation_required'})
                    continue
                # This must precede the first metadata await. It also covers a
                # crash after cleanup_begin committed but before this row existed.
                saved = {'state': 'preflight', 'plan_id': plan_id, 'resource_id': identity,
                         'resource_revision': operation['expected_revision']}
                with self.repo._transaction() as db:
                    db.execute('INSERT INTO hub_staging_dispatch_cleanup VALUES(?,?)', (command, _json(saved)))
                if not begun['dispatch']:
                    code = self._cleanup_not_dispatched(plan_id, operation, 'preflight_interrupted')
                    result['failed'].append({'id': identity, 'code': code})
                    continue
                try:
                    self._shared_guard(admission=True)
                    fact = await self.client.resource(identity)
                    self._shared_guard()
                    _require(fact['id'] == identity and fact['job_id'] == operation['job_id'] and fact['kind'] == operation['kind'] and
                             fact['state'] in ('ready', 'building') and not fact['refs'], 'staging_cleanup_scope_changed', 409)
                    payload = {'command_id': command, 'idempotency_key': command,
                               'resources': [{'resource_id': identity, 'expected_revision': fact['revision']}]}
                    self._shared_guard(admission=True)
                    with self.repo._transaction() as db:
                        local = self.resources._resource(db, identity)
                        self.repo._cas(local, operation['expected_revision'])
                        self.resources._authorize(db, actor, local['project'], 'cleanup')
                        _require(local['state'] == 'deleting', 'staging_cleanup_scope_changed', 409)
                        current = json.loads(db.execute('SELECT data FROM hub_staging_dispatch_cleanup WHERE id=?', (command,)).fetchone()[0])
                        _require(current == saved, 'staging_cleanup_outcome_uncertain', 409)
                        saved.update(state='dispatching', payload=payload)
                        db.execute('UPDATE hub_staging_dispatch_cleanup SET data=? WHERE id=?', (_json(saved), command))
                    try:
                        receipt = await self.client.cleanup(payload)
                        self._shared_guard()
                    except StagingNotAccepted as rejected:
                        # Only the first original POST reaches this branch.
                        # Persist the typed rejection before restoring metadata;
                        # replays never send POST or reinterpret unknown results.
                        with self.repo._transaction() as db:
                            current = json.loads(db.execute('SELECT data FROM hub_staging_dispatch_cleanup WHERE id=?', (command,)).fetchone()[0])
                            _require(current == saved, 'staging_cleanup_outcome_uncertain', 409)
                            saved.update(state='rejected', reason=rejected.reason)
                            db.execute('UPDATE hub_staging_dispatch_cleanup SET data=? WHERE id=?', (_json(saved), command))
                        code = self._cleanup_not_accepted(plan_id, operation, rejected.reason)
                        result['failed'].append({'id': identity, 'code': code})
                        continue
                    await self._cleanup_receipt(plan_id, operation, payload, receipt)
                    saved['state'] = 'verified'
                    with self.repo._transaction() as db:
                        db.execute('UPDATE hub_staging_dispatch_cleanup SET data=? WHERE id=?', (_json(saved), command))
                    result['removed'].append(identity)
                except asyncio.CancelledError:
                    self._cleanup_not_dispatched(plan_id, operation, 'preflight_cancelled')
                    raise
                except Exception:
                    code = self._cleanup_not_dispatched(plan_id, operation, 'preflight_failed')
                    result['failed'].append({'id': identity, 'code': code})
            return result

    def _cleanup_not_dispatched(self, plan_id, operation, reason):
        try:
            self.resources.cleanup_not_dispatched(self.principal, plan_id, operation['id'],
                operation['expected_revision'], receipt_id=operation['operation_id'], reason=reason)
            return 'not_dispatched'
        except Exception:
            # Once the durable boundary is dispatching, even a timeout before
            # response headers cannot establish that the POST had no effect.
            return 'reconciliation_required'

    def _cleanup_not_accepted(self, plan_id, operation, reason):
        try:
            self.resources.cleanup_not_accepted(self.principal, plan_id, operation['id'],
                operation['expected_revision'], receipt_id=operation['operation_id'], reason=reason)
            return 'not_accepted'
        except Exception:
            return 'reconciliation_required'

    async def _cleanup_receipt(self, plan_id, operation, payload, receipt):
        self._shared_guard()
        identity, command = operation['id'], operation['operation_id']
        _require(receipt.get('command_id') == command and receipt.get('job_id') is None and
                 receipt.get('state') == 'succeeded' and
                 receipt.get('result', {}).get('deleted_resource_ids') == [identity],
                 'staging_cleanup_receipt_invalid', 502)
        tombstone = await self.client.resource(identity)
        self._shared_guard()
        _require(tombstone['id'] == identity and tombstone['job_id'] == operation['job_id'] and
                 tombstone['kind'] == operation['kind'] and tombstone['state'] == 'deleted' and
                 tombstone['revision'] == payload['resources'][0]['expected_revision'] + 1 and
                 tombstone['manifest'] == {} and tombstone['bytes'] == 0 and not tombstone['refs'],
                 'staging_cleanup_absence_unverified', 502)
        self.resources.cleanup_finish(self.principal, plan_id, identity, operation['expected_revision'],
            absent=True, receipt_id=command)

    async def preview_file(self, identity, path):
        self._shared_guard()
        resource = self.resources.inspect_resource(self.principal, _id(identity))
        _require(resource['kind'] == 'preview' and resource['state'] == 'ready' and
                 resource['artifact_digest'] is not None, 'staging_preview_unavailable', 409)
        fact = await self.client.resource(identity)
        self._shared_guard()
        _require(fact.get('id') == identity and fact.get('job_id') == resource['job_id'] and
                 fact.get('kind') == 'preview' and fact.get('state') == 'ready' and
                 'sha256:' + hashlib.sha256(_json(fact['manifest']).encode()).hexdigest() == resource['artifact_digest'],
                 'staging_preview_binding_changed', 409)
        _require(type(path) is str and path in fact['manifest'], 'not_found', 404)
        received = await self.client.file(identity, path)
        self._shared_guard()
        _require(type(received) is tuple and len(received) == 2, 'staging_preview_response_invalid', 502)
        data, media_type = received
        expected = fact['manifest'][path]
        _require(type(data) is bytes and len(data) == expected['bytes'] and
                 hashlib.sha256(data).hexdigest() == expected['sha256'], 'staging_preview_bytes_changed', 502)
        _require(type(media_type) is str and media_type in _FILE_TYPES.get(path.rsplit('.', 1)[-1].lower(), ()),
                 'staging_preview_media_invalid', 502)
        return data, media_type

    async def start(self):
        _require(self.background is None or self.background.done(), 'staging_controller_already_started', 409)
        self.stopping = False
        self.wake.set()
        self.background = asyncio.create_task(self._loop())

    async def _loop(self):
        while not self.stopping:
            self.wake.clear()
            await self._recover_pending()
            try:
                while not self.stopping and await self.run_once() is not None:
                    pass
            except HubError as error:
                if not (self.stopping and error.code in {
                        'staging_controller_closing', 'execution_policy_admission_closed'}):
                    raise
                break
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=5)
            except TimeoutError:
                pass  # Reconsider canonical priority when an external reservation ends.

    async def _recover_pending(self):
        """Follow original receipts after restart; never repeat a command POST."""
        with self.repo._connection() as db:
            entries = [json.loads(row[0]) for row in db.execute('SELECT data FROM hub_staging_submissions')]
        for item in entries:
            if self.stopping or item.get('job_id') is None or item['phase'] not in ('queued', 'deposited', 'held', 'accepted', 'dispatching', 'verified'):
                continue
            due, delay = self.recheck_backoff.get(item['job_id'], (0, 15))
            if time.monotonic() < due:
                continue
            try:
                job = self.queue.get_job(item['job_id'])
            except HubError:
                continue
            if job['state'] not in ('claimed', 'held'):
                continue
            # An unexpired other-generation lease is not ours to replace.
            if (job['state'] == 'claimed' and not job.get('requires_reconciliation')
                    and job.get('worker') != self.worker.encoded()):
                continue
            try:
                await self.reconcile(item['job_id'])
            except Exception:
                pass  # One unavailable item cannot kill the execution service.
            finally:
                self.recheck_backoff[item['job_id']] = (time.monotonic() + delay, min(delay * 2, 300))

    def close_admission(self):
        """Veto new work synchronously, retaining the client for original drain."""
        self.stopping = True
        self.wake.set()

    async def drain(self):
        """Drain the original background/in-flight tasks without closing transport."""
        _require(self.stopping, 'staging_admission_still_open', 409)
        from .owned_tasks import finish_owned
        tasks = ([self.background] if self.background is not None else []) + list(self.inflight)
        if tasks:
            results = await finish_owned(asyncio.gather(*tasks, return_exceptions=True))
            for result in results:
                if isinstance(result, BaseException):
                    raise result

    async def close(self):
        self.close_admission()
        try:
            await self.drain()
        finally:
            await self.client.aclose()
