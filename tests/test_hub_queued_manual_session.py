"""Mandatory manual gate integration; real Linux owners, synthetic host facts only."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip('fcntl', reason='real Linux owner required')

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.events import NativeHubEventFeed
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.queued_pilot import QueuedContainerPilot
from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.startup_reconciliation import ExecutorSessionIdentity, OperatorReconciliation, StartupReconciliation
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget
from tests.test_hub_container_pilot import COMMIT, IMAGE, PRINCIPAL, pilot_manifest, reviewed_task
from tests.test_hub_queued_pilot import DistinctContainerClient, ACTION, MACHINE
from tests.test_hub_queued_pilot import rig as recovery_rig, submit as recovery_submit, reopen, machine_job, mapping
from tests.test_hub_queued_pilot_service import setup as private_service, options as private_options
from tests.test_hub_unified_queued_pilot_service import (
    rig as unified_service, start as start_unified, connections, options as unified_options,
)


@pytest.fixture
def composition(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    with PilotOwner(private / 'hub.sqlite3', allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(private / 'hub.sqlite3', execution_principal=PRINCIPAL, pilot_owner=owner)
        target = ContainerPilotTarget.from_manifest(pilot_manifest())
        client = DistinctContainerClient()
        workflow = ContainerPilotWorkflow(repo, client, [target], pilot_owner=owner)
        queue = WorkQueue(repo, {ACTION: WorkAction(MACHINE, project='engineering-hub')})
        identity = ExecutorSessionIdentity(str(uuid4()), 'sha256:' + 'e' * 64)
        observed = [identity]
        gate = StartupReconciliation(owner=owner, database=repo.path, target_id=target.target_id,
            manifest_digest=target.manifest_digest, executor=identity, lifetime_seconds=3600,
            current_executor_identity=lambda: observed[0])
        bridge = QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE, reconciliation=gate)
        yield SimpleNamespace(owner=owner, repo=repo, target=target, client=client, workflow=workflow,
                              queue=queue, gate=gate, bridge=bridge, identity=identity, observed=observed)
        gate.close()


def accept(s):
    s.gate.accept_operator_reconciliation(owner=s.owner, report=OperatorReconciliation(
        s.gate.challenge, s.owner.epoch, s.target.target_id, s.target.manifest_digest,
        s.identity, 'sha256:' + 'f' * 64, True, True, True))


def submit(s):
    task = reviewed_task(s.repo, str(uuid4()))
    return s.queue.submit(actor='codex', request_id=str(uuid4()), action_key=ACTION,
        environment='staging', target=WorkTarget(task['id'], COMMIT, IMAGE))


@pytest.mark.asyncio
async def test_pending_blocks_submission_and_direct_bridge_without_poisoning_acceptance(composition):
    s = composition
    assert s.bridge.reconciliation_state == 'pending'
    with pytest.raises(HubError):
        submit(s)
    with pytest.raises(HubError, match='queued_pilot_reconciliation_required'):
        await s.bridge.run_once()
    assert s.gate.admission_state == 'pending' and s.client.submissions == []
    accept(s)
    job = submit(s)
    assert (await s.bridge.run_once())['state'] == 'succeeded'
    assert len(s.client.submissions) == 2
    assert s.queue.get_job(job['id'])['state'] == 'succeeded'


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke', ['close', 'identity'])
async def test_revoked_gate_prevents_next_phase_but_retains_accepted_effect(composition, revoke):
    s = composition
    accept(s)
    job = submit(s)
    original = s.client.submit_command
    async def first(command):
        response = await original(command)
        if revoke == 'close':
            s.gate.close()
        else:
            s.observed[0] = ExecutorSessionIdentity(str(uuid4()), s.identity.artifact_digest)
        return response
    s.client.submit_command = first
    assert (await s.bridge.run_once())['state'] == 'held'
    assert len(s.client.submissions) == 1
    assert s.client.submissions[0].action == 'deploy_container'
    assert s.queue.get_job(job['id'])['state'] == 'held'
    with s.repo._connection() as db:
        assert db.execute('SELECT job_id FROM hub_work_machines').fetchone()[0] == job['id']
    s.owner.assert_active(s.repo.path)


@pytest.mark.asyncio
async def test_revocation_before_claim_does_not_allocate_run_or_reservation(composition):
    s = composition
    accept(s)
    job = submit(s)
    s.gate.close()
    with pytest.raises(HubError):
        await s.bridge.run_once()
    assert s.queue.claim(worker=s.bridge.worker, lease_seconds=600, action_keys=(ACTION,)) is None
    assert s.repo.list_runs() == [] and s.client.submissions == []
    assert s.queue.get_job(job['id'])['state'] == 'queued'
    with s.repo._connection() as db:
        assert db.execute('SELECT job_id FROM hub_work_machines WHERE job_id IS NOT NULL').fetchone() is None


@pytest.mark.asyncio
async def test_pending_native_runtime_accepts_later_session_then_closes_it_on_stop(composition):
    s = composition
    feed = NativeHubEventFeed(s.repo.path)
    runtime = QueuedPilotRuntime(s.bridge, feed)
    try:
        assert (await feed.start())['available']
        await runtime.start()
        assert runtime.status['state'] == 'startup_reconciliation_required'
        await asyncio.sleep(0)
        assert s.client.submissions == [] and s.gate.admission_state == 'pending'
        accept(s)
        job = submit(s)  # Normal native event wake, not a new poller.
        async with asyncio.timeout(5):
            while s.queue.get_job(job['id'])['state'] != 'succeeded':
                await asyncio.sleep(0.001)
        assert runtime.status['state'] == 'active'
    finally:
        await runtime.stop()
        await feed.stop()
    assert s.gate.admission_state == 'closed'
    s.owner.assert_active(s.repo.path)


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, True, 1])
async def test_private_service_rejects_missing_gate_before_client(private_service, value):
    s = private_service
    with pytest.raises(s.service.QueuedPilotServiceError):
        await s.service.serve_owned_queued_pilot(s.settings, s.manifest, s.client_factory,
            **private_options(s, reconciliation_factory=lambda owner, target: value))
    assert s.clients == [] and len(s.owners) == 1 and s.owners[0].close_count == 1


@pytest.mark.asyncio
async def test_unified_pending_and_closed_status_api_is_truthful_without_observer_io(unified_service):
    from tests.manual_session_fixtures import synthetic_reconciliation
    s = unified_service
    gates = []
    def pending_factory(owner, target):
        gate = synthetic_reconciliation(owner, target, accepted=False)
        gates.append(gate)
        return gate
    stop, task, addresses = await start_unified(s, reconciliation_factory=pending_factory)
    try:
        async with connections(addresses, s) as (agent, _browser):
            for expected in ['startup_reconciliation_required', 'manual_session_closed']:
                if expected == 'manual_session_closed':
                    gates[0].close()
                response = await agent.get('/api/hub/v1/autonomy')
                assert response.status_code == 200
                body = response.json()
                assert body['state'] == body['queued_pilot']['state'] == expected
                assert body['running'] is True and body['queued_pilot']['active'] is False
            assert s.requests == []
    finally:
        stop.set()
        await asyncio.wait_for(task, 8)
    assert gates[0].admission_state == 'closed'


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, False, 1])
async def test_unified_service_refuses_unbound_factory_result(unified_service, value):
    from engineering_hub import unified_queued_pilot_service as service
    s = unified_service
    with pytest.raises(service.QueuedPilotServiceError):
        await service.serve_owned_unified_queued_pilot(s.settings, s.manifest, s.client_factory,
            **unified_options(s, reconciliation_factory=lambda owner, target: value))
    assert s.clients == [] and s.owner.close_count == 1


@pytest.mark.asyncio
async def test_rejected_local_factory_task_drains_before_owner_close(unified_service):
    from engineering_hub import unified_queued_pilot_service as service
    s = unified_service
    entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))
    async def existing_work():
        try:
            entered.set()
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()
            s.owner.assert_active(s.settings.database)
            assert s.owner.close_count == 0
            finished.set()
    returned = asyncio.create_task(existing_work())
    await entered.wait()
    serving = asyncio.create_task(service.serve_owned_unified_queued_pilot(
        s.settings, s.manifest, s.client_factory,
        **unified_options(s, reconciliation_factory=lambda owner, target: returned)))
    try:
        await asyncio.wait_for(cleaning.wait(), 5)
        assert not serving.done() and s.owner.close_count == 0
        serving.cancel()
        await asyncio.sleep(0)
        assert not serving.done() and s.owner.close_count == 0
    finally:
        release.set()
        with pytest.raises((service.QueuedPilotServiceError, asyncio.CancelledError)):
            await asyncio.wait_for(serving, 8)
        await asyncio.gather(returned, return_exceptions=True)
    assert finished.is_set() and s.owner.close_count == 1 and s.clients == []


@pytest.mark.asyncio
async def test_returning_current_service_task_rejected_without_self_cancel_deadlock(unified_service):
    from engineering_hub import unified_queued_pilot_service as service
    s = unified_service
    with pytest.raises(service.QueuedPilotServiceError):
        await asyncio.wait_for(service.serve_owned_unified_queued_pilot(
            s.settings, s.manifest, s.client_factory,
            **unified_options(s, reconciliation_factory=lambda owner, target: asyncio.current_task())), 5)
    assert s.owner.close_count == 1 and s.clients == []


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['private', 'unified'])
async def test_returning_outer_service_task_never_cancels_or_gathers_its_own_lifecycle(request, kind):
    from engineering_hub import unified_queued_pilot_service as unified
    s = request.getfixturevalue('private_service' if kind == 'private' else 'unified_service')
    outer = None
    factory = lambda owner, target: outer
    if kind == 'private':
        serving = s.service.serve_owned_queued_pilot(s.settings, s.manifest, s.client_factory,
            **private_options(s, reconciliation_factory=factory))
    else:
        serving = unified.serve_owned_unified_queued_pilot(s.settings, s.manifest, s.client_factory,
            **unified_options(s, reconciliation_factory=factory))
    outer = asyncio.create_task(serving)
    with pytest.raises(unified.QueuedPilotServiceError):
        await asyncio.wait_for(asyncio.shield(outer), 5)
    assert outer.done() and not outer.cancelled() and s.clients == []
    owner = s.owners[0] if kind == 'private' else s.owner
    assert owner.close_count == 1


@pytest.mark.asyncio
async def test_fixed_selectors_without_trusted_factory_refuse_before_credentials(monkeypatch):
    from engineering_hub import unified_pilot_startup as startup
    from hub_container_executor import installed_coordinator as installed
    from tests.test_hub_container_executor_installed_coordinator import parse
    from engineering_hub.app import Settings
    monkeypatch.setattr(Settings, 'from_env', lambda *_: pytest.fail('credential read'))
    monkeypatch.setattr(startup, 'PilotOwner', lambda *_a, **_k: pytest.fail('owner construction'))
    with pytest.raises(startup.UnifiedPilotStartupError):
        await startup.serve_configuration('/not/read/settings.json', '/not/read/pilot.json')
    monkeypatch.setattr(installed, '_process', lambda: None)
    monkeypatch.setattr(installed, 'ImmutableTestSecretResolver', lambda *_a, **_k: pytest.fail('TEST credentials'))
    with pytest.raises(installed.InstalledCoordinatorError):
        await installed.run_installed_coordinator(parse())


@pytest.mark.asyncio
async def test_closed_recovery_may_get_evidence_but_cannot_release_machine(recovery_rig, monkeypatch):
    from tests.manual_session_fixtures import synthetic_workflow_reconciliation
    s = recovery_rig
    _, job = recovery_submit(s)
    original_complete = s.queue.complete
    def interrupted(**kwargs):
        if kwargs['outcome'] == 'succeeded':
            raise asyncio.CancelledError()
        return original_complete(**kwargs)
    monkeypatch.setattr(s.queue, 'complete', interrupted)
    with pytest.raises(asyncio.CancelledError):
        await s.bridge.run_once()
    s.clock[0] += 601
    reopen(s)
    s.bridge.reconciliation.close()
    recovered = await s.bridge.recover(job['id'])
    # Read-only expiry reporting need not mutate claimed -> held. Either state
    # retains the machine and explicitly requires authoritative reconciliation.
    assert recovered['state'] in {'claimed', 'held'} and recovered['requires_reconciliation'] is True
    assert len(s.client.inspections) == 2
    assert s.client.submissions == [] and machine_job(s) == job['id']
    with s.repo._connection() as db:
        intent = mapping(s, job['id'])
        evidence = s.bridge._evidence_id(intent)
        assert s.bridge._proof(db, intent) == 'succeeded'
    with s.bridge._scope(job['id'], 'recover'):
        with pytest.raises(HubError, match='work_reconciliation_required'):
            s.queue.reconcile(job_id=job['id'], evidence_id=evidence, outcome='succeeded')
    assert machine_job(s) == job['id']
    # Explicit fresh synthetic operator acceptance, not receipt-driven opening.
    s.bridge.reconciliation = synthetic_workflow_reconciliation(s.workflow)
    assert (await s.bridge.recover(job['id']))['state'] == 'succeeded'
    assert machine_job(s) is None and s.client.submissions == []
