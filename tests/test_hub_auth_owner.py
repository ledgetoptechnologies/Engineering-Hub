"""Authentication mutations obey the same live owner as the Hub ledger."""
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from engineering_hub.auth import SessionStore
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubError, HubRepository


def denied(call):
    with pytest.raises(HubError, match='^pilot_ownership_required$') as error:
        call()
    assert error.value.status == 409


@pytest.fixture
def enrolled(tmp_path):
    if sys.platform != 'linux':
        pytest.skip('Linux kernel pilot ownership capability')
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    path = directory / 'hub.sqlite'
    with PilotOwner(path, allow_ephemeral_test_storage=True) as owner:
        yield path, owner, HubRepository(path, pilot_owner=owner)


def tables(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        return db.execute('SELECT name, sql FROM sqlite_master ORDER BY name').fetchall()


def test_owner_auth_schema_state_and_session(enrolled):
    path, _, repo = enrolled
    store = SessionStore(path, repository=repo)
    state = store.create_state()
    assert store.consume_state(state['state']) == state['verifier']
    assert store.consume_state(state['state']) is None
    session = store.create_session(123)
    assert store.session_for(session['cookie_token'])['github_id'] == 123
    store.delete_session(session['cookie_token'])
    assert store.session_for(session['cookie_token']) is None


@pytest.mark.parametrize('operation', ['state', 'consume', 'session', 'delete', 'constructor'])
def test_closed_owner_denies_all_auth_mutations(enrolled, operation):
    path, owner, repo = enrolled
    store = SessionStore(path, repository=repo)
    state = store.create_state()
    session = store.create_session(123)
    before = tables(path)
    owner.close()
    action = {
        'state': store.create_state,
        'consume': lambda: store.consume_state(state['state']),
        'session': lambda: store.create_session(456),
        'delete': lambda: store.delete_session(session['cookie_token']),
        'constructor': lambda: SessionStore(path, repository=repo),
    }[operation]
    denied(action)
    assert tables(path) == before
    assert store.session_for(session['cookie_token'])['github_id'] == 123
    with store._read_db() as db:
        assert db.execute('SELECT count(*) FROM auth_states').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            db.execute('DELETE FROM auth_states')


def test_loss_during_auth_transaction_rolls_back(enrolled):
    path, owner, repo = enrolled
    store = SessionStore(path, repository=repo)
    store.clock = lambda: (owner.close(), 1000)[1]
    denied(lambda: store.create_session(123))
    with store._read_db() as db:
        assert db.execute('SELECT count(*) FROM auth_sessions').fetchone()[0] == 0


def test_unowned_auth_constructor_refuses_enrolled_database(enrolled):
    path, _, _ = enrolled
    before = tables(path)
    denied(lambda: SessionStore(path))
    assert tables(path) == before
    assert not any(name.startswith('auth_') for name, _ in before)


@pytest.mark.parametrize('marker', [
    'CREATE TABLE hub_pilot_owner (malformed TEXT)',
    'CREATE VIEW hub_pilot_owner AS SELECT 1',
    'CREATE TABLE HUB_PILOT_OWNER (malformed TEXT)',
])
def test_unowned_constructor_refuses_malformed_marker_without_schema_changes(tmp_path, marker):
    path = tmp_path / 'hub.sqlite'
    with sqlite3.connect(path) as db:
        db.execute(marker)
    before = tables(path)
    denied(lambda: SessionStore(path))
    assert tables(path) == before


@pytest.mark.parametrize('operation', ['state', 'consume', 'session', 'delete'])
def test_standalone_auth_refuses_late_enrollment(tmp_path, operation):
    path = tmp_path / 'hub.sqlite'
    store = SessionStore(path)
    state = store.create_state()
    session = store.create_session(123)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE hub_pilot_owner (malformed TEXT)')
    action = {
        'state': store.create_state,
        'consume': lambda: store.consume_state(state['state']),
        'session': lambda: store.create_session(456),
        'delete': lambda: store.delete_session(session['cookie_token']),
    }[operation]
    denied(action)
    assert store.session_for(session['cookie_token'])['github_id'] == 123
    with store._read_db() as db:
        assert db.execute('SELECT count(*) FROM auth_states').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM auth_sessions').fetchone()[0] == 1


def test_repository_mismatch_and_lookalike_are_sanitized_before_creation(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite')
    absent = tmp_path / 'secret-name.sqlite'
    denied(lambda: SessionStore(absent, repository=repo))
    denied(lambda: SessionStore(absent, repository=SimpleNamespace(path=str(absent))))
    assert not absent.exists()


def test_standalone_auth_does_not_create_hub_tables_and_read_is_readonly(tmp_path):
    path = tmp_path / 'auth.sqlite'
    store = SessionStore(path)
    session = store.create_session(123)
    assert {name for name, _ in tables(path) if not name.startswith('sqlite_')} == {
        'auth_sessions', 'auth_states'
    }
    assert store.session_for(session['cookie_token'])['github_id'] == 123
    with store._read_db() as db:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            db.execute('DELETE FROM auth_sessions')


def test_state_consumption_is_atomic_across_stores(tmp_path):
    path = tmp_path / 'auth.sqlite'
    first = SessionStore(path)
    second = SessionStore(path)
    state = first.create_state()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda store: store.consume_state(state['state']), (first, second)))
    assert results.count(state['verifier']) == 1
    assert results.count(None) == 1
