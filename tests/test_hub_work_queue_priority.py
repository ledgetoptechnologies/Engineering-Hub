"""Machine-wide queue priority with real SQLite and synthetic admission policy.

No executor or effects are involved. Worker-specific claim admission is not
evidence of another worker's presence, readiness, or ability to run a workload.
"""
from types import SimpleNamespace
import uuid

import pytest

from engineering_hub.repository import HubError, HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget, WorkWorker


STAGING = 'staging-action'
PRODUCTION = 'production-action'
OTHER = 'other-machine-action'
HOST = 'shared-host'
OTHER_HOST = 'other-host'
COMMIT = 'a' * 40
IMAGE = 'sha256:' + 'b' * 64


def worker(agent='codex'):
    return WorkWorker(agent, str(uuid.uuid5(uuid.NAMESPACE_URL, 'priority-' + agent)))


@pytest.fixture
def priority_queue(tmp_path):
    repo = HubRepository(tmp_path / 'priority.sqlite3')
    approvals = {'approved-production': True}
    revoked_targets = set()

    def reviewed(actor, action_key, environment, target):
        task = repo.get_task(target.task_id)
        review = task.get('review') or {}
        return (target.task_id not in revoked_targets and actor == task['owner']
            and task['status'] == 'verified' and task['candidate_commit'] == target.source_commit
            and target.artifact_digest == IMAGE and review.get('actor') != actor
            and review.get('commit') == target.source_commit and review.get('verdict') == 'approve')

    actions = {
        STAGING: WorkAction(HOST, project='engineering-hub', requires_execution_proof=False),
        PRODUCTION: WorkAction(HOST, frozenset({'production'}),
            project='engineering-hub', requires_execution_proof=False),
        OTHER: WorkAction(OTHER_HOST, project='engineering-hub', requires_execution_proof=False),
    }
    queue = WorkQueue(repo, actions, target_verifier=reviewed,
        approval_verifier=lambda actor, action, environment, identity, target:
            approvals.get(identity, False) is True)
    return SimpleNamespace(repo=repo, queue=queue, approvals=approvals,
        revoked_targets=revoked_targets)


def submit(state, key, action=STAGING, approval='approved-production'):
    repo = state.repo
    task = repo.create_task('human:1234', 'engineering-hub', 'Priority ' + key, key)
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], COMMIT)
    task = repo.review_task('claude', task['id'], task['revision'], COMMIT, 'approve')
    target = WorkTarget(task['id'], COMMIT, IMAGE)
    return state.queue.submit(actor='codex', request_id=key, action_key=action,
        environment='production' if action == PRODUCTION else 'staging', target=target,
        approval_id=approval if action == PRODUCTION else None)


def readiness(state, policy):
    previous = state.queue
    state.queue = WorkQueue(state.repo, previous.actions,
        target_verifier=previous.target_verifier, approval_verifier=previous.approval_verifier,
        claim_verifier=previous.claim_verifier, production_readiness_verifier=policy)
    return state.queue


@pytest.mark.parametrize('production_first', [False, True], ids=['staging-older', 'production-older'])
def test_narrow_action_filter_cannot_bypass_policy_eligible_production_on_same_machine(priority_queue, production_first):
    state = priority_queue
    if production_first:
        production = submit(state, 'production', PRODUCTION)
        staging = submit(state, 'staging')
    else:
        staging = submit(state, 'staging')
        production = submit(state, 'production', PRODUCTION)

    # A worker's action filter is a selection bound, not permission to reserve
    # a host ahead of an approved, target-verified production job on that host.
    assert state.queue.claim(worker=worker(), action_keys=(STAGING,)) is None
    assert state.queue.get_job(staging['id'])['state'] == 'queued'
    assert state.queue.get_job(production['id'])['state'] == 'queued'
    claimed = state.queue.claim(worker=worker('hermes'), action_keys=(PRODUCTION,))
    assert claimed['id'] == production['id']


def test_production_priority_does_not_starve_staging_on_another_machine(priority_queue):
    state = priority_queue
    blocked = submit(state, 'same-machine-stage')
    production = submit(state, 'production', PRODUCTION)
    parallel = submit(state, 'other-machine-stage', OTHER)
    claimed = state.queue.claim(worker=worker(), action_keys=(STAGING, OTHER))
    assert claimed['id'] == parallel['id']
    assert claimed['machine_id'] == OTHER_HOST
    assert state.queue.get_job(blocked['id'])['state'] == 'queued'
    assert state.queue.get_job(production['id'])['state'] == 'queued'
    assert state.queue.claim(worker=worker('hermes'), action_keys=(PRODUCTION,))['id'] == production['id']


@pytest.mark.parametrize('invalidated', ['missing-approval', 'revoked-approval', 'revoked-review',
    'revoked-target', 'removed-action', 'changed-machine'])
