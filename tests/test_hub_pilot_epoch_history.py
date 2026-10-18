"""Real Linux owner ancestry; no process replacement or service activation."""
import sqlite3
import sys
import uuid

import pytest

from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux ownership prerequisite')


@pytest.fixture
def database(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    return private / 'hub.sqlite'


def acquire(database):
    return PilotOwner(database, allow_ephemeral_test_storage=True)


def test_fresh_history_and_multiple_idle_restarts(database):
    with acquire(database) as first:
        original = first.epoch
        with sqlite3.connect(database) as db:
            assert db.execute('SELECT * FROM hub_pilot_owner_epochs').fetchall() == [(original, None)]
    prior = [original]
    for _ in range(4):
        with acquire(database) as owner:
            assert owner.previous_epoch == prior[-1]
            for epoch in prior:
                owner.assert_prior_epoch(epoch)
            with pytest.raises(PilotOwnershipError, match='^pilot ownership unavailable$'):
                owner.assert_prior_epoch(owner.epoch)
            prior.append(owner.epoch)


@pytest.mark.parametrize('invalid', [None, 1, '', 'not-an-epoch', str(uuid.uuid4()).upper(), str(uuid.uuid4())])
def test_unknown_or_noncanonical_epoch_is_not_authority(database, invalid):
    with acquire(database) as owner:
        with pytest.raises(PilotOwnershipError, match='^pilot ownership unavailable$'):
            owner.assert_prior_epoch(invalid)


def test_closed_capability_cannot_prove_ancestry(database):
    with acquire(database) as first:
        original = first.epoch
    with acquire(database) as owner:
        owner.assert_prior_epoch(original)
    with pytest.raises(PilotOwnershipError):
        owner.assert_prior_epoch(original)


def test_public_epoch_attribute_is_not_ancestry_authority(database):
    with acquire(database) as first:
        original = first.epoch
    with acquire(database) as owner:
        current = owner.epoch
        with pytest.raises(AttributeError):
            owner.epoch = original
        owner.assert_prior_epoch(original)
        with pytest.raises(PilotOwnershipError):
            owner.assert_prior_epoch(current)


@pytest.mark.parametrize('mutation', [
    'DROP TABLE hub_pilot_owner_epochs',
    'DELETE FROM hub_pilot_owner_epochs',
    "UPDATE hub_pilot_owner_epochs SET previous_epoch='invalid'",
])
def test_missing_or_mismatched_current_history_refuses_live_and_restart(database, mutation):
    with acquire(database) as owner:
        with sqlite3.connect(database) as db:
            db.execute(mutation)
        with pytest.raises(PilotOwnershipError):
            owner.assert_active(database)
    with pytest.raises(PilotOwnershipError):
        acquire(database)


def test_malformed_history_schema_is_not_silently_migrated(database):
    with acquire(database) as owner:
        epoch = owner.epoch
    with sqlite3.connect(database) as db:
        db.execute('DROP TABLE hub_pilot_owner_epochs')
        db.execute('CREATE TABLE hub_pilot_owner_epochs(epoch TEXT PRIMARY KEY, previous_epoch TEXT, extra TEXT)')
        db.execute('INSERT INTO hub_pilot_owner_epochs VALUES(?,?,?)', (epoch, None, None))
    with pytest.raises(PilotOwnershipError):
        acquire(database)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT epoch FROM hub_pilot_owner').fetchone() == (epoch,)


@pytest.mark.parametrize('kind', ['cycle', 'missing', 'invalid'])
def test_broken_older_ancestry_refuses_even_if_requested_epoch_seen(database, kind):
    with acquire(database) as first:
        original = first.epoch
    with acquire(database) as second:
        middle = second.epoch
    with acquire(database) as owner:
        with sqlite3.connect(database) as db:
            if kind == 'missing':
                db.execute('DELETE FROM hub_pilot_owner_epochs WHERE epoch=?', (original,))
            else:
                predecessor = middle if kind == 'cycle' else 'invalid'
                db.execute('UPDATE hub_pilot_owner_epochs SET previous_epoch=? WHERE epoch=?', (predecessor, original))
        # The current row still matches; historical proof must inspect the
        # entire chain rather than accepting the first matching UUID.
        owner.assert_active(database)
        with pytest.raises(PilotOwnershipError):
            owner.assert_prior_epoch(middle)


def test_history_walk_has_finite_bound(database):
    with acquire(database) as owner:
        ancestor = str(uuid.uuid4())
        with sqlite3.connect(database) as db:
            # Preserve the owner's current-row interpretation, and create a
            # longer chain only below its predecessor on the next enrollment.
            db.execute('INSERT INTO hub_pilot_owner_epochs VALUES(?,?)', (ancestor, None))
    with acquire(database) as successor:
        cursor = ancestor
        with sqlite3.connect(database) as db:
            for _ in range(10000):
                predecessor = str(uuid.uuid4())
                db.execute('INSERT INTO hub_pilot_owner_epochs VALUES(?,?)', (predecessor, cursor))
                cursor = predecessor
            db.execute('UPDATE hub_pilot_owner_epochs SET previous_epoch=? WHERE epoch=?', (cursor, owner.epoch))
        with pytest.raises(PilotOwnershipError):
            successor.assert_prior_epoch(owner.epoch)
