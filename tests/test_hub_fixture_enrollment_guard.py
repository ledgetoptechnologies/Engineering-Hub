"""Fixture/API bypass regression: enrolled ledgers never permit fixture effects."""
from types import SimpleNamespace

import pytest

from engineering_hub.fixture_workflow import FixtureTarget, FixtureWorkflow
from engineering_hub.repository import HubError, HubRepository
from tests.test_hub_fixture_workflow import (
    COMMIT, DIGEST, PRINCIPAL, ROLLBACK_COMMIT, ROLLBACK_DIGEST, FakeClient, ready_run,
)


def enroll_marker(repo):
    # Even corrupt or partial enrollment blocks legacy execution, regardless
    # of platform. No epoch string is treated as authority by this guard.
    with repo._connection() as db:
        db.execute('CREATE TABLE hub_pilot_owner (epoch TEXT)')


def snapshot(repo):
    with repo._connection() as db:
        return tuple(db.iterdump())


@pytest.fixture
def setup(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite', execution_principal=PRINCIPAL)
    target = FixtureTarget('engineering-hub:staging', DIGEST, COMMIT, ROLLBACK_DIGEST, ROLLBACK_COMMIT)
    return repo, target


def test_constructor_refuses_enrollment_without_creating_fixture_tables(setup):
    repo, target = setup
    enroll_marker(repo)
    before = snapshot(repo)
    with pytest.raises(HubError, match='fixture_ledger_unavailable'):
        FixtureWorkflow(repo, FakeClient(), [target])
    assert snapshot(repo) == before


@pytest.mark.asyncio
async def test_existing_fixture_instance_cannot_bypass_later_enrollment(setup):
    repo, target = setup
    client = FakeClient()
    workflow = FixtureWorkflow(repo, client, [target])
    run = ready_run(repo)
    enroll_marker(repo)
    before = snapshot(repo)
    for call in (
        lambda: workflow.create_run('codex', run['task_id'], 1, DIGEST, target.target_id),
        lambda: workflow._event(run, 'running'),
        lambda: workflow._pin_manifest(run, target),
        lambda: workflow._target(run),
        lambda: workflow._hold(run),
    ):
        with pytest.raises(HubError, match='fixture_ledger_unavailable'):
            call()
    for call in (
        lambda: workflow.advance(run['id']),
        lambda: workflow._advance_owned(run['id']),
        lambda: workflow._command(run, target, 'deploy_fixture'),
        lambda: workflow.inspect(run['id'], 'deploy_fixture'),
    ):
        with pytest.raises(HubError, match='fixture_ledger_unavailable'):
            await call()
    assert snapshot(repo) == before
    assert not client.submissions and not client.inspections


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter_raises', [True, False])
async def test_enrollment_during_submit_preserves_unknown_command_and_reservation(setup, adapter_raises):
    repo, target = setup

    class EnrollingClient(FakeClient):
        async def submit_command(self, command):
            self.submissions.append(command)
            enroll_marker(repo)
            if adapter_raises:
                raise RuntimeError('sensitive adapter details')
            return SimpleNamespace(state='succeeded')

    client = EnrollingClient()
    workflow = FixtureWorkflow(repo, client, [target])
    run = ready_run(repo)
    with pytest.raises(HubError, match='fixture_ledger_unavailable'):
        await workflow.advance(run['id'])
    assert len(client.submissions) == 1
    assert repo.get_run(run['id'])['status'] == 'running'
    with repo._connection() as db:
        assert db.execute('SELECT state FROM hub_fixture_commands').fetchone()[0] == 'unknown'
        assert db.execute('SELECT count(*) FROM hub_fixture_advances').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_enrollment_during_get_refuses_inspection_result_without_other_writes(setup):
    repo, target = setup

    class EnrollingClient(FakeClient):
        async def get_command(self, command):
            self.inspections.append(command)
            enroll_marker(repo)
            self.after_enrollment = snapshot(repo)
            return SimpleNamespace(state='succeeded')

    client = EnrollingClient()
    workflow = FixtureWorkflow(repo, client, [target])
    run = ready_run(repo)
    await workflow._command(run, target, 'deploy_fixture')
    with pytest.raises(HubError, match='fixture_ledger_unavailable'):
        await workflow.inspect(run['id'], 'deploy_fixture')
    assert len(client.inspections) == 1
    assert snapshot(repo) == client.after_enrollment
