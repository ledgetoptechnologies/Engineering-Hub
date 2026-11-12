"""Cancellation of unclaimed queue entries; no external services."""
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
from engineering_hub.repository import HubError
from engineering_hub.work_queue import WorkTarget
from tests.test_hub_work_queue import setup_queue, submit, w
from tests.test_hub_container_pilot import reviewed_task, COMMIT


def test_owner_cancel_is_idempotent_audited_and_unfreezes_task(setup_queue):
    queue, _, repo, _ = setup_queue
    task = reviewed_task(repo)
    job = submit(queue, target=WorkTarget(task['id'], COMMIT, 'sha256:' + 'b'*64))
    with pytest.raises(HubError, match='active_work_job'):
        repo.set_candidate('codex', task['id'], task['revision'], 'f'*40)
    with pytest.raises(HubError, match='forbidden'):
        queue.cancel_unclaimed(actor='claude', job_id=job['id'])
    assert queue.cancel_unclaimed(actor='codex', job_id=job['id'])['state'] == 'cancelled'
    assert queue.cancel_unclaimed(actor='codex', job_id=job['id'])['state'] == 'cancelled'
    with repo._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM hub_audit WHERE entity_id=? AND action='work.cancelled'", (job['id'],)).fetchone()[0] == 1
    assert repo.set_candidate('codex', task['id'], task['revision'], 'f'*40)['candidate_commit'] == 'f'*40
    assert queue.claim(worker=w('codex')) is None


@pytest.mark.parametrize('held', [False, True])
def test_claimed_and_held_never_cancel(setup_queue, held):
    queue, _, _, _ = setup_queue
    job = submit(queue)
    claim = queue.claim(worker=w('codex'))
    if held:
        queue.complete(job_id=job['id'], worker=w('codex'), fence=claim['fence'], outcome='held')
    with pytest.raises(HubError, match='work_not_unclaimed'):
        queue.cancel_unclaimed(actor='codex', job_id=job['id'])
    assert queue.get_job(job['id'])['state'] == ('held' if held else 'claimed')


def test_reservation_or_malformed_claim_blocks_cancellation(setup_queue):
    queue, _, repo, _ = setup_queue
    job = submit(queue)
    with repo._transaction() as db:
        db.execute('INSERT INTO hub_work_machines(machine_id,generation,job_id) VALUES(?,?,?)',('stray',1,job['id']))
    with pytest.raises(HubError, match='work_not_unclaimed'):
        queue.cancel_unclaimed(actor='codex', job_id=job['id'])


def test_claim_and_cancel_race_has_one_winner(setup_queue):
    queue, _, _, _ = setup_queue
    job = submit(queue)
    barrier = threading.Barrier(2)
    def cancel():
        barrier.wait()
        try: return queue.cancel_unclaimed(actor='codex', job_id=job['id'])['state']
        except HubError as exc:
            assert exc.code == 'work_not_unclaimed'
            return 'lost'
    def claim():
        barrier.wait()
        result = queue.claim(worker=w('codex'))
        return result['state'] if result else 'lost'
    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel_future, claim_future = pool.submit(cancel), pool.submit(claim)
        values = (cancel_future.result(), claim_future.result())
    assert values in [('cancelled', 'lost'), ('lost', 'claimed')]

def test_historical_claim_blocks_reset_even_if_row_looks_queued(setup_queue):
    queue, _, repo, _ = setup_queue
    job = submit(queue)
    with repo._transaction() as db:
        db.execute("INSERT INTO hub_audit(actor,action,entity_id,revision,created_at) VALUES(?,?,?,?,?)",
                   ('codex','work.claimed',job['id'],2,'fixture'))
    with pytest.raises(HubError, match='work_not_unclaimed'):
        queue.cancel_unclaimed(actor='codex', job_id=job['id'])


def test_retained_pilot_intent_blocks_cancel_even_if_job_looks_unclaimed(setup_queue):
    queue, _, repo, _ = setup_queue
    job = submit(queue)
    with repo._transaction() as db:
        db.execute('CREATE TABLE hub_queued_pilot_jobs(job_id TEXT PRIMARY KEY)')
        db.execute('INSERT INTO hub_queued_pilot_jobs(job_id) VALUES(?)', (job['id'],))
    with pytest.raises(HubError, match='work_not_unclaimed'):
        queue.cancel_unclaimed(actor='codex', job_id=job['id'])
    assert queue.get_job(job['id'])['state'] == 'queued'


