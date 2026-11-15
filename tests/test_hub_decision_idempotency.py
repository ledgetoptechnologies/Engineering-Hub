from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from engineering_hub.repository import HubError, HubRepository


def test_decision_receipts_survive_restart_and_are_scoped(tmp_path):
    path = tmp_path / 'hub.sqlite3'
    repo = HubRepository(path)
    def request(actor='codex', project='treedar', **kwargs):
        return repo.create_decision(actor, project, 'Question', ['Yes', 'No'],
                                    client_request_id='request-1', **kwargs)
    first = request(details='Details')
    repo = HubRepository(path)
    assert request(details='Details')['id'] == first['id']
    with pytest.raises(HubError, match='idempotency_conflict'):
        request(details='Changed')
    assert request('claude', details='Details')['id'] != first['id']
    assert request(project='project-alpha', details='Details')['id'] != first['id']
    # A task request using the same key occupies a different namespace.
    repo.propose_staging_task('codex', 'treedar', 'Task', 'request-1')
    assert len(repo.list_decisions()) == 3
    assert repo.create_decision('codex', 'treedar', 'Question', ['Yes', 'No'])['id'] != first['id']
    assert repo.create_decision('codex', 'treedar', 'Question', ['Yes', 'No'])['id'] != first['id']


def test_concurrent_decision_retries_commit_once(tmp_path):
    path = tmp_path / 'hub.sqlite3'
    repos = [HubRepository(path), HubRepository(path)]
    barrier = threading.Barrier(2)
    def request(repo):
        barrier.wait(timeout=5)
        return repo.create_decision('codex', 'treedar', 'Question', ['Yes', 'No'],
                                    client_request_id='same-request')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, repos))
    assert results[0]['id'] == results[1]['id']
    with repos[0]._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_decision_requests').fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM hub_audit WHERE action='decision.created'").fetchone()[0] == 1


def test_existing_receipt_table_failure_rolls_back(tmp_path):
    path = tmp_path / 'hub.sqlite3'
    repo = HubRepository(path)
    repo.create_decision('codex', 'treedar', 'First', ['Yes', 'No'], client_request_id='first')
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON hub_decision_requests "
                   "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected failure'):
        repo.create_decision('codex', 'treedar', 'Question', ['Yes', 'No'], client_request_id='key')
    assert len(repo.list_decisions()) == 1
    assert len(repo.list_audit()) == 1
    with sqlite3.connect(path) as db:
        db.execute('DROP TRIGGER reject_receipt')
    assert repo.create_decision('codex', 'treedar', 'Question', ['Yes', 'No'], client_request_id='key')


def test_failed_first_keyed_request_rolls_back_table_creation(tmp_path, monkeypatch):
    path = tmp_path / 'hub.sqlite3'
    repo = HubRepository(path)
    def fail(*args, **kwargs):
        raise RuntimeError('injected failure')
    monkeypatch.setattr(repo, '_save', fail)
    with pytest.raises(RuntimeError, match='injected failure'):
        repo.create_decision('codex', 'treedar', 'Question', ['Yes', 'No'], client_request_id='key')
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='hub_decision_requests'").fetchone()
