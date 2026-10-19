"""Actual client/ASGI wire with synthetic host assertions, never Docker proof."""
import json
import uuid

import httpx
import pytest

pytest.importorskip('fcntl', reason='Linux private PilotOwner required')

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.container_pilot_client import ContainerPilotClient, ContainerPilotResponse
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.queued_pilot import QueuedContainerPilot
from engineering_hub.repository import HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget
from hub_container_executor.asgi import PilotASGI
from tests.test_hub_container_pilot import COMMIT, IMAGE, PRINCIPAL, pilot_manifest, reviewed_task
from tests.test_hub_queued_pilot import rig, submit


class SyntheticHost:
    """Deliberate fake; typed health assertions are not observed Engine evidence."""
    def __init__(self):
        self.commands, self.inspected = {}, []

    def submit(self, command):
        assert command.command_id not in self.commands, 'effect POST replayed'
        response = ContainerPilotResponse(**command.model_dump(), state='succeeded',
            reason=None, replayed=False, evidence=dict(container_id='e' * 64,
                image_config_id=IMAGE, health='healthy', evidence_verified=True))
        self.commands[command.command_id] = response
        return response

    def get(self, command_id):
        self.inspected.append(command_id)
        return self.commands.get(command_id)

    def reconcile(self, command_id):
        pytest.fail('control recovery must only GET original commands')


class LoseSmokeResponse(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.transport = httpx.ASGITransport(app=app)
        self.lost = False

    async def handle_async_request(self, request):
        response = await self.transport.handle_async_request(request)
        if request.method == 'POST':
            payload = json.loads(await request.aread())
            if payload['action'] == 'smoke_container' and not self.lost:
                self.lost = True
                await response.aclose()
                raise httpx.ReadError('synthetic response lost after acceptance', request=request)
        return response

    async def aclose(self):
        await self.transport.aclose()


@pytest.mark.asyncio
async def test_lost_terminal_response_recovers_original_wire_commands_without_resend(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    host = SyntheticHost()
    transport = LoseSmokeResponse(PilotASGI(host, 'synthetic-private-host-token'))
    async with ContainerPilotClient('http://127.0.0.1', 'synthetic-private-host-token',
                                    transport=transport) as client:
        with PilotOwner(private / 'hub.sqlite3', allow_ephemeral_test_storage=True) as owner:
            repo = HubRepository(private / 'hub.sqlite3',
                                 execution_principal=PRINCIPAL, pilot_owner=owner)
            workflow = ContainerPilotWorkflow(repo, client,
                [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=owner)
            queue = WorkQueue(repo, {'engineering-hub.pilot': WorkAction('staging-pc',
                project='engineering-hub')})
            bridge = QueuedContainerPilot(queue, workflow,
                action_key='engineering-hub.pilot', machine_id='staging-pc')
            task = reviewed_task(repo)
            job = queue.submit(actor='codex', request_id=str(uuid.uuid4()),
                action_key='engineering-hub.pilot', environment='staging',
                target=WorkTarget(task['id'], COMMIT, IMAGE))
            assert (await bridge.run_once())['state'] == 'held'
            assert transport.lost and len(host.commands) == 2
            original_ids = set(host.commands)
            assert (await bridge.recover(job['id']))['state'] == 'succeeded'
            assert set(host.commands) == original_ids
            assert set(host.inspected) == original_ids and len(host.inspected) == 2
            assert len(repo.list_runs()) == 1
            assert await bridge.run_once() is None
            assert repo.get_task(task['id'])['status'] == 'verified'


@pytest.mark.asyncio
async def test_public_routes_cannot_bypass_enrolled_queue_or_select_private_worker(rig):
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    task, job = submit(rig)
    app = create_app(Settings(rig.repo.path,
        AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude'),
        AuthConfig('https://hub.test', beau_github_id=42)),
        repository=rig.repo, work_queue=rig.queue)
    paths = ['/api/hub/v1/container-pilot/runs',
        '/api/hub/v1/container-pilot/runs/' + str(uuid.uuid4()) + '/dispatch',
        '/api/hub/v1/container-pilot/runs/' + str(uuid.uuid4()) + '/recover']
    payload = dict(task_id=task['id'], expected_revision=task['revision'],
                   artifact_digest=IMAGE, target='engineering-hub:staging')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url='https://hub.test') as http:
        for path in paths:
            body = payload if path == paths[0] else {}
            assert (await http.post(path, json=body)).status_code == 401
            response = await http.post(path, json=body,
                headers={'Authorization': 'Bearer synthetic-codex'})
            assert response.status_code == 423
            assert response.json()['detail']['code'] == 'queued_pilot_direct_route_disabled'
        for agent in ('hermes', 'codex', 'claude'):
            response = await http.post('/api/hub/v1/work/claim',
                json={'instance_id': str(uuid.uuid4())},
                headers={'Authorization': 'Bearer synthetic-' + agent})
            assert response.status_code == 200 and response.json() == {'job': None}
        response = await http.post('/api/hub/v1/work/claim',
            json={'instance_id': str(uuid.uuid4()), 'agent': 'executor:container-pilot'},
            headers={'Authorization': 'Bearer synthetic-codex'})
        assert response.status_code == 422
    assert rig.queue.get_job(job['id'])['state'] == 'queued'
    assert rig.repo.list_runs() == [] and rig.client.submissions == []
