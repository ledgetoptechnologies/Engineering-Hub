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
        assert job['fencing_generation'] == index + 1
        job = repo.append_run_event(EXECUTOR, job['id'], 'failed', str(index), job['fencing_generation'], 1)
        current = repo.get_task(item['id'])
        if index < 2:
            with pytest.raises(HubError, match='active_run'):
                repo.create_run('codex', current['id'], current['revision'], DIGEST, 'engineering-hub:staging')
        repo.append_run_event(EXECUTOR, job['id'], 'rolled_back', str(index), job['fencing_generation'], job['revision'])
    item = repo.get_task(item['id'])
    assert item['repair_count'] == 3
    item = repo.set_candidate('codex', item['id'], item['revision'], 'c' * 40)
    assert item['repair_count'] == 3 and item['review'] is None
    item = repo.review_task('hermes', item['id'], item['revision'], 'c' * 40, 'approve')
    with pytest.raises(HubError, match='repair_limit'):
        repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging')


def test_target_generation_survives_restart_and_rejects_old_fence(repo):
    first = run(repo)
    first = repo.append_run_event(EXECUTOR, first['id'], 'running', 'first', first['fencing_generation'], 1)
    first = repo.append_run_event(EXECUTOR, first['id'], 'succeeded', 'first', first['fencing_generation'], first['revision'])
    reopened = HubRepository(repo.path, execution_principal=EXECUTOR)
    item = reopened.get_task(first['task_id'])
    second = reopened.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging')
    assert second['fencing_generation'] > first['fencing_generation']
    with pytest.raises(HubError, match='stale_fence'):
        reopened.append_run_event(EXECUTOR, second['id'], 'running', 'second', first['fencing_generation'], 1)
    second = reopened.append_run_event(EXECUTOR, second['id'], 'running', 'second', second['fencing_generation'], 1)
    assert second['status'] == 'running'


def test_failed_run_creation_rolls_back_fence_allocation(repo, monkeypatch):
    item = verified(repo)
    before = len(repo.list_audit())
    def fail(*args):
        raise sqlite3.OperationalError('simulated outbox failure')
    with monkeypatch.context() as context:
        context.setattr(repo, '_emit', fail)
        with pytest.raises(sqlite3.OperationalError):
            repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging')
    assert repo.list_runs() == []
    assert len(repo.list_audit()) == len(repo.list_outbox()) == before
    job = repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging')
    assert job['fencing_generation'] == 1


@pytest.mark.parametrize('manifest', ['latest', 'sha256:' + 'G' * 64, 'sha256:' + 'a' * 63, 123, True])
def test_invalid_execution_manifest_does_not_allocate_run_or_fence(repo, manifest):
    item = verified(repo)
    before = len(repo.list_audit())
    with pytest.raises(HubError, match='invalid_manifest_digest'):
        repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging', execution_manifest_digest=manifest)
    assert repo.list_runs() == []
    assert len(repo.list_audit()) == len(repo.list_outbox()) == before
    with repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_target_fences').fetchone()[0] == 0


def test_execution_manifest_atomically_snapshotted_and_preserved_by_event(repo):
    item = verified(repo)
    manifest = 'sha256:' + 'e' * 64
    job = repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging', execution_manifest_digest=manifest)
    assert repo.get_run(job['id'])['execution_manifest_digest'] == manifest
    job = repo.append_run_event(EXECUTOR, job['id'], 'running', 'one', job['fencing_generation'], job['revision'])
    assert job['execution_manifest_digest'] == manifest


def test_run_acceptance_receipt_survives_pause_state_change_and_restart(repo):
    item = verified(repo)
    manifest = 'sha256:' + 'e' * 64
    args = ('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging')
    first = repo.create_run(*args, execution_manifest_digest=manifest, client_request_id='request-one')
    first = repo.append_run_event(EXECUTOR, first['id'], 'running', 'one', first['fencing_generation'], first['revision'])
    repo.set_pause('human:1234', True)
    before = len(repo.list_audit())
    reopened = HubRepository(repo.path, execution_principal=EXECUTOR)
    assert reopened.create_run(*args, execution_manifest_digest=manifest, client_request_id='request-one') == first
    assert reopened.get_run_request('codex', 'request-one') == first
    assert reopened.get_run_request('hermes', 'request-one') is None
    assert len(reopened.list_audit()) == len(reopened.list_outbox()) == before
    assert len(reopened.list_runs()) == 1
    with repo._connection() as db:
        assert db.execute('SELECT generation FROM hub_target_fences').fetchone()[0] == 1


@pytest.mark.parametrize('change', ['revision', 'digest', 'manifest', 'target', 'task'])
def test_run_receipt_conflict(repo, change):
    item = verified(repo)
    kwargs = dict(actor='codex', task_id=item['id'], expected_revision=item['revision'], artifact_digest=DIGEST,
                  target='engineering-hub:staging', execution_manifest_digest='sha256:' + 'e' * 64,
                  client_request_id='request-one')
    repo.create_run(**kwargs)
    if change == 'revision':
        kwargs['expected_revision'] += 1
    elif change == 'digest':
        kwargs['artifact_digest'] = 'sha256:' + 'f' * 64
    elif change == 'manifest':
        kwargs['execution_manifest_digest'] = 'sha256:' + 'f' * 64
    elif change == 'target':
        kwargs['target'] = 'treedar:staging'
    else:
        kwargs['task_id'] = 'another-task'
    with pytest.raises(HubError, match='idempotency_conflict'):
        repo.create_run(**kwargs)
    assert len(repo.list_runs()) == 1


@pytest.mark.parametrize('failure', ['receipt', 'audit'])
def test_run_receipt_and_run_creation_rollback_atomically(repo, monkeypatch, failure):
    item = verified(repo)
    before = len(repo.list_audit())
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('simulated failure')
    with monkeypatch.context() as context:
        if failure == 'receipt':
            context.setattr(repo, '_record_run_request', fail)
        else:
            with repo._connection() as db:
                db.execute("CREATE TRIGGER reject_run_audit BEFORE INSERT ON hub_audit WHEN NEW.action = 'run.created' BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END")
        with pytest.raises(sqlite3.DatabaseError):
            repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging', client_request_id='receipt')
    assert repo.get_run_request('codex', 'receipt') is None
    assert repo.list_runs() == []
    assert len(repo.list_audit()) == len(repo.list_outbox()) == before
    assert repo.get_task(item['id'])['revision'] == item['revision']
    with repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_target_fences').fetchone()[0] == 0


@pytest.mark.parametrize('key', ['', ' ', 'x' * 129, 123])
def test_invalid_run_request_key(repo, key):
    item = verified(repo)
    with pytest.raises(HubError, match='invalid_input'):
        repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging', client_request_id=key)
    assert repo.list_runs() == []


@pytest.mark.parametrize('title,options', [('', ['a', 'b']), ('x'*201, ['a', 'b']), ('ok', ['a']), ('ok', ['a', 'a'])])
def test_decision_validation(repo, title, options):
    with pytest.raises(HubError):
        repo.create_decision('codex', 'treedar', title, options)
