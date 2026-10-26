"""Shared-family machine precedence with real SQLite; no executor effects."""
from types import SimpleNamespace
import uuid

import pytest

from engineering_hub.execution_policy import SharedExecutionPolicies
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget, WorkWorker

PROD, STAGE, OTHER, AUX = 'pilot.production', 'modular.stage', 'modular.other', 'pilot.aux'
HOST, ELSEWHERE = 'shared-host', 'other-host'


def rig(tmp_path, *, ready=True, approval=True, bind_production=True, approval_id="approval", changed_durable_action=False):
    repo = HubRepository(tmp_path / 'priority.sqlite3')
    values = dict(ready=ready, approval=approval, target=True)
    def answer(value):
        if isinstance(value, Exception):
            raise value
        return value
    def target(*args):
        return answer(values['target']) if args[1] == PROD else True
    def approvals(*args):
        return answer(values['approval'])
    def readiness(*args):
        return answer(values['ready'])
    actions = {
        PROD: WorkAction(HOST, frozenset({'production'}), project='engineering-hub'),
        STAGE: WorkAction(HOST, project='engineering-hub'),
        OTHER: WorkAction(ELSEWHERE, project='engineering-hub'),
        AUX: WorkAction(HOST, project='engineering-hub'),
    }
    q = WorkQueue(repo, actions, target_verifier=target,
                  approval_verifier=None if approval == "absent" else approvals,
                  production_readiness_verifier=None if ready == 'absent' else readiness)
    jobs = {}
    # Submit before installing the private composition; admission remains real.
    for key in (STAGE, PROD, OTHER):
        task = repo.create_task('human:1234', 'engineering-hub', key, key)
        task = repo.claim_task('codex', task['id'], task['revision'])
        task = repo.set_candidate('codex', task['id'], task['revision'], 'a' * 40)
        task = repo.review_task('claude', task['id'], task['revision'], 'a' * 40, 'approve')
        jobs[key] = q.submit(actor='codex', request_id=key, action_key=key,
            environment='production' if key == PROD else 'staging',
            target=WorkTarget(task['id'], 'a' * 40, 'sha256:' + 'b' * 64),
            approval_id=approval_id if key == PROD else None)
    pilot = WorkWorker('executor:container-pilot', str(uuid.uuid4()))
    modular = WorkWorker('executor:staging-service', str(uuid.uuid4()))
    if changed_durable_action:
        q.actions[PROD] = WorkAction(ELSEWHERE, frozenset({'production'}), project='engineering-hub')
    policy = SharedExecutionPolicies(repo, q)
    policy.bind('pilot', {k: q.actions[k] for k in ((PROD, AUX) if bind_production else (AUX,))},
                pilot, target=target, claim=lambda *_: True,
                completion=lambda *_: True, reconciliation=lambda *_: True)
    policy.bind('modular', {k: q.actions[k] for k in (STAGE, OTHER)}, modular,
                target=target, claim=lambda *_: True,
                completion=lambda *_: True, reconciliation=lambda *_: True)
    policy.install()
    policy.ready('pilot'); policy.ready('modular')
    if bind_production:
        policy.open()
    return SimpleNamespace(repo=repo, queue=q, policy=policy, values=values,
                           jobs=jobs, pilot=pilot, modular=modular)


@pytest.mark.parametrize('ready', [True, None, 0, 'ready', RuntimeError('private'), 'absent'])
@pytest.mark.parametrize('filtered', [True, False])
@pytest.mark.parametrize('target', [False, None, RuntimeError('private-target')])
def test_shared_target_denial_or_unknown_never_bypasses_production(tmp_path, ready, filtered, target):
    state = rig(tmp_path, ready=ready)
    state.values['target'] = target
    claimed = state.queue.claim(worker=state.modular, action_keys=(STAGE,) if filtered else None)
    if filtered:
        assert claimed is None
    else:
        assert claimed['id'] == state.jobs[OTHER]['id']
    # Restrict to this machine so an elsewhere claim cannot obscure the barrier.
    if filtered:
        assert state.queue.get_job(state.jobs[STAGE]['id'])['state'] == 'queued'
    else:
        assert state.queue.get_job(state.jobs[OTHER]['id'])['state'] == 'claimed'
    assert state.queue.get_job(state.jobs[PROD]['id'])['state'] == 'queued'
    assert state.queue.get_job(state.jobs[STAGE]['id'])['state'] == 'queued'


@pytest.mark.parametrize('approval', [None, 0, 1, 'yes', RuntimeError('approval-private')])
def test_unknown_approval_preserves_barrier_even_global_unavailable(tmp_path, approval):
    state = rig(tmp_path, ready=False)
    state.values['approval'] = approval
    assert state.queue.claim(worker=state.modular, action_keys=(STAGE,)) is None
    assert state.queue.claim(worker=state.pilot, action_keys=(PROD,)) is None
    assert state.queue.claim(worker=state.modular, action_keys=(OTHER,))['id'] == state.jobs[OTHER]['id']


@pytest.mark.parametrize('ready', [True, False, None])
def test_exact_revoked_approval_releases_priority_without_claiming_production(tmp_path, ready):
    state = rig(tmp_path, ready=ready)
    state.values['approval'] = False
    assert state.queue.claim(worker=state.pilot, action_keys=(PROD,)) is None
    assert state.queue.claim(worker=state.modular, action_keys=(STAGE,))['id'] == state.jobs[STAGE]['id']


