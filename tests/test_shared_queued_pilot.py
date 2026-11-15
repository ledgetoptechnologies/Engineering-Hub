"""Optional real PilotOwner/shared queue integration; simulated executor only."""
import asyncio
import json
import sys
from types import SimpleNamespace
import uuid

import pytest

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.execution_policy import SharedExecutionPolicies
from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError
from engineering_hub.queued_pilot import QueuedContainerPilot
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.work_queue import (STAGING_SERVICE_EXECUTOR, WorkAction, WorkQueue,
                                        WorkTarget, WorkWorker)
from tests.manual_session_fixtures import synthetic_workflow_reconciliation
from tests.test_hub_container_pilot import ACTIONS, COMMIT, IMAGE, PRINCIPAL, pilot_manifest, reviewed_task
from tests.test_hub_queued_pilot import (ACTION, MACHINE, DistinctContainerClient,
                                       machine_job, mapping, submit)
from tests.test_hub_queued_pilot_async import async_pilot_case, accept

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux PilotOwner and SQLite fencing')
MODULAR = 'engineering-hub.modular-build'


def make_context(path, clock, *, activate=True):
    owner = PilotOwner(path, allow_ephemeral_test_storage=True)
    repo = HubRepository(path, execution_principal=PRINCIPAL, pilot_owner=owner)
    client = DistinctContainerClient()
    workflow = ContainerPilotWorkflow(repo, client,
        [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=owner)
    queue = WorkQueue(repo, {key: WorkAction(MACHINE, project='engineering-hub')
                            for key in (ACTION, MODULAR)}, clock=lambda: clock[0])
    policies = SharedExecutionPolicies(repo, queue)
    bridge = QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE,
        reconciliation=synthetic_workflow_reconciliation(workflow), execution_policies=policies)
    modular_worker = WorkWorker(STAGING_SERVICE_EXECUTOR, str(uuid.uuid4()))
    policies.bind('modular', {MODULAR: queue.actions[MODULAR]}, worker=modular_worker,
                  target=lambda *args: True, claim=lambda *args: True,
                  completion=lambda *args: False, reconciliation=lambda *args: False)
    policies.install()
    value = SimpleNamespace(owner=owner, repo=repo, client=client, workflow=workflow,
        queue=queue, bridge=bridge, policies=policies, clock=clock, modular_worker=modular_worker)
    if activate:
        policies.ready('pilot'); policies.ready('modular'); policies.open()
    return value


