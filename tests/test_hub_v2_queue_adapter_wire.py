"""Source-only v2 queue-to-adapter test with synthetic effects and evidence.

The host backend and its evidence verifier are test doubles; this does not
establish installed service, daemon, deployment, or real recovery acceptance.
"""
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.container_pilot_client import ContainerPilotClient
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.queued_pilot import QueuedContainerPilot
from engineering_hub.repository import HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget
from hub_container_executor.asgi import PilotASGI
from tests.manual_session_fixtures import synthetic_workflow_reconciliation
from tests.test_hub_container_executor_adapter import make_fixture
from tests.test_hub_container_pilot import PRINCIPAL, reviewed_task
from tests.test_hub_queued_pilot import ACTION, MACHINE


class FaultTransport(httpx.AsyncBaseTransport):
    def __init__(self, app, *, fault):
        self.inner = httpx.ASGITransport(app=app)
        self.fault = fault
        self.posts = []
        self.acks = []
        self.ack_status = []
        self.lost = False

    async def handle_async_request(self, request):
        if request.method == 'POST':
            body = json.loads(await request.aread())
            if request.url.path.endswith('/acknowledge'):
                self.acks.append(body['request']['command_id'])
            elif request.url.path.endswith('/commands'):
                self.posts.append(body['command_id'])
        ack = request.url.path.endswith('/acknowledge')
        command = request.url.path.endswith('/commands') and request.method == 'POST'
        if ack and self.fault == 'ack-request' and not self.lost:
            self.lost = True
            raise httpx.WriteError('synthetic lost ACK request before server', request=request)
        response = await self.inner.handle_async_request(request)
        if request.url.path.endswith('/acknowledge'):
            self.ack_status.append(response.status_code)
        if not self.lost and ((ack and self.fault == 'ack-response') or
                              (command and self.fault == 'command-response')):
            assert response.status_code == 200
            self.lost = True
            await response.aclose()
            raise httpx.ReadError('synthetic lost response after acceptance', request=request)
        return response

    async def aclose(self):
        await self.inner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['none', 'command-response', 'ack-request', 'ack-response'])
async def test_v2_queue_ack_and_recovery_do_not_repeat_effect(tmp_path, monkeypatch, fault):
    pytest.importorskip('fcntl')
    from hub_container_executor import single_pilot_evidence
    adapter, backend, state, manifest = make_fixture(tmp_path)
    backend.probe_health = ['healthy', 'healthy']
    # Synthetic boundaries deliberately stand in for enrolled Engine and state
    # evidence; the real adapter, protocol ledger, ASGI and queue remain active.
    backend.operation_journal = SimpleNamespace(require_no_pending=lambda: None)
    state.require_complete_reservations = lambda **_: None
    monkeypatch.setattr(single_pilot_evidence, 'verify_phase', lambda *_: None)
    adapter.ledger.enable_single_pilot_protocol()
    adapter._single_pilot_protocol = True
    transport = FaultTransport(PilotASGI(adapter, 'synthetic-token'), fault=fault)
    private = tmp_path / 'controller'
    private.mkdir(mode=0o700)
    try:
        async with ContainerPilotClient('http://127.0.0.1', 'synthetic-token',
                transport=transport, single_pilot_protocol=True) as client:
            with PilotOwner(private / 'hub.sqlite3', allow_ephemeral_test_storage=True) as owner:
                repo = HubRepository(private / 'hub.sqlite3', execution_principal=PRINCIPAL, pilot_owner=owner)
                target = ContainerPilotTarget.from_manifest(manifest)
                workflow = ContainerPilotWorkflow(repo, client, [target], pilot_owner=owner,
                                                  single_pilot_protocol=True)
                queue = WorkQueue(repo, {ACTION: WorkAction(MACHINE, project='engineering-hub')})
                bridge = QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE,
                    reconciliation=synthetic_workflow_reconciliation(workflow))
                task = reviewed_task(repo)
                job = queue.submit(actor='codex', request_id=str(uuid4()), action_key=ACTION,
                    environment='staging', target=WorkTarget(task['id'],
                        manifest.candidate.source_commit, manifest.candidate.image_config_id))
                outcome = await bridge.run_once()
                if fault == 'none':
                    assert outcome['state'] == 'succeeded'
                    assert len(transport.posts) == len(set(transport.posts)) == 2
                    assert transport.ack_status == [200, 200]
                    assert transport.acks == transport.posts
                    assert backend.events.count('create') == backend.events.count('start') == 1
                    assert backend.events.count('probe') == 1
                    assert repo.list_runs()[0]['status'] == 'succeeded'
                    assert adapter.ledger.inspect_single_pilot() is None
                    return
                assert outcome['state'] == 'held'
                assert repo.list_runs()[0]['events'][-1]['reason'] == 'candidate_outcome_uncertain'
                assert transport.lost and len(transport.posts) == 1
                assert backend.events.count('create') == backend.events.count('start') == 1
                first = transport.posts[0]
                effects_before = [event for event in backend.events if event in {'create', 'start', 'stop', 'probe'}]
                state_calls_before = list(state.calls)
                next_task = reviewed_task(repo, key='second-task')
                next_job = queue.submit(actor='codex', request_id=str(uuid4()), action_key=ACTION,
                    environment='staging', target=WorkTarget(next_task['id'],
                        manifest.candidate.source_commit, manifest.candidate.image_config_id))
                def reserved_job():
                    with repo._connection() as db:
                        return db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?',
                                          (MACHINE,)).fetchone()['job_id']
                assert reserved_job() == job['id']
                assert await bridge.run_once() is None
                assert queue.get_job(next_job['id'])['state'] == 'queued'
                assert len(transport.posts) == 1
                assert [event for event in backend.events if event in {'create', 'start', 'stop', 'probe'}] == effects_before
                assert state.calls == state_calls_before
                recovered = await bridge.recover(job['id'])
                # Recovery may inspect and acknowledge the original command,
                # but cannot dispatch the previously unissued smoke phase.
                assert recovered['state'] == 'held'
                assert transport.posts.count(first) == 1
                assert transport.acks.count(first) == (1 if fault == 'command-response' else 2)
                assert transport.ack_status == ([200, 200] if fault == 'ack-response' else [200])
                assert len(transport.posts) == 1
                assert backend.events.count('create') == backend.events.count('start') == 1
                assert backend.events.count('probe') == 0
                assert [event for event in backend.events if event in {'create', 'start', 'stop', 'probe'}] == effects_before
                assert state.calls == state_calls_before
                assert repo.list_runs()[0]['status'] == 'running'
                assert adapter.ledger.inspect_single_pilot() is None
                assert reserved_job() == job['id']
                assert await bridge.run_once() is None
                assert queue.get_job(next_job['id'])['state'] == 'queued'
                assert len(transport.posts) == 1
                assert [event for event in backend.events if event in {'create', 'start', 'stop', 'probe'}] == effects_before
                assert state.calls == state_calls_before
    finally:
        adapter.close()
