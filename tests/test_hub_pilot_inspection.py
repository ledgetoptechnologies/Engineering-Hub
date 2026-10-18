"""Inspection uses exact original identities and never repairs ledger state."""
import json
import uuid

import pytest

from engineering_hub.repository import HubError, HubRepository
from engineering_hub.container_pilot import ContainerPilotTarget
from tests.test_hub_container_pilot import FakeClient, PRINCIPAL, pilot_manifest, ready_run


@pytest.fixture
def setup(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite', execution_principal=PRINCIPAL)
    client = FakeClient()
    workflow, run = ready_run(repo, ContainerPilotTarget.from_manifest(pilot_manifest()), client)
    return repo, client, workflow, run


def snapshot(repo):
    with repo._connection() as db:
        return tuple(db.iterdump())


@pytest.mark.asyncio
async def test_inspection_without_binding_does_not_pin_or_change_state(setup):
    repo, client, workflow, run = setup
    await workflow._command(run, workflow.targets[run['target']], 'deploy_container')
    before = snapshot(repo)
    result = await workflow.inspect(run['id'], 'deploy_container')
    assert result.state == 'succeeded'
    assert snapshot(repo) == before
    assert client.inspections == client.submissions


@pytest.mark.asyncio
async def test_changed_saved_command_is_not_used_for_foreign_get(setup):
    repo, client, workflow, run = setup
    await workflow._command(run, workflow.targets[run['target']], 'deploy_container')
    with repo._transaction() as db:
        payload = json.loads(db.execute('SELECT payload FROM hub_container_pilot_commands').fetchone()[0])
        payload['command_id'] = str(uuid.uuid4())
        db.execute('UPDATE hub_container_pilot_commands SET payload=?', (json.dumps(payload, sort_keys=True, separators=(',', ':')),))
    before = snapshot(repo)
    with pytest.raises(HubError, match='container_pilot_manifest_changed'):
        await workflow.inspect(run['id'], 'deploy_container')
    assert not client.inspections
    assert snapshot(repo) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['echo', 'boolean_revision', 'untyped'])
async def test_injected_inspection_response_revalidated_without_state_change(setup, bad):
    repo, client, workflow, run = setup
    await workflow._command(run, workflow.targets[run['target']], 'deploy_container')
    async def corrupt(command):
        response = client.response(command)
        if bad == 'echo':
            return response.model_copy(update={'run_id': str(uuid.uuid4())})
        if bad == 'boolean_revision':
            return response.model_copy(update={'policy_revision': True})
        return response.model_dump()
    client.get_command = corrupt
    before = snapshot(repo)
    with pytest.raises(HubError, match='container_pilot_inspection_unverified'):
        await workflow.inspect(run['id'], 'deploy_container')
    assert snapshot(repo) == before
