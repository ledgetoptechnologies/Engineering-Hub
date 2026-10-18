"""Reservation epoch evidence only; no recovery or host effects."""
import sqlite3
import sys
import uuid

import pytest

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubError, HubRepository
from tests.test_hub_container_pilot import IMAGE, PRINCIPAL, FakeClient, pilot_manifest, reviewed_task


linux = pytest.mark.skipif(sys.platform != 'linux', reason='Linux kernel ownership capability')


def workflow_run(repo, owner, client):
    target = ContainerPilotTarget.from_manifest(pilot_manifest())
    workflow = ContainerPilotWorkflow(repo, client, [target], pilot_owner=owner)
    task = reviewed_task(repo)
    run = workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
    return workflow, run, target


def reservations(repo):
    with repo._connection() as db:
        advances = [tuple(row) for row in db.execute('SELECT * FROM hub_container_pilot_advances')]
        epochs = [tuple(row) for row in db.execute('SELECT * FROM hub_container_pilot_advance_epochs')]
        return advances, epochs


@pytest.fixture
def enrolled(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    with PilotOwner(private / 'hub.sqlite', allow_ephemeral_test_storage=True) as owner:
        yield HubRepository(private / 'hub.sqlite', execution_principal=PRINCIPAL, pilot_owner=owner), owner


@linux
@pytest.mark.asyncio
async def test_epoch_reservation_matches_live_owner_and_cleans_both_rows(enrolled):
    repo, owner = enrolled

    class InspectingClient(FakeClient):
        async def submit_command(self, command):
            advances, epochs = reservations(repo)
            assert len(advances) == len(epochs) == 1
            assert epochs == [(command.run_id, advances[0][1], owner.epoch)]
            uuid.UUID(advances[0][1])
            return await super().submit_command(command)

    workflow, run, _ = workflow_run(repo, owner, InspectingClient())
    assert (await workflow.advance(run['id']))['status'] == 'succeeded'
    assert reservations(repo) == ([], [])


@linux
@pytest.mark.asyncio
async def test_lost_owner_preserves_exact_original_epoch_after_new_owner_acquires(enrolled):
    repo, owner = enrolled
    old_epoch = owner.epoch

    class ClosingClient(FakeClient):
        async def submit_command(self, command):
            self.submissions.append(command)
            owner.close()
            return self.response(command)

    client = ClosingClient()
    workflow, run, target = workflow_run(repo, owner, client)
    with pytest.raises(HubError, match='container_pilot_ownership_unavailable'):
        await workflow.advance(run['id'])
    original = reservations(repo)
    assert original[1] == [(run['id'], original[0][0][1], old_epoch)]
    with PilotOwner(repo.path, allow_ephemeral_test_storage=True) as successor:
        assert successor.previous_epoch == old_epoch
        assert successor.epoch != old_epoch
        successor_repo = HubRepository(repo.path, execution_principal=PRINCIPAL, pilot_owner=successor)
        replacement = ContainerPilotWorkflow(successor_repo, FakeClient(), [target], pilot_owner=successor)
        with pytest.raises(HubError, match='advance_requires_reconciliation'):
            await replacement.advance(run['id'])
        assert reservations(repo) == original
    assert len(client.submissions) == 1


@linux
@pytest.mark.asyncio
@pytest.mark.parametrize('orphan', [False, True])
async def test_existing_legacy_or_orphan_epoch_is_not_overwritten(enrolled, orphan):
    repo, owner = enrolled
    client = FakeClient()
    workflow, run, _ = workflow_run(repo, owner, client)
    with repo._transaction() as db:
        if orphan:
            db.execute('INSERT INTO hub_container_pilot_advance_epochs VALUES(?,?,?)',
                       (run['id'], str(uuid.uuid4()), owner.epoch))
        else:
            db.execute('INSERT INTO hub_container_pilot_advances VALUES(?,?)', (run['id'], 'legacy-owner'))
    original = reservations(repo)
    with pytest.raises(HubError, match='advance_requires_reconciliation'):
        await workflow.advance(run['id'])
    assert reservations(repo) == original
    assert client.submissions == []


@linux
@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['advance_owner', 'epoch', 'reservation_owner'])
async def test_mismatched_pair_is_preserved_by_cleanup(enrolled, field):
    repo, owner = enrolled

    class TamperingClient(FakeClient):
        async def submit_command(self, command):
            with repo._transaction() as db:
                if field == 'reservation_owner':
                    db.execute('UPDATE hub_container_pilot_advances SET owner=?', (str(uuid.uuid4()),))
                else:
                    db.execute(f'UPDATE hub_container_pilot_advance_epochs SET {field}=?', (str(uuid.uuid4()),))
            return await super().submit_command(command)

    workflow, run, _ = workflow_run(repo, owner, TamperingClient())
    assert (await workflow.advance(run['id']))['status'] == 'succeeded'
    advances, epochs = reservations(repo)
    assert len(advances) == len(epochs) == 1
    assert advances[0][0] == epochs[0][0] == run['id']


@pytest.mark.asyncio
async def test_legacy_reservation_has_no_epoch_authority_and_cleans_success(tmp_path):
    repo = HubRepository(tmp_path / 'legacy.sqlite', execution_principal=PRINCIPAL)

    class InspectingClient(FakeClient):
        async def submit_command(self, command):
            advances, epochs = reservations(repo)
            assert len(advances) == 1
            assert epochs == []
            return await super().submit_command(command)

    workflow, run, _ = workflow_run(repo, None, InspectingClient())
    assert (await workflow.advance(run['id']))['status'] == 'succeeded'
    assert reservations(repo) == ([], [])


@linux
@pytest.mark.asyncio
async def test_pair_reservation_is_atomic_if_epoch_insert_fails(enrolled):
    repo, owner = enrolled
    client = FakeClient()
    workflow, run, _ = workflow_run(repo, owner, client)
    with repo._transaction() as db:
        db.execute("CREATE TRIGGER deny_epoch BEFORE INSERT ON hub_container_pilot_advance_epochs "
                   "BEGIN SELECT RAISE(ABORT, 'test insertion failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='test insertion failure'):
        await workflow.advance(run['id'])
    assert reservations(repo) == ([], [])
    assert client.submissions == []
