"""One real Linux owner/ledger/queue, real controllers, simulated remote effects.

This does not prove Docker execution, staging host capacity, NAS or reboot acceptance.
"""
from contextlib import contextmanager
from types import SimpleNamespace
import json
import sys
import uuid

import pytest

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.execution_policy import SharedExecutionPolicies
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.queued_pilot import QueuedContainerPilot
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.staging_controller import StagingController, compose_queue
from engineering_hub.staging_resources import StagingResourceStore
from engineering_hub.work_queue import WorkAction
from tests.manual_session_fixtures import synthetic_workflow_reconciliation
from tests.test_hub_container_pilot import ACTIONS, PRINCIPAL, pilot_manifest
from tests.test_hub_queued_pilot import ACTION, MACHINE, DistinctContainerClient, machine_job, submit
from tests.test_hub_staging_controller import Client, payload


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux PilotOwner exclusion')


@contextmanager
def joint_case(tmp_path, *, reopening=False, clients=None, activate=True):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700, exist_ok=reopening)
    database = private / 'hub.sqlite3'
    owner = PilotOwner(database, allow_ephemeral_test_storage=True)
    try:
        repo = HubRepository(database, execution_principal=PRINCIPAL, pilot_owner=owner)
        pilot_client, modular_client = clients or (DistinctContainerClient(), Client())
        workflow = ContainerPilotWorkflow(repo, pilot_client,
            [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=owner)
        queue, actions = compose_queue(repo, None, {'engineering-hub': ['static']}, MACHINE)
        queue.actions[ACTION] = WorkAction(MACHINE, project='engineering-hub')
        policies = SharedExecutionPolicies(repo, queue)
        principal = object()
        resources = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}), control_principal=principal)
        if not reopening:
            resources.set_policy('human:1', 'engineering-hub', 0, {'codex': ['read', 'build', 'test']})
        modular = StagingController(repo, queue, modular_client, resources=resources,
            control_principal=principal, actions=actions, execution_policies=policies)
        bridge = QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE,
            reconciliation=synthetic_workflow_reconciliation(workflow), execution_policies=policies,
            modular_controller=modular)
        policies.install()
        if activate:
            policies.ready('pilot'); policies.ready('modular'); policies.open()
        yield SimpleNamespace(owner=owner, repo=repo, queue=queue, bridge=bridge, workflow=workflow,
            client=pilot_client, modular=modular, modular_client=modular_client, policies=policies)
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_real_controllers_share_one_owner_and_release_same_machine_sequentially(tmp_path):
    with joint_case(tmp_path) as case:
        modular = await case.modular.submit('codex', payload())
        _, pilot = submit(case)
        assert case.modular.queue is case.bridge.queue is case.queue
        assert case.modular.repo is case.workflow.repo is case.repo
        assert (await case.modular.run_once())['job']['state'] == 'succeeded'
        assert case.repo.list_runs() == []
        assert (await case.bridge.run_once())['state'] == 'succeeded'
        assert len(case.repo.list_runs()) == 1 and machine_job(case) is None
        assert case.repo.execution_principal == PRINCIPAL
        assert case.queue.get_job(modular['job']['id'])['worker'].startswith('executor:staging-service:')
        assert case.queue.get_job(pilot['id'])['worker'].startswith('executor:container-pilot:')


@pytest.mark.asyncio
async def test_lost_modular_ack_blocks_pilot_until_original_get_recovery(tmp_path):
    with joint_case(tmp_path) as case:
        case.modular_client.lost_ack = True
        modular = await case.modular.submit('codex', payload())
        _, pilot = submit(case)
        assert (await case.modular.run_once())['job']['state'] == 'held'
        assert await case.bridge.run_once() is None
        assert case.client.submissions == [] and machine_job(case) == modular['job']['id']
        assert (await case.modular.reconcile(modular['job']['id']))['job']['state'] == 'succeeded'
        assert case.modular_client.posts == 1
        assert (await case.bridge.run_once())['id'] == pilot['id']


@pytest.mark.asyncio
async def test_held_pilot_blocks_modular_without_creating_another_run(tmp_path):
    with joint_case(tmp_path) as case:
        case.client.outcomes['deploy_container'] = TimeoutError()
        _, pilot = submit(case)
        modular = await case.modular.submit('codex', payload())
        assert (await case.bridge.run_once())['state'] == 'held'
        assert await case.modular.run_once() is None
        assert machine_job(case) == pilot['id'] and case.modular_client.posts == 0
        assert case.queue.get_job(modular['job']['id'])['state'] == 'queued'
        assert len(case.repo.list_runs()) == 1


@pytest.mark.asyncio
async def test_real_pilot_health_failure_rolls_back_before_modular_can_run(tmp_path):
    with joint_case(tmp_path) as case:
        case.client.outcomes['smoke_container'] = 'failed'
        _, pilot = submit(case)
        await case.modular.submit('codex', payload())
        assert (await case.bridge.run_once())['state'] == 'failed'
        assert [command.action for command in case.client.submissions] == ACTIONS
        assert case.repo.list_runs()[0]['status'] == 'rolled_back'
        assert case.queue.get_job(pilot['id'])['state'] == 'failed' and machine_job(case) is None
        assert (await case.modular.run_once())['job']['state'] == 'succeeded'


def original_hold_rows(case, job_id):
    with case.repo._connection() as db:
        return (
            tuple(db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job_id,)).fetchone()),
            tuple(db.execute('SELECT * FROM hub_work_machines WHERE machine_id=?', (MACHINE,)).fetchone()),
            tuple(db.execute("SELECT * FROM hub_staging_submissions WHERE json_extract(data,'$.job_id')=?", (job_id,)).fetchone()),
            tuple(tuple(row) for row in db.execute('SELECT * FROM hub_staging_resource_ownership WHERE job_id=? ORDER BY id', (job_id,))),
            tuple(row[0] for row in db.execute("SELECT data FROM hub_entities WHERE kind='staging_resource' AND json_extract(data,'$.job_id')=? ORDER BY id", (job_id,))),
        )


