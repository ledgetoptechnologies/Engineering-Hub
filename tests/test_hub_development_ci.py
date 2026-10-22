"""Owned Linux ledger with private fake CI observations; no host execution."""
import asyncio
from dataclasses import replace
import json
import sys
from types import SimpleNamespace

import pytest

from engineering_hub.development_ci import (
    DevelopmentCIManifest, DevelopmentCITerminal, QueuedDevelopmentCI)
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.work_queue import ClosedClaimPolicy, WorkAction, WorkQueue, WorkTarget, WorkWorker
from tests.test_hub_container_pilot import reviewed_task

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='Real Linux owned ledger')
ACTION='engineering-hub.development-ci'
MACHINE='staging-pc'
COMMIT='c'*40
IMAGE='sha256:'+'d'*64


class Provider:
    def __init__(self):
        self.calls=[]
        self.inspections=[]
        self.started=asyncio.Event()
        self.release=None
        self.change=None
        self.failure=None
        self.bad=None
        self.exit_code=0
        self.inspect_started=asyncio.Event()
        self.inspect_release=None

    async def invoke(self,attempt,guard):
        guard()
        self.calls.append(attempt)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.change is not None:
            self.change()
        guard()
        if self.failure:
            raise self.failure
        return 'receipt-'+attempt.attempt_id

    async def verify_terminal(self,attempt,receipt):
        self.inspections.append((attempt,receipt))
        self.inspect_started.set()
        if self.inspect_release is not None:
            await self.inspect_release.wait()
        proof=DevelopmentCITerminal(attempt,receipt,'engine-one',IMAGE,'f'*64,self.exit_code,True,True)
        if self.bad=='dictionary': return {'terminal_verified':True}
        if self.bad=='attempt': return replace(proof,attempt=replace(attempt,fence=attempt.fence+1))
        if self.bad=='receipt': return replace(proof,receipt_id='foreign')
        if self.bad=='engine': return replace(proof,engine_id='foreign')
        if self.bad=='image': return replace(proof,image_id='sha256:'+'a'*64)
        if self.bad=='descendants': return replace(proof,descendants_empty=False)
        return proof


@pytest.fixture
def rig(tmp_path):
    private=tmp_path/'private'
    private.mkdir(mode=0o700)
    owner=PilotOwner(private/'hub.sqlite3',allow_ephemeral_test_storage=True)
    repo=HubRepository(private/'hub.sqlite3',execution_principal='executor:software-validation',pilot_owner=owner)
    clock=[1000.0]
    def verified(actor,action,environment,target):
        task=repo.get_task(target.task_id)
        return (task['owner']==actor and task['status']=='verified'
                and task['candidate_commit']==target.source_commit)
    queue=WorkQueue(repo,{ACTION:WorkAction(MACHINE,project='engineering-hub'),
        'engineering-hub.production':WorkAction(MACHINE,frozenset({'production'}),project='engineering-hub')},
        claim_verifier=ClosedClaimPolicy(),target_verifier=verified,
        approval_verifier=lambda *args:True,clock=lambda:clock[0])
    manifest=DevelopmentCIManifest(MACHINE,'engine-one',IMAGE,'sha256:'+'e'*64,('sha256:'+'a'*64,)*4)
    provider=Provider()
    bridge=QueuedDevelopmentCI(queue,owner,manifest,provider,action_key=ACTION,renew_interval=.01)
    value=SimpleNamespace(owner=owner,repo=repo,queue=queue,manifest=manifest,provider=provider,bridge=bridge,clock=clock)
    try: yield value
    finally: owner.close()


def submit(rig,key='one',production=False):
    task=reviewed_task(rig.repo,key)
    job=rig.queue.submit(actor='codex',request_id=key,
        action_key='engineering-hub.production' if production else ACTION,
        environment='production' if production else 'staging',approval_id='approval' if production else None,
        target=WorkTarget(task['id'],task['candidate_commit'],rig.manifest.digest))
    return task,job


