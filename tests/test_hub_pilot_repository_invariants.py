"""Repository consumption gates and durable pilot hold invariants; no host effects."""
import json

import pytest

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.dispatcher import StagingDispatcher
from engineering_hub.repository import HELD_REASONS, HubError, HubRepository
from tests.test_hub_container_pilot import FakeClient, IMAGE, PRINCIPAL, pilot_manifest, reviewed_task


@pytest.fixture
def repo(tmp_path):
    return HubRepository(tmp_path / 'pilot.sqlite3', execution_principal=PRINCIPAL)


def corrupt_task(repo, task, **changes):
    changed = {**task, **changes}
    with repo._transaction() as db:
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(changed), task['id']))
    return changed


def assert_no_execution_authority(repo):
    assert repo.list_runs() == []
    with repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_target_fences').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM hub_run_requests').fetchone()[0] == 0


INVALID_REVIEWS = [None, {}, [], 'approve',
                   {'actor': 'codex', 'commit': 'a' * 40, 'verdict': 'approve'},
                   {'actor': 'human:1234', 'commit': 'a' * 40, 'verdict': 'approve'},
                   {'actor': 'unknown', 'commit': 'a' * 40, 'verdict': 'approve'},
                   {'actor': [], 'commit': 'a' * 40, 'verdict': 'approve'},
                   {'actor': 'claude', 'commit': 'a' * 40, 'verdict': 'reject'},
                   {'actor': 'claude', 'commit': 'a' * 40},
                   {'actor': 'claude', 'commit': 'b' * 40, 'verdict': 'approve'}]


@pytest.mark.parametrize('review', INVALID_REVIEWS)
def test_direct_create_rechecks_persisted_review_before_fence(repo, review):
    task = corrupt_task(repo, reviewed_task(repo), review=review)
    audit = repo.list_audit()
    with pytest.raises(HubError, match='review_required'):
        repo.create_run('codex', task['id'], task['revision'], IMAGE, 'engineering-hub:staging',
                        client_request_id='invalid-review')
    assert_no_execution_authority(repo)
    assert repo.list_audit() == audit
    assert repo.get_task(task['id']) == task


@pytest.mark.parametrize('candidate', [None, '', 'main', 'A' * 40, 'a' * 39, 123])
def test_matching_but_invalid_candidate_is_not_consumable(repo, candidate):
    task = corrupt_task(repo, reviewed_task(repo), candidate_commit=candidate,
                        review={'actor': 'claude', 'commit': candidate, 'verdict': 'approve'})
    with pytest.raises(HubError, match='review_required'):
        repo.create_run('codex', task['id'], task['revision'], IMAGE, 'engineering-hub:staging')
    assert_no_execution_authority(repo)


@pytest.mark.asyncio
@pytest.mark.parametrize('review', [INVALID_REVIEWS[i] for i in (1, 4, 5, 6, 8, 9, 10)])
async def test_dispatch_skips_invalid_persisted_review_without_host_submission(repo, review):
    client = FakeClient()
    workflow = ContainerPilotWorkflow(repo, client, [ContainerPilotTarget.from_manifest(pilot_manifest())])
    task = corrupt_task(repo, reviewed_task(repo), review=review)
    dispatcher = StagingDispatcher(repo, workflow)
    result = await dispatcher.drain_batch()
    assert result['dispatched'] == 0
    assert dispatcher.receipts() == []
    assert client.submissions == []
    assert repo.get_task(task['id']) == task
    assert_no_execution_authority(repo)


@pytest.mark.asyncio
@pytest.mark.parametrize('reviewer,verdict', [('codex', 'approve'), ('unregistered', 'approve'),
                                          ('claude', 'reject')])
async def test_shared_repository_review_change_after_dispatch_reservation_is_rechecked(repo, reviewer, verdict):
    client = FakeClient()
    workflow = ContainerPilotWorkflow(repo, client, [ContainerPilotTarget.from_manifest(pilot_manifest())])
    task = reviewed_task(repo)
    dispatcher = StagingDispatcher(repo, workflow)
    reservation = None
    for _ in range(4):
        reservation = dispatcher._reserve_next()
    assert reservation['task']['id'] == task['id']
    # Separate repository simulates a preexisting row edit between the
    # dispatcher's eligibility snapshot and transactional run consumption.
    other = HubRepository(repo.path, execution_principal=PRINCIPAL)
    corrupt_task(other, task, review={'actor': reviewer, 'commit': task['candidate_commit'], 'verdict': verdict})
    assert await dispatcher._execute(reservation) == 0
    assert dispatcher.receipts()[0]['reason'] == 'review_required'
    assert dispatcher.receipts()[0]['state'] == 'needs_reconciliation'
    assert client.submissions == []
    assert_no_execution_authority(repo)


