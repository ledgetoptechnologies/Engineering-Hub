from concurrent.futures import ThreadPoolExecutor
import uuid

import pytest

from engineering_hub.repository import HubError, HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget, WorkWorker


def w(agent, instance='one'):
    return WorkWorker(agent, str(uuid.uuid5(uuid.NAMESPACE_DNS, instance)))


def target(commit='a' * 40):
    return WorkTarget(str(uuid.uuid4()), commit, 'sha256:' + 'b' * 64)


@pytest.fixture
def setup_queue(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    clock = [1000.0]
    actions = {'validate': WorkAction('machine-one', frozenset({'production', 'staging'}), project='engineering-hub', requires_execution_proof=False),
               'other': WorkAction('machine-two', project='engineering-hub', requires_execution_proof=False)}
    queue = WorkQueue(repo, actions, target_verifier=lambda *args: True, clock=lambda: clock[0],
                      approval_verifier=lambda actor, action, env, approval, work: approval == 'approved')
    return queue, clock, repo, actions


def submit(queue, key='one', env='staging', action='validate', **kwargs):
    return queue.submit(actor='codex', request_id=key, action_key=action,
                        environment=env, target=kwargs.pop('target', target()), **kwargs)


def finish(queue, claimed, outcome='succeeded'):
    return queue.complete(job_id=claimed['id'], worker=WorkWorker(*claimed['worker'].split(':', 1)), fence=claimed['fence'], outcome=outcome)


def test_live_admission_rechecks_policy_target_registry_and_ownership(setup_queue):
    q, clock, _, _ = setup_queue
    submit(q)
    worker = w('codex')
    job = q.claim(worker=worker)
    args = dict(job_id=job['id'], worker=worker, fence=job['fence'])
    assert q.live_claim(**args) == job
    with pytest.raises(HubError, match='work_ownership_lost'):
        q.live_claim(**{**args, 'worker': w('hermes')})
    q.set_paused(True)
    with pytest.raises(HubError, match='work_queue_paused'):
        q.live_claim(**args)
    q.set_paused(False)
    q.target_verifier = lambda *args: False
    with pytest.raises(HubError, match='work_admission_changed'):
        q.live_claim(**args)
    q.target_verifier = lambda *args: True
    q.actions['validate'] = WorkAction('different-machine', project='engineering-hub')
    with pytest.raises(HubError, match='work_admission_changed'):
        q.live_claim(**args)
    clock[0] += 60
    with pytest.raises(HubError, match='work_ownership_lost'):
        q.live_claim(**args)


def test_production_first_among_approved_fifo_non_preemption(setup_queue):
    q, _, _, _ = setup_queue
    first = submit(q)
    running = q.claim(worker=w('codex'))
    denied = submit(q, 'denied', 'production')
    second = submit(q, 'second')
    prod = submit(q, 'prod', 'production', approval_id='approved')
    assert running['id'] == first['id']
    assert q.claim(worker=w('hermes')) is None
    finish(q, running)
    claim = q.claim(worker=w('hermes'))
    assert claim['id'] == prod['id']
    assert claim['fence'] == running['fence'] + 1
    finish(q, claim)
    claim = q.claim(worker=w('claude'))
    assert claim['id'] == second['id']
    finish(q, claim)
    assert q.claim(worker=w('codex')) is None
    assert denied['state'] == 'queued'


def test_expiry_holds_machine_after_reopen_no_replay(setup_queue):
    q, clock, repo, actions = setup_queue
    original = submit(q)
    claimed = q.claim(worker=w('codex'), lease_seconds=10)
    clock[0] += 10
    q = WorkQueue(repo, actions, target_verifier=lambda *args: True, clock=lambda: clock[0])
    assert q.status()['jobs'][0]['requires_reconciliation']
    submit(q, 'blocked')
    other = submit(q, 'parallel', action='other')
    assert q.claim(worker=w('hermes'))['id'] == other['id']
    assert q.claim(worker=w('claude')) is None
    held = next(j for j in q.status()['jobs'] if j['id'] == original['id'])
    assert held['state'] == 'held'
    with pytest.raises(HubError, match='work_ownership_lost'):
        finish(q, claimed)
    with pytest.raises(HubError, match='work_reconciliation_required'):
        q.reconcile(job_id=claimed['id'], evidence_id='proof', outcome='failed')


def test_reconcile_requires_exact_authoritative_evidence(setup_queue):
    q, clock, repo, actions = setup_queue
    job = submit(q)
    claim = q.claim(worker=w('codex'))
    finish(q, claim, 'held')
    q = WorkQueue(repo, actions, target_verifier=lambda *args: True, clock=lambda: clock[0], reconciliation_verifier=lambda row, evidence, outcome: row['target'] == job['target'] and evidence == 'verified-stopped' and outcome == 'failed')
    with pytest.raises(HubError, match='work_reconciliation_required'):
        q.reconcile(job_id=job['id'], evidence_id='unverified', outcome='failed')
    assert q.reconcile(job_id=job['id'], evidence_id='verified-stopped', outcome='failed')['state'] == 'failed'
    submit(q, 'next')
    assert q.claim(worker=w('claude'))['fence'] == 2


def test_idempotence_full_target_and_capacity(setup_queue):
    q, _, repo, actions = setup_queue
    q = WorkQueue(repo, actions, target_verifier=lambda *args: True, max_jobs=1)
    work = target()
    first = submit(q, target=work)
    assert submit(q, target=work) == first
    with pytest.raises(HubError, match='work_request_conflict'):
        submit(q, target=target())
    with pytest.raises(HubError, match='work_queue_capacity'):
        submit(q, 'two')


def test_production_verifier_receives_exact_immutable_target(setup_queue):
    _, _, repo, actions = setup_queue
    seen = []
    q = WorkQueue(repo, actions, target_verifier=lambda *args: True, approval_verifier=lambda *args: seen.append(args) or True)
    work = target()
    submit(q, env='production', approval_id='decision-one', target=work)
    assert q.claim(worker=w('hermes'))
    assert seen == [('codex', 'validate', 'production', 'decision-one', work)]


def test_approval_exception_and_non_boolean_fail_closed(setup_queue):
    q, _, _, _ = setup_queue
    submit(q, env='production', approval_id='approved')
    q.approval_verifier = lambda *args: 1
    assert q.claim(worker=w('codex')) is None
    def broken(*args):
        raise RuntimeError('not logged')
    q.approval_verifier = broken
    assert q.claim(worker=w('codex')) is None


def test_pause_persists_and_does_not_preempt(setup_queue):
    q, _, repo, actions = setup_queue
    submit(q)
    running = q.claim(worker=w('codex'))
    q.set_paused(True)
    assert finish(q, running)['state'] == 'succeeded'
    submit(q, 'next')
    reopened = WorkQueue(repo, actions, target_verifier=lambda *args: True)
    assert reopened.status()['paused']
    assert reopened.claim(worker=w('codex')) is None
    reopened.set_paused(False)
    assert reopened.claim(worker=w('codex'))


def test_fencing_and_renewal(setup_queue):
    q, clock, _, _ = setup_queue
    submit(q)
    claim = q.claim(worker=w('codex'), lease_seconds=10)
    clock[0] += 5
    renewed = q.renew(job_id=claim['id'], worker=w('codex'), fence=claim['fence'], lease_seconds=20)
    assert renewed['lease_until'] == 1025
    for worker, fence in [('claude', claim['fence']), ('codex', claim['fence'] + 1), ('codex', True)]:
        with pytest.raises(HubError):
            q.complete(job_id=claim['id'], worker=w(worker), fence=fence, outcome='succeeded')
    clock[0] = 1025
    with pytest.raises(HubError, match='work_ownership_lost'):
        q.renew(job_id=claim['id'], worker=w('codex'), fence=claim['fence'])


def test_concurrent_claims_machine_exclusive(setup_queue):
    q, _, _, _ = setup_queue
    for i in range(8):
        submit(q, 'job-' + str(i))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: q.claim(worker=w('codex', str(i))), range(8)))
    assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize('lease', [True, 0, -1, 601, 1.5, '60', None])
