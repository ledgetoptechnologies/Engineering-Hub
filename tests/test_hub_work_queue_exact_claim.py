"""Read-only exact selection plans are metadata, never executor capabilities."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import os

import pytest

from engineering_hub.repository import HubError
from engineering_hub.work_queue import WorkAction, WorkClaimPlan, WorkQueue
from tests.test_hub_work_queue import setup_queue, submit, finish, w


def database_snapshot(repo):
    with repo._connection() as db:
        return tuple(db.iterdump())


def test_plan_is_frozen_stable_read_only_and_returns_safe_copies(setup_queue):
    queue, _, repo, _ = setup_queue
    job = submit(queue)
    before = database_snapshot(repo)
    worker = w('codex')
    plan = queue.plan_claim(worker=worker)
    assert type(plan) is WorkClaimPlan and plan.queue is queue and plan.process_id == os.getpid()
    assert plan.job_id == job['id'] and plan.job == job and plan.machine_generation is None
    assert queue.plan_claim(worker=worker).binding_digest == plan.binding_digest
    assert plan.binding_digest.startswith('sha256:') and len(plan.binding_digest) == 71
    with pytest.raises(FrozenInstanceError):
        plan.process_id = 0
    raw = plan.raw_job
    raw['state'] = 'claimed'
    public = plan.job
    public['target']['source_commit'] = 'f' * 40
    assert plan.raw_job['state'] == 'queued' and plan.job == job
    assert database_snapshot(repo) == before
    claimed = queue.claim_exact(plan, worker=worker, lease_seconds=10)
    assert claimed['id'] == job['id'] and claimed['fence'] == 1
    assert queue.claim_exact(plan, worker=worker) is None


@pytest.mark.parametrize('change', ['worker', 'process', 'queue', 'queue-generation', 'generation-type'])
def test_mismatched_plan_identity_never_reaches_claim_callback(setup_queue, change):
    queue, _, repo, actions = setup_queue
    submit(queue)
    worker = w('codex')
    plan = queue.plan_claim(worker=worker)
    if change == 'worker': worker = w('hermes')
    elif change == 'process': plan = replace(plan, process_id=plan.process_id + 1)
    elif change == 'queue': plan = replace(plan, queue=WorkQueue(repo, actions, target_verifier=lambda *_: True))
    elif change == 'queue-generation':
        digest = plan.binding_digest
        queue._claim_plan_identity = 'changed-local-queue-instance'
        assert plan.binding_digest == digest
    else: plan = replace(plan, machine_generation=True)
    queue.claim_verifier = lambda *_: pytest.fail('Invalid plan must fail before callbacks')
    with pytest.raises(HubError, match='invalid_work_claim_plan'):
        queue.claim_exact(plan, worker=worker)
    assert queue.get_job(plan.job_id)['state'] == 'queued'


def test_reconstructed_plan_is_not_authority_and_callback_runs_again(setup_queue):
    queue, _, _, _ = setup_queue
    submit(queue)
    calls = []
    allowed = [True]
    def verifier(*args):
        calls.append(args)
        return allowed[0]
    queue.claim_verifier = verifier
    worker = w('codex')
    plan = queue.plan_claim(worker=worker)
    copy = replace(plan)
    assert copy is not plan and copy.binding_digest == plan.binding_digest and len(calls) == 1
    allowed[0] = False
    assert queue.claim_exact(copy, worker=worker) is None
    assert len(calls) == 2 and queue.get_job(plan.job_id)['state'] == 'queued'


@pytest.mark.parametrize('change', ['new-production', 'raw-revision', 'pause', 'target-policy', 'callback-policy'])
def test_changed_selection_claims_nothing_not_another_candidate(setup_queue, change):
    queue, _, repo, _ = setup_queue
    first, second = submit(queue), submit(queue, 'second')
    worker = w('codex')
    plan = queue.plan_claim(worker=worker)
    assert plan.job_id == first['id']
    if change == 'new-production':
        production = submit(queue, 'production', 'production', approval_id='approved')
    elif change == 'raw-revision':
        with repo._transaction() as db:
            db.execute('UPDATE hub_work_jobs SET revision=revision+1 WHERE id=?', (first['id'],))
    elif change == 'pause': queue.set_paused(True)
    elif change == 'target-policy':
        queue.target_verifier = lambda actor, action, env, target: target.task_id != first['target']['task_id']
    else:
        queue.claim_verifier = lambda worker, action, env, machine, target: target.task_id != first['target']['task_id']
    assert queue.claim_exact(plan, worker=worker) is None
    assert queue.get_job(first['id'])['state'] == queue.get_job(second['id'])['state'] == 'queued'
    if change == 'new-production':
        assert queue.get_job(production['id'])['state'] == 'queued'


def test_machine_generation_reuse_invalidates_plan_even_when_selected_job_unchanged(setup_queue):
    queue, _, _, _ = setup_queue
    queue.actions['other'] = WorkAction('machine-one', project='engineering-hub', requires_execution_proof=False)
    first = submit(queue)
    worker = w('codex')
    plan = queue.plan_claim(worker=worker, action_keys=('validate',))
    other = submit(queue, 'other-job', action='other')
    claimed = queue.claim(worker=w('hermes'), action_keys=('other',))
    assert claimed['id'] == other['id']
    finish(queue, claimed)
    assert queue.claim_exact(plan, worker=worker) is None
    new_plan = queue.plan_claim(worker=worker, action_keys=('validate',))
    assert new_plan.job_id == first['id'] and new_plan.machine_generation == 1
    assert new_plan.binding_digest != plan.binding_digest
    assert queue.claim_exact(new_plan, worker=worker)['fence'] == 2


def test_production_unknown_blocks_filtered_staging_and_false_readiness_does_not(setup_queue):
    queue, _, _, _ = setup_queue
    stage = submit(queue)
    submit(queue, 'production', 'production', approval_id='approved')
    queue.claim_verifier = lambda worker, action, env, machine, target: env == 'staging'
    worker = w('codex')
    assert queue.plan_claim(worker=worker, action_keys=('validate',)) is None
    ready = [False]
    queue.production_readiness_verifier = lambda *_: ready[0]
    plan = queue.plan_claim(worker=worker, action_keys=('validate',))
    assert plan.job_id == stage['id']
    ready[0] = None
    assert queue.claim_exact(plan, worker=worker) is None
    ready[0] = False
    assert queue.claim_exact(plan, worker=worker)['id'] == stage['id']


def test_plan_does_not_expire_or_release_other_claims(setup_queue):
    queue, clock, repo, _ = setup_queue
    old = submit(queue)
    claim = queue.claim(worker=w('hermes'), lease_seconds=1)
    clock[0] += 2
    free = submit(queue, 'free-machine', action='other')
    before = database_snapshot(repo)
    plan = queue.plan_claim(worker=w('codex'))
    assert plan.job_id == free['id']
    assert database_snapshot(repo) == before
    assert queue.get_job(old['id'])['state'] == 'claimed'
    assert queue.claim_exact(plan, worker=w('codex'))['id'] == free['id']
    assert queue.get_job(old['id'])['state'] == 'held'
    with repo._connection() as db:
        assert db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?', (claim['machine_id'],)).fetchone()[0] == old['id']


def test_competing_exact_claims_reserve_once_without_fallthrough(setup_queue):
    queue, _, _, _ = setup_queue
    first, second = submit(queue), submit(queue, 'second')
    worker = w('codex')
    plan = queue.plan_claim(worker=worker)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: queue.claim_exact(plan, worker=worker), range(2)))
    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1 and claimed[0]['id'] == first['id']
    assert queue.get_job(second['id'])['state'] == 'queued'


@pytest.mark.parametrize('filters', [(), [], ('unknown',), ('validate', 'validate')])
def test_invalid_filters_are_not_preview_bypasses(setup_queue, filters):
    queue, _, _, _ = setup_queue
    with pytest.raises(HubError):
        queue.plan_claim(worker=w('codex'), action_keys=filters)
