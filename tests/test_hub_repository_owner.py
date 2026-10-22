"""Generic repository writes cannot bypass enrolled pilot ownership."""
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubError, HubRepository


@pytest.fixture
def enrolled(tmp_path):
    if sys.platform != 'linux':
        pytest.skip('Linux kernel pilot ownership capability')
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    path = directory / 'hub.sqlite'
    with PilotOwner(path, allow_ephemeral_test_storage=True) as owner:
        yield path, owner


def assert_denied(call):
    with pytest.raises(HubError, match='^pilot_ownership_required$') as error:
        call()
    assert error.value.status == 409


def test_missing_owner_constructor_never_creates_hub_schema_or_enables_wal(enrolled):
    path, _ = enrolled
    assert_denied(lambda: HubRepository(path))
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert db.execute("SELECT name FROM sqlite_master WHERE name='hub_entities'").fetchone() is None


@pytest.mark.parametrize('owner', [True, False, 'saved-epoch', SimpleNamespace(assert_active=lambda path: None)])
def test_non_capability_denied_without_creating_database(tmp_path, owner):
    path = tmp_path / 'absent.sqlite'
    assert_denied(lambda: HubRepository(path, pilot_owner=owner))
    assert not path.exists()


def test_live_owner_allows_generic_task_writes(enrolled):
    path, owner = enrolled
    repo = HubRepository(path, pilot_owner=owner)
    task = repo.create_task('human:123', 'engineering-hub', 'Owner guarded', 'first')
    claimed = repo.claim_task('codex', task['id'], task['revision'])
    assert claimed['owner'] == 'codex'
    assert len(repo.list_audit()) == 2


def test_owner_bound_to_exact_database(enrolled, tmp_path):
    _, owner = enrolled
    other = tmp_path / 'different.sqlite'
    assert_denied(lambda: HubRepository(other, pilot_owner=owner))
    assert not other.exists()


def test_closed_owner_denies_write_but_allows_existing_repository_reads(enrolled):
    path, owner = enrolled
    repo = HubRepository(path, pilot_owner=owner)
    task = repo.create_task('human:123', 'engineering-hub', 'Read after close', 'first')
    before = repo.list_audit()
    owner.close()
    assert_denied(lambda: repo.create_task('human:123', 'engineering-hub', 'Denied', 'next'))
    assert_denied(lambda: HubRepository(path, pilot_owner=owner))
    assert repo.get_task(task['id']) == task
    assert repo.list_audit() == before


@pytest.mark.parametrize('operation', ['create_task', 'append_run_event'])
def test_enrolled_repository_cannot_mutate_without_its_capability(enrolled, operation):
    path, owner = enrolled
    repo = HubRepository(path, pilot_owner=owner, execution_principal='executor:pilot')
    repo.pilot_owner = None
    if operation == 'create_task':
        action = lambda: repo.create_task('human:123', 'engineering-hub', 'Denied', 'first')
    else:
        action = lambda: repo.append_run_event('executor:pilot', 'missing', 'running', 'missing', 1, 1)
    assert_denied(action)
    assert repo.list_tasks() == []
    assert repo.list_audit() == []


@pytest.mark.parametrize('operation', ['create_task', 'append_run_event'])
def test_legacy_repository_rechecks_late_enrollment_before_mutation(tmp_path, operation):
    repo = HubRepository(tmp_path / 'hub.sqlite', execution_principal='executor:pilot')
    # A marker is sufficient to deny an unowned writer, even when malformed.
    with repo._connection() as db:
        db.execute('CREATE TABLE hub_pilot_owner (untrusted TEXT)')
    before = repo.list_audit()
    if operation == 'create_task':
        action = lambda: repo.create_task('human:123', 'engineering-hub', 'Denied', 'first')
    else:
        action = lambda: repo.append_run_event('executor:pilot', 'missing', 'running', 'missing', 1, 1)
    assert_denied(action)
    assert repo.list_audit() == before


def test_loss_before_commit_rolls_back_entities_audit_outbox_and_request(enrolled, monkeypatch):
    path, owner = enrolled
    repo = HubRepository(path, pilot_owner=owner)
    original = repo._save

    def save_then_lose_owner(*args, **kwargs):
        result = original(*args, **kwargs)
        owner.close()
        return result

    monkeypatch.setattr(repo, '_save', save_then_lose_owner)
    assert_denied(lambda: repo.create_task('human:123', 'engineering-hub', 'Rolled back', 'first'))
    with repo._connection() as db:
        for table in ('hub_entities', 'hub_audit', 'hub_outbox', 'hub_requests'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_unenrolled_legacy_repository_remains_compatible(tmp_path):
    path = tmp_path / 'legacy.sqlite'
    repo = HubRepository(path)
    task = repo.create_task('human:123', 'engineering-hub', 'Legacy', 'first')
    assert HubRepository(path).get_task(task['id']) == task


@pytest.mark.parametrize('operation', ['task', 'proposal', 'decision'])
def test_missing_enrolled_database_is_not_recreated(enrolled, operation):
    path, owner = enrolled
    repo = HubRepository(path, pilot_owner=owner)
    path.rename(path.with_name('preserved.sqlite'))
    actions = {
        'task': lambda: repo.create_task('human:123', 'engineering-hub', 'Denied', 'missing'),
        'proposal': lambda: repo.propose_staging_task('codex', 'engineering-hub', 'Denied', 'missing'),
        'decision': lambda: repo.create_decision('codex', 'engineering-hub', 'Denied', ['A', 'B']),
    }
    assert_denied(actions[operation])
    assert not path.exists()
