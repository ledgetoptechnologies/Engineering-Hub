"""Explicit canonical-queue adapter for the separately deployed staging worker.

Only this controller's registered actions use its verifiers. Existing actions,
production priority, machine reservations and proofs retain their verifiers.
Dispatch intent is durable before an HTTP effect; uncertainty is never retried.
"""
import asyncio
import base64
import hashlib
import json
import re
import time
import uuid

from .repository import AGENTS, HubError
from .staging_resources import KINDS
from .staging_client import _FILE_TYPES
from .work_queue import WorkAction, WorkQueue, WorkTarget, WorkWorker, STAGING_SERVICE_EXECUTOR


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
                 actions, worker=None, poll_interval=1, max_poll_seconds=300):
        _require(queue.repo is repo and resources.repo is repo and resources._control is control_principal,
                 'invalid_staging_configuration')
        _require(type(actions) is dict and bool(actions) and 0 < poll_interval <= 30 and
                 0 < max_poll_seconds <= 1800, 'invalid_staging_configuration')
        self.repo, self.queue, self.client, self.resources = repo, queue, client, resources
        self.principal, self.actions = control_principal, dict(actions)
        self.worker = worker or WorkWorker(STAGING_SERVICE_EXECUTOR, str(uuid.uuid4()))
        _require(self.worker.agent == STAGING_SERVICE_EXECUTOR, 'invalid_staging_worker')
        self.poll_interval, self.max_poll_seconds = poll_interval, max_poll_seconds
        self.lock, self.wake = asyncio.Lock(), asyncio.Event()
        self.background, self.stopping = None, False
        self.inflight = set()
        for (project, recipe), key in self.actions.items():
            repo._project(project)
            _require(key in queue.actions and queue.actions[key].project == project and
                     queue.actions[key].environments == frozenset({'staging'}) and
                     queue.actions[key].requires_execution_proof, 'invalid_staging_action')
        with repo._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS hub_staging_submissions (id TEXT PRIMARY KEY,actor TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,data TEXT NOT NULL,UNIQUE(actor,request_id))')
            db.execute('CREATE TABLE IF NOT EXISTS hub_staging_dispatch_cleanup (id TEXT PRIMARY KEY,data TEXT NOT NULL)')
        self._install_verifiers()

    def _read(self, identity=None, *, job_id=None):
        with self.repo._connection() as db:
            if identity is not None:
                row = db.execute('SELECT data FROM hub_staging_submissions WHERE id=?', (identity,)).fetchone()
                if row:
                    return json.loads(row[0])
            else:
                for row in db.execute('SELECT data FROM hub_staging_submissions'):
                    value = json.loads(row[0])
                    if value.get('job_id') == job_id:
                        return value
        raise HubError('staging_job_not_found', 404)

    def _save(self, item):
        with self.repo._transaction() as db:
            db.execute('UPDATE hub_staging_submissions SET data=? WHERE id=?', (_json(item), item['id']))

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
                item = self._read(value.task_id)
                self._grants(item['actor'], item['project'])
                return (actor == item['actor'] and environment == 'staging' and item['phase'] != 'depositing'
                        and action == self.actions[(item['project'], item['recipe'])]
                        and value.source_commit == item['source_commit']
                        and value.artifact_digest == 'sha256:' + item['source_digest'])
            except Exception:
                return False

        def claim(worker, action, environment, machine, value):
            if action not in owned:
                return old_claim(worker, action, environment, machine, value) if old_claim else True
            if self._unknown_production(machine, old_target):
                return False
            return worker == self.worker and target(self._read(value.task_id)['actor'], action, environment, value)

        def proof(job, evidence_id, outcome):
            if job['action_key'] not in owned:
                return old_complete(job, evidence_id, outcome) if old_complete else False
            try:
                item = self._read(job_id=job['id'])
                return item['phase'] == 'verified' and item['evidence_id'] == evidence_id and item['outcome'] == outcome
            except Exception:
                return False

        def reconciled(job, evidence_id, outcome):
            if job['action_key'] not in owned:
                return old_reconcile(job, evidence_id, outcome) if old_reconcile else False
            return proof(job, evidence_id, outcome)
        queue.target_verifier, queue.claim_verifier = target, claim
        queue.completion_verifier, queue.reconciliation_verifier = proof, reconciled

    def _unknown_production(self, machine, previous_target):
        # Called from claim admission as well as preflight. Unknown persisted
        # production cannot disappear merely because a registry was reloaded.
        with self.repo._connection() as db:
            rows = db.execute("SELECT action_key FROM hub_work_jobs WHERE state='queued' AND environment='production' AND machine_id=? AND approval_id IS NOT NULL", (machine,)).fetchall()
        return any(row['action_key'] not in self.queue.actions or
                   row['action_key'] not in self.actions.values() and previous_target is None for row in rows)

    async def submit(self, actor, payload):
        _require(actor in AGENTS, 'staging_agent_required', 403)
        fields = {'request_id', 'project', 'recipe', 'source_commit', 'source_digest', 'source_zip_base64'}
        _require(type(payload) is dict and set(payload) in (fields, fields | {'topic'}))
        _id(payload['request_id'])
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
        topic = payload.get('topic')
        _require(topic is None or type(topic) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', topic))
        public = {key: value for key, value in payload.items() if key != 'source_zip_base64'}
        public['topic'] = topic  # Omitted and explicit null have one request identity.
        fingerprint = hashlib.sha256(_json(public).encode()).hexdigest()
        async with self.lock:
            with self.repo._transaction() as db:
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
                receipt = await self.client.put_source({'source_id': item['source_id'], 'project': project,
                    'source_digest': item['source_digest'], 'source_zip_base64': encoded})
                _require(all(receipt.get(key) == item[key] for key in ('source_id', 'source_digest', 'project')),
                         'staging_source_receipt_mismatch', 502)
                item['phase'] = 'deposited'; self._save(item)
            if item['job_id'] is None:
                target = WorkTarget(item['id'], item['source_commit'], 'sha256:' + item['source_digest'])
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
        return {'job': self.queue.get_job(item['job_id']), 'source_id': item['source_id'],
                'resource_ids': item['resource_ids'], 'phase': item['phase']}

    async def status(self, actor=None):
        result = {'configured': True, 'running': self.background is not None and not self.background.done(),
                  'reachable': False, 'profiles': {}}
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
                result['profiles'][project] = remote.get('projects', {}).get(project, [])
        return result

    def _hold(self, item, claim=None):
        item['phase'] = 'held'; self._save(item)
        job = self.resources.inspect_job(self.principal, item['job_id'])
        protected = list(job['protected'])
        hold = {'reason': 'held_campaign', 'ref': item['job_id']}
        if hold not in protected:
            protected.append(hold)
        self.resources.update_job(self.principal, item['job_id'], job['revision'], terminal=job['terminal'],
            protected=protected)
        if claim:
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
        task.add_done_callback(self.inflight.discard)
        return await asyncio.shield(task)

    async def _run_once(self):
        async with self.lock:
            claim = self.queue.claim(worker=self.worker, action_keys=tuple(sorted(self.actions.values())), lease_seconds=600)
            if claim is None:
                return None
            item = self._read(job_id=claim['id'])
            try:
                _require(item['phase'] == 'queued', 'staging_dispatch_already_attempted', 409)
                self._grants(item['actor'], item['project'])
                self.queue.live_claim(job_id=claim['id'], worker=self.worker, fence=claim['fence'])
                item['phase'], item['claim'] = 'dispatching', claim
                self._save(item)  # Never retry POST after this durable boundary.
                receipt = await self.client.submit({'job_id': item['job_id'], 'command_id': item['command_id'],
                    'idempotency_key': item['command_id'], 'project': item['project'], 'recipe': item['recipe'],
                    'source_commit': item['source_commit'], 'source_digest': item['source_digest'],
                    'source_id': item['source_id'], 'resource_ids': item['resource_ids']})
                item['phase'] = 'accepted'; self._save(item)
                return await self._follow(item, receipt, claim)
            except Exception:
                self._hold(item, claim)
                return self._view(item)

    async def _follow(self, item, receipt, claim):
        deadline = time.monotonic() + self.max_poll_seconds
        while True:
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

    async def _finish(self, item, receipt, claim):
        outcome = receipt['state']
        if outcome == 'succeeded':
            result = receipt['result']
            _require(all(result.get(key) == item[key] for key in ('source_commit', 'source_digest', 'resource_ids'))
                     and result.get('source_commit_verification') == 'declared' and result.get('preview_ready') is True
                     and set(result.get('stages', {})) == {'build', 'test'}
                     and all(stage.get('exit_code') == 0 for stage in result['stages'].values()),
                     'staging_completion_proof_invalid', 502)
        metadata = {}
        for kind, identity in item['resource_ids'].items():
            if outcome == 'succeeded':
                fact = await self.client.resource(identity)
                _require(fact.get('id') == identity and fact.get('kind') == kind and
                         fact.get('job_id') == item['job_id'] and fact.get('state') == 'ready',
                         'staging_resource_proof_invalid', 502)
                metadata[kind] = fact
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
        current = self.queue.get_job(item['job_id'])
        if current['state'] == 'held':
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
            job = self.resources.inspect_job(self.principal, prior['job_id'])
            protections = [p for p in job['protected'] if not(p['reason'] == 'latest_failure' and p['ref'] == prior['job_id'])]
            self.resources.update_job(self.principal, prior['job_id'], job['revision'], terminal=True, protected=protections)
            for identity in prior['resource_ids'].values():
                resource = self.resources.inspect_resource(self.principal, identity)
                if resource['state'] in ('deleting', 'deleted'):
                    continue
                protected = [p for p in resource['protected'] if not(p['reason'] == 'latest_failure' and p['ref'] == prior['job_id'])]
                self.resources.set_protection(self.principal, identity, resource['revision'], protected)

    async def reconcile(self, identity):
        async with self.lock:
            item = self._read(job_id=_id(identity))
            current = self.queue.get_job(identity)
            if item['phase'] == 'verified' and current['state'] == item['outcome']:
                return self.get_job(item['actor'], identity)
            _require(item['phase'] in ('held', 'dispatching', 'accepted', 'verified'), 'staging_reconcile_not_needed', 409)
            try:
                return await self._follow(item, await self.client.job(identity), item.get('claim'))
            except Exception:
                self._hold(item, item.get('claim'))
                return {'job': self.queue.get_job(identity), 'resource_ids': item['resource_ids'], 'phase': 'held'}

    async def cleanup_apply(self, actor, plan_id, digest):
        async with self.lock:
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
                if not begun['dispatch']:
                    result['failed'].append({'id': identity, 'code': 'reconciliation_required'}); continue
                try:
                    fact = await self.client.resource(identity)
                    _require(fact['job_id'] == operation['job_id'] and fact['kind'] == operation['kind'] and
                             fact['state'] in ('ready', 'building') and not fact['refs'], 'staging_cleanup_scope_changed', 409)
                    payload = {'command_id': command, 'idempotency_key': command,
                               'resources': [{'resource_id': identity, 'expected_revision': fact['revision']}]}
                    saved = {'state': 'dispatching', 'payload': payload}
                    with self.repo._transaction() as db:
                        db.execute('INSERT INTO hub_staging_dispatch_cleanup VALUES(?,?)', (command, _json(saved)))
                    receipt = await self.client.cleanup(payload)
                    await self._cleanup_receipt(plan_id, operation, payload, receipt)
                    saved['state'] = 'verified'
                    with self.repo._transaction() as db:
                        db.execute('UPDATE hub_staging_dispatch_cleanup SET data=? WHERE id=?', (_json(saved), command))
                    result['removed'].append(identity)
                except Exception:
                    result['failed'].append({'id': identity, 'code': 'reconciliation_required'})
            return result

    async def _cleanup_receipt(self, plan_id, operation, payload, receipt):
        identity, command = operation['id'], operation['operation_id']
        _require(receipt.get('command_id') == command and receipt.get('job_id') is None and
                 receipt.get('state') == 'succeeded' and
                 receipt.get('result', {}).get('deleted_resource_ids') == [identity],
                 'staging_cleanup_receipt_invalid', 502)
        tombstone = await self.client.resource(identity)
        _require(tombstone['id'] == identity and tombstone['job_id'] == operation['job_id'] and
                 tombstone['kind'] == operation['kind'] and tombstone['state'] == 'deleted' and
                 tombstone['revision'] == payload['resources'][0]['expected_revision'] + 1 and
                 tombstone['manifest'] == {} and tombstone['bytes'] == 0 and not tombstone['refs'],
                 'staging_cleanup_absence_unverified', 502)
        self.resources.cleanup_finish(self.principal, plan_id, identity, operation['expected_revision'],
            absent=True, receipt_id=command)

    async def preview_file(self, identity, path):
        resource = self.resources.inspect_resource(self.principal, _id(identity))
        _require(resource['kind'] == 'preview' and resource['state'] == 'ready' and
                 resource['artifact_digest'] is not None, 'staging_preview_unavailable', 409)
        fact = await self.client.resource(identity)
        _require(fact.get('id') == identity and fact.get('job_id') == resource['job_id'] and
                 fact.get('kind') == 'preview' and fact.get('state') == 'ready' and
                 'sha256:' + hashlib.sha256(_json(fact['manifest']).encode()).hexdigest() == resource['artifact_digest'],
                 'staging_preview_binding_changed', 409)
        _require(type(path) is str and path in fact['manifest'], 'not_found', 404)
        received = await self.client.file(identity, path)
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
            while not self.stopping and await self.run_once() is not None:
                pass
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=5)
            except TimeoutError:
                pass  # Reconsider canonical priority when an external reservation ends.

    async def close(self):
        self.stopping = True
        self.wake.set()
        if self.background:
            await self.background  # Drain the current accepted operation, never cancel it.
        if self.inflight:
            await asyncio.gather(*tuple(self.inflight))
        await self.client.aclose()