def test_ineligible_production_does_not_starve_same_machine_staging(priority_queue, invalidated):
    state = priority_queue
    staging = submit(state, 'stage')
    production = submit(state, 'production', PRODUCTION,
        approval=None if invalidated == 'missing-approval' else 'approved-production')
    if invalidated == 'revoked-approval':
        state.approvals['approved-production'] = False
    elif invalidated == 'revoked-review':
        task = state.repo.get_task(production['target']['task_id'])
        state.repo.set_candidate('codex', task['id'], task['revision'], 'c' * 40)
        assert state.repo.get_task(task['id'])['review'] is None
    elif invalidated == 'revoked-target':
        state.revoked_targets.add(production['target']['task_id'])
    elif invalidated == 'removed-action':
        state.queue.actions.pop(PRODUCTION)
    elif invalidated == 'changed-machine':
        state.queue.actions[PRODUCTION] = WorkAction(OTHER_HOST, frozenset({'production'}),
            project='engineering-hub', requires_execution_proof=False)
    claimed = state.queue.claim(worker=worker(), action_keys=(STAGING,))
    assert claimed['id'] == staging['id']
    assert state.queue.get_job(production['id'])['state'] == 'queued'


def test_new_production_does_not_preempt_an_already_claimed_staging_job(priority_queue):
    state = priority_queue
    staging = submit(state, 'stage')
    owner = worker()
    claimed = state.queue.claim(worker=owner, action_keys=(STAGING,))
    production = submit(state, 'production', PRODUCTION)
    assert claimed['id'] == staging['id']
    assert state.queue.claim(worker=worker('hermes'), action_keys=(PRODUCTION,)) is None
    assert state.queue.live_claim(job_id=claimed['id'], worker=owner,
                                  fence=claimed['fence'])['id'] == staging['id']
    state.queue.complete(job_id=claimed['id'], worker=owner, fence=claimed['fence'],
                         outcome='succeeded')
    next_claim = state.queue.claim(worker=worker('hermes'))
    assert next_claim['id'] == production['id']
    assert next_claim['fence'] == claimed['fence'] + 1


def test_worker_specific_admission_is_local_not_a_global_worker_readiness_registry(priority_queue):
    state = priority_queue
    production = submit(state, 'production', PRODUCTION)
    state.queue.claim_verifier = (lambda candidate, action, environment, machine, target:
        candidate.agent == 'hermes')
    # This characterizes the existing public callback contract without
    # inventing whether Hermes is online, enrolled, or available elsewhere.
    assert state.queue.claim(worker=worker('codex')) is None
    assert state.queue.get_job(production['id'])['state'] == 'queued'
    accepted = state.queue.claim(worker=worker('hermes'))
    assert accepted['id'] == production['id']


@pytest.mark.parametrize('provider', ['absent', 'ready'])
@pytest.mark.parametrize('filtered', [False, True], ids=['all-actions', 'staging-only'])
def test_worker_local_production_denial_cannot_authorize_same_host_staging(priority_queue, provider, filtered):
    state = priority_queue
    staging = submit(state, 'stage')
    production = submit(state, 'production', PRODUCTION)
    state.queue.claim_verifier = (lambda candidate, action, environment, machine, target:
        candidate.agent == ('hermes' if environment == 'production' else 'codex'))
    if provider == 'ready':
        readiness(state, lambda *_: True)
    # An absent global provider means unknown, not unavailable. The worker's
    # personal inability to claim production is not a machine-wide bypass.
    assert state.queue.claim(worker=worker('codex'), action_keys=(STAGING,) if filtered else None) is None
    assert state.queue.get_job(staging['id'])['state'] == 'queued'
    assert state.queue.claim(worker=worker('hermes'), action_keys=(PRODUCTION,))['id'] == production['id']


def test_explicit_unavailable_production_is_not_claimed_and_releases_staging_priority_barrier(priority_queue):
    state = priority_queue
    staging = submit(state, 'stage')
    production = submit(state, 'production', PRODUCTION)
    seen = []
    def unavailable(*args):
        seen.append(args)
        return False
    readiness(state, unavailable)
    assert state.queue.claim(worker=worker('hermes'), action_keys=(PRODUCTION,)) is None
    assert state.queue.claim(worker=worker(), action_keys=(STAGING,))['id'] == staging['id']
    assert state.queue.get_job(production['id'])['state'] == 'queued'
    expected_target = WorkTarget(**production['target'])
    assert seen and all(args == ('codex', PRODUCTION, 'production', HOST, expected_target) for args in seen)


@pytest.mark.parametrize('result', [None, 0, 1, 'ready', {}, []])
def test_configured_unknown_readiness_denies_new_production_and_same_host_staging_but_not_other_host(priority_queue, result):
    state = priority_queue
    staging = submit(state, 'stage')
    production = submit(state, 'production', PRODUCTION)
    parallel = submit(state, 'parallel', OTHER)
    readiness(state, lambda *_: result)
    assert state.queue.claim(worker=worker('hermes'), action_keys=(PRODUCTION,)) is None
    assert state.queue.claim(worker=worker(), action_keys=(STAGING,)) is None
    assert state.queue.claim(worker=worker(), action_keys=(STAGING, OTHER))['id'] == parallel['id']
    assert state.queue.get_job(staging['id'])['state'] == 'queued'
    assert state.queue.get_job(production['id'])['state'] == 'queued'


