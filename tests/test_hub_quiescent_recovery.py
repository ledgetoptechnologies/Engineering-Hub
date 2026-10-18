"""Quiescent controller recovery, using actual Linux ownership and fake host GETs."""
import sys
import uuid

import pytest

from engineering_hub.pilot_recovery import ACTIONS
from engineering_hub.repository import HubError
from tests.test_hub_container_pilot import IMAGE, reviewed_task
from tests.test_hub_pilot_recovery import RecoveryClient, reservations, rig


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux ownership tests')


def attempt(repo, run_id):
    with repo._connection() as db:
        row = db.execute('SELECT advance_owner,epoch,state FROM hub_container_pilot_attempts WHERE run_id=?',
                         (run_id,)).fetchone()
        return tuple(row) if row else None


async def completed_attempt(rig, *, paused=False):
    current = rig.restart()

    class Client(RecoveryClient):
        async def submit_command(self, command):
            result = await super().submit_command(command)
            if paused and command.action == ACTIONS[0]:
                current.repo.set_pause('human:1234', True)
            return result

    current.client = current.workflow.client = Client({ACTIONS[0]: 'running'} if not paused else {})
    task = reviewed_task(current.repo)
    run = current.workflow.create_run('codex', task['id'], task['revision'], IMAGE, rig.target.target_id)
    if paused:
        with pytest.raises(HubError, match='paused'):
            await current.workflow.advance(run['id'])
    else:
        assert (await current.workflow.advance(run['id']))['status'] == 'held'
    assert reservations(current.repo, run['id']) == ([], [])
    assert attempt(current.repo, run['id'])[2] == 'quiescent'
    return current, run


@pytest.mark.asyncio
@pytest.mark.parametrize('restart', [False, True])
async def test_normally_held_attempt_get_then_only_never_reserved_phase(rig, restart):
    previous, run = await completed_attempt(rig)
    saved_attempt = attempt(previous.repo, run['id'])
    original = list(previous.client.submissions)
    if restart:
        previous.owner.close()
        current = rig.restart()
    else:
        current = previous
        current.client = current.workflow.client = RecoveryClient()
    result = await current.recovery.recover(run['id'])
    assert result['recovery'] == 'recovered' and result['run']['status'] == 'running'
    assert current.client.submissions == []
    assert current.client.inspections == original
    assert attempt(current.repo, run['id']) == saved_attempt
    assert (await current.workflow.advance(run['id']))['status'] == 'succeeded'
    assert [command.action for command in current.client.submissions] == [ACTIONS[1]]
    assert len({command.command_id for command in original + current.client.submissions}) == 2
    assert all(command.fencing_generation == run['fencing_generation']
               and command.manifest_digest == run['execution_manifest_digest']
               for command in original + current.client.submissions)


@pytest.mark.asyncio
@pytest.mark.parametrize('restart', [False, True])
async def test_paused_normal_cleanup_records_get_then_recovers_after_unpause(rig, restart):
    previous, run = await completed_attempt(rig, paused=True)
    saved_attempt = attempt(previous.repo, run['id'])
    original = list(previous.client.submissions)
    if restart:
        previous.owner.close()
        current = rig.restart()
    else:
        current = previous
        current.client = current.workflow.client = RecoveryClient()
    before = current.repo.get_run(run['id'])
    paused = await current.recovery.recover(run['id'])
    assert paused['recovery'] == 'paused' and paused['run'] == before
    assert attempt(current.repo, run['id']) == saved_attempt
    assert current.client.inspections == original and current.client.submissions == []
    current.repo.set_pause('human:1234', False)
    result = await current.recovery.recover(run['id'])
    assert result['recovery'] == 'recovered'
    assert (await current.workflow.advance(run['id']))['status'] == 'succeeded'
    assert [command.action for command in current.client.submissions] == [ACTIONS[1]]
    assert current.repo.get_task(run['task_id'])['repair_count'] == 0


@pytest.mark.asyncio
async def test_unknown_quiescent_receipt_stays_held_without_new_post(rig):
    current, run = await completed_attempt(rig)
    saved = attempt(current.repo, run['id'])
    result = await current.recovery.recover(run['id'])
    assert result['recovery'] == 'held'
    assert attempt(current.repo, run['id']) == saved
    assert len(current.client.submissions) == len(current.client.inspections) == 1
    assert (await current.workflow.advance(run['id']))['status'] == 'held'
    assert len(current.client.submissions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['active_without_pair', 'missing_attempt', 'bad_uuid', 'foreign_epoch',
                                    'partial_pair', 'quiescent_with_pair', 'pair_owner_mismatch'])
