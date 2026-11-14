"""Synthetic second family, real queue/ledger; no pilot enrollment or HTTP proof."""
import uuid

import pytest

from engineering_hub.execution_policy import SharedExecutionPolicies
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.staging_controller import StagingController, compose_queue
from engineering_hub.staging_resources import StagingResourceStore
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkWorker, CONTAINER_PILOT_EXECUTOR
from tests.test_hub_staging_controller import Client, payload


@pytest.fixture
def shared(tmp_path):
    repo = HubRepository(tmp_path / 'shared.db', execution_principal=CONTAINER_PILOT_EXECUTOR)
    now, principal = [1800000000], object()
    resources = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}),
                                    control_principal=principal, clock=lambda: now[0])
    resources.set_policy('human:1', 'engineering-hub', 0,
                         {'codex': ['read', 'build', 'test', 'cleanup', 'preview']})
    queue, actions = compose_queue(repo, None, {'engineering-hub': ['static']}, 'staging-pc')
    queue.actions['pilot-fixture'] = WorkAction('staging-pc', project='engineering-hub')
    policies = SharedExecutionPolicies(repo, queue)
    client = Client()
    controller = StagingController(repo, queue, client, resources=resources,
        control_principal=principal, actions=actions, execution_policies=policies,
        poll_interval=0.001, max_poll_seconds=0.1)
    # These callbacks stand in for a separately guarded deployment family.
    # They provide no evidence about real ContainerPilot composition/recovery.
    policies.bind('pilot', {'pilot-fixture': queue.actions['pilot-fixture']},
        WorkWorker(CONTAINER_PILOT_EXECUTOR, str(uuid.uuid4())), target=lambda *a: True,
        claim=lambda *a: True, completion=lambda *a: True, reconciliation=lambda *a: True)
    policies.install()
    return controller, policies, client, resources, now, repo


def opened(shared):
    controller, policies, *_ = shared
    policies.ready('pilot')
    policies.ready('modular')
    policies.open()
    return controller


@pytest.mark.asyncio
async def test_closed_startup_never_deposits_or_posts(shared):
    controller, policies, client, *_ = shared
    with pytest.raises(HubError, match='admission_closed'):
        await controller.submit('codex', payload(shared[-1]))
    policies.ready('modular')
    with pytest.raises(HubError):
        policies.open()
    assert client.deposits == client.posts == 0


@pytest.mark.asyncio
async def test_shared_modular_job_preserves_principal_and_creates_no_pilot_run(shared):
    controller = opened(shared)
    repo = shared[-1]
    submitted = await controller.submit('codex', payload(shared[-1]))
    finished = await controller.run_once()
    assert finished['job']['state'] == 'succeeded', (controller._read(job_id=submitted['job']['id']), shared[2].posts)
    assert repo.execution_principal == CONTAINER_PILOT_EXECUTOR
    with repo._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM hub_entities WHERE kind='run'").fetchone()[0] == 0
    assert submitted['job']['worker'] is None


@pytest.mark.asyncio
async def test_close_during_upload_preserves_receipt_but_never_queues_or_executes(shared):
    controller = opened(shared)
    policies, client = shared[1:3]
    original = client.put_source
    async def upload(request):
        result = await original(request)
        policies.close()
        return result
    client.put_source = upload
    with pytest.raises(HubError, match='admission_closed'):
        await controller.submit('codex', payload(shared[-1]))
    with shared[-1]._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_work_jobs').fetchone()[0] == 0
    assert client.deposits == 1 and client.posts == 0


@pytest.mark.asyncio
async def test_close_after_original_job_acceptance_drains_without_new_post(shared):
    controller = opened(shared)
    policies, client = shared[1:3]
    await controller.submit('codex', payload(shared[-1]))
    original = client.submit
    async def accepted(request):
        result = await original(request)
        policies.close()
        return result
    client.submit = accepted
    assert (await controller.run_once())['job']['state'] == 'succeeded'
    assert client.posts == 1
    with pytest.raises(HubError, match='admission_closed'):
        await controller.run_once()
    assert client.posts == 1


@pytest.mark.asyncio
async def test_context_drift_after_job_post_retains_original_claim_and_reconciles_without_repost(shared):
    controller = opened(shared)
    client = shared[2]
    submitted = await controller.submit('codex', payload(shared[-1]))
    original = client.submit
    async def accepted(request):
        result = await original(request)
        controller.client = Client()
        return result
    client.submit = accepted
    with pytest.raises(HubError, match='staging_policy_changed'):
        await controller.run_once()
    assert controller.queue.get_job(submitted['job']['id'])['state'] == 'claimed'
    assert controller._read(job_id=submitted['job']['id'])['phase'] == 'dispatching'
    controller.client = client
    assert (await controller.reconcile(submitted['job']['id']))['job']['state'] == 'succeeded'
    assert client.posts == 1