def reservation(rig):
    with rig.repo._connection() as db:
        row=db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?',(MACHINE,)).fetchone()
        return row['job_id'] if row else None


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_code,state',[(0,'succeeded'),(1,'failed')])
async def test_exact_bound_terminal_releases_shared_machine(rig,exit_code,state):
    _,job=submit(rig)
    rig.provider.exit_code=exit_code
    result=await rig.bridge.run_once()
    assert result['id']==job['id'] and result['state']==state
    assert len(rig.provider.calls)==len(rig.provider.inspections)==1
    attempt=rig.provider.calls[0]
    assert attempt.epoch==rig.owner.epoch and attempt.manifest_digest==rig.manifest.digest
    assert reservation(rig) is None
    assert await rig.bridge.run_once() is None


@pytest.mark.asyncio
async def test_intent_exists_before_provider_and_public_agent_cannot_claim_or_complete(rig):
    _,job=submit(rig)
    assert rig.queue.claim(worker=WorkWorker('codex',rig.owner.epoch)) is None
    rig.provider.release=asyncio.Event()
    running=asyncio.create_task(rig.bridge.run_once())
    await rig.provider.started.wait()
    with rig.repo._connection() as db:
        row=db.execute('SELECT state,attempt FROM hub_development_ci_attempts WHERE job_id=?',(job['id'],)).fetchone()
    assert row['state']=='intent' and json.loads(row['attempt'])['job_id']==job['id']
    current=rig.queue.get_job(job['id'])
    with pytest.raises(HubError,match='work_completion_proof_required'):
        rig.queue.complete(job_id=job['id'],worker=rig.bridge.worker,fence=current['fence'],
                           outcome='succeeded',evidence_id='forged')
    rig.provider.release.set()
    await running


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['dictionary','attempt','receipt','engine','image','descendants'])
async def test_untrusted_or_mismatched_terminal_holds_and_does_not_retry(rig,bad):
    _,job=submit(rig)
    rig.provider.bad=bad
    result=await rig.bridge.run_once()
    assert result['state']=='held' and reservation(rig)==job['id']
    assert await rig.bridge.run_once() is None and len(rig.provider.calls)==1


@pytest.mark.asyncio
async def test_provider_error_stays_held(rig):
    _,job=submit(rig)
    rig.provider.failure=RuntimeError('private-provider-error')
    assert (await rig.bridge.run_once())['state']=='held'
    assert reservation(rig)==job['id'] and not rig.provider.inspections


@pytest.mark.asyncio
async def test_unknown_approved_production_precedes_staging_on_same_machine(rig):
    submit(rig,'staging')
    submit(rig,'production',production=True)
    assert await rig.bridge.run_once() is None
    assert not rig.provider.calls and reservation(rig) is None


@pytest.mark.asyncio
async def test_production_from_another_registry_cannot_be_filtered_away(rig):
    submit(rig,'staging')
    other=WorkQueue(rig.repo,{'unknown.production':WorkAction(MACHINE,frozenset({'production'}),project='engineering-hub')},
        target_verifier=lambda *args:True,approval_verifier=lambda *args:True,clock=lambda:rig.clock[0])
    task=reviewed_task(rig.repo,'other-production')
    other.submit(actor='codex',request_id='other-production',action_key='unknown.production',environment='production',
        approval_id='accepted',target=WorkTarget(task['id'],task['candidate_commit'],rig.manifest.digest))
    assert 'unknown.production' not in rig.queue.actions
    assert await rig.bridge.run_once() is None
    assert not rig.provider.calls and reservation(rig) is None


@pytest.mark.asyncio
async def test_pause_after_accepted_phase_vetoes_future_effect_and_holds(rig):
    _,job=submit(rig)
    rig.provider.change=lambda:rig.queue.set_paused(True)
    assert (await rig.bridge.run_once())['state']=='held'
    assert reservation(rig)==job['id'] and not rig.provider.inspections


