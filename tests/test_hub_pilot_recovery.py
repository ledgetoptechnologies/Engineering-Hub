"""Actual Linux owner restarts with simulated, original-command GET receipts.

These are controller recovery tests, not Docker or real-host acceptance.
"""
import json
import sys
import uuid
from types import SimpleNamespace

import pytest

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.pilot_recovery import ACTIONS, PilotRecovery
from engineering_hub.repository import HubError, HubRepository
from tests.test_hub_container_pilot import (
    FakeClient, IMAGE, PRINCIPAL, pilot_manifest, reviewed_task,
)


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux ownership restart tests')


@pytest.mark.asyncio
async def test_correctable_local_validation_error_does_not_strand_current_claim(rig):
    _, run, _ = await rig.crash()
    current = rig.restart()
    original = current.workflow.targets
    current.workflow.targets = {}
    with pytest.raises(HubError, match='container_pilot_manifest_mismatch'):
        await current.recovery.recover(run['id'])
    assert current.client.submissions == []
    assert len(reservations(current.repo, run['id'])[0]) == 1
    current.workflow.targets = original
    result = await current.recovery.recover(run['id'])
    assert result['recovery'] == 'recovered'
    assert current.client.submissions == []


class RecoveryClient(FakeClient):
    def response(self, command):
        response = super().response(command)
        container = ('d' if command.action in ACTIONS[2:] else 'e') * 64
        return response.model_copy(update={'evidence': response.evidence.model_copy(update={'container_id': container})})


class CrashClient(RecoveryClient):
    def __init__(self, owner, phase, outcomes):
        super().__init__(outcomes)
        self.owner, self.phase = owner, phase

    async def submit_command(self, command):
        result = await super().submit_command(command)
        if command.action == self.phase:
            self.owner.close()
        return result


class InspectClient(RecoveryClient):
    def __init__(self, outcomes=None, alter=None):
        super().__init__(outcomes)
        self.alter = alter

    async def get_command(self, command):
        result = await super().get_command(command)
        return self.alter(command, result) if self.alter else result


@pytest.fixture
def rig(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    path = private / 'hub.sqlite'
    owners = []
    target = ContainerPilotTarget.from_manifest(pilot_manifest())

    def restart(client=None):
        owner = PilotOwner(path, allow_ephemeral_test_storage=True)
        owners.append(owner)
        repo = HubRepository(path, execution_principal=PRINCIPAL, pilot_owner=owner)
        client = client or RecoveryClient()
        workflow = ContainerPilotWorkflow(repo, client, [target], pilot_owner=owner)
        return SimpleNamespace(owner=owner, repo=repo, client=client, workflow=workflow,
                               recovery=PilotRecovery(workflow))

    async def crash(phase='deploy_container'):
        current = restart()
        outcomes = {'smoke_container': 'failed'} if phase in ACTIONS[2:] else {}
        client = CrashClient(current.owner, phase, outcomes)
        current.client = current.workflow.client = client
        task = reviewed_task(current.repo)
        run = current.workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
        with pytest.raises(HubError, match='container_pilot_ownership_unavailable'):
            await current.workflow.advance(run['id'])
        return current, run, outcomes

    yield SimpleNamespace(restart=restart, crash=crash, path=path, target=target)
    for owner in reversed(owners):
        owner.close()


def reservations(repo, run_id):
    with repo._connection() as db:
        return (db.execute('SELECT * FROM hub_container_pilot_advances WHERE run_id=?', (run_id,)).fetchall(),
                db.execute('SELECT * FROM hub_container_pilot_advance_epochs WHERE run_id=?', (run_id,)).fetchall())


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ACTIONS)
async def test_each_crash_phase_get_only_then_only_never_reserved_posts(rig, phase):
    previous, run, outcomes = await rig.crash(phase)
    original = list(previous.client.submissions)
    resumed = rig.restart(RecoveryClient(outcomes))
    before = reservations(resumed.repo, run['id'])
    assert len(before[0]) == len(before[1]) == 1
    result = await resumed.recovery.recover(run['id'])
    assert result['recovery'] == 'recovered'
    assert resumed.client.submissions == []
    assert resumed.client.inspections == original
    assert reservations(resumed.repo, run['id']) == ([], [])
    final = await resumed.workflow.advance(run['id'])
    expected_all = list(ACTIONS) if outcomes else list(ACTIONS[:2])
    assert [item.action for item in original + resumed.client.submissions] == expected_all
    assert len({item.command_id for item in original + resumed.client.submissions}) == len(expected_all)
    assert final['status'] == ('rolled_back' if outcomes else 'succeeded')
    assert final['fencing_generation'] == run['fencing_generation']
    assert final['execution_manifest_digest'] == run['execution_manifest_digest']
    assert resumed.repo.get_task(run['task_id'])['repair_count'] == (1 if outcomes else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['pending', 'running', 'held', TimeoutError(), HubError('not_found', 404)])
