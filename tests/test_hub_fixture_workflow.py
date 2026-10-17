from dataclasses import replace
from types import SimpleNamespace
import asyncio

import pytest

from engineering_hub.fixture_workflow import FixtureTarget, FixtureWorkflow
from engineering_hub.executor_client import CommandRequest
from engineering_hub.repository import HubError, HubRepository


COMMIT = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
ROLLBACK_COMMIT = 'c' * 40
ROLLBACK_DIGEST = 'sha256:' + 'd' * 64
PRINCIPAL = 'executor:fixture'


class FakeClient:
    def __init__(self, states=None):
        self.states = states or {}
        self.submissions = []
        self.inspections = []

    async def submit_command(self, command):
        assert isinstance(command, CommandRequest)
        self.submissions.append(command)
        state = self.states.get(command.action, 'succeeded')
        if isinstance(state, Exception):
            raise state
        return SimpleNamespace(state=state)

    async def get_command(self, command):
        self.inspections.append(command)
        return SimpleNamespace(state='succeeded')


@pytest.fixture
def repo(tmp_path):
    return HubRepository(tmp_path / 'hub.sqlite3', execution_principal=PRINCIPAL)


@pytest.fixture
def target():
    return FixtureTarget('engineering-hub:staging', DIGEST, COMMIT, ROLLBACK_DIGEST, ROLLBACK_COMMIT)


def ready_run(repo, key='one'):
    task = repo.create_task('human:1234', 'engineering-hub', 'Fixture pilot', key)
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], COMMIT)
    task = repo.review_task('claude', task['id'], task['revision'], COMMIT, 'approve')
    return repo.create_run('codex', task['id'], task['revision'], DIGEST, 'engineering-hub:staging')


@pytest.mark.asyncio
async def test_full_success_and_repeat_advance_no_extra_submit(repo, target):
    run = ready_run(repo)
    client = FakeClient()
    workflow = FixtureWorkflow(repo, client, [target])
    result = await workflow.advance(run['id'])
    assert result['status'] == 'succeeded'
    assert [command.action for command in client.submissions] == ['deploy_fixture', 'smoke_fixture']
    assert all(command.artifact_digest == DIGEST and command.candidate_commit == COMMIT for command in client.submissions)
    assert all(command.fencing_generation == run['fencing_generation'] for command in client.submissions)
    assert await workflow.advance(run['id']) == result
    assert len(client.submissions) == 2


@pytest.mark.asyncio
async def test_smoke_failure_rolls_back_exact_manifest_and_counts_failure(repo, target):
    run = ready_run(repo)
    client = FakeClient({'smoke_fixture': 'failed'})
    result = await FixtureWorkflow(repo, client, [target]).advance(run['id'])
    assert result['status'] == 'rolled_back'
    assert [command.action for command in client.submissions] == ['deploy_fixture', 'smoke_fixture', 'rollback_fixture']
    rollback = client.submissions[-1]
    assert rollback.artifact_digest == ROLLBACK_DIGEST and rollback.candidate_commit == ROLLBACK_COMMIT
    assert repo.get_task(run['task_id'])['repair_count'] == 1


@pytest.mark.asyncio
async def test_unknown_timeout_holds_and_reopen_never_reposts(repo, target):
    run = ready_run(repo)
    client = FakeClient({'deploy_fixture': TimeoutError('sensitive exception')})
    result = await FixtureWorkflow(repo, client, [target]).advance(run['id'])
    assert result['status'] == 'held'
    assert len(client.submissions) == 1
    reopened = HubRepository(repo.path, execution_principal=PRINCIPAL)
    next_client = FakeClient()
    workflow = FixtureWorkflow(reopened, next_client, [target])
    assert (await workflow.advance(run['id']))['status'] == 'held'
    assert next_client.submissions == []
    inspected = await workflow.inspect(run['id'], 'deploy_fixture')
    assert inspected.state == 'succeeded'
    assert len(next_client.inspections) == 1
    assert next_client.inspections[0].command_id == client.submissions[0].command_id
    assert repo.get_run(run['id'])['status'] == 'held'


