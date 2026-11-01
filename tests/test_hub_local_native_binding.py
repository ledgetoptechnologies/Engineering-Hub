"""Native queue binding with synthetic executor; no Docker or host effects."""
import asyncio
import json
import sys
import uuid
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from agent_message_bus.sqlite_events import native_event_support
from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.events import NativeHubEventFeed
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.queued_pilot import QueuedContainerPilot
from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget
from scripts.hub_local_native_binding import LocalNativeBinding
from tests.manual_session_fixtures import synthetic_workflow_reconciliation
from tests.test_hub_container_pilot import COMMIT, IMAGE, PRINCIPAL, pilot_manifest, reviewed_task
from tests.test_hub_queued_pilot import ACTION, MACHINE, DistinctContainerClient


@pytest.mark.skipif(sys.platform != 'linux', reason='native Linux source required')
@pytest.mark.asyncio
@pytest.mark.parametrize('modern', [False, True], ids=['legacy', 'async-admission'])
async def test_native_run_is_bound_after_real_queue_claim_and_before_synthetic_dispatch(tmp_path, modern, monkeypatch):
    owner_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    data = json.loads(pilot_manifest().canonical_bytes())
    data['runtime']['resource_name_prefix'] = 'hub-pilot-' + uuid.UUID(owner_id).hex[:24]
    manifest = PilotManifest.from_bytes(json.dumps(data).encode())
    private = tmp_path / 'private'; private.mkdir(mode=0o700)
    path = private / 'hub.sqlite3'
    owner = PilotOwner(path, allow_ephemeral_test_storage=True)
    repo = HubRepository(path, execution_principal=PRINCIPAL, pilot_owner=owner)
    captured = []
    class Client(DistinctContainerClient):
        async def submit_command(self, command):
            if not modern and command.action == 'deploy_container':
                try: captured.append(binding.bind_first_deploy(command))
                except Exception as error:
                    captured.append(error)
                    raise
            return await super().submit_command(command)
    client = Client()
    workflow = ContainerPilotWorkflow(repo, client, [ContainerPilotTarget.from_manifest(manifest)], pilot_owner=owner)
    queue = WorkQueue(repo, {ACTION: WorkAction(MACHINE, project='engineering-hub')})
    bridge = QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE,
                                  reconciliation=synthetic_workflow_reconciliation(workflow))
    binding = LocalNativeBinding(bridge=bridge, manifest=manifest, owner=owner_id)
    if modern:
        original_consume = bridge._consume
        @contextmanager
        def observe_admission(prepared, subject):
            with original_consume(prepared, subject):
                if subject.kind == 'command' and subject.action == 'deploy_container':
                    run = repo.get_run(subject.run_id)
                    command = workflow._request(run, bridge.target, 'deploy_container')
                    captured.append(binding.bind_first_deploy(command))
                yield
        bridge._consume = observe_admission
    supported, reason = native_event_support(path)
    if not supported:
        owner.close(); pytest.skip(reason)
    feed = NativeHubEventFeed(path); runtime = QueuedPilotRuntime(bridge, feed)
    if modern:
        from tests.test_hub_queued_pilot_async import async_pilot_case
        case = async_pilot_case(SimpleNamespace(bridge=bridge, owner=owner, repo=repo, queue=queue, client=client))
    else:
        case = nullcontext()
    try:
        with case as active:
            if modern:
                await active.broker.decide_async(actor='human:123', request_id=active.broker._id,
                    expected_revision=1, action='accept')
            task = reviewed_task(repo, 'native-binding')
            job = queue.submit(actor='codex', request_id='native-binding', action_key=ACTION,
                environment='staging', target=WorkTarget(task['id'], COMMIT, IMAGE))
            assert (await feed.start())['available']
            await runtime.start()
            async with asyncio.timeout(8):
                while runtime.status['last_job'] is None:
                    await asyncio.sleep(.01)
            assert runtime.status['last_job'] == {'id': job['id'], 'state': 'succeeded'}, captured
            assert len(captured) == 1
            bound = captured[0]; run = repo.list_runs()[0]
            assert bound.run_id == run['id'] and bound.fence == run['fencing_generation']
            assert bound.candidate_name.endswith(run['id'][:12] + '-candidate')
            assert bound.rollback_name.endswith(run['id'][:12] + '-rollback')
            assert bound.baseline_name.endswith('00000000-000-rollback')
            assert bound.candidate_generation != bound.rollback_generation
            from hub_container_executor.state_provider import FrozenSQLiteStateProvisioner
            provider = object.__new__(FrozenSQLiteStateProvisioner)
            provider._manifest_digest = manifest.digest()
            for role, action, operation_kind in (('candidate', 'deploy_container', 'state_copy'),
                                                 ('rollback', 'rollback_container', 'rollback_state')):
                command_id = uuid.uuid5(uuid.UUID(run['id']), 'container_pilot_v1:' + action)
                operation_id = str(uuid.uuid5(command_id, operation_kind))
                assert getattr(bound, role + '_generation') == provider._generation(run['id'], operation_id, role)
            with pytest.raises(ValueError):
                binding.bind_first_deploy(client.submissions[0])
            outside = LocalNativeBinding(bridge=bridge, manifest=manifest, owner=owner_id)
            with pytest.raises(HubError):
                outside.bind_first_deploy(client.submissions[0])
            with monkeypatch.context() as patch:
                patch.setattr(repo, 'queued_pilot_enrolled', lambda _db=None: False)
                unenrolled = LocalNativeBinding(bridge=bridge, manifest=manifest, owner=owner_id)
                with pytest.raises(ValueError, match='native queue enrollment unavailable'):
                    unenrolled.bind_first_deploy(client.submissions[0])
    finally:
        try:
            await runtime.stop()
        finally:
            await feed.stop(); owner.close()
