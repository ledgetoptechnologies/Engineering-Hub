"""Real bounded review writes and exact enrollment cell exceptions; no hosts."""
from contextlib import closing
import sqlite3

import pytest

from engineering_hub.components import ProjectComponentRepository
from engineering_hub.interactive_reviews import InteractiveReviewRepository, MAX_REVIEW_BYTES
from engineering_hub import pilot_enrollment as enrollment
from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubError, HubRepository


def fixture(tmp_path, count=360):
    repo = HubRepository(tmp_path / 'review.sqlite3')
    ProjectRepository(repo)
    components = ProjectComponentRepository(repo)
    component = components.create_component('human:1', 'engineering-hub', 'size-review', 'Size review',
                                            'component', status='active')
    store = InteractiveReviewRepository(repo, components, authorize=lambda *args: True)
    review = store.create_review('codex', 'engineering-hub', component['id'], 1,
        'size-topic', 'sha256:' + 'a' * 64, 'Synthetic size review',
        [{'id': 'item' + str(i), 'title': 'Item', 'prompt': 'Synthetic prompt'} for i in range(count)], 'create')
    return repo, store, review


def answer(store, review, start, end):
    return store.answer_review('human:1', review['id'], review['revision'], 'answers-' + str(start),
        [{'item_id': 'item' + str(i), 'value': 'é' * 2000} for i in range(start, end)])


def test_near_four_mib_normalized_review_is_saved_but_larger_batch_rolls_back(tmp_path):
    repo, store, review = fixture(tmp_path)
    for start, end in ((0, 100), (100, 200), (200, 300), (300, 340)):
        review = answer(store, review, start, end)
    with repo._connection() as db:
        before = db.execute('SELECT data FROM hub_interactive_reviews').fetchone()[0]
        audit = db.execute('SELECT count(*) FROM hub_audit').fetchone()[0]
        assert 3_900_000 < len(before.encode('utf-8')) <= MAX_REVIEW_BYTES
    with pytest.raises(HubError, match='review_state_too_large') as error:
        answer(store, review, 340, 360)
    assert error.value.status == 413
    with repo._connection() as db:
        assert db.execute('SELECT data FROM hub_interactive_reviews').fetchone()[0] == before
        assert db.execute('SELECT count(*) FROM hub_audit').fetchone()[0] == audit
        assert db.execute('SELECT count(*) FROM hub_interactive_review_answer_events').fetchone()[0] == 340
    assert store.get_state('human:1', review['id'])['answered_count'] == 340


def test_exact_snapshot_accepts_review_data_above_one_mib_without_relaxing_other_cells(tmp_path):
    repo, store, review = fixture(tmp_path, 120)
    review = answer(store, review, 0, 100)
    with closing(sqlite3.connect(repo.path)) as db:
        assert len(db.execute('SELECT data FROM hub_interactive_reviews').fetchone()[0].encode()) > 1024**2
        snapshot = enrollment._snapshot(db)
        assert snapshot['tables']['hub_interactive_reviews']['rows'] == 1
        assert snapshot['tables']['hub_interactive_review_answer_events']['rows'] == 100
        db.execute('INSERT INTO hub_entities VALUES(?,?,?)', ('synthetic-extra', 'task', 'x' * (1024**2 + 1)))
        db.commit()
        with pytest.raises(enrollment.EnrollmentError):
            enrollment._snapshot(db)


@pytest.mark.parametrize('column,value', [
    ('project', 'x' * (1024**2 + 1)),
    ('data', b'x' * (1024**2 + 1)),
    ('data', 'x' * (4 * 1024**2 + 1)),
], ids=['other-text-column', 'blob-data', 'oversized-data'])
def test_review_cell_exception_is_only_bounded_data_text(tmp_path, column, value):
    repo, _, _ = fixture(tmp_path, 1)
    with closing(sqlite3.connect(repo.path)) as db:
        db.execute('UPDATE hub_interactive_reviews SET ' + column + '=?', (value,))
        db.commit()
        with pytest.raises((enrollment.EnrollmentError, sqlite3.DataError)):
            enrollment._snapshot(db)
