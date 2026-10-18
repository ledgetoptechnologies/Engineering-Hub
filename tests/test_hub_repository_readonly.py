"""Read handles are SQLite read-only, not an alternate enrolled writer API."""
import sqlite3
import sys

import pytest

from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubError, HubRepository


@pytest.fixture
def enrolled(tmp_path):
    if sys.platform != 'linux':
        pytest.skip('Actual Linux ownership is required')
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    path = private / 'hub.sqlite'
    owner = PilotOwner(path, allow_ephemeral_test_storage=True)
    try:
        yield owner, HubRepository(path, pilot_owner=owner)
    finally:
        owner.close()


def assert_readonly(db):
    with pytest.raises(sqlite3.OperationalError) as error:
        db.execute("INSERT INTO hub_entities VALUES('forged','task','{}')")
    assert error.value.sqlite_errorcode == sqlite3.SQLITE_READONLY
    with pytest.raises(sqlite3.OperationalError) as error:
        db.execute('CREATE TABLE accidental_write (id INTEGER)')
    assert error.value.sqlite_errorcode == sqlite3.SQLITE_READONLY


def test_active_owner_read_context_is_sqlite_readonly(enrolled):
    _, repo = enrolled
    with repo._connection() as db:
        assert_readonly(db)
        assert db.execute('SELECT count(*) FROM hub_entities').fetchone()[0] == 0


def test_late_marker_makes_preexisting_unowned_repository_readonly(tmp_path, monkeypatch):
    repo = HubRepository(tmp_path / 'hub.sqlite')
    with repo._connection() as db:
        # A synthetic marker is enough to deny an unowned writer. It is not
        # accepted as a PilotOwner enrollment or used to authorize anything.
        db.execute('CREATE TABLE hub_pilot_owner (id INTEGER)')
        db.commit()
    opened = []
    original_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        opened.append((database, kwargs.get('uri')))
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    with repo._connection() as db:
        assert_readonly(db)
    assert len(opened) == 1 and opened[0][0].endswith('?mode=ro') and opened[0][1] is True
    with pytest.raises(HubError, match='pilot_ownership_required'):
        repo.create_task('human:1234', 'engineering-hub', 'Denied', 'denied')


def test_real_enrolled_read_context_without_capability_remains_readonly(enrolled):
    _, repo = enrolled
    read_view = object.__new__(HubRepository)
    read_view.path, read_view.pilot_owner = repo.path, None
    with read_view._connection() as db:
        assert_readonly(db)


def test_enrolled_normal_task_mutations_use_guarded_writer(enrolled):
    _, repo = enrolled
    task = repo.create_task('human:1234', 'engineering-hub', 'Normal', 'normal')
    claimed = repo.claim_task('codex', task['id'], task['revision'])
    assert claimed['owner'] == 'codex'
    assert repo.get_task(task['id']) == claimed


def test_owner_loss_does_not_permit_read_handle_or_task_mutation(enrolled):
    owner, repo = enrolled
    task = repo.create_task('human:1234', 'engineering-hub', 'Original', 'original')
    with repo._connection() as db:
        before = list(db.execute('SELECT * FROM hub_entities'))
    owner.close()
    with repo._connection() as db:
        assert_readonly(db)
    with pytest.raises(HubError, match='pilot_ownership_required'):
        repo.claim_task('codex', task['id'], task['revision'])
    with repo._connection() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM hub_entities')] == [tuple(row) for row in before]


def test_owner_missing_database_read_context_never_recreates(enrolled):
    _, repo = enrolled
    from pathlib import Path
    path = Path(repo.path)
    path.unlink()
    with pytest.raises(sqlite3.OperationalError):
        with repo._connection():
            pass
    assert not path.exists()
    with pytest.raises(HubError, match='pilot_ownership_required'):
        repo.create_task('human:1234', 'engineering-hub', 'Denied', 'missing')
    assert not path.exists()
