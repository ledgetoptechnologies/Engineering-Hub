"""Real ledger/queue policy isolation; no executor or host effects."""
from dataclasses import FrozenInstanceError
from types import MethodType
import uuid

import pytest

from engineering_hub.execution_policy import SharedExecutionPolicies
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.work_queue import (ClosedClaimPolicy, CONTAINER_PILOT_EXECUTOR,
    STAGING_SERVICE_EXECUTOR, WorkAction, WorkQueue, WorkTarget, WorkWorker)


class Family:
    def __init__(self, name):
        self.name, self.calls, self.answer = name, [], True
    def target(self, *args):
        self.calls.append(('target', args))
        return self.answer
    def claim(self, *args):
        self.calls.append(('claim', args))
        return self.answer
    def completion(self, job, evidence, outcome):
        self.calls.append(('completion', job['id']))
        return self.answer if evidence == self.name else False
    def reconciliation(self, job, evidence, outcome):
        self.calls.append(('reconciliation', job['id']))
        return self.answer if evidence == self.name else False


def worker(agent):
    return WorkWorker(agent, str(uuid.uuid4()))


def target():
    return WorkTarget(str(uuid.uuid4()), 'a' * 40, 'sha256:' + 'b' * 64)


@pytest.fixture
def system(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    clock = [1000.0]
    actions = {n: WorkAction('staging-pc', project='engineering-hub') for n in ('pilot', 'modular')}
    queue = WorkQueue(repo, actions, claim_verifier=ClosedClaimPolicy(), clock=lambda: clock[0])
    registry = SharedExecutionPolicies(repo, queue)
    families = {name: Family(name) for name in actions}
    workers = {'pilot': worker(CONTAINER_PILOT_EXECUTOR), 'modular': worker(STAGING_SERVICE_EXECUTOR)}
    bindings = {}
    for name, family in families.items():
        bindings[name] = registry.bind(name, {name: queue.actions[name]}, workers[name],
            target=family.target, claim=family.claim, completion=family.completion,
            reconciliation=family.reconciliation)
    registry.install()
    return repo, queue, registry, families, workers, bindings, clock


def activate(system):
    registry = system[2]
    registry.ready('pilot'); registry.ready('modular'); registry.open()


def submit(system, family='pilot'):
    return system[1].submit(actor='codex', request_id=str(uuid.uuid4()),
        action_key=family, environment='staging', target=target())


def test_closed_startup_requires_both_recoveries_before_queue_admission(system):
    _, queue, registry, families, workers, bindings, _ = system
    assert queue._shared_execution_policies is registry and queue._shared_execution_required is True
    assert registry.assert_integrity() is None
    assert registry.action_bound('pilot') is True
    for name in ('pilot', 'modular'):
        assert queue.target_verifier('codex', name, 'staging', target()) is False
        assert queue.claim_verifier(workers[name], name, 'staging', 'staging-pc', target()) is False
        with pytest.raises(HubError, match='execution_policy_admission_closed'):
            registry.assert_binding(name, bindings[name], admission=True)
    registry.ready('pilot')
    with pytest.raises(HubError, match='execution_policy_not_ready'):
        registry.open()
    registry.ready('modular'); registry.open()
    assert registry.assert_binding('modular', bindings['modular'], admission=True) is bindings['modular']
    assert submit(system)['action_key'] == 'pilot'


def test_stable_dispatchers_and_disjoint_immutable_bindings(system):
    _, queue, registry, _, _, bindings, _ = system
    assert registry.claim_verifier is registry.claim_verifier is queue.claim_verifier
    with pytest.raises(TypeError):
        bindings['pilot'].actions['foreign'] = queue.actions['modular']
    with pytest.raises(FrozenInstanceError):
        bindings['pilot'].claim = lambda *args: True
    with pytest.raises(HubError):
        registry.install()


def test_action_overlap_refused_before_install(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    queue = WorkQueue(repo, {'one': WorkAction('machine', project='engineering-hub')})
    registry = SharedExecutionPolicies(repo, queue)
    handler = Family('one')
    kwargs = dict(target=handler.target, claim=handler.claim,
                  completion=handler.completion, reconciliation=handler.reconciliation)
    registry.bind('pilot', dict(queue.actions), worker(CONTAINER_PILOT_EXECUTOR), **kwargs)
    with pytest.raises(HubError, match='execution_policy_action_overlap'):
        registry.bind('modular', dict(queue.actions), worker(STAGING_SERVICE_EXECUTOR), **kwargs)
    with pytest.raises(HubError, match='execution_policy_not_ready'):
        registry.install()
    assert queue.claim_verifier is None
    assert registry.claim_verifier(worker(CONTAINER_PILOT_EXECUTOR), 'one', 'staging', 'machine', target()) is False


def test_other_repository_is_rejected(system, tmp_path):
    with pytest.raises(HubError, match='invalid_execution_policy_queue'):
        SharedExecutionPolicies(HubRepository(tmp_path / 'other.db'), system[1])


@pytest.mark.parametrize('value', [None, 1, 'yes', [], object()])
def test_non_boolean_handlers_deny_without_fallback(system, value):
    activate(system)
    _, queue, _, families, workers, _, _ = system
    families['pilot'].answer = value
    assert queue.target_verifier('codex', 'pilot', 'staging', target()) is False
    assert queue.claim_verifier(workers['pilot'], 'pilot', 'staging', 'staging-pc', target()) is False
    assert families['modular'].calls == []


@pytest.mark.parametrize('value', [None, 1, 'yes', [], object()])
def test_non_boolean_proof_handlers_deny(system, value):
    activate(system)
    _, queue, _, families, workers, _, _ = system
    submit(system)
    claimed = queue.claim(worker=workers['pilot'])
    families['pilot'].answer = value
    assert queue.completion_verifier(claimed, 'pilot', 'succeeded') is False
    assert queue.reconciliation_verifier(claimed, 'pilot', 'failed') is False


def test_handler_exceptions_fail_closed_for_every_dispatcher(system, monkeypatch):
    activate(system)
    _, queue, _, families, workers, _, _ = system
    submit(system)
    claimed = queue.claim(worker=workers['pilot'])
    def unavailable(_):
        raise RuntimeError('synthetic private policy failure')
    monkeypatch.setattr(Family, 'answer', property(unavailable), raising=False)
    assert queue.target_verifier('codex', 'pilot', 'staging', target()) is False
    assert queue.claim_verifier(workers['pilot'], 'pilot', 'staging', 'staging-pc', target()) is False
    assert queue.completion_verifier(claimed, 'pilot', 'succeeded') is False
    assert queue.reconciliation_verifier(claimed, 'pilot', 'failed') is False
    assert families['modular'].calls == []


@pytest.mark.parametrize('which', ['queue_callback', 'registry_callback', 'added_action', 'replaced_dict',
    'equal_action_replacement', 'action_value', 'bound_handler', 'binding_handler',
    'queue_repo', 'registry_repo', 'approval', 'readiness', 'attachment', 'sticky',
    'routing_map', 'binding_map', 'handler_map'])
def test_single_identity_or_handler_drift_denies_all_routes(system, which):
    activate(system)
    repo, queue, registry, families, workers, bindings, _ = system
    if which == 'queue_callback': queue.claim_verifier = None
    elif which == 'registry_callback': registry.target_verifier = lambda *args: True
    elif which == 'added_action': queue.actions['extra'] = WorkAction('staging-pc', project='engineering-hub')
    elif which == 'replaced_dict': queue.actions = dict(queue.actions)
    elif which == 'equal_action_replacement': queue.actions['pilot'] = WorkAction('staging-pc', project='engineering-hub')
    elif which == 'action_value': object.__setattr__(queue.actions['pilot'], 'machine_id', 'foreign')
    elif which == 'bound_handler': families['pilot'].claim = MethodType(lambda self, *a: True, families['pilot'])
    elif which == 'binding_handler': object.__setattr__(bindings['pilot'], 'target', lambda *args: True)
    elif which == 'queue_repo': queue.repo = object()
    elif which == 'registry_repo': registry.repo = object()
    elif which == 'approval': queue.approval_verifier = lambda *args: True
    elif which == 'readiness': queue.production_readiness_verifier = lambda *args: False
    elif which == 'attachment': del queue._shared_execution_policies
    elif which == 'sticky': queue._shared_execution_required = False
    elif which == 'routing_map': registry._routing = dict(registry._routing)
    elif which == 'binding_map': registry._bindings = dict(registry._bindings)
    elif which == 'handler_map': registry._snapshots = dict(registry._snapshots)
    with pytest.raises(HubError, match='execution_policy_changed'):
        registry.assert_integrity()
    assert registry.action_bound('pilot') is False
    target_check, claim_check, completion_check, reconcile_check = registry._dispatchers
    assert target_check('codex', 'pilot', 'staging', target()) is False
    assert claim_check(workers['pilot'], 'pilot', 'staging', 'staging-pc', target()) is False
    assert completion_check({'id': str(uuid.uuid4())}, 'pilot', 'succeeded') is False
    assert reconcile_check({'id': str(uuid.uuid4())}, 'pilot', 'failed') is False


def test_unknown_action_and_cross_family_worker_never_admit(system):
    activate(system)
    _, queue, registry, families, workers, _, _ = system
    assert queue.target_verifier('codex', 'unknown', 'staging', target()) is False
    assert queue.claim_verifier(workers['pilot'], 'unknown', 'staging', 'staging-pc', target()) is False
    assert queue.claim_verifier(workers['modular'], 'pilot', 'staging', 'staging-pc', target()) is False
    assert all(not value.calls for value in families.values())


def test_closed_admission_still_drains_original_completion(system):
    activate(system)
    _, queue, registry, _, workers, _, _ = system
    job = submit(system)
    claimed = queue.claim(worker=workers['pilot'], action_keys=('pilot',))
    assert claimed['id'] == job['id']
    registry.close()
    with pytest.raises(HubError): registry.open()
    assert registry.action_bound('pilot') is True
    registry.assert_binding('pilot')
    done = queue.complete(job_id=job['id'], worker=workers['pilot'], fence=claimed['fence'],
                          evidence_id='pilot', outcome='succeeded')
    assert done['state'] == 'succeeded'


def test_stored_job_action_and_worker_prevent_cross_family_proof(system):
    activate(system)
    _, queue, _, families, workers, _, _ = system
    job = submit(system)
    claimed = queue.claim(worker=workers['pilot'])
    assert queue.completion_verifier(claimed, 'modular', 'succeeded') is False
    forged = claimed | {'action_key': 'modular', 'worker': workers['modular'].encoded()}
    assert queue.completion_verifier(forged, 'modular', 'succeeded') is False
    assert queue.reconciliation_verifier(forged, 'modular', 'failed') is False
    assert families['modular'].calls == []
    assert queue.complete(job_id=job['id'], worker=workers['pilot'], fence=claimed['fence'],
                          evidence_id='pilot', outcome='succeeded')['state'] == 'succeeded'


@pytest.mark.parametrize('change', ['close', 'action', 'pause'])
def test_claim_plan_rechecks_barrier_drift_and_queue_pause(system, change):
    activate(system)
    _, queue, registry, _, workers, _, _ = system
    job = submit(system)
    plan = queue.plan_claim(worker=workers['pilot'], action_keys=('pilot',))
    assert plan is not None
    if change == 'close': registry.close()
    elif change == 'action': queue.actions['pilot'] = WorkAction('changed', project='engineering-hub')
    else: queue.set_paused(True)
    assert queue.claim_exact(plan, worker=workers['pilot']) is None
    assert queue.get_job(job['id'])['state'] == 'queued'


def test_lease_expiry_holds_across_other_family_filter_and_drains_after_close(system):
    activate(system)
    repo, queue, registry, _, workers, _, clock = system
    first = submit(system, 'pilot')
    second = submit(system, 'modular')
    queue.claim(worker=workers['pilot'], action_keys=('pilot',), lease_seconds=10)
    clock[0] += 11
    assert queue.claim(worker=workers['modular'], action_keys=('modular',)) is None
    assert queue.get_job(first['id'])['state'] == 'held'
    assert queue.get_job(second['id'])['state'] == 'queued'
    registry.close()
    assert queue.reconcile(job_id=first['id'], evidence_id='pilot', outcome='failed')['state'] == 'failed'
    with repo._connection() as db:
        assert db.execute('SELECT job_id FROM hub_work_machines').fetchone()[0] is None


def test_unknown_reserved_family_denies_open_and_preserves_hold_bytes(system):
    activate(system)
    repo, queue, registry, _, workers, _, _ = system
    job = submit(system)
    queue.claim(worker=workers['pilot'])
    with repo._transaction() as db:
        db.execute("UPDATE hub_work_jobs SET action_key='unknown-family',state='held' WHERE id=?", (job['id'],))
    def snapshot():
        with repo._connection() as db:
            return tuple(db.execute('SELECT * FROM hub_work_jobs').fetchone()), tuple(db.execute('SELECT * FROM hub_work_machines').fetchone())
    before = snapshot()
    with pytest.raises(HubError, match='foreign_hold_family_absent'):
        registry.open()
    assert snapshot() == before
    assert queue.target_verifier('codex', 'pilot', 'staging', target()) is False


def test_open_requires_every_snapshot_action_explicitly_bound(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    queue = WorkQueue(repo, {n: WorkAction('machine', project='engineering-hub') for n in ('pilot', 'modular', 'unbound')})
    registry = SharedExecutionPolicies(repo, queue)
    for name in ('pilot', 'modular'):
        h = Family(name)
        registry.bind(name, {name: queue.actions[name]}, worker(CONTAINER_PILOT_EXECUTOR),
                      target=h.target, claim=h.claim, completion=h.completion, reconciliation=h.reconciliation)
    registry.install()
    registry.ready('pilot'); registry.ready('modular')
    with pytest.raises(HubError, match='execution_policy_not_ready'):
        registry.open()
    assert registry.claim_verifier(worker(CONTAINER_PILOT_EXECUTOR), 'unbound', 'staging', 'machine', target()) is False