@pytest.mark.asyncio
async def test_reopened_owner_preserves_exact_modular_hold_then_original_get_recovers(tmp_path):
    with joint_case(tmp_path) as initial:
        initial.modular_client.lost_ack = True
        submitted = await initial.modular.submit('codex', payload())
        held = await initial.modular.run_once()
        assert held['job']['state'] == 'held'
        before = original_hold_rows(initial, submitted['job']['id'])
        old_worker, old_fence = held['job']['worker'], held['job']['fence']
        clients = initial.client, initial.modular_client
        initial.policies.close()
        initial.owner.close()
        with joint_case(tmp_path, reopening=True, clients=clients, activate=False) as restored:
            assert restored.modular.worker.encoded() != old_worker
            assert original_hold_rows(restored, submitted['job']['id']) == before
            assert machine_job(restored) == submitted['job']['id']
            with pytest.raises(HubError, match='admission_closed'):
                await restored.bridge.run_once()
            assert clients[0].submissions == [] and clients[1].posts == 1
            result = await restored.modular.reconcile(submitted['job']['id'])
            assert result['job']['state'] == 'succeeded'
            assert result['job']['worker'] == old_worker and result['job']['fence'] == old_fence
            assert machine_job(restored) is None
            assert clients[1].posts == clients[1].deposits == 1


@pytest.mark.asyncio
async def test_inconsistent_original_modular_submission_still_denies_reenrollment(tmp_path):
    with joint_case(tmp_path) as initial:
        initial.modular_client.lost_ack = True
        submitted = await initial.modular.submit('codex', payload())
        await initial.modular.run_once()
        with initial.repo._transaction() as db:
            row = db.execute("SELECT id,data FROM hub_staging_submissions WHERE json_extract(data,'$.job_id')=?",
                             (submitted['job']['id'],)).fetchone()
            data = json.loads(row['data'])
            data['source_digest'] = 'f' * 64
            db.execute('UPDATE hub_staging_submissions SET data=? WHERE id=?', (json.dumps(data), row['id']))
        before = original_hold_rows(initial, submitted['job']['id'])
        clients = initial.client, initial.modular_client
        initial.policies.close()
        initial.owner.close()
        with pytest.raises(HubError, match='queued_pilot_unbound_reservation_present'):
            with joint_case(tmp_path, reopening=True, clients=clients, activate=False):
                pytest.fail('Inconsistent hold was enrolled')
        # Only owner/enrollment metadata may change; the original work remains held.
        with PilotOwner(initial.repo.path, allow_ephemeral_test_storage=True) as owner:
            repo = HubRepository(initial.repo.path, execution_principal=PRINCIPAL, pilot_owner=owner)
            assert original_hold_rows(SimpleNamespace(repo=repo), submitted['job']['id']) == before
        assert clients[0].submissions == [] and clients[1].posts == 1


@pytest.mark.asyncio
async def test_live_original_modular_lease_is_preserved_without_recovery_or_adoption(tmp_path):
    with joint_case(tmp_path) as initial:
        submitted = await initial.modular.submit('codex', payload())
        initial.queue.claim(worker=initial.modular.worker,
            action_keys=tuple(initial.modular.actions.values()), lease_seconds=600)
        before = original_hold_rows(initial, submitted['job']['id'])
        clients = initial.client, initial.modular_client
        initial.policies.close()
        initial.owner.close()
        with joint_case(tmp_path, reopening=True, clients=clients, activate=False) as restored:
            await restored.modular._recover_pending()
            assert original_hold_rows(restored, submitted['job']['id']) == before
            restored.policies.ready('pilot'); restored.policies.ready('modular'); restored.policies.open()
            submit(restored, 'waiting-behind-old-lease')
            assert await restored.bridge.run_once() is None
            assert machine_job(restored) == submitted['job']['id']
            assert clients[0].submissions == [] and clients[1].posts == 0


@pytest.mark.asyncio
async def test_journal_coherence_does_not_authorize_a_mismatched_original_receipt(tmp_path):
    with joint_case(tmp_path) as initial:
        initial.modular_client.lost_ack = True
        submitted = await initial.modular.submit('codex', payload())
        await initial.modular.run_once()
        with initial.repo._transaction() as db:
            row = db.execute("SELECT id,data FROM hub_staging_submissions WHERE json_extract(data,'$.job_id')=?",
                             (submitted['job']['id'],)).fetchone()
            data = json.loads(row['data'])
            data['command_id'] = str(uuid.uuid4())  # Coherent UUID is not remote attestation.
            db.execute('UPDATE hub_staging_submissions SET data=? WHERE id=?', (json.dumps(data), row['id']))
        before = original_hold_rows(initial, submitted['job']['id'])
        clients = initial.client, initial.modular_client
        initial.policies.close()
        initial.owner.close()
        with joint_case(tmp_path, reopening=True, clients=clients, activate=False) as restored:
            assert original_hold_rows(restored, submitted['job']['id']) == before
            assert (await restored.modular.reconcile(submitted['job']['id']))['job']['state'] == 'held'
            assert original_hold_rows(restored, submitted['job']['id']) == before
            assert machine_job(restored) == submitted['job']['id']
            restored.policies.ready('pilot'); restored.policies.ready('modular'); restored.policies.open()
            submit(restored, 'blocked-by-mismatched-receipt')
            assert await restored.bridge.run_once() is None
            assert clients[0].submissions == [] and clients[1].posts == 1
