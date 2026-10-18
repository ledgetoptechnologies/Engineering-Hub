"""Linux owner-bound dispatch continuation; host command receipts are simulated.

These tests exercise dispatcher/run identity across pause and owner restart,
not Docker, installed service supervision or real-host reboot acceptance.
"""
import asyncio
from dataclasses import replace
import json
import sys
import uuid

import pytest

from engineering_hub.dispatcher import StagingDispatcher
from engineering_hub.repository import HubError
from tests.test_hub_container_pilot import reviewed_task
from tests.test_hub_pilot_recovery import RecoveryClient, rig


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux pilot ownership')


@pytest.mark.asyncio
async def test_same_run_external_terminal_event_finishes_dispatch_receipt(rig):
    current, run, request = await paused_dispatch(rig, 'after_deploy')
    current.repo.set_pause('human:1234', False)
    row = receipt(current.dispatcher)
    with current.repo._transaction() as db:
        head = db.execute('SELECT MAX(sequence) FROM hub_outbox').fetchone()[0]
        db.execute('UPDATE hub_dispatch_receipts SET attempt_head=? WHERE sequence=?', (head, row['sequence']))
    assert (await current.workflow.advance(run['id']))['status'] == 'succeeded'
    assert len(current.client.submissions) == 2
    await current.dispatcher.drain_batch()
    assert receipt(current.dispatcher)['state'] == 'succeeded'
    assert len(current.client.submissions) == 2
    assert current.client.inspections == []
    assert_original_identity(current, run, request)


def receipt(dispatcher):
    rows = dispatcher.receipts()
    assert len(rows) == 1
    return rows[0]


def saved_request(repo, sequence):
    with repo._connection() as db:
        return json.loads(db.execute('SELECT request FROM hub_dispatch_receipts WHERE sequence=?',
                                    (sequence,)).fetchone()[0])


def owner_record(repo, sequence):
    with repo._connection() as db:
        row = db.execute('SELECT owner,epoch,state FROM hub_dispatch_owners WHERE sequence=?',
                         (sequence,)).fetchone()
        return tuple(row) if row else None


def receipt_changed(repo, run, *, entity_id=None, action='pilot.receipt_changed'):
    # Internal, owner-guarded native invalidation only: no new public endpoint
    # or provider authentication is implied by this test helper.
    with repo._transaction() as db:
        repo._emit(db, action, {**run, 'id': entity_id or run['id']})


def assert_original_identity(current, before, original_request):
    after = current.repo.get_run(before['id'])
    for field in ('id', 'task_id', 'fencing_generation', 'execution_manifest_digest', 'artifact_digest'):
        assert after[field] == before[field]
    assert len(current.repo.list_runs()) == 1
    assert current.repo.get_task(before['task_id'])['repair_count'] == 0
    request = saved_request(current.repo, receipt(current.dispatcher)['sequence'])
    assert request == original_request
    for command in current.client.submissions + current.client.inspections:
        assert command.run_id == before['id']
        assert command.fencing_generation == before['fencing_generation']
        assert command.manifest_digest == before['execution_manifest_digest']


async def paused_dispatch(rig, boundary):
    current = rig.restart()
    current.dispatcher = StagingDispatcher(current.repo, current.workflow)
    if boundary == 'after_deploy':
        class Client(RecoveryClient):
            async def submit_command(self, command):
                response = await super().submit_command(command)
                if command.action == 'deploy_container':
                    current.repo.set_pause('human:1234', True)
                return response
        current.client = current.workflow.client = Client()
    else:
        original = current.workflow.advance
        async def pause_before_advance(run_id):
            current.repo.set_pause('human:1234', True)
            return await original(run_id)
        current.workflow.advance = pause_before_advance
    reviewed_task(current.repo)
    await current.dispatcher.drain_batch()
    row = receipt(current.dispatcher)
    assert row['state'] == 'needs_reconciliation' and row['reason'] == 'paused'
    run = current.repo.get_run(row['run_id'])
    assert run['status'] == ('running' if boundary == 'after_deploy' else 'pending')
    assert owner_record(current.repo, row['sequence'])[2] == 'quiescent'
    if boundary != 'after_deploy':
        current.workflow.advance = original
    return current, run, saved_request(current.repo, row['sequence'])


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['after_deploy', 'before_advance'])
async def test_unpause_drain_continues_accepted_original_run(rig, boundary):
    current, run, request = await paused_dispatch(rig, boundary)
    original = list(current.client.submissions)
    current.client = current.workflow.client = RecoveryClient()
    current.repo.set_pause('human:1234', False)
    await current.dispatcher.drain_batch()
    assert receipt(current.dispatcher)['state'] == 'succeeded'
    expected = ['smoke_container'] if boundary == 'after_deploy' else ['deploy_container', 'smoke_container']
    assert [item.action for item in current.client.submissions] == expected
    assert [item.action for item in original + current.client.submissions] == ['deploy_container', 'smoke_container']
    assert len({item.command_id for item in original + current.client.submissions}) == 2
    assert_original_identity(current, run, request)