@pytest.fixture
def rig(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    value = make_context(private / 'hub.sqlite3', [1000.0])
    try:
        yield value
    finally:
        value.owner.close()


@pytest.mark.asyncio
async def test_real_pilot_reuses_shared_dispatchers_and_original_success_workflow(rig):
    callbacks = tuple(getattr(rig.queue, key) for key in
        ('target_verifier', 'claim_verifier', 'completion_verifier', 'reconciliation_verifier'))
    assert callbacks == rig.policies._dispatchers
    task, job = submit(rig)
    assert (await rig.bridge.run_once())['state'] == 'succeeded'
    assert [item.action for item in rig.client.submissions] == ACTIONS[:2]
    assert len(rig.repo.list_runs()) == 1 and machine_job(rig) is None
    assert tuple(getattr(rig.queue, key) for key in
        ('target_verifier', 'claim_verifier', 'completion_verifier', 'reconciliation_verifier')) == callbacks
    assert rig.repo.execution_principal == PRINCIPAL


@pytest.mark.asyncio
async def test_existing_four_phase_failed_health_rollback_remains_original(rig):
    rig.client.outcomes['smoke_container'] = 'failed'
    submit(rig)
    assert (await rig.bridge.run_once())['state'] == 'failed'
    assert [item.action for item in rig.client.submissions] == ACTIONS
    assert rig.repo.list_runs()[0]['status'] == 'rolled_back'
    assert machine_job(rig) is None


@pytest.mark.asyncio
async def test_closed_registry_blocks_new_admission_without_run_or_post(rig):
    _, job = submit(rig)
    rig.policies.close()
    with pytest.raises(HubError, match='execution_policy_admission_closed'):
        await rig.bridge.run_once()
    assert rig.queue.get_job(job['id'])['state'] == 'queued'
    assert not rig.repo.list_runs() and not rig.client.submissions


@pytest.mark.asyncio
async def test_final_accepted_response_and_original_completion_drain_after_close(rig):
    original = rig.client.submit_command
    async def close_after_final_response(command):
        response = await original(command)
        if command.action == 'smoke_container':
            rig.policies.close()
        return response
    rig.client.submit_command = close_after_final_response
    _, job = submit(rig)
    result = await rig.bridge.run_once()
    assert result['id'] == job['id'] and result['state'] == 'succeeded'
    assert [item.action for item in rig.client.submissions] == ACTIONS[:2]
    assert rig.repo.list_runs()[0]['status'] == 'succeeded' and machine_job(rig) is None


@pytest.mark.asyncio
async def test_close_after_first_accepted_post_records_response_but_sends_no_next_command(rig):
    original = rig.client.submit_command
    async def close_after_deploy(command):
        response = await original(command)
        rig.policies.close()
        return response
    rig.client.submit_command = close_after_deploy
    _, job = submit(rig)
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert [item.action for item in rig.client.submissions] == ['deploy_container']
    assert machine_job(rig) == job['id']
    with rig.repo._connection() as db:
        receipt = db.execute('SELECT state,response_json FROM hub_container_pilot_commands').fetchone()
    assert receipt['state'] == 'succeeded' and json.loads(receipt['response_json'])['action'] == 'deploy_container'


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['claim', 'command'])
async def test_registry_close_during_fresh_preparation_denies_next_claim_or_post(rig, phase):
    with async_pilot_case(rig) as case:
        await accept(case)
        _, job = submit(case)
        original = case.bridge.reconciliation.prepare_admission
        async def close_after_observation(**kwargs):
            prepared = await original(**kwargs)
            if kwargs['subject'].kind == phase:
                case.policies.close()
            return prepared
        case.bridge.reconciliation.prepare_admission = close_after_observation
        if phase == 'claim':
            with pytest.raises(HubError, match='execution_policy_admission_closed'):
                await case.bridge.run_once()
            assert case.queue.get_job(job['id'])['state'] == 'queued'
            assert case.repo.list_runs() == []
        else:
            assert (await case.bridge.run_once())['state'] == 'held'
            assert machine_job(case) == job['id']
        assert case.client.submissions == []
        # Rejected fresh preparation follows the gate's existing fail-closed
        # disposal rule; registry closure is not a substitute recovery proof.
        await asyncio.sleep(0)
        assert case.bridge.reconciliation.admission_state == 'closed'


@pytest.mark.asyncio
async def test_runtime_shutdown_after_fresh_observation_does_not_claim(rig, monkeypatch):
    from engineering_hub.events import NativeHubEventFeed
    from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime

    with async_pilot_case(rig) as case:
        await accept(case)
        _, job = submit(case)
        feed = NativeHubEventFeed(case.repo.path)
        runtime = QueuedPilotRuntime(case.bridge, feed)
        original = case.bridge._preclaim_guard
        guards = 0
        reached = asyncio.Event()

        def close_after_observation():
            nonlocal guards
            guards += 1
            if guards == 2:
                case.policies.close()
                runtime.suspend_claims()
                reached.set()
            return original()

        monkeypatch.setattr(case.bridge, '_preclaim_guard', close_after_observation)
        try:
            assert (await feed.start())['available']
            await runtime.start()
            await asyncio.wait_for(reached.wait(), 5)
            await asyncio.wait_for(runtime.drain_current(), 5)
            await asyncio.wait_for(runtime.stop(), 5)
            assert case.queue.get_job(job['id'])['state'] == 'queued'
            assert case.repo.list_runs() == [] and case.client.submissions == []
        finally:
            await runtime.stop()
            await feed.stop()


@pytest.mark.asyncio
async def test_prior_owner_terminal_proof_recovers_before_new_startup_opens(rig):
    _, job = submit(rig)
    original = rig.queue.complete
    def cancel_completion(**kwargs):
        if kwargs['outcome'] == 'succeeded':
            raise asyncio.CancelledError()
        return original(**kwargs)
    rig.queue.complete = cancel_completion
    with pytest.raises(asyncio.CancelledError):
        await rig.bridge.run_once()
    saved = mapping(rig, job['id'])
    assert rig.repo.get_run(saved['run_id'])['status'] == 'succeeded'
    path, clock = rig.repo.path, rig.clock
    rig.owner.close()
    clock[0] += 601
    replacement = make_context(path, clock, activate=False)
    try:
        result = await replacement.bridge.recover(job['id'])
        assert result['state'] == 'succeeded'
        assert replacement.client.submissions == []
        assert mapping(replacement, job['id'])['worker'] == saved['worker']
        assert mapping(replacement, job['id'])['fence'] == saved['fence']
        assert mapping(replacement, job['id'])['run_id'] == saved['run_id']
        assert machine_job(replacement) is None
    finally:
        replacement.owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['callback', 'action', 'worker', 'binding'])