def test_exact_global_unavailability_releases_only_bound_production(tmp_path):
    state = rig(tmp_path, ready=False)
    state.values['target'] = False
    assert state.queue.claim(worker=state.pilot, action_keys=(PROD,)) is None
    assert state.queue.claim(worker=state.modular, action_keys=(STAGE,))['id'] == state.jobs[STAGE]['id']


@pytest.mark.parametrize('approval', [True, False])
def test_unbound_production_retains_barrier_even_global_unavailable_or_revoked(tmp_path, approval):
    state = rig(tmp_path, ready=False, bind_production=False)
    state.values['approval'] = approval
    assert state.queue.claim(worker=state.modular, action_keys=(STAGE,)) is None
    # Incomplete registry cannot open any family's admissions.
    assert state.queue.claim(worker=state.modular, action_keys=(OTHER,)) is None


def test_shared_ready_production_claims_then_reservation_excludes_other_family(tmp_path):
    state = rig(tmp_path)
    assert state.queue.claim(worker=state.modular, action_keys=(STAGE,)) is None
    claim = state.queue.claim(worker=state.pilot, action_keys=(PROD,))
    assert claim['id'] == state.jobs[PROD]['id']
    assert state.queue.claim(worker=state.modular, action_keys=(STAGE,)) is None
    state.queue.complete(job_id=claim['id'], worker=state.pilot, fence=claim['fence'],
                         outcome='succeeded', evidence_id='pilot-proof')
    assert state.queue.claim(worker=state.modular, action_keys=(STAGE,))['id'] == state.jobs[STAGE]['id']


@pytest.mark.parametrize('tamper', ['none-claim', 'none-target', 'foreign-registry', 'none-registry', 'deleted-registry', 'action'])
def test_shared_tamper_cannot_revert_to_standalone_admission(tmp_path, tamper):
    state = rig(tmp_path, ready=False)
    if tamper == 'none-claim': state.queue.claim_verifier = None
    elif tamper == 'none-target': state.queue.target_verifier = None
    elif tamper == 'foreign-registry': state.queue._shared_execution_policies = object()
    elif tamper == 'none-registry': state.queue._shared_execution_policies = None
    elif tamper == 'deleted-registry': del state.queue._shared_execution_policies
    else: state.queue.actions[PROD] = WorkAction(ELSEWHERE, frozenset({'production'}), project='engineering-hub')
    assert state.queue.claim(worker=state.modular, action_keys=(STAGE,)) is None
    assert state.queue.claim(worker=state.modular, action_keys=(OTHER,)) is None


@pytest.mark.parametrize('callback', ['completion_verifier', 'reconciliation_verifier'])
def test_shared_tamper_preserves_owned_reservation(tmp_path, callback):
    state = rig(tmp_path)
    claim = state.queue.claim(worker=state.pilot, action_keys=(PROD,))
    if callback == 'reconciliation_verifier':
        state.queue.complete(job_id=claim['id'], worker=state.pilot, fence=claim['fence'], outcome='held')
    setattr(state.queue, callback, None)
    with pytest.raises(HubError):
        if callback == 'completion_verifier':
            state.queue.complete(job_id=claim['id'], worker=state.pilot, fence=claim['fence'],
                                 outcome='succeeded', evidence_id='proof')
        else:
            state.queue.reconcile(job_id=claim['id'], outcome='succeeded', evidence_id='proof')
    assert state.queue.claim(worker=state.modular, action_keys=(STAGE,)) is None
    assert state.queue.get_job(claim['id'])['state'] == ('claimed' if callback == 'completion_verifier' else 'held')


@pytest.mark.parametrize('missing', ['identity', 'provider'])
def test_shared_missing_approval_is_unknown_not_explicit_revocation(tmp_path, missing):
    state = rig(tmp_path, ready=False,
        approval='absent' if missing == 'provider' else True,
        approval_id=None if missing == 'identity' else 'approval')
    assert state.queue.claim(worker=state.pilot, action_keys=(PROD,)) is None
    assert state.queue.claim(worker=state.modular, action_keys=(STAGE,)) is None
    assert state.queue.claim(worker=state.modular, action_keys=(OTHER,))['id'] == state.jobs[OTHER]['id']


def test_shared_policy_close_preserves_bound_original_completion(tmp_path):
    state = rig(tmp_path)
    claim = state.queue.claim(worker=state.pilot, action_keys=(PROD,))
    state.policy.close()
    assert state.queue.claim(worker=state.modular, action_keys=(STAGE,)) is None
    completed = state.queue.complete(job_id=claim['id'], worker=state.pilot,
        fence=claim['fence'], outcome='succeeded', evidence_id='original-pilot-proof')
    assert completed['state'] == 'succeeded'


def test_shared_live_claim_rechecks_callback_integrity(tmp_path):
    state = rig(tmp_path)
    claim = state.queue.claim(worker=state.pilot, action_keys=(PROD,))
    state.queue.claim_verifier = None
    with pytest.raises(HubError, match='work_admission_changed'):
        state.queue.live_claim(job_id=claim['id'], worker=state.pilot, fence=claim['fence'])
    assert state.queue.get_job(claim['id'])['state'] == 'claimed'


def test_durable_production_action_mismatch_keeps_original_machine_barrier(tmp_path):
    state = rig(tmp_path, ready=False, changed_durable_action=True)
    assert state.queue.claim(worker=state.modular, action_keys=(STAGE,)) is None
    assert state.queue.claim(worker=state.modular, action_keys=(OTHER,))['id'] == state.jobs[OTHER]['id']
    assert state.queue.get_job(state.jobs[PROD]['id'])['state'] == 'queued'