@pytest.mark.asyncio
@pytest.mark.parametrize('trigger', ['receipt', 'unpause', 'explicit'])
async def test_held_quiescent_attempt_gets_terminal_proof_before_next_phase(rig, trigger):
    current = rig.restart(RecoveryClient({'deploy_container': 'running'}))
    current.dispatcher = StagingDispatcher(current.repo, current.workflow)
    reviewed_task(current.repo)
    await current.dispatcher.drain_batch()
    row = receipt(current.dispatcher)
    run = current.repo.get_run(row['run_id'])
    request = saved_request(current.repo, row['sequence'])
    original = list(current.client.submissions)
    assert run['status'] == 'held'
    assert owner_record(current.repo, row['sequence'])[2] == 'quiescent'
    current.client = current.workflow.client = RecoveryClient()
    current.repo.create_decision('hermes', 'engineering-hub', 'Unrelated question', ['inspect', 'wait'])
    current.repo.create_task('human:1234', 'engineering-hub', 'Unrelated task', 'unrelated-task')
    receipt_changed(current.repo, run, entity_id=str(uuid.uuid4()))
    receipt_changed(current.repo, run, action='run.held')
    for _ in range(5):
        await current.dispatcher.drain_batch()
    assert current.client.inspections == current.client.submissions == []
    if trigger == 'receipt':
        receipt_changed(current.repo, run)
        await current.dispatcher.drain_batch()
    elif trigger == 'unpause':
        current.repo.set_pause('human:1234', True)
        await current.dispatcher.drain_batch()
        assert current.client.inspections == current.client.submissions == []
        current.repo.set_pause('human:1234', False)
        await current.dispatcher.drain_batch()
    else:
        await current.dispatcher.resume_accepted(row['sequence'], expected_owner=row['owner'])
    assert current.client.inspections == original
    assert [item.action for item in current.client.submissions] == ['smoke_container']
    assert receipt(current.dispatcher)['state'] == 'succeeded'
    assert_original_identity(current, run, request)


@pytest.mark.asyncio
async def test_restarted_owner_adopts_dispatch_and_gets_only_original_command(rig):
    previous = rig.restart()
    previous.dispatcher = StagingDispatcher(previous.repo, previous.workflow)
    class CrashClient(RecoveryClient):
        async def submit_command(self, command):
            response = await super().submit_command(command)
            if command.action == 'deploy_container':
                previous.owner.close()
            return response
    previous.client = previous.workflow.client = CrashClient()
    reviewed_task(previous.repo)
    with pytest.raises(HubError):
        await previous.dispatcher.drain_batch()
    old = receipt(previous.dispatcher)
    request = saved_request(previous.repo, old['sequence'])
    run = previous.repo.get_run(old['run_id'])
    original = list(previous.client.submissions)
    old_epoch = owner_record(previous.repo, old['sequence'])[1]
    current = rig.restart()
    current.dispatcher = StagingDispatcher(current.repo, current.workflow)
    assert current.owner.epoch != old_epoch
    await current.dispatcher.drain_batch()
    assert receipt(current.dispatcher)['state'] == 'succeeded'
    assert current.client.inspections == original
    assert [item.action for item in current.client.submissions] == ['smoke_container']
    assert_original_identity(current, run, request)


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['missing', 'bad_owner', 'bad_epoch', 'foreign_epoch', 'invalid_state'])
async def test_unverified_dispatch_ownership_cannot_resume(rig, damage):
    current, run, request = await paused_dispatch(rig, 'after_deploy')
    row = receipt(current.dispatcher)
    current.repo.set_pause('human:1234', False)
    with current.repo._transaction() as db:
        if damage == 'missing':
            db.execute('DELETE FROM hub_dispatch_owners WHERE sequence=?', (row['sequence'],))
        else:
            field, value = {
                'bad_owner': ('owner', 'not-a-uuid'),
                'bad_epoch': ('epoch', 'not-a-uuid'),
                'foreign_epoch': ('epoch', str(uuid.uuid4())),
                'invalid_state': ('state', 'expired'),
            }[damage]
            db.execute(f'UPDATE hub_dispatch_owners SET {field}=? WHERE sequence=?', (value, row['sequence']))
    current.client = current.workflow.client = RecoveryClient()
    with pytest.raises(HubError):
        await current.dispatcher.resume_accepted(row['sequence'], expected_owner=row['owner'])
    assert current.client.submissions == current.client.inspections == []
    assert current.repo.get_run(run['id']) == run
    assert saved_request(current.repo, row['sequence']) == request