def test_readiness_exception_is_unknown_not_unavailable_or_public_error(priority_queue):
    state = priority_queue
    staging = submit(state, 'stage')
    production = submit(state, 'production', PRODUCTION)
    def broken(*_):
        raise RuntimeError('private-readiness-error-not-public')
    readiness(state, broken)
    assert state.queue.claim(worker=worker(), action_keys=(STAGING,)) is None
    assert state.queue.claim(worker=worker('hermes'), action_keys=(PRODUCTION,)) is None
    assert state.queue.get_job(staging['id'])['state'] == 'queued'
    assert state.queue.get_job(production['id'])['state'] == 'queued'


def test_readiness_true_cannot_grant_current_worker_admission(priority_queue):
    state = priority_queue
    staging = submit(state, 'stage')
    production = submit(state, 'production', PRODUCTION)
    state.queue.claim_verifier = lambda *_: False
    readiness(state, lambda *_: True)
    assert state.queue.claim(worker=worker('hermes'), action_keys=(PRODUCTION,)) is None
    assert state.queue.claim(worker=worker(), action_keys=(STAGING,)) is None
    assert state.queue.get_job(staging['id'])['state'] == 'queued'
    assert state.queue.get_job(production['id'])['state'] == 'queued'


@pytest.mark.parametrize('missing', ['approval', 'review'])
def test_readiness_true_cannot_grant_missing_approval_or_current_independent_review(priority_queue, missing):
    state = priority_queue
    staging = submit(state, 'stage')
    production = submit(state, 'production', PRODUCTION,
        approval=None if missing == 'approval' else 'approved-production')
    if missing == 'review':
        task = state.repo.get_task(production['target']['task_id'])
        state.repo.set_candidate('codex', task['id'], task['revision'], 'c' * 40)
    readiness(state, lambda *_: True)
    assert state.queue.claim(worker=worker('hermes'), action_keys=(PRODUCTION,)) is None
    assert state.queue.claim(worker=worker(), action_keys=(STAGING,))['id'] == staging['id']
    assert state.queue.get_job(production['id'])['state'] == 'queued'


@pytest.mark.parametrize('later_readiness', [True, False, None])
def test_readiness_changes_and_arriving_production_do_not_preempt_or_revoke_running_staging(priority_queue, later_readiness):
    state = priority_queue
    readiness(state, lambda *_: True)
    staging = submit(state, 'stage')
    owner = worker()
    claimed = state.queue.claim(worker=owner, action_keys=(STAGING,))
    assert claimed['id'] == staging['id']
    production = submit(state, 'production', PRODUCTION)
    state.queue.production_readiness_verifier = lambda *_: later_readiness
    assert state.queue.claim(worker=worker('hermes'), action_keys=(PRODUCTION,)) is None
    assert state.queue.live_claim(job_id=claimed['id'], worker=owner,
                                  fence=claimed['fence'])['id'] == staging['id']
    renewed = state.queue.renew(job_id=claimed['id'], worker=owner,
                                fence=claimed['fence'], lease_seconds=120)
    assert renewed['id'] == staging['id'] and renewed['fence'] == claimed['fence']
    assert state.queue.get_job(production['id'])['state'] == 'queued'


@pytest.mark.parametrize('policy', [False, 1, 'ready', object()])
def test_noncallable_readiness_configuration_rejected_without_queue_tables(tmp_path, policy):
    repo = HubRepository(tmp_path / 'invalid-priority.sqlite3')
    with pytest.raises(HubError):
        WorkQueue(repo, {STAGING: WorkAction(HOST, project='engineering-hub')},
                  production_readiness_verifier=policy)
    with repo._connection() as database:
        assert database.execute("SELECT name FROM sqlite_master WHERE name LIKE 'hub_work_%'").fetchall() == []


def test_accidentally_async_readiness_is_unknown_and_unstarted_coroutine_is_closed(priority_queue):
    state = priority_queue
    staging = submit(state, 'stage')
    production = submit(state, 'production', PRODUCTION)
    returned = []
    async def result():
        return True
    def misdeclared(*_):
        coroutine = result()
        returned.append(coroutine)
        return coroutine
    readiness(state, misdeclared)
    try:
        assert state.queue.claim(worker=worker(), action_keys=(STAGING,)) is None
        assert state.queue.claim(worker=worker('hermes'), action_keys=(PRODUCTION,)) is None
        assert returned and all(coroutine.cr_frame is None for coroutine in returned)
        assert state.queue.get_job(staging['id'])['state'] == 'queued'
        assert state.queue.get_job(production['id'])['state'] == 'queued'
    finally:
        for coroutine in returned:
            coroutine.close()
