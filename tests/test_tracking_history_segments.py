import pytest

from engineering_hub.repository import HubError, HubRepository, _json
from engineering_hub.pilot_enrollment import EnrollmentError, _snapshot
import sqlite3


def test_progress_rollover_preserves_history_and_allows_completion(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    task = repo.propose_tracking_task('codex', 'engineering-hub', 'Long work', 'propose')
    identity = task['id']
    for index in range(101):
        task = repo.update_tracking_task('codex', identity, task['revision'], f'update-{index}',
            progress=f'progress {index}', evidence_links=[])
    assert len(task['updates']) == 3
    assert task['history_segments']['updates'] == 2
    older = repo.tracking_history(identity, 'updates')
    assert len(older['records']) == 49 and older['records'][0]['progress'] == 'progress 49'
    assert older['next_before'] == 1
    oldest = repo.tracking_history(identity, 'updates', older['next_before'])
    assert oldest['records'][0]['progress'] == 'progress 0' and oldest['next_before'] is None
    claim = repo.update_tracking_task('codex', identity, task['revision'], 'completion',
        progress='Done', evidence_links=[{'label': 'proof', 'url': 'https://example.test/proof'}], completion_claim=True)
    result = repo.review_tracking_task('claude', identity, claim['revision'], 'review', verdict='verify',
        note='Checked', evidence_links=claim['updates'][-1]['evidence_links'])
    assert result['status'] == 'completion_verified'


def test_review_rollover_preserves_reopen_and_independence(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    task = repo.propose_tracking_task('codex', 'engineering-hub', 'Recurring work', 'propose')
    proof = [{'label': 'proof', 'url': 'https://example.test/proof'}]
    for index in range(26):
        task = repo.update_tracking_task('codex', task['id'], task['revision'], f'claim-{index}',
            progress='Done', evidence_links=proof, completion_claim=True)
        with pytest.raises(HubError, match='independent_review_required'):
            repo.review_tracking_task('codex', task['id'], task['revision'], f'self-{index}',
                verdict='verify', note='Checked', evidence_links=proof)
        task = repo.review_tracking_task('claude', task['id'], task['revision'], f'verify-{index}',
            verdict='verify', note='Checked', evidence_links=proof)
        task = repo.review_tracking_task('claude', task['id'], task['revision'], f'reopen-{index}',
            verdict='reopen', note='More work', evidence_links=[])
    assert task['history_segments']['reviews'] == 1 and len(task['reviews']) == 3
    history = repo.tracking_history(task['id'], 'reviews')
    assert len(history['records']) == 49 and history['records'][0]['verdict'] == 'verify'
    assert task['status'] == 'open'


def test_bad_history_cursor_and_corrupt_segment_fail_closed(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    task = repo.propose_tracking_task('codex', 'engineering-hub', 'Long work', 'propose')
    for index in range(51):
        task = repo.update_tracking_task('codex', task['id'], task['revision'], f'update-{index}',
            progress='progress', evidence_links=[])
    with pytest.raises(HubError, match='invalid_cursor'):
        repo.tracking_history(task['id'], 'updates', 2)
    with repo._transaction() as db:
        db.execute('DELETE FROM hub_entities WHERE id=?', (repo._tracking_segment_id(task['id'], 'updates', 0),))
    with pytest.raises(HubError, match='tracking_history_corrupt'):
        repo.tracking_history(task['id'], 'updates')


def test_large_unicode_history_remains_enrollable_and_retrievable(tmp_path):
    path = tmp_path / 'hub.db'
    repo = HubRepository(path)
    task = repo.propose_tracking_task('codex', 'engineering-hub', 'Long work', 'propose')
    for index in range(50):
        task = repo.update_tracking_task('codex', task['id'], task['revision'], f'update-{index}',
            progress='😀' * 4096, evidence_links=[])
    segments = task['history_segments']['updates']
    total = len(task['updates'])
    cursor = None
    for _ in range(segments):
        page = repo.tracking_history(task['id'], 'updates', cursor)
        total += len(page['records'])
        assert all(record['progress'] == '😀' * 4096 for record in page['records'])
        cursor = page['next_before']
    assert total == 50 and cursor is None
    with sqlite3.connect(path) as db:
        _snapshot(db)


def test_large_completion_claim_does_not_block_first_review(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    task = repo.propose_tracking_task('codex', 'engineering-hub', 'Large claim', 'propose')
    for index in range(5):
        task = repo.update_tracking_task('codex', task['id'], task['revision'], f'update-{index}',
            progress='😀' * 4096, evidence_links=[])
    proof = [{'label': 'proof', 'url': 'https://example.test/proof'}]
    task = repo.update_tracking_task('codex', task['id'], task['revision'], 'claim',
        progress='😀' * 4096, evidence_links=proof, completion_claim=True)
    task = repo.review_tracking_task('claude', task['id'], task['revision'], 'verify',
        verdict='verify', note='Checked', evidence_links=proof)
    assert task['status'] == 'completion_verified'
    assert task['updates'][-1]['completion_claim'] is True
    assert task['history_segments']['updates'] >= 1


def test_oversized_legacy_window_is_split_without_loss_by_explicit_compaction(tmp_path):
    path = tmp_path / 'hub.db'
    repo = HubRepository(path)
    task = repo.propose_tracking_task('codex', 'engineering-hub', 'Legacy', 'propose')
    # Simulate an untouched legacy task with valid but oversized embedded data.
    task['updates'] = [dict(actor='codex', progress='😀' * 4096, evidence_links=[],
                            completion_claim=False, actor_family='codex', revision=i + 2) for i in range(50)]
    task['revision'] = 51
    with repo._transaction() as db:
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (_json(task), task['id']))
    with sqlite3.connect(path) as db, pytest.raises(EnrollmentError):
        _snapshot(db)
    task = repo.compact_tracking_history('codex', task['id'], 51, 'compact-legacy')
    assert task['revision'] == 52 and task['status'] == 'open'
    assert repo.compact_tracking_history('codex', task['id'], 51, 'compact-legacy') == task
    with pytest.raises(HubError, match='idempotency_conflict'):
        repo.compact_tracking_history('codex', task['id'], 52, 'compact-legacy')
    count = len(task['updates'])
    cursor = None
    for _ in range(task['history_segments']['updates']):
        page = repo.tracking_history(task['id'], 'updates', cursor)
        count += len(page['records'])
        cursor = page['next_before']
    assert count == 50 and cursor is None
    with sqlite3.connect(path) as db:
        _snapshot(db)


def test_oversized_legacy_updates_compact_when_reopening_one_review(tmp_path):
    path = tmp_path / 'hub.db'
    repo = HubRepository(path)
    task = repo.propose_tracking_task('codex', 'engineering-hub', 'Legacy verified', 'propose')
    proof = [{'label': 'proof', 'url': 'https://example.test/proof'}]
    task['updates'] = [dict(actor='codex', progress='😀' * 4096, evidence_links=proof,
                            completion_claim=i == 49, actor_family='codex', revision=i + 2) for i in range(50)]
    task['reviews'] = [dict(actor='claude', actor_family='claude', verdict='verify', note='Checked',
                            evidence_links=proof, claim_revision=51)]
    task['status'], task['revision'] = 'completion_verified', 52
    with repo._transaction() as db:
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (_json(task), task['id']))
    reopened = repo.review_tracking_task('claude', task['id'], 52, 'reopen-large',
                                          verdict='reopen', note='Further work', evidence_links=[])
    assert reopened['status'] == 'open' and reopened['updates'][-1]['completion_claim'] is True
    assert reopened['reviews'][-1]['claim_revision'] == 51
    with sqlite3.connect(path) as db:
        _snapshot(db)


def test_legacy_claim_revision_survives_explicit_compaction_and_review(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    proof = [{'label': 'proof', 'url': 'https://example.test/proof'}]
    task = repo.propose_tracking_task('codex', 'engineering-hub', 'Legacy claim', 'propose')
    task = repo.update_tracking_task('codex', task['id'], 1, 'claim', progress='Done',
                                     evidence_links=proof, completion_claim=True)
    del task['updates'][-1]['revision']
    with repo._transaction() as db:
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (_json(task), task['id']))
    compacted = repo.compact_tracking_history('codex', task['id'], 2, 'compact')
    assert compacted['revision'] == 3 and compacted['updates'][-1]['revision'] == 2
    with pytest.raises(HubError, match='independent_review_required'):
        repo.review_tracking_task('codex', task['id'], 3, 'self', verdict='verify', note='No', evidence_links=proof)
    verified = repo.review_tracking_task('claude', task['id'], 3, 'verify', verdict='verify', note='Checked', evidence_links=proof)
    assert verified['reviews'][-1]['claim_revision'] == 2
    reopened = repo.review_tracking_task('claude', task['id'], 4, 'reopen', verdict='reopen', note='More work', evidence_links=[])
    assert reopened['reviews'][-1]['claim_revision'] == 2


def test_segment_collision_rolls_back_task_and_request(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    task = repo.propose_tracking_task('codex', 'engineering-hub', 'Collision', 'propose')
    for i in range(50):
        task = repo.update_tracking_task('codex', task['id'], task['revision'], f'progress-{i}',
                                         progress='Progress', evidence_links=[])
    segment_id = repo._tracking_segment_id(task['id'], 'updates', 0)
    with repo._transaction() as db:
        db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)', (segment_id, 'other', '{}'))
    with pytest.raises(HubError, match='tracking_history_corrupt'):
        repo.update_tracking_task('codex', task['id'], task['revision'], 'collision', progress='Next', evidence_links=[])
    assert repo._read(task['id'], 'tracking_task') == task
    with repo._connection() as db:
        assert db.execute('SELECT 1 FROM hub_requests WHERE actor=? AND key=?', ('codex', 'collision')).fetchone() is None