def test_queued_freezes_human_context_until_cancelled(setup_queue):
    queue, _, repo, _ = setup_queue
    task = reviewed_task(repo)
    job = submit(queue, target=WorkTarget(task['id'], COMMIT, 'sha256:' + 'b'*64))
    with pytest.raises(HubError, match='active_work_job'):
        repo.update_context('human:1234', 'task', task['id'], task['revision'], 'changed', [])
    queue.cancel_unclaimed(actor='codex', job_id=job['id'])
    assert repo.update_context('human:1234', 'task', task['id'], task['revision'], 'changed', [])['status'] == 'claimed'


def test_same_task_single_open_idempotent_and_cancel_allows_new(setup_queue):
    queue, _, repo, _ = setup_queue
    task=reviewed_task(repo)
    target=WorkTarget(task['id'], COMMIT, 'sha256:'+'b'*64)
    first=submit(queue,'same',target=target)
    assert submit(queue,'same',target=target)['id']==first['id']
    with pytest.raises(HubError,match='work_task_already_queued'):
        submit(queue,'second',target=target)
    queue.cancel_unclaimed(actor='codex',job_id=first['id'])
    second=submit(queue,'second',target=target)
    assert second['id']!=first['id']
    claim=queue.claim(worker=w('codex'))
    with pytest.raises(HubError,match='work_task_already_queued'):
        submit(queue,'third',target=target)
    queue.complete(job_id=second['id'],worker=w('codex'),fence=claim['fence'],outcome='held')
    with pytest.raises(HubError,match='work_task_already_queued'):
        submit(queue,'fourth',target=target)


def test_task_changes_during_preflight_never_freezes_stale_job(setup_queue):
    queue, _, repo, _ = setup_queue
    task=reviewed_task(repo)
    target=WorkTarget(task['id'],COMMIT,'sha256:'+'b'*64)
    def update(*args):
        repo.set_candidate('codex',task['id'],task['revision'],'f'*40)
        return True
    queue.target_verifier=update
    with pytest.raises(HubError,match='work_review_changed'):
        submit(queue,'race',target=target)
    assert queue.status()['jobs']==[]


def test_tampered_family_denied_at_transaction(setup_queue):
    import json
    queue, _, repo, _=setup_queue
    task=reviewed_task(repo)
    task['review']['family']='codex'
    with repo._transaction() as db:
        db.execute('UPDATE hub_entities SET data=? WHERE id=?',(json.dumps(task),task['id']))
    with pytest.raises(HubError,match='work_review_changed'):
        submit(queue,'bad',target=WorkTarget(task['id'],COMMIT,'sha256:'+'b'*64))


@pytest.mark.parametrize('provenance_valid', [True, False])
def test_submission_uses_persisted_installation_reviewer_lineage(setup_queue, provenance_valid):
    import json
    queue, _, repo, _ = setup_queue
    task = reviewed_task(repo)
    installation = 'review-installation'
    actor = 'installation:' + installation
    task['review'].update(actor=actor, family='claude')
    with repo._transaction() as db:
        db.execute('CREATE TABLE hub_session_provenance '
                   '(audit_sequence INTEGER PRIMARY KEY, installation_id TEXT, session_id TEXT, '
                   'family TEXT, capability TEXT)')
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(task), task['id']))
        db.execute("UPDATE hub_audit SET actor=? WHERE entity_id=? AND action='task.reviewed' AND revision=?",
                   (actor, task['id'], task['revision']))
        sequence = db.execute("SELECT sequence FROM hub_audit WHERE entity_id=? AND action='task.reviewed' AND revision=?",
                              (task['id'], task['revision'])).fetchone()[0]
        db.execute('INSERT INTO hub_session_provenance VALUES(?,?,?,?,?)',
                   (sequence, installation, 'review-session', 'claude' if provenance_valid else 'codex', 'task.review'))
    target = WorkTarget(task['id'], COMMIT, 'sha256:'+'b'*64)
    if provenance_valid:
        assert submit(queue, 'session-review', target=target)['state'] == 'queued'
    else:
        with pytest.raises(HubError, match='work_review_changed'):
            submit(queue, 'session-review', target=target)


def test_concurrent_same_task_only_one_submission(setup_queue):
    queue, _, repo, _=setup_queue
    task=reviewed_task(repo)
    target=WorkTarget(task['id'],COMMIT,'sha256:'+'b'*64)
    barrier=threading.Barrier(2)
    def run(key):
        barrier.wait()
        try: return submit(queue,key,target=target)['state']
        except HubError as e: return e.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        first,second=pool.submit(run,'a'),pool.submit(run,'b')
        assert sorted([first.result(),second.result()]) == ['queued','work_task_already_queued']


