"""Normal-attempt completion evidence; simulated host effects, actual Linux owner."""
import sys
import uuid
from types import SimpleNamespace

import pytest

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubError, HubRepository
from tests.test_hub_container_pilot import FakeClient, IMAGE, PRINCIPAL, pilot_manifest, reviewed_task


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux owner capability')


@pytest.fixture
def rig(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    with PilotOwner(private / 'hub.sqlite', allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(private / 'hub.sqlite',
                             execution_principal=PRINCIPAL, pilot_owner=owner)
        target = ContainerPilotTarget.from_manifest(pilot_manifest())
        client = FakeClient()
        workflow = ContainerPilotWorkflow(repo, client, [target], pilot_owner=owner)
        task = reviewed_task(repo)
        run = workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
        yield SimpleNamespace(owner=owner, repo=repo, workflow=workflow, client=client, run=run)


def rows(rig):
    with rig.repo._connection() as db:
        return tuple([tuple(row) for row in db.execute('SELECT * FROM ' + table + ' WHERE run_id=?', (rig.run['id'],))]
                     for table in ('hub_container_pilot_advances', 'hub_container_pilot_advance_epochs',
                                   'hub_container_pilot_attempts'))


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['success', 'pause', 'held'])
async def test_normal_completion_records_quiescence_even_on_pause_or_hold(rig, mode):
    if mode == 'pause':
        rig.repo.set_pause('human:1234', True)
        with pytest.raises(HubError, match='paused'):
            await rig.workflow.advance(rig.run['id'])
    else:
        if mode == 'held':
            rig.client.outcomes['deploy_container'] = 'held'
        result = await rig.workflow.advance(rig.run['id'])
        assert result['status'] == ('held' if mode == 'held' else 'succeeded')
    active, epochs, attempts = rows(rig)
    assert active == epochs == []
    assert len(attempts) == 1
    assert attempts[0][0] == rig.run['id']
    assert str(uuid.UUID(attempts[0][1])) == attempts[0][1]
    assert attempts[0][2:] == (rig.owner.epoch, 'quiescent')
    old_owner = attempts[0][1]
    if mode == 'pause':
        rig.repo.set_pause('human:1234', False)
    result = await rig.workflow.advance(rig.run['id'])
    assert result['status'] == ('held' if mode == 'held' else 'succeeded')
    assert rows(rig)[2][0][1] != old_owner
    assert rows(rig)[2][0][3] == 'quiescent'


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['active', 'invalid'])
async def test_orphan_attempt_does_not_become_authority_to_restart(rig, state):
    prior = (rig.run['id'], str(uuid.uuid4()), rig.owner.epoch, state)
    with rig.repo._transaction() as db:
        db.execute('INSERT INTO hub_container_pilot_attempts VALUES(?,?,?,?)', prior)
    before = rows(rig)
    with pytest.raises(HubError, match='advance_requires_reconciliation'):
        await rig.workflow.advance(rig.run['id'])
    assert rows(rig) == before
    assert rig.client.submissions == []


@pytest.mark.asyncio
async def test_active_recovery_claim_blocks_new_attempt(rig):
    with rig.repo._transaction() as db:
        db.execute('CREATE TABLE hub_container_pilot_recovery_claims '
                   '(run_id TEXT PRIMARY KEY,occurrence TEXT NOT NULL,epoch TEXT NOT NULL,state TEXT NOT NULL)')
        db.execute('INSERT INTO hub_container_pilot_recovery_claims VALUES(?,?,?,?)',
                   (rig.run['id'], str(uuid.uuid4()), rig.owner.epoch, 'inspecting'))
    with pytest.raises(HubError, match='advance_requires_reconciliation'):
        await rig.workflow.advance(rig.run['id'])
    assert rows(rig) == ([], [], [])
    assert rig.client.submissions == []


@pytest.mark.asyncio
@pytest.mark.parametrize('alteration', ['advance_owner', 'epoch_owner', 'epoch', 'attempt_owner',
                                     'attempt_epoch', 'attempt_state', 'attempt_missing'])
async def test_finally_preserves_all_metadata_unless_exact_active_tuple_matches(rig, alteration):
    original = rig.client.submit_command
    preserved = None

    async def changed(command):
        nonlocal preserved
        response = await original(command)
        with rig.repo._transaction() as db:
            table, column = {
                'advance_owner': ('advances', 'owner'),
                'epoch_owner': ('advance_epochs', 'advance_owner'),
                'epoch': ('advance_epochs', 'epoch'),
                'attempt_owner': ('attempts', 'advance_owner'),
                'attempt_epoch': ('attempts', 'epoch'),
                'attempt_state': ('attempts', 'state'),
                'attempt_missing': ('attempts', None),
            }[alteration]
            if column:
                db.execute('UPDATE hub_container_pilot_' + table + ' SET ' + column + '=? WHERE run_id=?',
                           ('changed', rig.run['id']))
            else:
                db.execute('DELETE FROM hub_container_pilot_attempts WHERE run_id=?', (rig.run['id'],))
        preserved = rows(rig)
        return response

    rig.client.submit_command = changed
    await rig.workflow.advance(rig.run['id'])
    assert preserved is not None
    assert rows(rig) == preserved


@pytest.mark.asyncio
async def test_owner_loss_leaves_active_attempt_and_original_pair(rig):
    original = rig.client.submit_command
    preserved = None

    async def lost(command):
        nonlocal preserved
        response = await original(command)
        preserved = rows(rig)
        rig.owner.close()
        return response

    rig.client.submit_command = lost
    with pytest.raises(HubError, match='container_pilot_ownership_unavailable'):
        await rig.workflow.advance(rig.run['id'])
    assert rows(rig) == preserved
    assert rows(rig)[2][0][3] == 'active'
    assert len(rig.client.submissions) == 1