@pytest.mark.asyncio
async def test_review_drift_holds_and_never_inspects_as_completed(rig):
    task,job=submit(rig)
    def change():
        with rig.repo._transaction() as db:
            data=rig.repo._get(db,task['id'],'task')
            data['review']['actor']='hermes'
            rig.repo._save(db,'hermes','test.review-drift','task',data)
    rig.provider.change=change
    assert (await rig.bridge.run_once())['state']=='held'
    assert reservation(rig)==job['id']


@pytest.mark.asyncio
async def test_review_race_inside_completion_transaction_refuses_release(rig,monkeypatch):
    task,job=submit(rig)
    complete=rig.queue.complete
    def race(**kwargs):
        if kwargs['outcome']=='succeeded':
            with rig.repo._transaction() as db:
                data=rig.repo._get(db,task['id'],'task')
                data['review']['actor']='hermes'
                rig.repo._save(db,'hermes','test.review-race','task',data)
        return complete(**kwargs)
    monkeypatch.setattr(rig.queue,'complete',race)
    assert (await rig.bridge.run_once())['state']=='held'
    assert reservation(rig)==job['id']


@pytest.mark.asyncio
async def test_provider_replacement_during_attempt_holds_without_followup(rig):
    _,job=submit(rig)
    rig.provider.change=lambda:setattr(rig.bridge,'provider',Provider())
    assert (await rig.bridge.run_once())['state']=='held'
    assert reservation(rig)==job['id'] and not rig.provider.inspections


@pytest.mark.asyncio
async def test_duplicate_submission_is_one_durable_attempt(rig):
    task,job=submit(rig)
    duplicate=rig.queue.submit(actor='codex',request_id='one',action_key=ACTION,environment='staging',
        target=WorkTarget(task['id'],task['candidate_commit'],rig.manifest.digest))
    assert duplicate['id']==job['id']
    await rig.bridge.run_once()
    assert await rig.bridge.run_once() is None and len(rig.provider.calls)==1


@pytest.mark.asyncio
async def test_owner_loss_never_releases_reservation(rig):
    _,job=submit(rig)
    rig.provider.change=rig.owner.close
    result=await rig.bridge.run_once()
    # Lost ownership forbids even the attempted held-state write. A read-only
    # projection may still report the original claimed record; it is neither
    # completed nor evidence that the physical reservation became available.
    assert result['id']==job['id'] and result['state']=='claimed'
    assert reservation(rig)==job['id']
    # Inspect only through a newly acquired owned process; the old bridge cannot
    # mutate a new epoch or pretend its external attempt was stopped.
    owner=PilotOwner(rig.repo.path,allow_ephemeral_test_storage=True,require_existing=True)
    try:
        repo=HubRepository(rig.repo.path,execution_principal='executor:software-validation',pilot_owner=owner)
        queue=WorkQueue(repo,rig.queue.actions,target_verifier=lambda *args:True,
            claim_verifier=ClosedClaimPolicy(),clock=lambda:rig.clock[0])
        provider=Provider()
        bridge=QueuedDevelopmentCI(queue,owner,rig.manifest,provider,action_key=ACTION,renew_interval=.01)
        rig.clock[0]+=601
        assert await bridge.run_once() is None
        assert queue.get_job(job['id'])['state']=='held' and not provider.calls
        with repo._connection() as db:
            assert db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?',(MACHINE,)).fetchone()[0]==job['id']
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_renewal_keeps_accepted_job_live_beyond_original_lease(rig,monkeypatch):
    _,job=submit(rig)
    renewed=asyncio.Event()
    actual=rig.queue.renew
    def observe(**kwargs):
        result=actual(**kwargs)
        renewed.set()
        return result
    monkeypatch.setattr(rig.queue,'renew',observe)
    rig.provider.release=asyncio.Event()
    running=asyncio.create_task(rig.bridge.run_once())
    await rig.provider.started.wait()
    rig.clock[0]+=500
    await asyncio.wait_for(renewed.wait(),timeout=2)
    assert rig.queue.get_job(job['id'])['lease_until']==2100
    rig.clock[0]+=200
    rig.provider.release.set()
    assert (await running)['state']=='succeeded'


