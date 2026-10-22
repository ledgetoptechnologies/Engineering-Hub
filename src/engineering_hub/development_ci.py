"""Explicit source-only queue adapter for a privately enrolled CI provider.

No provider, endpoint, host command, daemon or environment activation is supplied.
The operator must register the complete physical-machine action registry and
exclude runners outside this same queue. This initial composition refuses other
active claim/completion providers; it is not a multi-provider runtime router.
Private operator code supplies both invocation and authoritative inspection.
Neither a dataclass, public note, caller receipt ID nor result JSON is authority.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import hashlib
import inspect
import json
import math
import re
import time
import uuid

from .lifecycle import finish_owned
from .pilot_ownership import PilotOwner
from .repository import AGENTS, HubError
from .work_queue import (ClosedClaimPolicy, SOFTWARE_VALIDATION_EXECUTOR,
                         WorkQueue, WorkTarget, WorkWorker)


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def _digest(value):
    return isinstance(value,str) and re.fullmatch(r'sha256:[a-f0-9]{64}',value) is not None


@dataclass(frozen=True)
class DevelopmentCIManifest:
    """Reviewed environment identity, reusable across exact candidate commits.

    WorkTarget.artifact_digest must equal this manifest's digest, not its cache
    image ID. The provider privately maps these identities to installed trusted
    paths/engine and checks candidate dependency locks before running any code.
    """
    machine_id: str
    engine_id: str
    image_id: str
    controller_digest: str
    dependency_digests: tuple[str, str, str, str]
    profile: str = 'all'
    budget_seconds: int = 900

    def __post_init__(self):
        if (not isinstance(self.machine_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',self.machine_id)
            or not isinstance(self.engine_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',self.engine_id)
            or not _digest(self.image_id) or not _digest(self.controller_digest)
            or type(self.dependency_digests) is not tuple or len(self.dependency_digests)!=4
            or not all(_digest(value) for value in self.dependency_digests)
            or self.profile not in {'python','ui','all'}
            or type(self.budget_seconds) is not int or not 1<=self.budget_seconds<=900):
            raise ValueError('Invalid private development CI manifest')

    @property
    def digest(self):
        return 'sha256:'+hashlib.sha256(_canonical(asdict(self)).encode()).hexdigest()


@dataclass(frozen=True)
class DevelopmentCIAttempt:
    attempt_id: str
    job_id: str
    fence: int
    epoch: str
    task_id: str
    task_revision: int
    review: str
    actor: str
    source_commit: str
    manifest_digest: str


@dataclass(frozen=True)
class DevelopmentCITerminal:
    """Private verifier's observation, not self-authenticating public evidence."""
    attempt: DevelopmentCIAttempt
    receipt_id: str
    engine_id: str
    image_id: str
    container_id: str
    exit_code: int
    terminal_verified: bool
    descendants_empty: bool