def test_invalid_leases(setup_queue, lease):
    with pytest.raises(HubError, match='invalid_work_lease'):
        setup_queue[0].claim(worker=w('codex'), lease_seconds=lease)


@pytest.mark.parametrize('fields', [('', 'a'*40, 'sha256:'+'b'*64), (str(uuid.uuid4()), 'A'*40, 'sha256:'+'b'*64), (str(uuid.uuid4()), 'a'*40, 'b'*64), (None, 'a'*40, 'sha256:'+'b'*64)])
def test_invalid_targets(fields):
    with pytest.raises(HubError, match='invalid_work_target'):
        WorkTarget(*fields)


def test_registry_change_cannot_redirect_existing_work(setup_queue):
    q, _, repo, _ = setup_queue
    submit(q)
    changed = WorkQueue(repo, {'validate': WorkAction('replacement', project='engineering-hub', requires_execution_proof=False)}, target_verifier=lambda *args: True)
    assert changed.claim(worker=w('codex')) is None


def test_status_read_does_not_change_expired_claim(setup_queue):
    q, clock, repo, _ = setup_queue
    submit(q)
    q.claim(worker=w('codex'), lease_seconds=1)
    clock[0] += 1
    assert q.status()['jobs'][0]['requires_reconciliation']
    with repo._connection() as db:
        assert db.execute('SELECT state FROM hub_work_jobs').fetchone()[0] == 'claimed'