async def test_shared_drift_refuses_before_any_host_post(rig, drift):
    submit(rig)
    if drift == 'callback':
        rig.queue.claim_verifier = None
    elif drift == 'action':
        rig.queue.actions[ACTION] = WorkAction(MACHINE, project='engineering-hub')
    elif drift == 'worker':
        rig.bridge.worker = WorkWorker(rig.bridge.worker.agent, rig.bridge.worker.instance_id)
    else:
        rig.bridge._execution_binding = None
    with pytest.raises(HubError):
        await rig.bridge.run_once()
    assert rig.client.submissions == [] and rig.repo.list_runs() == []


def test_wrong_registry_queue_rejected_without_replacing_dispatchers(rig):
    other_queue = WorkQueue(rig.repo, dict(rig.queue.actions))
    other = SharedExecutionPolicies(rig.repo, other_queue)
    original = rig.queue.claim_verifier
    with pytest.raises(HubError, match='queued_pilot_policy_conflict'):
        QueuedContainerPilot(rig.queue, rig.workflow, action_key=ACTION, machine_id=MACHINE,
            reconciliation=rig.bridge.reconciliation, execution_policies=other)
    assert rig.queue.claim_verifier is original


def test_whole_bridge_context_substitution_cannot_retain_original_policy(rig, tmp_path):
    private = tmp_path / 'other-private'
    private.mkdir(mode=0o700)
    other = make_context(private / 'hub.sqlite3', [1000.0])
    try:
        rig.bridge.queue, rig.bridge.repo, rig.bridge.workflow = other.queue, other.repo, other.workflow
        rig.bridge.owner = other.owner
        other.repo._queued_pilot = rig.bridge
        with pytest.raises(HubError, match='queued_pilot_policy_changed'):
            rig.bridge._guard()
    finally:
        other.owner.close()


@pytest.mark.asyncio
async def test_owner_loss_and_private_permit_and_changed_review_still_deny(rig):
    task, job = submit(rig)
    with pytest.raises(HubError):
        rig.workflow.create_run('codex', task['id'], task['revision'], IMAGE, 'engineering-hub:staging')
    claimed = rig.queue.claim(worker=rig.bridge.worker, action_keys=(ACTION,))
    assert rig.queue.completion_verifier(claimed, job['id'], 'succeeded') is False
    current = rig.repo.get_task(task['id'])
    with pytest.raises(HubError, match='active_work_job'):
        rig.repo.set_candidate('codex', task['id'], current['revision'], 'f' * 40)
    # Simulate stale persisted review independently of the public guard.
    with rig.repo._transaction() as db:
        changed = rig.repo._get(db, task['id'], 'task')
        changed.update(candidate_commit='f' * 40, review=None, status='in_review')
        rig.repo._save(db, 'codex', 'task.candidate', 'task', changed)
    with rig.bridge._scope(job['id'], 'execute'):
        with pytest.raises(HubError):
            rig.bridge._intent(claimed)
    rig.owner.close()
    with pytest.raises((PilotOwnershipError, HubError)):
        rig.bridge._guard()
    assert rig.client.submissions == []


def test_foreign_modular_reservation_still_blocks_pilot_enrollment(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    with PilotOwner(private / 'hub.db', allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(private / 'hub.db', execution_principal=PRINCIPAL, pilot_owner=owner)
        client = DistinctContainerClient()
        workflow = ContainerPilotWorkflow(repo, client,
            [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=owner)
        actions = {key: WorkAction(MACHINE, project='engineering-hub') for key in (ACTION, MODULAR)}
        seed = WorkQueue(repo, actions, target_verifier=lambda *args: True)
        task = reviewed_task(repo)
        job = seed.submit(actor='codex', request_id='modular', action_key=MODULAR,
            environment='staging', target=WorkTarget(task['id'], COMMIT, IMAGE))
        seed.claim(worker=WorkWorker(STAGING_SERVICE_EXECUTOR, str(uuid.uuid4())))
        queue = WorkQueue(repo, actions)
        policies = SharedExecutionPolicies(repo, queue)
        with pytest.raises(HubError, match='queued_pilot_unbound_reservation_present'):
            QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE,
                reconciliation=synthetic_workflow_reconciliation(workflow), execution_policies=policies)
        assert queue.get_job(job['id'])['state'] == 'claimed'
        assert client.submissions == [] and repo.list_runs() == []
