"""Server-owned executor admission, not additional bearer authentication."""
from uuid import uuid4

import pytest

from engineering_hub.repository import HubError, HubRepository
from engineering_hub.work_queue import ClosedClaimPolicy, WorkAction, WorkQueue, WorkTarget, WorkWorker
from engineering_hub.work_queue_config import configured_work_queue


@pytest.fixture
def rig(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    task = repo.create_task('human:42', 'engineering-hub', 'Mock reviewed admission test', str(uuid4()))
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], 'a' * 40)
    task = repo.review_task('claude', task['id'], task['revision'], 'a' * 40, 'approve')
    target = WorkTarget(task['id'], 'a' * 40, 'sha256:' + 'b' * 64)
    worker = WorkWorker('codex', str(uuid4()))
    return repo, target, worker


def submit(queue, target, *, request='one', action='engineering-hub.validate', environment='staging', approval_id=None):
    return queue.submit(actor='codex', request_id=request, action_key=action,
                        environment=environment, target=target, approval_id=approval_id)


def test_flag_factory_alone_never_reserves_machine(rig):
    repo, target, worker = rig
    queue = configured_work_queue(repo)
    submitted = submit(queue, target)
    with repo._connection() as db:
        before = db.execute('SELECT COUNT(*) FROM hub_outbox').fetchone()[0]
    for agent in ('codex', 'hermes', 'claude'):
        assert queue.claim(worker=WorkWorker(agent, str(uuid4()))) is None
    status = queue.status()
    assert status['claim_admission_state'] == 'unconfigured'
    job = status['jobs'][0]
    assert job['state'] == 'queued' and job['id'] == submitted['id']
    assert not job['eligible'] and job['blocked_reason'] == 'admission_not_configured'
    with repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_work_machines').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM hub_outbox').fetchone()[0] == before


def test_explicit_trusted_policy_binds_worker_action_environment_and_machine(rig):
    repo, target, worker = rig
    calls = []
    def policy(actual, action, environment, machine, bound_target):
        calls.append((actual, action, environment, machine, bound_target))
        return actual == worker and action == 'engineering-hub.validate' and environment == 'staging' and machine == 'staging-pc' and bound_target == target
    queue = configured_work_queue(repo, claim_verifier=policy)
    submitted = submit(queue, target)
    assert queue.claim(worker=WorkWorker('codex', str(uuid4()))) is None
    assert queue.claim(worker=WorkWorker('hermes', worker.instance_id)) is None
    job = queue.claim(worker=worker)
    assert job['id'] == submitted['id']
    assert calls[-1] == (worker, 'engineering-hub.validate', 'staging', 'staging-pc', target)
    assert queue.live_claim(job_id=job['id'], worker=worker, fence=job['fence'])['id'] == job['id']
    queue.claim_verifier = ClosedClaimPolicy()
    with pytest.raises(HubError, match='work_admission_changed'):
        queue.live_claim(job_id=job['id'], worker=worker, fence=job['fence'])


@pytest.mark.parametrize('returned', [False, None, 1, 'yes', {}, []])
def test_claim_policy_must_return_exact_true(rig, returned):
    repo, target, worker = rig
    queue = configured_work_queue(repo, claim_verifier=lambda *args: returned)
    submit(queue, target)
    assert queue.claim(worker=worker) is None


def test_claim_policy_failure_is_closed(rig):
    repo, target, worker = rig
    def broken(*args):
        raise RuntimeError('not included in status or response')
    queue = configured_work_queue(repo, claim_verifier=broken)
    submit(queue, target)
    assert queue.claim(worker=worker) is None


def test_worker_specific_status_is_not_an_assertion_of_caller_eligibility(rig):
    repo, target, worker = rig
    queue = configured_work_queue(repo, claim_verifier=lambda actual, *args: actual == worker)
    submit(queue, target)
    status = queue.status()
    assert status['claim_admission_state'] == 'worker_policy'
    assert status['jobs'][0]['eligible_requires_worker_admission']


def test_action_filter_only_narrows_server_admission_and_priority(rig):
    repo, target, worker = rig
    actions = {name: WorkAction('staging-pc', frozenset({'production', 'staging'}),
                                project='engineering-hub') for name in ('first', 'second')}
    queue = WorkQueue(repo, actions, target_verifier=lambda *args: True,
                      approval_verifier=lambda *args: True,
                      claim_verifier=lambda actual, action, *args: actual == worker and action == 'second')
    submit(queue, target, action='first', environment='production', approval_id='synthetic')
    stage = submit(queue, target, request='two', action='second')
    assert queue.claim(worker=worker, action_keys=('first',)) is None
    assert queue.claim(worker=WorkWorker('claude', str(uuid4())), action_keys=('second',)) is None
    assert queue.claim(worker=worker, action_keys=('second',))['id'] == stage['id']


@pytest.mark.parametrize('action_keys', [(), [], ['engineering-hub.validate'],
                                       ('engineering-hub.validate',) * 2, ('x',) * 33,
                                       (True,), ('../../x',)])
def test_invalid_action_filters_rejected_before_mutation(rig, action_keys):
    repo, _, worker = rig
    queue = configured_work_queue(repo)
    with pytest.raises(HubError):
        queue.claim(worker=worker, action_keys=action_keys)


def test_unknown_action_filter_does_not_grant_capability(rig):
    repo, _, worker = rig
    queue = configured_work_queue(repo)
    with pytest.raises(HubError, match='work_action_not_registered'):
        queue.claim(worker=worker, action_keys=('unknown',))


def test_production_requires_separate_approval_even_with_worker_policy(rig):
    repo, target, worker = rig
    queue = configured_work_queue(repo, claim_verifier=lambda *args: True)
    submit(queue, target, environment='production')
    assert queue.claim(worker=worker) is None
    assert queue.status()['jobs'][0]['blocked_reason'] == 'production_approval_required'


def test_fixed_worker_enrollment_does_not_claim_other_target_or_future_commit(rig):
    repo, target, worker = rig
    # Synthetic target verification allows all shapes only to isolate the
    # independent fixed-worker admission boundary in this unit test.
    queue = WorkQueue(repo, {'validate': WorkAction('staging-pc', project='engineering-hub')},
                      target_verifier=lambda *args: True,
                      claim_verifier=lambda actual, action, env, machine, enrolled:
                          actual == worker and enrolled == target)
    variants = (WorkTarget(str(uuid4()), target.source_commit, target.artifact_digest),
                WorkTarget(target.task_id, 'c' * 40, target.artifact_digest),
                WorkTarget(target.task_id, target.source_commit, 'sha256:' + 'd' * 64))
    for position, other in enumerate(variants):
        submit(queue, other, request='different-' + str(position), action='validate')
    assert queue.claim(worker=worker) is None
    expected = submit(queue, target, request='exact', action='validate')
    claim = queue.claim(worker=worker)
    assert claim['id'] == expected['id']
    queue.claim_verifier = lambda actual, action, env, machine, enrolled: enrolled == variants[0]
    with pytest.raises(HubError, match='work_admission_changed'):
        queue.live_claim(job_id=claim['id'], worker=worker, fence=claim['fence'])