class QueuedDevelopmentCI:
    """One explicit job at a time, with owned drain and no uncertain replay.

    provider.invoke(attempt, admission_guard) and
    provider.verify_terminal(attempt, receipt_id) must be async private operator
    methods. invoke must persist its own original host intent, apply the enrolled
    caps/deadline, and call the synchronous guard before each new effect. An
    accepted phase must drain on loss; it must not be cancelled or replaced.
    verify_terminal must inspect that original attempt, not trust an agent's
    report. This module implements neither that host protocol nor reconciliation.
    """
    def __init__(self, queue, owner, manifest, provider, *, action_key,
                 renew_interval=120, monotonic=time.monotonic):
        if (type(queue) is not WorkQueue or type(owner) is not PilotOwner
            or type(manifest) is not DevelopmentCIManifest
            or queue.repo.pilot_owner is not owner
            or queue.repo.execution_principal != SOFTWARE_VALIDATION_EXECUTOR
            or type(queue.claim_verifier) is not ClosedClaimPolicy
            or queue.completion_verifier is not None or queue.reconciliation_verifier is not None
            or not callable(queue.target_verifier)
            or getattr(queue.repo,'_queued_pilot',None) is not None
            or getattr(queue.repo,'_development_ci',None) is not None
            or not callable(getattr(provider,'invoke',None))
            or not callable(getattr(provider,'verify_terminal',None))
            or not inspect.iscoroutinefunction(provider.invoke)
            or not inspect.iscoroutinefunction(provider.verify_terminal)
            or type(renew_interval) not in {float,int} or not math.isfinite(renew_interval)
            or not 0<renew_interval<=120 or not callable(monotonic)):
            raise HubError('development_ci_composition_required',409)
        owner.assert_active(queue.repo.path)
        action=queue.actions.get(action_key)
        if (action is None or action.project!='engineering-hub'
            or action.machine_id!=manifest.machine_id or action.environments!=frozenset({'staging'})
            or not action.requires_execution_proof):
            raise HubError('development_ci_binding_required',409)
        self.queue,self.repo,self.owner,self.manifest,self.provider=queue,queue.repo,owner,manifest,provider
        self._manifest,self._provider=manifest,provider
        self._invoke,self._verify=provider.invoke,provider.verify_terminal
        self.action_key,self.epoch=action_key,owner.epoch
        self.worker=WorkWorker(SOFTWARE_VALIDATION_EXECUTOR,self.epoch)
        self.renew_interval,self.monotonic=renew_interval,monotonic
        self._registry=dict(queue.actions)
        self._target=queue.target_verifier
        self._claim=queue.claim_verifier
        self._approval=queue.approval_verifier
        self._readiness=queue.production_readiness_verifier
        self._permit=ContextVar('development-ci-completion',default=None)
        self._lock=asyncio.Lock()
        with self.repo._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS hub_development_ci_policy (
                id INTEGER PRIMARY KEY CHECK(id=1), action_key TEXT NOT NULL,
                machine_id TEXT NOT NULL, manifest_digest TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS hub_development_ci_attempts (
                job_id TEXT PRIMARY KEY, attempt TEXT NOT NULL, state TEXT NOT NULL,
                receipt_id TEXT, proof TEXT)''')
            expected=(action_key,manifest.machine_id,manifest.digest)
            old=db.execute('SELECT action_key,machine_id,manifest_digest FROM hub_development_ci_policy WHERE id=1').fetchone()
            if old is not None and tuple(old)!=expected:
                raise HubError('development_ci_enrollment_changed',409)
            db.execute('INSERT OR IGNORE INTO hub_development_ci_policy VALUES(1,?,?,?)',expected)
        self.repo._development_ci=self
        queue.target_verifier=self.target_verifier
        queue.claim_verifier=self.claim_verifier
        queue.completion_verifier=self.completion_verifier

    def _guard(self):
        self.owner.assert_active(self.repo.path)
        if (self.owner.epoch!=self.epoch or self.repo.pilot_owner is not self.owner
            or self.repo.execution_principal!=SOFTWARE_VALIDATION_EXECUTOR
            or self.manifest is not self._manifest or self.provider is not self._provider
            or self.provider.invoke!=self._invoke or self.provider.verify_terminal!=self._verify
            or self.queue.repo is not self.repo or getattr(self.repo,'_development_ci',None) is not self
            or self.queue.actions!=self._registry or self.queue.target_verifier!=self.target_verifier
            or self.queue.claim_verifier!=self.claim_verifier
            or self.queue.completion_verifier!=self.completion_verifier
            or self.queue.reconciliation_verifier is not None
            or self.queue.approval_verifier is not self._approval
            or self.queue.production_readiness_verifier is not self._readiness):
            raise HubError('development_ci_ownership_changed',409)

    def _review(self, actor, target):
        task=self.repo.get_task(target.task_id)
        review=task.get('review')
        if (actor not in AGENTS or task['project']!='engineering-hub' or task['owner']!=actor
            or task['status']!='verified' or task['candidate_commit']!=target.source_commit
            or type(review) is not dict or review.get('actor') not in AGENTS
            or review.get('actor')==actor or review.get('verdict')!='approve'
            or review.get('commit')!=target.source_commit or target.artifact_digest!=self.manifest.digest):
            raise HubError('development_ci_review_changed',409)
        return task

    def target_verifier(self,actor,action_key,environment,target):
        try:
            self._guard()
            if self._target(actor,action_key,environment,target) is not True:
                return False
            if action_key!=self.action_key:
                return True
            if environment!='staging' or type(target) is not WorkTarget:
                return False
            self._review(actor,target)
            return True
        except Exception:
            return False

    def claim_verifier(self,worker,action_key,environment,machine_id,target):
        try:
            self._guard()
            # This callback runs inside WorkQueue.claim's admission transaction.
            # A different queue object can persist a production action absent
            # from this process's registry without changing our Python dict.
            # Unknown same-machine production must not become invisible to CI.
            with self.repo._connection() as db:
                production=db.execute("SELECT action_key,project,requires_execution_proof FROM hub_work_jobs "
                    "WHERE machine_id=? AND environment='production' AND state='queued'",
                    (self.manifest.machine_id,)).fetchall()
            for row in production:
                action=self._registry.get(row['action_key'])
                if (action is None or action.machine_id!=self.manifest.machine_id
                    or 'production' not in action.environments or action.project!=row['project']
                    or action.requires_execution_proof!=bool(row['requires_execution_proof'])):
                    return False
            return (worker==self.worker and action_key==self.action_key and environment=='staging'
                and machine_id==self.manifest.machine_id and type(target) is WorkTarget
                and target.artifact_digest==self.manifest.digest)
        except Exception:
            return False

    def _bound(self,attempt):
        self._guard()
        job=self.queue.live_claim(job_id=attempt.job_id,worker=self.worker,fence=attempt.fence)
        target=WorkTarget(**job['target'])
        task=self._review(job['actor'],target)
        with self.repo._connection() as db:
            saved=db.execute('SELECT attempt FROM hub_development_ci_attempts WHERE job_id=?',(attempt.job_id,)).fetchone()
            policy=db.execute('SELECT action_key,machine_id,manifest_digest FROM hub_development_ci_policy WHERE id=1').fetchone()
        if (saved is None or saved['attempt']!=_canonical(asdict(attempt))
            or policy is None or tuple(policy)!=(self.action_key,self.manifest.machine_id,self.manifest.digest)
            or attempt.epoch!=self.epoch or job['worker']!=self.worker.encoded()
            or job['action_key']!=self.action_key or job['machine_id']!=self.manifest.machine_id
            or job['environment']!='staging' or job['actor']!=attempt.actor
            or target!=WorkTarget(attempt.task_id,attempt.source_commit,attempt.manifest_digest)
            or task['revision']!=attempt.task_revision or _canonical(task['review'])!=attempt.review):
            raise HubError('development_ci_attempt_changed',409)
        return job

    def completion_verifier(self,job,evidence_id,outcome):
        # Called under WorkQueue's transaction: avoid reentering its live_claim
        # transaction here. Only the private run scope can enable completion.
        try:
            self._guard()
            if self._permit.get()!=(job['id'],job['fence'],evidence_id,outcome):
                return False
            with self.repo._connection() as db:
                row=db.execute('SELECT attempt,state,receipt_id,proof FROM hub_development_ci_attempts WHERE job_id=?',(job['id'],)).fetchone()
                policy=db.execute('SELECT action_key,machine_id,manifest_digest FROM hub_development_ci_policy WHERE id=1').fetchone()
            if row is None:
                return False
            attempt=DevelopmentCIAttempt(**json.loads(row['attempt']))
            task=self._review(job['actor'],WorkTarget(**job['target']))
            return (row['state']=='verified' and row['receipt_id']==evidence_id
                and policy is not None and tuple(policy)==(self.action_key,self.manifest.machine_id,self.manifest.digest)
                and job['worker']==self.worker.encoded() and job['action_key']==self.action_key
                and job['environment']=='staging' and job['machine_id']==self.manifest.machine_id
                and job['project']=='engineering-hub' and job['requires_execution_proof'] is True
                and attempt.job_id==job['id'] and attempt.manifest_digest==self.manifest.digest
                and WorkTarget(**job['target'])==WorkTarget(attempt.task_id,attempt.source_commit,attempt.manifest_digest)
                and attempt.epoch==self.epoch and attempt.fence==job['fence']
                and attempt.actor==job['actor'] and task['revision']==attempt.task_revision
                and _canonical(task['review'])==attempt.review
                and json.loads(row['proof'])['outcome']==outcome)
        except Exception:
            return False

    def _hold(self,job):
        try:
            self.queue.complete(job_id=job['id'],worker=self.worker,fence=job['fence'],outcome='held')
        except Exception:
            # Owner/lease loss cannot justify releasing the physical reservation.
            pass
        return self.queue.get_job(job['id'])

    async def run_once(self):
        async with self._lock:
            return await finish_owned(self._run())

    async def _run(self):
        self._guard()
        job=self.queue.claim(worker=self.worker,lease_seconds=600,action_keys=(self.action_key,))
        if job is None:
            return None
        try:
            task=self._review(job['actor'],WorkTarget(**job['target']))
            attempt=DevelopmentCIAttempt(str(uuid.uuid4()),job['id'],job['fence'],self.epoch,
                task['id'],task['revision'],_canonical(task['review']),job['actor'],
                task['candidate_commit'],self.manifest.digest)
            with self.repo._transaction() as db:
                # Unique job identity deliberately refuses replay of any prior
                # ambiguous intent, including one written under an older epoch.
                db.execute('INSERT INTO hub_development_ci_attempts VALUES(?,?,?,NULL,NULL)',
                           (job['id'],_canonical(asdict(attempt)),'intent'))
            lost=False
            started=self.monotonic()

            def admission_guard():
                if lost or self.monotonic()-started>=self.manifest.budget_seconds:
                    raise HubError('development_ci_admission_lost',409)
                return self._bound(attempt)

            async def supervised_stage(awaitable):
                nonlocal lost
                pending=asyncio.create_task(awaitable)
                while not pending.done():
                    await asyncio.wait({pending},timeout=self.renew_interval)
                    if pending.done():
                        break
                    try:
                        admission_guard()
                        self.queue.renew(job_id=job['id'],worker=self.worker,fence=job['fence'],lease_seconds=600)
                    except Exception:
                        lost=True
                        # Drain an accepted phase, including terminal inspection.
                        # Never cancel, extend a lost lease or start a new effect.
                        break
                result=await pending
                admission_guard()
                return result

            admission_guard()
            receipt=await supervised_stage(self._invoke(attempt,admission_guard))
            if not isinstance(receipt,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',receipt):
                return self._hold(job)
            with self.repo._transaction() as db:
                db.execute("UPDATE hub_development_ci_attempts SET state='observed',receipt_id=? WHERE job_id=?",
                           (receipt,job['id']))
            proof=await supervised_stage(self._verify(attempt,receipt))
            if (type(proof) is not DevelopmentCITerminal or proof.attempt!=attempt
                or proof.receipt_id!=receipt or proof.engine_id!=self.manifest.engine_id
                or proof.image_id!=self.manifest.image_id
                or not isinstance(proof.container_id,str) or not re.fullmatch('[a-f0-9]{64}',proof.container_id)
                or type(proof.exit_code) is not int or not 0<=proof.exit_code<=255
                or proof.terminal_verified is not True or proof.descendants_empty is not True):
                return self._hold(job)
            outcome='succeeded' if proof.exit_code==0 else 'failed'
            with self.repo._transaction() as db:
                db.execute("UPDATE hub_development_ci_attempts SET state='verified',receipt_id=?,proof=? WHERE job_id=?",
                    (receipt,_canonical({'outcome':outcome,'terminal':asdict(proof)}),job['id']))
            token=self._permit.set((job['id'],job['fence'],receipt,outcome))
            try:
                return self.queue.complete(job_id=job['id'],worker=self.worker,fence=job['fence'],outcome=outcome,evidence_id=receipt)
            finally:
                self._permit.reset(token)
        except (Exception,asyncio.CancelledError):
            return self._hold(job)