@pytest.mark.asyncio
async def test_manifest_candidate_mismatch_denies_before_io(repo, target):
    run = ready_run(repo)
    client = FakeClient()
    for changed in [replace(target, candidate_commit='e' * 40), replace(target, artifact_digest='sha256:' + 'e' * 64),
                    replace(target, policy_revision=2)]:
        with pytest.raises(HubError, match='fixture_manifest_mismatch'):
            await FixtureWorkflow(repo, client, [changed]).advance(run['id'])
    assert client.submissions == []
    assert repo.get_run(run['id'])['status'] == 'pending'


@pytest.mark.asyncio
async def test_manifest_rollback_change_rejected_after_restart(repo, target):
    run = ready_run(repo)
    await FixtureWorkflow(repo, FakeClient({'deploy_fixture': TimeoutError()}), [target]).advance(run['id'])
    reopened = HubRepository(repo.path, execution_principal=PRINCIPAL)
    client = FakeClient()
    changed = replace(target, rollback_digest='sha256:' + 'e' * 64, rollback_commit='e' * 40)
    with pytest.raises(HubError, match='fixture_manifest_changed'):
        await FixtureWorkflow(reopened, client, [changed]).advance(run['id'])
    assert client.submissions == []


@pytest.mark.asyncio
async def test_crash_after_command_reservation_reopens_inspect_only(repo, target):
    class SimulatedCrash(BaseException):
        pass
    class CrashClient(FakeClient):
        async def submit_command(self, command):
            self.submissions.append(command)
            raise SimulatedCrash()
    run = ready_run(repo)
    crashing = CrashClient()
    with pytest.raises(SimulatedCrash):
        await FixtureWorkflow(repo, crashing, [target]).advance(run['id'])
    reopened = HubRepository(repo.path, execution_principal=PRINCIPAL)
    client = FakeClient()
    result = await FixtureWorkflow(reopened, client, [target]).advance(run['id'])
    assert result['status'] == 'held'
    assert client.submissions == []
    assert len(crashing.submissions) == 1


@pytest.mark.asyncio
async def test_persisted_crash_lock_requires_reconciliation_but_inspection_allowed(repo, target):
    run = ready_run(repo)
    workflow = FixtureWorkflow(repo, FakeClient({'deploy_fixture': TimeoutError()}), [target])
    await workflow.advance(run['id'])
    # Model the durable lock left by process termination: no Python finally ran.
    with repo._transaction() as db:
        db.execute('INSERT INTO hub_fixture_advances VALUES(?,?)', (run['id'], 'terminated-controller'))
    reopened = HubRepository(repo.path, execution_principal=PRINCIPAL)
    client = FakeClient()
    recovered = FixtureWorkflow(reopened, client, [target])
    with pytest.raises(HubError, match='advance_requires_reconciliation'):
        await recovered.advance(run['id'])
    assert client.submissions == []
    assert (await recovered.inspect(run['id'], 'deploy_fixture')).state == 'succeeded'
    assert len(client.inspections) == 1
    with repo._connection() as db:
        assert db.execute('SELECT owner FROM hub_fixture_advances WHERE run_id=?', (run['id'],)).fetchone()['owner'] == 'terminated-controller'


@pytest.mark.asyncio
async def test_concurrent_advance_rejected_without_duplicate_effects(repo, target):
    entered, release = asyncio.Event(), asyncio.Event()
    class BlockingClient(FakeClient):
        async def submit_command(self, command):
            self.submissions.append(command)
            if command.action == 'deploy_fixture':
                entered.set()
                await release.wait()
            return SimpleNamespace(state='succeeded')
    run = ready_run(repo)
    client = BlockingClient()
    workflow = FixtureWorkflow(repo, client, [target])
    first = asyncio.create_task(workflow.advance(run['id']))
    await entered.wait()
    try:
        with pytest.raises(HubError, match='advance_requires_reconciliation'):
            await workflow.advance(run['id'])
    finally:
        release.set()
        await first
    assert [command.action for command in client.submissions] == ['deploy_fixture', 'smoke_fixture']
    assert repo.get_run(run['id'])['status'] == 'succeeded'


