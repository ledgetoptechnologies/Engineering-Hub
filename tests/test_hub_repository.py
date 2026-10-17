from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from engineering_hub.repository import HubError, HubRepository


COMMIT = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
EXECUTOR = 'executor:viewer-staging'


@pytest.fixture
def repo(tmp_path):
    return HubRepository(tmp_path / 'hub.sqlite3', execution_principal=EXECUTOR)


def task(repo):
    item = repo.create_task('human:1234', 'engineering-hub', 'Pilot', 'request1')
    return repo.claim_task('codex', item['id'], item['revision'])


def verified(repo):
    item = task(repo)
    item = repo.set_candidate('codex', item['id'], item['revision'], COMMIT)
    return repo.review_task('claude', item['id'], item['revision'], COMMIT, 'approve')


def run(repo):
    item = verified(repo)
    return repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging')


def test_persistence_and_atomic_audit(repo):
    item = task(repo)
    repo.close()
    reopened = HubRepository(repo.path)
    assert reopened.get_task(item['id']) == item
    assert len(reopened.list_audit()) == len(reopened.list_outbox()) == 2
    assert 'title' not in str(reopened.list_outbox())


def test_task_idempotency_and_payload_conflict(repo):
    first = repo.create_task('human:1234', 'treedar', 'Pilot', 'same')
    assert repo.create_task('human:1234', 'treedar', 'Pilot', 'same') == first
    assert len(repo.list_outbox()) == 1
    with pytest.raises(HubError, match='idempotency_conflict'):
        repo.create_task('human:1234', 'treedar', 'Different', 'same')
    assert len(repo.list_tasks()) == 1


@pytest.mark.parametrize('actor', ['codex', 'unknown', 'human:', 'human:beau', 'human:0', 'human:-1', 'human:1.0'])
def test_create_requires_human(repo, actor):
    with pytest.raises(HubError, match='forbidden'):
        repo.create_task(actor, 'treedar', 'Pilot', 'key')


def test_claim_contention(repo):
    item = repo.create_task('human:1234', 'treedar', 'Pilot', 'key')
    def claim(actor):
        try:
            return HubRepository(repo.path).claim_task(actor, item['id'], 1)['owner']
        except HubError:
            return None
    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(claim, ['hermes', 'codex']))
    assert sum(value is not None for value in result) == 1
    assert len(repo.list_outbox()) == 2


def test_outbox_failure_rolls_back_everything(repo, monkeypatch):
    def fail(*args):
        raise sqlite3.OperationalError('simulated')
    monkeypatch.setattr(repo, '_emit', fail)
    with pytest.raises(sqlite3.OperationalError):
        repo.create_task('human:1234', 'treedar', 'Pilot', 'key')
    assert repo.list_tasks() == repo.list_audit() == repo.list_outbox() == []


def test_candidate_invalidates_review_and_independent_review(repo):
    item = verified(repo)
    item = repo.set_candidate('codex', item['id'], item['revision'], 'c' * 40)
    assert item['review'] is None and item['status'] == 'in_review'
    with pytest.raises(HubError, match='independent_review_required'):
        repo.review_task('codex', item['id'], item['revision'], 'c' * 40, 'approve')
    with pytest.raises(HubError, match='stale_review'):
        repo.review_task('hermes', item['id'], item['revision'], COMMIT, 'approve')
    item = repo.review_task('hermes', item['id'], item['revision'], 'c' * 40, 'reject')
    assert item['status'] == 'claimed' and item['review'] is None


def test_cas_and_owner(repo):
    item = task(repo)
    with pytest.raises(HubError, match='revision_conflict'):
        repo.set_candidate('codex', item['id'], 1, COMMIT)
    with pytest.raises(HubError, match='forbidden'):
        repo.set_candidate('claude', item['id'], item['revision'], COMMIT)


@pytest.mark.parametrize('protected', [False, True])
def test_decisions_no_agent_approval(repo, protected):
    item = repo.create_decision('codex', 'treedar', 'Storage?', ['NAS', 'SSD'], protected=protected)
    assert item['execution_authorization'] is False
    with pytest.raises(HubError, match='forbidden'):
        repo.answer_decision('hermes', item['id'], 1, 'NAS')
    answer = repo.answer_decision('human:1234', item['id'], 1, 'NAS')
    assert answer['answered_by'] == 'human:1234'
    assert answer['execution_authorization'] is False
    with pytest.raises(HubError):
        repo.answer_decision('human:1234', item['id'], 1, 'SSD')