async def test_inconsistent_quiescence_rejected_before_get(rig, damage):
    current, run = await completed_attempt(rig)
    saved = attempt(current.repo, run['id'])
    with current.repo._transaction() as db:
        if damage == 'active_without_pair':
            db.execute("UPDATE hub_container_pilot_attempts SET state='active' WHERE run_id=?", (run['id'],))
        elif damage == 'missing_attempt':
            db.execute('DELETE FROM hub_container_pilot_attempts WHERE run_id=?', (run['id'],))
        elif damage == 'bad_uuid':
            db.execute("UPDATE hub_container_pilot_attempts SET advance_owner='not-a-uuid' WHERE run_id=?", (run['id'],))
        elif damage == 'foreign_epoch':
            db.execute('UPDATE hub_container_pilot_attempts SET epoch=? WHERE run_id=?', (str(uuid.uuid4()), run['id']))
        else:
            db.execute('INSERT INTO hub_container_pilot_advances VALUES(?,?)', (run['id'], saved[0]))
            if damage != 'partial_pair':
                db.execute('INSERT INTO hub_container_pilot_advance_epochs VALUES(?,?,?)',
                           (run['id'], str(uuid.uuid4()) if damage == 'pair_owner_mismatch' else saved[0], saved[1]))
    with pytest.raises(HubError, match='pilot_recovery_reservation_unverified'):
        await current.recovery.recover(run['id'])
    assert current.client.inspections == [] and len(current.client.submissions) == 1


@pytest.mark.asyncio
async def test_recovery_claim_blocks_normal_advance_during_get(rig):
    current, run = await completed_attempt(rig)

    class Client(RecoveryClient):
        async def get_command(self, command):
            with pytest.raises(HubError, match='advance_requires_reconciliation'):
                await current.workflow.advance(run['id'])
            return await super().get_command(command)

    current.client = current.workflow.client = Client()
    assert (await current.recovery.recover(run['id']))['recovery'] == 'recovered'
    assert len(current.client.inspections) == 1 and current.client.submissions == []


@pytest.mark.asyncio
async def test_attempt_changed_during_get_fails_final_snapshot_cas(rig):
    current, run = await completed_attempt(rig)
    before = current.repo.get_run(run['id'])

    class Client(RecoveryClient):
        async def get_command(self, command):
            response = await super().get_command(command)
            with current.repo._transaction() as db:
                db.execute('UPDATE hub_container_pilot_attempts SET advance_owner=? WHERE run_id=?',
                           (str(uuid.uuid4()), run['id']))
            return response

    current.client = current.workflow.client = Client()
    with pytest.raises(HubError, match='pilot_recovery_conflict'):
        await current.recovery.recover(run['id'])
    assert current.repo.get_run(run['id']) == before
    assert current.client.submissions == []


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_prior_active_pair_recovers_with_matching_attempt_or_legacy_absence(rig, legacy):
    previous, run, _ = await rig.crash()
    current = rig.restart()
    saved = attempt(current.repo, run['id'])
    assert saved[1:] == (previous.owner.epoch, 'active')
    if legacy:
        with current.repo._transaction() as db:
            db.execute('DELETE FROM hub_container_pilot_attempts WHERE run_id=?', (run['id'],))
    original = list(previous.client.submissions)
    assert (await current.recovery.recover(run['id']))['recovery'] == 'recovered'
    assert current.client.inspections == original and current.client.submissions == []
    assert reservations(current.repo, run['id']) == ([], [])
    assert attempt(current.repo, run['id']) == (None if legacy else (saved[0], saved[1], 'quiescent'))


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['owner', 'epoch', 'state', 'noncanonical_owner'])
async def test_prior_active_pair_inconsistent_with_attempt_denies_before_get(rig, damage):
    _, run, _ = await rig.crash()
    current = rig.restart()
    with current.repo._transaction() as db:
        if damage == 'owner':
            db.execute('UPDATE hub_container_pilot_attempts SET advance_owner=? WHERE run_id=?',
                       (str(uuid.uuid4()), run['id']))
        elif damage == 'epoch':
            db.execute('UPDATE hub_container_pilot_attempts SET epoch=? WHERE run_id=?', (current.owner.epoch, run['id']))
        elif damage == 'state':
            db.execute("UPDATE hub_container_pilot_attempts SET state='quiescent' WHERE run_id=?", (run['id'],))
        else:
            noncanonical = str(uuid.uuid4()).upper()
            db.execute('UPDATE hub_container_pilot_attempts SET advance_owner=? WHERE run_id=?', (noncanonical, run['id']))
            db.execute('UPDATE hub_container_pilot_advances SET owner=? WHERE run_id=?', (noncanonical, run['id']))
            db.execute('UPDATE hub_container_pilot_advance_epochs SET advance_owner=? WHERE run_id=?', (noncanonical, run['id']))
    with pytest.raises(HubError, match='pilot_recovery_reservation_unverified'):
        await current.recovery.recover(run['id'])
    assert current.client.inspections == current.client.submissions == []