@pytest.mark.asyncio
async def test_current_epoch_active_dispatch_cannot_be_stolen(rig):
    current = rig.restart()
    current.dispatcher = StagingDispatcher(current.repo, current.workflow)
    entered, release = asyncio.Event(), asyncio.Event()
    class BlockingClient(RecoveryClient):
        async def submit_command(self, command):
            if command.action == 'deploy_container':
                entered.set()
                await release.wait()
            return await super().submit_command(command)
    current.client = current.workflow.client = BlockingClient()
    reviewed_task(current.repo)
    running = asyncio.create_task(current.dispatcher.drain_batch())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        row = receipt(current.dispatcher)
        other = StagingDispatcher(current.repo, current.workflow)
        assert owner_record(current.repo, row['sequence'])[2] == 'active'
        with pytest.raises(HubError):
            await other.resume_accepted(row['sequence'], expected_owner=row['owner'])
        await other.drain_batch()
        assert current.client.inspections == [] and current.client.submissions == []
    finally:
        release.set()
        await asyncio.wait_for(running, timeout=5)
    assert [item.action for item in current.client.submissions] == ['deploy_container', 'smoke_container']


@pytest.mark.asyncio
async def test_unknown_receipt_one_attempt_per_new_head_no_hot_loop(rig):
    current = rig.restart(RecoveryClient({'deploy_container': 'running'}))
    current.dispatcher = StagingDispatcher(current.repo, current.workflow)
    reviewed_task(current.repo)
    await current.dispatcher.drain_batch()
    row = receipt(current.dispatcher)
    run = current.repo.get_run(row['run_id'])
    request = saved_request(current.repo, row['sequence'])
    receipt_changed(current.repo, run)
    for _ in range(8):
        result = await current.dispatcher.drain_batch(limit=1)
        assert result['examined'] <= 1
        if current.client.inspections:
            break
    assert len(current.client.inspections) == 1
    count = len(current.client.inspections)
    for _ in range(5):
        result = await current.dispatcher.drain_batch(limit=1)
        assert result['examined'] <= 1
    assert len(current.client.inspections) == count
    assert len(current.client.submissions) == 1
    assert current.repo.get_run(run['id'])['status'] == 'held'
    assert_original_identity(current, run, request)


@pytest.mark.asyncio
async def test_manifest_change_never_rebinds_accepted_dispatch(rig):
    current, run, request = await paused_dispatch(rig, 'after_deploy')
    row = receipt(current.dispatcher)
    current.repo.set_pause('human:1234', False)
    target = rig.target
    current.workflow.targets[target.target_id] = replace(target, rollback_commit='c' * 40)
    current.client = current.workflow.client = RecoveryClient()
    result = await current.dispatcher.resume_accepted(row['sequence'], expected_owner=row['owner'])
    assert result['state'] == 'needs_reconciliation'
    assert result['reason'] == 'fixture_manifest_changed'
    assert current.client.submissions == current.client.inspections == []
    assert current.repo.get_run(run['id']) == run
    assert saved_request(current.repo, row['sequence']) == request