@pytest.mark.asyncio
async def test_context_drift_after_resource_read_does_not_mark_local_resource_ready(shared):
    controller = opened(shared)
    client, resources = shared[2:4]
    submitted = await controller.submit('codex', payload(shared[-1]))
    original = client.resource
    async def observed(identity):
        result = await original(identity)
        controller.resources = object()
        return result
    client.resource = observed
    with pytest.raises(HubError, match='staging_policy_changed'):
        await controller.run_once()
    assert all(resources.get_resource('codex', identity)['state'] == 'provisioning'
               for identity in submitted['resource_ids'].values())
    assert controller.queue.get_job(submitted['job']['id'])['state'] == 'claimed'
    controller.resources = resources
    client.resource = original
    assert (await controller.reconcile(submitted['job']['id']))['job']['state'] == 'succeeded'
    assert client.posts == 1


@pytest.mark.parametrize('change', ['callback', 'action_map', 'client', 'registry'])
@pytest.mark.asyncio
async def test_binding_drift_before_claim_never_posts(shared, change):
    controller = opened(shared)
    await controller.submit('codex', payload(shared[-1]))
    if change == 'callback':
        controller.queue.claim_verifier = None
    elif change == 'action_map':
        controller.actions.clear()
    elif change == 'client':
        controller.client = Client()
    else:
        controller.execution_policies = None
    with pytest.raises(HubError):
        await controller.run_once()
    assert shared[2].posts == 0


@pytest.mark.asyncio
async def test_close_during_cleanup_preflight_never_deletes(shared):
    controller = opened(shared)
    policies, client, resources, now = shared[1:5]
    submitted = await controller.submit('codex', payload(shared[-1]))
    await controller.run_once()
    now[0] += 32 * 86400
    plan = resources.cleanup_plan('codex', 'engineering-hub', kinds=['preview'])
    original = client.resource
    async def observed(identity):
        result = await original(identity)
        policies.close()
        return result
    client.resource = observed
    result = await controller.cleanup_apply('codex', plan['plan_id'], plan['digest'])
    assert result['removed'] == [] and client.cleanup_posts == 0
    assert resources.get_resource('codex', submitted['resource_ids']['preview'])['state'] == 'ready'


@pytest.mark.asyncio
async def test_close_after_lost_cleanup_ack_reconciles_only_original_operation(shared):
    controller = opened(shared)
    policies, client, resources, now = shared[1:5]
    submitted = await controller.submit('codex', payload(shared[-1]))
    await controller.run_once()
    now[0] += 32 * 86400
    plan = resources.cleanup_plan('codex', 'engineering-hub', kinds=['preview'])
    client.cleanup_lost = True
    assert (await controller.cleanup_apply('codex', plan['plan_id'], plan['digest']))['failed']
    policies.close()
    result = await controller.cleanup_apply('codex', plan['plan_id'], plan['digest'])
    assert result['removed'] == [submitted['resource_ids']['preview']]
    assert client.cleanup_posts == 1


@pytest.mark.asyncio
async def test_recreated_controller_recovers_lost_ack_before_open_without_repost(shared):
    controller = opened(shared)
    old_policies, client, resources, _, repo = shared[1:]
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(shared[-1]))
    held = await controller.run_once()
    assert held['job']['state'] == 'held'
    original_worker, original_fence = held['job']['worker'], held['job']['fence']
    old_policies.close()

    queue = WorkQueue(repo, dict(controller.queue.actions))
    policies = SharedExecutionPolicies(repo, queue)
    restored = StagingController(repo, queue, client, resources=resources,
        control_principal=controller.principal, actions=controller.actions, execution_policies=policies)
    policies.bind('pilot', {'pilot-fixture': queue.actions['pilot-fixture']},
        WorkWorker(CONTAINER_PILOT_EXECUTOR, str(uuid.uuid4())), target=lambda *a: True,
        claim=lambda *a: True, completion=lambda *a: True, reconciliation=lambda *a: True)
    policies.install()  # Admission remains closed until original recovery completes.
    assert restored.worker != controller.worker
    result = await restored.reconcile(submitted['job']['id'])
    assert result['job']['state'] == 'succeeded'
    assert result['job']['worker'] == original_worker and result['job']['fence'] == original_fence
    assert client.posts == client.deposits == 1
    with pytest.raises(HubError, match='admission_closed'):
        await restored.run_once()