@pytest.mark.asyncio
async def test_renewal_supervises_separate_terminal_inspection(rig,monkeypatch):
    _,job=submit(rig)
    renewed=asyncio.Event()
    actual=rig.queue.renew
    def observe(**kwargs):
        result=actual(**kwargs)
        renewed.set()
        return result
    monkeypatch.setattr(rig.queue,'renew',observe)
    rig.provider.inspect_release=asyncio.Event()
    running=asyncio.create_task(rig.bridge.run_once())
    await rig.provider.inspect_started.wait()
    rig.clock[0]+=500
    await asyncio.wait_for(renewed.wait(),timeout=2)
    assert rig.queue.get_job(job['id'])['lease_until']==2100
    rig.clock[0]+=200
    rig.provider.inspect_release.set()
    assert (await running)['state']=='succeeded'


@pytest.mark.asyncio
async def test_lease_expiry_preserves_machine_after_provider_drains(rig,monkeypatch):
    _,job=submit(rig)
    inspected=asyncio.Event()
    actual=rig.queue.live_claim
    def observe(**kwargs):
        try:
            return actual(**kwargs)
        finally:
            if rig.clock[0]>1600:
                inspected.set()
    monkeypatch.setattr(rig.queue,'live_claim',observe)
    rig.provider.release=asyncio.Event()
    running=asyncio.create_task(rig.bridge.run_once())
    await rig.provider.started.wait()
    rig.clock[0]+=601
    await asyncio.wait_for(inspected.wait(),timeout=2)
    rig.provider.release.set()
    retained=await running
    assert retained['state']=='claimed' and retained['requires_reconciliation'] is True
    # Read-only status cannot convert expiry into a write. A later admission
    # performs the queue's normal expiry-to-held transition without replay.
    assert await rig.bridge.run_once() is None
    assert rig.queue.get_job(job['id'])['state']=='held' and reservation(rig)==job['id']
    assert len(rig.provider.calls)==1


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_before_propagation_and_serializes(rig):
    _,job=submit(rig)
    rig.provider.release=asyncio.Event()
    running=asyncio.create_task(rig.bridge.run_once())
    await rig.provider.started.wait()
    running.cancel()
    await asyncio.sleep(0)
    running.cancel()
    second=asyncio.create_task(rig.bridge.run_once())
    await asyncio.sleep(0)
    assert not running.done() and not second.done() and reservation(rig)==job['id']
    rig.provider.release.set()
    with pytest.raises(asyncio.CancelledError): await running
    assert await second is None
    assert rig.queue.get_job(job['id'])['state']=='succeeded' and len(rig.provider.calls)==1


@pytest.mark.asyncio
async def test_registry_mutation_during_claim_fails_closed(rig):
    submit(rig)
    original=rig.bridge._target
    def changed(*args):
        rig.queue.actions['unknown.production']=WorkAction(MACHINE,frozenset({'production'}),project='engineering-hub')
        return original(*args)
    rig.bridge._target=changed
    assert await rig.bridge.run_once() is None
    assert not rig.provider.calls


def test_second_provider_composition_refused(rig):
    with pytest.raises(HubError,match='development_ci_composition_required'):
        QueuedDevelopmentCI(rig.queue,rig.owner,rig.manifest,Provider(),action_key=ACTION)


def test_manifest_reusable_and_digest_binds_exact_environment(rig):
    assert rig.manifest.digest!=replace(rig.manifest,profile='python').digest
    assert rig.manifest.digest!=replace(rig.manifest,image_id='sha256:'+'b'*64).digest
    assert 'source_commit' not in rig.manifest.__dataclass_fields__