@pytest.mark.asyncio
async def test_matching_malformed_owner_fields_are_not_quiescence_proof(rig):
    current, run, request = await paused_dispatch(rig, 'after_deploy')
    row = receipt(current.dispatcher)
    current.repo.set_pause('human:1234', False)
    with current.repo._transaction() as db:
        db.execute("UPDATE hub_dispatch_receipts SET owner='invalid' WHERE sequence=?", (row['sequence'],))
        db.execute("UPDATE hub_dispatch_owners SET owner='invalid' WHERE sequence=?", (row['sequence'],))
    current.client = current.workflow.client = RecoveryClient()
    with pytest.raises(HubError):
        await current.dispatcher.resume_accepted(row['sequence'], expected_owner='invalid')
    assert current.client.submissions == current.client.inspections == []
    assert current.repo.get_run(run['id']) == run
    assert saved_request(current.repo, row['sequence']) == request


@pytest.mark.asyncio
async def test_invalid_prior_epoch_prefix_cannot_starve_new_review_at_limit_one(rig):
    previous = rig.restart()
    StagingDispatcher(previous.repo, previous.workflow)
    # These rows deliberately have invalid ownership and cannot authorize I/O.
    # Three candidates exceed the bounded drain budget of one on every call.
    with previous.repo._transaction() as db:
        for sequence in range(1000, 1003):
            db.execute('INSERT INTO hub_dispatch_receipts VALUES(?,?,?,?,?,?,?,?,?)',
                       (sequence, str(uuid.uuid4()), 1, 'invalid', 'reserved', None, None, '{}', 0))
            db.execute('INSERT INTO hub_dispatch_owners VALUES(?,?,?,?)',
                       (sequence, 'invalid', previous.owner.epoch, 'active'))
    previous.owner.close()
    current = rig.restart()
    current.dispatcher = StagingDispatcher(current.repo, current.workflow)
    task = reviewed_task(current.repo)
    for _ in range(32):
        result = await current.dispatcher.drain_batch(limit=1)
        assert result['examined'] <= 1
        if current.repo.list_runs() and current.repo.list_runs()[0]['status'] == 'succeeded':
            break
        assert result['more'], 'Catchup must report remaining recovery/outbox work'
    else:
        pytest.fail('Invalid recovery prefix starved an independently reviewed task')
    assert len(current.repo.list_runs()) == 1
    assert current.repo.list_runs()[0]['task_id'] == task['id']
    assert [item.action for item in current.client.submissions] == ['deploy_container', 'smoke_container']
    assert current.client.inspections == []
    with current.repo._connection() as db:
        rows = db.execute('SELECT state,run_id FROM hub_dispatch_receipts WHERE sequence>=1000').fetchall()
        assert [tuple(row) for row in rows] == [('reserved', None)] * 3


@pytest.mark.asyncio
async def test_unpause_arriving_just_before_pause_receipt_record_is_not_lost(rig):
    current = rig.restart()
    current.dispatcher = StagingDispatcher(current.repo, current.workflow)
    class PausingClient(RecoveryClient):
        async def submit_command(self, command):
            response = await super().submit_command(command)
            if command.action == 'deploy_container':
                current.repo.set_pause('human:1234', True)
            return response
    current.client = current.workflow.client = PausingClient()
    record = current.dispatcher._record
    unpaused = []
    def record_after_unpause(reservation, state, **kwargs):
        if state == 'needs_reconciliation' and kwargs.get('reason') == 'paused':
            current.repo.set_pause('human:1234', False)
            unpaused.append(True)
        return record(reservation, state, **kwargs)
    current.dispatcher._record = record_after_unpause
    reviewed_task(current.repo)
    await current.dispatcher.drain_batch()
    assert unpaused == [True] and current.repo.get_paused() is False
    row = receipt(current.dispatcher)
    run = current.repo.get_run(row['run_id'])
    request = saved_request(current.repo, row['sequence'])
    original = list(current.client.submissions)
    current.client = current.workflow.client = RecoveryClient()
    await current.dispatcher.drain_batch()
    assert receipt(current.dispatcher)['state'] == 'succeeded'
    assert current.client.inspections == original
    assert [item.action for item in current.client.submissions] == ['smoke_container']
    assert_original_identity(current, run, request)