def test_durable_modular_submission_denies_custom_action_cancel(setup_queue):
    import json
    queue, _, repo, _=setup_queue
    source=target=WorkTarget(str(__import__('uuid').uuid4()),'a'*40,'sha256:'+'b'*64)
    with repo._transaction() as db:
        db.execute('CREATE TABLE hub_staging_submissions(id TEXT PRIMARY KEY,actor TEXT,request_id TEXT,data TEXT)')
        db.execute('INSERT INTO hub_staging_submissions VALUES(?,?,?,?)',
            (source.task_id,'codex','source-request',json.dumps({'job_id':None,'source_id':'deposited'})))
    job=submit(queue,'staging:source-request',target=target)
    with pytest.raises(HubError,match='work_controller_cancel_required'):
        queue.cancel_unclaimed(actor='codex',job_id=job['id'])
    assert queue.get_job(job['id'])['state']=='queued'
    with repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_staging_submissions').fetchone()[0]==1


def test_reserved_modular_namespace_denies_without_controller_state(setup_queue):
    from engineering_hub.work_queue import WorkAction
    queue, _, _, _=setup_queue
    queue.actions['modular.synthetic']=WorkAction('machine-one',project='engineering-hub')
    job=submit(queue,'reserved',action='modular.synthetic')
    with pytest.raises(HubError,match='work_controller_cancel_required'):
        queue.cancel_unclaimed(actor='codex',job_id=job['id'])


@pytest.mark.parametrize('match', ['actor_request', 'submission_id', 'recorded_job_id'])
def test_each_persisted_staging_ownership_predicate_is_independently_enforced(setup_queue, match):
    import json
    from uuid import uuid4
    queue, _, repo, _=setup_queue
    source=WorkTarget(str(uuid4()),'a'*40,'sha256:'+'b'*64)
    request='staging:owned-request' if match=='actor_request' else 'other-request'
    job=submit(queue,request,target=source)
    submission_id=str(uuid4())
    with repo._transaction() as db:
        db.execute('CREATE TABLE hub_staging_submissions(id TEXT PRIMARY KEY,actor TEXT,request_id TEXT,data TEXT)')
        if match=='submission_id':
            target=dict(job['target'],submission_id=submission_id)
            db.execute('UPDATE hub_work_jobs SET target=? WHERE id=?',(json.dumps(target),job['id']))
        data={'job_id':job['id'] if match=='recorded_job_id' else None,'source_id':'retained'}
        db.execute('INSERT INTO hub_staging_submissions VALUES(?,?,?,?)',
                   (submission_id,'codex','owned-request' if match=='actor_request' else 'unrelated',json.dumps(data)))
    with pytest.raises(HubError,match='work_controller_cancel_required'):
        queue.cancel_unclaimed(actor='codex',job_id=job['id'])
    assert queue.get_job(job['id'])['state']=='queued'
    with repo._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM hub_audit WHERE entity_id=? AND action='work.cancelled'",(job['id'],)).fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM hub_staging_submissions').fetchone()[0]==1


def test_corrupt_staging_record_denies_cancellation_with_fixed_error(setup_queue):
    from uuid import uuid4
    queue, _, repo, _=setup_queue
    job=submit(queue,'ordinary')
    with repo._transaction() as db:
        db.execute('CREATE TABLE hub_staging_submissions(id TEXT PRIMARY KEY,actor TEXT,request_id TEXT,data TEXT)')
        db.execute('INSERT INTO hub_staging_submissions VALUES(?,?,?,?)',(str(uuid4()),'other','other','{invalid'))
    with pytest.raises(HubError,match='work_controller_cancel_required') as failure:
        queue.cancel_unclaimed(actor='codex',job_id=job['id'])
    assert failure.value.status==409 and queue.get_job(job['id'])['state']=='queued'


def test_reserved_staging_request_denied_when_submission_table_missing(setup_queue):
    queue, _, repo, _=setup_queue
    job=submit(queue,'staging:orphaned-submission')
    with repo._connection() as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_staging_submissions'").fetchone() is None
    with pytest.raises(HubError,match='work_controller_cancel_required'):
        queue.cancel_unclaimed(actor='codex',job_id=job['id'])
    assert queue.get_job(job['id'])['state']=='queued'


def test_non_reserved_prefix_still_cancellable(setup_queue):
    queue, _, _, _=setup_queue
    job=submit(queue,'staging-other')
    assert queue.cancel_unclaimed(actor='codex',job_id=job['id'])['state']=='cancelled'
