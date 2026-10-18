"""Linux ownership guards for dispatcher mutations, not crash adoption tests."""
import sys
from types import SimpleNamespace

import pytest

from engineering_hub.container_pilot import ContainerPilotWorkflow, ContainerPilotTarget
from engineering_hub.dispatcher import StagingDispatcher
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubRepository, HubError
from tests.test_hub_container_pilot import FakeClient, PRINCIPAL, pilot_manifest, reviewed_task

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux pilot owner capability')


@pytest.fixture
def enrolled(tmp_path):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    path = directory / 'hub.sqlite'
    with PilotOwner(path, allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(path, execution_principal=PRINCIPAL)
        yield repo, owner


def test_legacy_dispatcher_cannot_initialize_enrolled_ledger(enrolled):
    repo, _ = enrolled
    with pytest.raises(HubError, match='pilot_ownership_required'):
        StagingDispatcher(repo, SimpleNamespace(repo=repo))
    with repo._connection() as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='hub_dispatch_cursor'").fetchone() is None


@pytest.mark.asyncio
async def test_closed_owner_does_not_advance_dispatch_cursor(enrolled):
    repo, owner = enrolled
    client = FakeClient()
    workflow = ContainerPilotWorkflow(repo, client, [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=owner)
    dispatcher = StagingDispatcher(repo, workflow)
    reviewed_task(repo)
    before = dispatcher.cursor
    owner.close()
    with pytest.raises(HubError):
        await dispatcher.drain_batch()
    assert dispatcher.cursor == before
    assert dispatcher.receipts() == []
    assert client.submissions == []


@pytest.mark.asyncio
async def test_boolean_quiescence_cannot_adopt_enrolled_dispatcher(enrolled):
    repo, owner = enrolled
    workflow = ContainerPilotWorkflow(repo, FakeClient(), [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=owner)
    dispatcher = StagingDispatcher(repo, workflow)
    with pytest.raises(HubError, match='dispatch_owner_recovery_unavailable'):
        await dispatcher.reconcile(1, expected_owner='not-authority', confirmed_quiescent=True)
    assert dispatcher.receipts() == []


@pytest.mark.asyncio
async def test_live_owner_dispatches_exact_reviewed_pilot(enrolled):
    repo, owner = enrolled
    client = FakeClient()
    workflow = ContainerPilotWorkflow(repo, client, [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=owner)
    dispatcher = StagingDispatcher(repo, workflow)
    reviewed_task(repo)
    result = await dispatcher.drain_batch()
    assert result['dispatched'] == 1
    assert [command.action for command in client.submissions] == ['deploy_container', 'smoke_container']
    assert dispatcher.receipts()[0]['state'] == 'succeeded'
