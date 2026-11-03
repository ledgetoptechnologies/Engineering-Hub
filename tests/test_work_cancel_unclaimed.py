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
    with pytest.raises(HubError, match='task_in_progress'):
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


def test_queued_freezes_human_context_until_cancelled(setup_queue):
    queue, _, repo, _ = setup_queue
    task = reviewed_task(repo)
    job = submit(queue, target=WorkTarget(task['id'], COMMIT, 'sha256:' + 'b'*64))
    with pytest.raises(HubError, match='task_in_progress'):
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