def new_run(repo):
    task = reviewed_task(repo)
    return repo.create_run('codex', task['id'], task['revision'], IMAGE, 'engineering-hub:staging')


def event(repo, run, name, **kwargs):
    return repo.append_run_event(PRINCIPAL, run['id'], name, 'command',
                                 run['fencing_generation'], run['revision'], **kwargs)


@pytest.mark.parametrize('reason', sorted(HELD_REASONS))
def test_failed_to_held_persists_reason_only_in_event_and_retains_reservations(repo, reason):
    run = event(repo, new_run(repo), 'failed')
    task = repo.get_task(run['task_id'])
    assert task['repair_count'] == 1
    held = event(repo, run, 'held', reason=reason)
    assert held['status'] == 'held'
    assert 'reason' not in held
    assert held['events'][-1]['reason'] == reason
    assert held['fencing_generation'] == run['fencing_generation']
    assert repo.get_task(task['id']) == task
    reopened = HubRepository(repo.path, execution_principal=PRINCIPAL)
    assert reopened.get_run(run['id']) == held
    assert event(reopened, run, 'held', reason=reason) == held  # replay ignores old CAS
    with pytest.raises(HubError, match='active_run'):
        reopened.create_run('codex', task['id'], task['revision'], IMAGE, held['target'])
    with pytest.raises(HubError, match='active_run'):
        reopened.set_candidate('codex', task['id'], task['revision'], 'c' * 40)
    other = reviewed_task(reopened, 'another-task')
    with pytest.raises(HubError, match='active_run'):
        reopened.create_run('codex', other['id'], other['revision'], IMAGE, held['target'])
    assert len(reopened.list_runs()) == 1
    assert reopened.get_task(task['id'])['repair_count'] == 1


@pytest.mark.parametrize('first,second', [(None, 'staging_paused'), ('staging_paused', None),
                                        ('staging_paused', 'rollback_failed')])
def test_reason_is_part_of_event_replay_identity(repo, first, second):
    run = new_run(repo)
    held = event(repo, run, 'held', reason=first)
    assert event(repo, run, 'held', reason=first) == held
    with pytest.raises(HubError, match='event_conflict'):
        event(repo, run, 'held', reason=second)
    assert repo.get_run(run['id']) == held


def test_omitted_reason_preserves_legacy_entry_and_fingerprint(repo):
    run = new_run(repo)
    held = event(repo, run, 'held')
    assert 'reason' not in held['events'][-1]
    with repo._connection() as db:
        fingerprint = db.execute('SELECT fingerprint FROM hub_events WHERE run_id=?', (run['id'],)).fetchone()[0]
    assert fingerprint == json.dumps([PRINCIPAL, run['fencing_generation']], separators=(',', ':'))


@pytest.mark.parametrize('name,reason', [('failed', 'rollback_failed'), ('running', 'staging_paused'),
                                      ('rolled_back', 'rollback_failed'), ('held', 'arbitrary'),
                                      ('held', ''), ('held', []), ('held', True)])
def test_invalid_or_nonheld_reason_has_no_write(repo, name, reason):
    run = new_run(repo)
    before = repo.list_audit()
    with pytest.raises(HubError, match='invalid_event'):
        event(repo, run, name, reason=reason)
    assert repo.get_run(run['id']) == run
    assert repo.list_audit() == before


def test_failed_hold_failure_rolls_back_event_and_does_not_increment_repairs(repo, monkeypatch):
    run = event(repo, new_run(repo), 'failed')
    task = repo.get_task(run['task_id'])
    before = repo.list_audit()
    def fail(*args):
        raise RuntimeError('simulated outbox failure')
    with monkeypatch.context() as context:
        context.setattr(repo, '_emit', fail)
        with pytest.raises(RuntimeError, match='simulated'):
            event(repo, run, 'held', reason='rollback_outcome_uncertain')
    assert repo.get_run(run['id']) == run
    assert repo.get_task(task['id']) == task
    assert repo.list_audit() == before
    assert event(repo, run, 'held', reason='rollback_outcome_uncertain')['status'] == 'held'