async def test_unknown_original_command_keeps_held_and_reserved(rig, outcome):
    _, run, _ = await rig.crash()
    current = rig.restart(RecoveryClient({'deploy_container': outcome}))
    prior_reservations = reservations(current.repo, run['id'])
    result = await current.recovery.recover(run['id'])
    assert result['recovery'] == 'held'
    assert result['run']['status'] == 'held'
    assert current.client.submissions == []
    assert len(current.client.inspections) == 1
    assert reservations(current.repo, run['id']) == prior_reservations
    assert current.repo.get_task(run['task_id'])['repair_count'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['echo', 'image', 'health', 'unverified', 'invalid_type'])
async def test_invalid_get_evidence_does_not_release(rig, kind):
    # Smoke is used so the health requirement is tested, too.
    _, run, _ = await rig.crash('smoke_container')

    def alter(command, response):
        if command.action != 'smoke_container':
            return response
        if kind == 'echo':
            return response.model_copy(update={'command_id': str(uuid.uuid4())})
        if kind == 'invalid_type':
            return response.model_copy(update={'policy_revision': True})
        update = {'image_config_id': 'sha256:' + '0' * 64} if kind == 'image' else (
            {'health': 'unhealthy'} if kind == 'health' else {'evidence_verified': False})
        return response.model_copy(update={'evidence': response.evidence.model_copy(update=update)})

    current = rig.restart(InspectClient(alter=alter))
    result = await current.recovery.recover(run['id'])
    assert result['recovery'] == 'held'
    assert current.client.submissions == []
    assert reservations(current.repo, run['id'])[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['state', 'container', 'image', 'stored_malformed'])
async def test_conflicting_saved_terminal_receipt_holds(rig, kind):
    _, run, _ = await rig.crash('smoke_container')

    def alter(command, response):
        if command.action != 'deploy_container':
            return response
        if kind == 'state':
            return response.model_copy(update={'state': 'failed'})
        if kind == 'container':
            return response.model_copy(update={'evidence': response.evidence.model_copy(update={'container_id': '1' * 64})})
        if kind == 'image':
            return response.model_copy(update={'evidence': response.evidence.model_copy(update={'image_config_id': 'sha256:' + '2' * 64})})
        return response

    current = rig.restart(InspectClient(alter=alter))
    if kind == 'stored_malformed':
        with current.repo._transaction() as db:
            db.execute("UPDATE hub_container_pilot_commands SET response_json='{}' WHERE run_id=? AND action='deploy_container'", (run['id'],))
    result = await current.recovery.recover(run['id'])
    assert result['recovery'] == 'held'
    assert current.client.submissions == []
    assert reservations(current.repo, run['id'])[0]


@pytest.mark.asyncio
async def test_repeated_unknown_inspection_has_distinct_occurrence_and_audit(rig):
    _, run, _ = await rig.crash()
    current = rig.restart(RecoveryClient({'deploy_container': 'running'}))
    first = await current.recovery.recover(run['id'])
    second = await current.recovery.recover(run['id'])
    assert first['recovery'] == second['recovery'] == 'held'
    assert first['occurrence'] != second['occurrence']
    events = second['run']['events'][-2:]
    assert [item['event'] for item in events] == ['held', 'held']
    assert events[0]['event_id'] != events[1]['event_id']
    with current.repo._connection() as db:
        rows = db.execute('SELECT state,evidence FROM hub_container_pilot_recoveries WHERE run_id=?', (run['id'],)).fetchall()
    assert len(rows) == 2 and all(row['state'] == 'held' and json.loads(row['evidence']) for row in rows)
    assert current.client.submissions == []


@pytest.mark.asyncio
async def test_pause_records_get_evidence_without_release_then_unpause_recovers(rig):
    _, run, _ = await rig.crash()
    current = rig.restart()
    current.repo.set_pause('human:1234', True)
    prior = current.repo.get_run(run['id'])
    reserved = reservations(current.repo, run['id'])
    paused = await current.recovery.recover(run['id'])
    assert paused['recovery'] == 'paused' and paused['run'] == prior
    assert reservations(current.repo, run['id']) == reserved
    assert current.client.submissions == [] and len(current.client.inspections) == 1
    current.repo.set_pause('human:1234', False)
    recovered = await current.recovery.recover(run['id'])
    assert recovered['recovery'] == 'recovered'
    assert current.client.submissions == []
    assert (await current.workflow.advance(run['id']))['status'] == 'succeeded'
    assert [item.action for item in current.client.submissions] == ['smoke_container']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['current_epoch', 'missing_epoch', 'wrong_owner'])
async def test_unproven_reservation_never_inspects_or_adopts(rig, kind):
    _, run, _ = await rig.crash()
    current = rig.restart()
    with current.repo._transaction() as db:
        if kind == 'current_epoch':
            db.execute('UPDATE hub_container_pilot_advance_epochs SET epoch=? WHERE run_id=?', (current.owner.epoch, run['id']))
        elif kind == 'missing_epoch':
            db.execute('DELETE FROM hub_container_pilot_advance_epochs WHERE run_id=?', (run['id'],))
        else:
            db.execute('UPDATE hub_container_pilot_advance_epochs SET advance_owner=? WHERE run_id=?', (str(uuid.uuid4()), run['id']))
    with pytest.raises(HubError, match='pilot_recovery_reservation_unverified'):
        await current.recovery.recover(run['id'])
    assert current.client.inspections == current.client.submissions == []