def test_runs_exact_review_staging_only_and_single_active(repo):
    item = verified(repo)
    with pytest.raises(HubError, match='invalid_target'):
        repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:production')
    job = repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging')
    current = repo.get_task(item['id'])
    with pytest.raises(HubError, match='active_run'):
        repo.create_run('codex', item['id'], current['revision'], DIGEST, 'engineering-hub:staging')
    with pytest.raises(HubError, match='active_run'):
        repo.set_candidate('codex', item['id'], current['revision'], 'c' * 40)
    assert job['commit'] == COMMIT and job['review']['actor'] == 'claude'


def test_target_reservation_prevents_conflicting_tasks(repo):
    run(repo)
    item = repo.create_task('human:1234', 'engineering-hub', 'Other task', 'other')
    item = repo.claim_task('hermes', item['id'], item['revision'])
    item = repo.set_candidate('hermes', item['id'], item['revision'], COMMIT)
    item = repo.review_task('claude', item['id'], item['revision'], COMMIT, 'approve')
    with pytest.raises(HubError, match='active_run'):
        repo.create_run('hermes', item['id'], item['revision'], DIGEST, 'engineering-hub:staging')


def test_run_events_fences_idempotence_and_recovery(repo):
    job = run(repo)
    for actor in ['codex', 'hermes', 'claude', 'human:1234', 'executor:other']:
        with pytest.raises(HubError, match='forbidden'):
            repo.append_run_event(actor, job['id'], 'running', 'command1', 1, 1)
    job = repo.append_run_event(EXECUTOR, job['id'], 'running', 'command1', 1, 1)
    assert repo.append_run_event(EXECUTOR, job['id'], 'running', 'command1', 1, 1) == job
    with pytest.raises(HubError, match='forbidden'):
        repo.append_run_event('hermes', job['id'], 'succeeded', 'command1', 1, job['revision'])
    with pytest.raises(HubError, match='command_conflict'):
        repo.append_run_event(EXECUTOR, job['id'], 'succeeded', 'other', 1, job['revision'])
    with pytest.raises(HubError, match='stale_fence'):
        repo.append_run_event(EXECUTOR, job['id'], 'succeeded', 'command1', 2, job['revision'])
    job = repo.append_run_event(EXECUTOR, job['id'], 'held', 'command1', 1, job['revision'])
    with pytest.raises(HubError, match='stale_fence'):
        repo.append_run_event(EXECUTOR, job['id'], 'running', 'command2', 2, job['revision'])
    assert repo.append_run_event(EXECUTOR, job['id'], 'running', 'command1', 1, job['revision'])['status'] == 'held'
    job = repo.append_run_event(EXECUTOR, job['id'], 'failed', 'command1', 1, job['revision'])
    job = repo.append_run_event(EXECUTOR, job['id'], 'rolled_back', 'command1', 1, job['revision'])
    assert repo.get_run(job['id']) == job


def test_executor_default_disabled_and_configuration_binding(repo):
    job = run(repo)
    default = HubRepository(repo.path)
    with pytest.raises(HubError, match='forbidden'):
        default.append_run_event(EXECUTOR, job['id'], 'running', 'command1', 1, 1)
    other = HubRepository(repo.path, execution_principal='executor:other')
    with pytest.raises(HubError, match='forbidden'):
        other.append_run_event('executor:other', job['id'], 'running', 'command1', 1, 1)


@pytest.mark.parametrize('identity', ['codex', 'human:1234', 'executor:', 'executor:UPPER', 'executor:a/b'])
def test_invalid_executor_configuration(tmp_path, identity):
    with pytest.raises(HubError, match='invalid_execution_principal'):
        HubRepository(tmp_path / 'invalid.sqlite3', execution_principal=identity)


def test_pause_and_three_repairs(repo):
    item = verified(repo)
    repo.set_pause('human:1234', True)
    with pytest.raises(HubError, match='paused'):
        repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging')
    with pytest.raises(HubError, match='forbidden'):
        repo.set_pause('hermes', False)
    repo.set_pause('human:1234', False)
    for index in range(3):
        item = repo.get_task(item['id'])
        job = repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging')
        repo.append_run_event(EXECUTOR, job['id'], 'failed', str(index), 1, 1)
    item = repo.get_task(item['id'])
    assert item['repair_count'] == 3
    item = repo.set_candidate('codex', item['id'], item['revision'], 'c' * 40)
    assert item['repair_count'] == 3 and item['review'] is None
    item = repo.review_task('hermes', item['id'], item['revision'], 'c' * 40, 'approve')
    with pytest.raises(HubError, match='repair_limit'):
        repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging')


@pytest.mark.parametrize('title,options', [('', ['a', 'b']), ('x'*201, ['a', 'b']), ('ok', ['a']), ('ok', ['a', 'a'])])
def test_decision_validation(repo, title, options):
    with pytest.raises(HubError):
        repo.create_decision('codex', 'treedar', title, options)