def test_stale_review_is_revalidated_before_claim(setup_queue):
    q, _, _, _ = setup_queue
    submit(q)
    q.target_verifier = lambda *args: False
    assert q.claim(worker=w('codex')) is None
    assert q.status()['jobs'][0]['state'] == 'queued'


def test_target_verifier_required_and_strict(setup_queue):
    _, _, repo, actions = setup_queue
    for callback in (None, lambda *args: 1, lambda *args: False):
        q = WorkQueue(repo, actions, target_verifier=callback)
        with pytest.raises(HubError, match='work_target_not_verified'):
            submit(q)


def test_worker_instances_not_interchangeable(setup_queue):
    q, _, _, _ = setup_queue
    submit(q)
    claim = q.claim(worker=w('codex', 'first-chat'))
    for method, kwargs in ((q.renew, {}), (q.complete, {'outcome': 'succeeded'})):
        with pytest.raises(HubError, match='work_ownership_lost'):
            method(job_id=claim['id'], worker=w('codex', 'second-chat'), fence=claim['fence'], **kwargs)
    assert finish(q, claim)['state'] == 'succeeded'


def test_changed_project_registry_cannot_redirect_queued_target(setup_queue):
    q, _, repo, _ = setup_queue
    submit(q)
    changed = WorkQueue(repo, {'validate': WorkAction('machine-one', project='treedar', requires_execution_proof=False)}, target_verifier=lambda *args: True)
    assert changed.claim(worker=w('codex')) is None


@pytest.mark.parametrize('method,kwargs', [('claim', {'worker': 'codex'}), ('set_paused', {'paused': 1}), ('status', {'limit': True}), ('status', {'limit': 101})])
def test_strict_control_input(setup_queue, method, kwargs):
    with pytest.raises(HubError):
        getattr(setup_queue[0], method)(**kwargs)


@pytest.mark.parametrize('outcome', ['succeeded', 'failed'])
def test_executable_completion_requires_authoritative_proof(setup_queue, outcome):
    _, _, repo, _ = setup_queue
    q = WorkQueue(repo, {'execute': WorkAction('machine-one', project='engineering-hub')}, target_verifier=lambda *args: True)
    job = submit(q, action='execute')
    claim = q.claim(worker=w('codex'))
    assert not job['coordination_only']
    assert q.status()['actions']['execute']['requires_execution_proof']
    for verifier, evidence in [(None, None), (None, 'forged'), (lambda *args: 1, 'forged'), (lambda *args: False, 'forged')]:
        q.completion_verifier = verifier
        with pytest.raises(HubError, match='work_completion_proof_required'):
            q.complete(job_id=claim['id'], worker=w('codex'), fence=claim['fence'], outcome=outcome, evidence_id=evidence)
        submit(q, 'blocked', action='execute', target=target()) if evidence is None else None
        assert q.claim(worker=w('hermes')) is None
        assert next(row for row in q.status()['jobs'] if row['id'] == claim['id'])['state'] == 'claimed'
    seen = []
    q.completion_verifier = lambda row, evidence, result: seen.append((row, evidence, result)) or (row['target'] == job['target'] and evidence == 'verified-execution' and result == outcome)
    completed = q.complete(job_id=claim['id'], worker=w('codex'), fence=claim['fence'], outcome=outcome, evidence_id='verified-execution')
    assert completed['state'] == outcome
    assert seen[0][0]['fence'] == claim['fence']
    assert q.claim(worker=w('hermes'))