@pytest.mark.asyncio
async def test_pause_during_deploy_prevents_smoke_and_holds(repo, target):
    class PausingClient(FakeClient):
        async def submit_command(self, command):
            self.submissions.append(command)
            repo.set_pause('human:1234', True)
            return SimpleNamespace(state='succeeded')
    run = ready_run(repo)
    client = PausingClient()
    result = await FixtureWorkflow(repo, client, [target]).advance(run['id'])
    assert result['status'] == 'held'
    assert [command.action for command in client.submissions] == ['deploy_fixture']


@pytest.mark.asyncio
async def test_pause_before_work_keeps_pending_and_no_submit(repo, target):
    run = ready_run(repo)
    repo.set_pause('human:1234', True)
    client = FakeClient()
    with pytest.raises(HubError, match='paused'):
        await FixtureWorkflow(repo, client, [target]).advance(run['id'])
    assert client.submissions == []
    assert repo.get_run(run['id'])['status'] == 'pending'


@pytest.mark.asyncio
async def test_third_failure_prevents_fourth_attempt(repo, target):
    run = ready_run(repo)
    client = FakeClient({'smoke_fixture': 'failed'})
    workflow = FixtureWorkflow(repo, client, [target])
    for index in range(3):
        assert (await workflow.advance(run['id']))['status'] == 'rolled_back'
        task = repo.get_task(run['task_id'])
        assert task['repair_count'] == index + 1
        if index < 2:
            run = repo.create_run('codex', task['id'], task['revision'], DIGEST, target.target_id)
    with pytest.raises(HubError, match='repair_limit'):
        repo.create_run('codex', task['id'], task['revision'], DIGEST, target.target_id)
    assert len(client.submissions) == 9


@pytest.mark.asyncio
async def test_unknown_rollback_preserves_reservation_and_no_retry(repo, target):
    run = ready_run(repo)
    client = FakeClient({'smoke_fixture': 'failed', 'rollback_fixture': TimeoutError()})
    workflow = FixtureWorkflow(repo, client, [target])
    result = await workflow.advance(run['id'])
    assert result['status'] == 'failed'
    assert len(client.submissions) == 3
    task = repo.get_task(run['task_id'])
    with pytest.raises(HubError, match='active_run'):
        repo.create_run('codex', task['id'], task['revision'], DIGEST, target.target_id)
    assert (await workflow.advance(run['id']))['status'] == 'failed'
    assert len(client.submissions) == 3


@pytest.mark.parametrize('changes', [dict(driver='shell'), dict(target_id='engineering-hub:production'),
    dict(target_id='../bad:staging'), dict(artifact_digest='latest'), dict(candidate_commit='main'),
    dict(rollback_digest='latest'), dict(rollback_commit='main'), dict(policy_revision=0)])
def test_strict_fixture_target(target, changes):
    with pytest.raises(ValueError):
        replace(target, **changes)


def test_configured_target_duplicate_and_missing_identity(repo, target):
    with pytest.raises(ValueError, match='duplicate_target'):
        FixtureWorkflow(repo, FakeClient(), [target, target])
    disabled = HubRepository(repo.path)
    with pytest.raises(ValueError, match='execution_identity_required'):
        FixtureWorkflow(disabled, FakeClient(), [target])


def test_run_requires_independent_review(repo):
    item = repo.create_task('human:1234', 'engineering-hub', 'Fixture', 'review')
    item = repo.claim_task('codex', item['id'], item['revision'])
    item = repo.set_candidate('codex', item['id'], item['revision'], COMMIT)
    with pytest.raises(HubError, match='independent_review_required'):
        repo.review_task('codex', item['id'], item['revision'], COMMIT, 'approve')
    with pytest.raises(HubError, match='review_required'):
        repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:staging')


def test_viewer_target_is_valid():
    configured = FixtureTarget('3d-viewer:staging', DIGEST, COMMIT, ROLLBACK_DIGEST, ROLLBACK_COMMIT)
    assert configured.target_id == '3d-viewer:staging'
