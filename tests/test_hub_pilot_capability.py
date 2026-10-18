"""Live Linux ownership guards; no Docker or production activation."""
import sys

import pytest

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubError, HubRepository
from tests.test_hub_container_pilot import (
    ACTIONS, IMAGE, PRINCIPAL, FakeClient, pilot_manifest, reviewed_task,
)

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux kernel ownership capability')


@pytest.fixture
def enrolled(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    with PilotOwner(private / 'hub.sqlite', allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(private / 'hub.sqlite', execution_principal=PRINCIPAL)
        yield repo, owner, ContainerPilotTarget.from_manifest(pilot_manifest())


def snapshot(repo):
    with repo._connection() as db:
        return tuple(db.iterdump())


def create_workflow_run(repo, owner, target, client=None):
    workflow = ContainerPilotWorkflow(repo, client or FakeClient(), [target], pilot_owner=owner)
    task = reviewed_task(repo)
    run = workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
    return workflow, run


@pytest.mark.parametrize('supplied', [None, True, 'saved-epoch', object()])
def test_enrolled_constructor_requires_exact_live_capability_before_writes(enrolled, supplied):
    repo, owner, target = enrolled
    before = snapshot(repo)
    with pytest.raises(HubError, match='container_pilot_ownership_unavailable'):
        ContainerPilotWorkflow(repo, FakeClient(), [target], pilot_owner=supplied)
    assert snapshot(repo) == before
    owner.assert_active(repo.path)


def test_different_database_owner_is_not_authority(enrolled, tmp_path):
    repo, _, target = enrolled
    other = tmp_path / 'other'
    other.mkdir(mode=0o700)
    before = snapshot(repo)
    with PilotOwner(other / 'hub.sqlite', allow_ephemeral_test_storage=True) as wrong:
        with pytest.raises(HubError, match='container_pilot_ownership_unavailable'):
            ContainerPilotWorkflow(repo, FakeClient(), [target], pilot_owner=wrong)
    assert snapshot(repo) == before


@pytest.mark.asyncio
async def test_owned_workflow_succeeds_without_changing_effect_ids(enrolled):
    repo, owner, target = enrolled
    client = FakeClient()
    workflow, run = create_workflow_run(repo, owner, target, client)
    result = await workflow.advance(run['id'])
    assert result['status'] == 'succeeded'
    assert [c.action for c in client.submissions] == ACTIONS[:2]
    owner.assert_active(repo.path)


@pytest.mark.asyncio
async def test_closed_capability_denies_all_entrypoints_without_writes_or_io(enrolled):
    repo, owner, target = enrolled
    client = FakeClient()
    workflow, run = create_workflow_run(repo, owner, target, client)
    task = repo.get_task(run['task_id'])
    owner.close()
    before = snapshot(repo)
    sync_calls = [
        lambda: workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id),
        lambda: workflow._target(run),
        lambda: workflow._event(run, 'running', phase='deploy_container'),
    ]
    for call in sync_calls:
        with pytest.raises(HubError, match='container_pilot_ownership_unavailable'):
            call()
    for awaitable in (workflow.advance(run['id']), workflow.inspect(run['id'], ACTIONS[0]),
                      workflow._command(run, target, ACTIONS[0])):
        with pytest.raises(HubError, match='container_pilot_ownership_unavailable'):
            await awaitable
    assert snapshot(repo) == before
    assert not client.submissions and not client.inspections


@pytest.mark.asyncio
@pytest.mark.parametrize('raises', [False, True])
async def test_owner_loss_during_submit_preserves_unknown_receipt_and_advance(enrolled, raises):
    repo, owner, target = enrolled

    class ClosingClient(FakeClient):
        async def submit_command(self, command):
            self.submissions.append(command)
            owner.close()
            if raises:
                raise RuntimeError('sensitive adapter detail')
            return self.response(command)

    client = ClosingClient()
    workflow, run = create_workflow_run(repo, owner, target, client)
    with pytest.raises(HubError, match='container_pilot_ownership_unavailable'):
        await workflow.advance(run['id'])
    assert len(client.submissions) == 1
    assert repo.get_run(run['id'])['status'] == 'running'
    with repo._connection() as db:
        receipt = db.execute('SELECT state,response_json FROM hub_container_pilot_commands').fetchone()
        assert tuple(receipt) == ('unknown', None)
        assert db.execute('SELECT count(*) FROM hub_container_pilot_advances').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_owner_loss_during_inspect_refuses_response(enrolled):
    repo, owner, target = enrolled

    class ClosingClient(FakeClient):
        async def get_command(self, command):
            self.inspections.append(command)
            owner.close()
            return self.response(command)

    client = ClosingClient()
    workflow, run = create_workflow_run(repo, owner, target, client)
    await workflow._command(run, target, ACTIONS[0])
    with pytest.raises(HubError, match='container_pilot_ownership_unavailable'):
        await workflow.inspect(run['id'], ACTIONS[0])
    assert len(client.inspections) == 1
    # inspect's existing binding pin is allowed before loss; no receipt update.
    with repo._connection() as db:
        assert db.execute('SELECT state FROM hub_container_pilot_commands').fetchone()[0] == 'succeeded'


@pytest.mark.asyncio
async def test_legacy_instance_rechecks_later_enrollment_before_mutation(tmp_path):
    repo = HubRepository(tmp_path / 'legacy.sqlite', execution_principal=PRINCIPAL)
    target = ContainerPilotTarget.from_manifest(pilot_manifest())
    client = FakeClient()
    workflow = ContainerPilotWorkflow(repo, client, [target])
    task = reviewed_task(repo)
    run = workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
    # Even malformed/partial enrollment must fail closed, not bypass the guard.
    with repo._connection() as db:
        db.execute('CREATE TABLE hub_pilot_owner (epoch TEXT)')
    before = snapshot(repo)
    with pytest.raises(HubError, match='container_pilot_ownership_unavailable'):
        await workflow.advance(run['id'])
    with pytest.raises(HubError, match='container_pilot_ownership_unavailable'):
        await workflow._command(run, target, ACTIONS[0])
    assert snapshot(repo) == before
    assert not client.submissions