def test_executable_hold_does_not_require_proof_or_release(setup_queue):
    _, _, repo, _ = setup_queue
    q = WorkQueue(repo, {'execute': WorkAction('machine-one', project='engineering-hub')}, target_verifier=lambda *args: True)
    submit(q, action='execute')
    claim = q.claim(worker=w('codex'))
    assert finish(q, claim, 'held')['state'] == 'held'
    submit(q, 'blocked', action='execute')
    assert q.claim(worker=w('hermes')) is None


def test_coordination_only_policy_visible_and_not_mutable_for_old_job(setup_queue):
    q, _, repo, _ = setup_queue
    job = submit(q)
    assert job['coordination_only']
    assert q.status()['actions']['validate']['coordination_only']
    replaced = WorkQueue(repo, {'validate': WorkAction('machine-one', project='engineering-hub')}, target_verifier=lambda *args: True)
    assert replaced.claim(worker=w('codex')) is None


def events(repo):
    with repo._connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM hub_outbox ORDER BY sequence')]


def test_mutation_outbox_audit_revision_atomicity(setup_queue):
    q, clock, repo, _ = setup_queue
    work = target()
    job = submit(q, target=work)
    assert len(events(repo)) == 1
    assert submit(q, target=work) == job
    assert len(events(repo)) == 1
    claim = q.claim(worker=w('codex'))
    assert claim['revision'] == 2
    assert len(events(repo)) == 2
    q.renew(job_id=claim['id'], worker=w('codex'), fence=claim['fence'])
    assert len(events(repo)) == 2  # Same clock and lease is an exact no-op.
    clock[0] += 1
    q.renew(job_id=claim['id'], worker=w('codex'), fence=claim['fence'])
    assert len(events(repo)) == 3
    with pytest.raises(HubError):
        q.complete(job_id=claim['id'], worker=w('hermes'), fence=claim['fence'], outcome='succeeded')
    assert len(events(repo)) == 3
    finished = finish(q, claim)
    assert finished['revision'] == 4
    assert [row['action'] for row in events(repo)] == ['work.submitted', 'work.claimed', 'work.renewed', 'work.succeeded']
    assert [row['revision'] for row in events(repo)] == [1, 2, 3, 4]
    with repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_audit').fetchone()[0] == 4
    assert all(set(row) == {'sequence', 'action', 'entity_id', 'revision', 'created_at'} for row in events(repo))
    before = events(repo)
    q.status()
    assert events(repo) == before


def test_expiry_and_reconciliation_events_transactional(setup_queue):
    q, clock, repo, _ = setup_queue
    job = submit(q)
    q.claim(worker=w('codex'), lease_seconds=1)
    clock[0] += 1
    with pytest.raises(HubError, match='work_reconciliation_required'):
        q.reconcile(job_id=job['id'], evidence_id='not-proof', outcome='failed')
    assert len(events(repo)) == 2  # Failed reconciliation rolls back expiry too.
    assert q.claim(worker=w('hermes')) is None
    assert events(repo)[-1]['action'] == 'work.held'
    assert len(events(repo)) == 3
    assert q.claim(worker=w('hermes')) is None
    assert len(events(repo)) == 3
    q.reconciliation_verifier = lambda *args: True
    q.reconcile(job_id=job['id'], evidence_id='verified-stopped', outcome='failed')
    assert events(repo)[-1]['action'] == 'work.reconciled'
    assert events(repo)[-1]['revision'] == 4


def test_pause_events_noop_and_effective_master_pause(setup_queue):
    q, _, repo, _ = setup_queue
    submit(q)
    q.set_paused(True)
    assert events(repo)[-1]['action'] == 'work.paused'
    before = events(repo)
    q.set_paused(True)
    assert events(repo) == before
    q.set_paused(False)
    repo.set_pause('human:1', True)
    status = q.status()
    assert status['paused'] and status['master_paused'] and not status['queue_paused']
    assert status['jobs'][0]['blocked_reason'] == 'paused'
    assert q.claim(worker=w('codex')) is None


def test_production_status_explains_unapproved_job(setup_queue):
    q, _, _, _ = setup_queue
    submit(q, env='production')
    status = q.status()['jobs'][0]
    assert status['blocked_reason'] == 'production_approval_required'
    assert not status['eligible']