@pytest.mark.asyncio
async def test_multiple_idle_restarts_preserve_original_epoch_recoverability(rig):
    previous, run, _ = await rig.crash()
    for _ in range(3):
        idle = rig.restart()
        idle.owner.close()
    current = rig.restart()
    current.owner.assert_prior_epoch(previous.owner.epoch)
    assert (await current.recovery.recover(run['id']))['recovery'] == 'recovered'
    assert current.client.submissions == []


@pytest.mark.asyncio
async def test_candidate_failure_recovery_counts_once_then_rollback(rig):
    _, run, _ = await rig.crash()
    current = rig.restart(RecoveryClient({'deploy_container': 'failed'}))
    result = await current.recovery.recover(run['id'])
    assert result['run']['status'] == 'failed'
    assert current.repo.get_task(run['task_id'])['repair_count'] == 1
    assert (await current.workflow.advance(run['id']))['status'] == 'rolled_back'
    assert current.repo.get_task(run['task_id'])['repair_count'] == 1
    assert [item.action for item in current.client.submissions] == list(ACTIONS[2:])


@pytest.mark.asyncio
async def test_terminal_run_leftover_reservation_is_cleaned_without_posts(rig):
    first = rig.restart()
    task = reviewed_task(first.repo)
    run = first.workflow.create_run('codex', task['id'], task['revision'], IMAGE, rig.target.target_id)
    assert (await first.workflow.advance(run['id']))['status'] == 'succeeded'
    reservation = str(uuid.uuid4())
    with first.repo._transaction() as db:
        db.execute('INSERT INTO hub_container_pilot_advances VALUES(?,?)', (run['id'], reservation))
        db.execute('INSERT INTO hub_container_pilot_advance_epochs VALUES(?,?,?)', (run['id'], reservation, first.owner.epoch))
    first.owner.close()
    current = rig.restart()
    result = await current.recovery.recover(run['id'])
    assert result['recovery'] == 'recovered' and result['run']['status'] == 'succeeded'
    assert reservations(current.repo, run['id']) == ([], [])
    assert current.client.submissions == []


@pytest.mark.asyncio
async def test_failed_history_cannot_rewind_to_candidate_success(rig):
    _, run, _ = await rig.crash('rollback_container')
    current = rig.restart()
    with current.repo._transaction() as db:
        db.execute("DELETE FROM hub_container_pilot_commands WHERE run_id=? AND action IN ('smoke_container','rollback_container')", (run['id'],))
    result = await current.recovery.recover(run['id'])
    assert result['recovery'] == 'held' and result['run']['status'] == 'held'
    assert current.repo.get_task(run['task_id'])['repair_count'] == 1
    assert reservations(current.repo, run['id'])[0]
    assert current.client.submissions == []


@pytest.mark.asyncio
async def test_foreign_run_command_id_denies_before_get(rig):
    _, run, _ = await rig.crash()
    current = rig.restart()
    with current.repo._transaction() as db:
        changed = current.repo._get(db, run['id'], 'run')
        changed['command_id'] = str(uuid.uuid4())
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(changed), run['id']))
    with pytest.raises(HubError):
        await current.recovery.recover(run['id'])
    assert current.client.inspections == current.client.submissions == []


@pytest.mark.asyncio
async def test_distinct_image_roles_cannot_share_one_container(rig):
    _, run, outcomes = await rig.crash('rollback_container')

    def collide(command, response):
        if command.action in ACTIONS[2:]:
            return response.model_copy(update={'evidence': response.evidence.model_copy(update={'container_id': 'e' * 64})})
        return response

    current = rig.restart(InspectClient(outcomes, alter=collide))
    result = await current.recovery.recover(run['id'])
    assert result['recovery'] == 'held'
    assert current.repo.get_task(run['task_id'])['repair_count'] == 1
    assert current.client.submissions == []


@pytest.mark.asyncio
async def test_changed_original_request_is_not_reconstructed_or_queried(rig):
    _, run, _ = await rig.crash()
    current = rig.restart()
    with current.repo._transaction() as db:
        row = db.execute('SELECT payload FROM hub_container_pilot_commands WHERE run_id=?', (run['id'],)).fetchone()
        changed = json.loads(row['payload'])
        changed['command_id'] = str(uuid.uuid4())
        db.execute('UPDATE hub_container_pilot_commands SET payload=? WHERE run_id=?', (json.dumps(changed), run['id']))
    result = await current.recovery.recover(run['id'])
    assert result['recovery'] == 'held'
    assert current.client.inspections == current.client.submissions == []
    assert reservations(current.repo, run['id'])[0]
