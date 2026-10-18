import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from engineering_hub.dispatcher import StagingDispatcher
from engineering_hub.fixture_workflow import FixtureTarget, FixtureWorkflow
from engineering_hub.repository import HubError, HubRepository


COMMIT = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


class Client:
    def __init__(self, states=None):
        self.commands = []
        self.states = states or {}

    async def submit_command(self, command):
        self.commands.append(command)
        state = self.states.get(command.action, 'succeeded')
        if isinstance(state, BaseException):
            raise state
        return SimpleNamespace(state=state)


@pytest.fixture
def setup(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3', execution_principal='executor:fixture')
    target = FixtureTarget('engineering-hub:staging', DIGEST, COMMIT, 'sha256:' + 'd' * 64, 'c' * 40)
    client = Client()
    workflow = FixtureWorkflow(repo, client, [target])
    return repo, target, client, workflow, StagingDispatcher(repo, workflow)


def ready(repo, key='one', commit=COMMIT, verdict='approve'):
    task = repo.create_task('human:123', 'engineering-hub', 'Fixture', key)
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], commit)
    return repo.review_task('claude', task['id'], task['revision'], commit, verdict)


@pytest.mark.asyncio
async def test_success_durable_receipt_no_replay(setup):
    repo, target, client, workflow, dispatcher = setup
    task = ready(repo)
    result = await dispatcher.drain_batch()
    assert result['dispatched'] == 1
    assert [command.action for command in client.commands] == ['deploy_fixture', 'smoke_fixture']
    receipt = dispatcher.receipts()[0]
    assert receipt['state'] == 'succeeded' and receipt['needs_reconciliation'] is False
    assert receipt['entity_id'] == task['id']
    assert repo.get_run_request('codex', 'hub-dispatch:' + str(receipt['sequence']))['id'] == receipt['run_id']
    reopened = StagingDispatcher(repo, workflow)
    await reopened.drain_batch()
    assert len(client.commands) == 2 and len(repo.list_runs()) == 1


@pytest.mark.asyncio
async def test_bounded_history_catchup_not_only_latest_event(setup):
    repo, target, client, workflow, dispatcher = setup
    ready(repo)
    for index in range(12):
        repo.create_decision('hermes', 'engineering-hub', 'Unrelated', ['a', 'b'])
    first = await dispatcher.drain_batch(limit=2)
    assert first['examined'] == 2 and first['more'] is True and not client.commands
    for _ in range(20):
        if not (await dispatcher.drain_batch(limit=2))['more']:
            break
    assert len(repo.list_runs()) == 1 and len(client.commands) == 2


@pytest.mark.asyncio
async def test_stale_candidate_and_rejected_review_skipped(setup):
    repo, target, client, workflow, dispatcher = setup
    task = ready(repo)
    task = repo.set_candidate('codex', task['id'], task['revision'], 'e' * 40)
    repo.review_task('claude', task['id'], task['revision'], 'e' * 40, 'reject')
    await dispatcher.drain_batch()
    assert client.commands == [] and dispatcher.receipts() == []


@pytest.mark.asyncio
async def test_only_current_exact_manifest_review_can_dispatch(setup):
    repo, target, client, workflow, dispatcher = setup
    task = ready(repo, commit='e' * 40)
    await dispatcher.drain_batch()
    assert not repo.list_runs()
    task = repo.set_candidate('codex', task['id'], task['revision'], COMMIT)
    repo.review_task('hermes', task['id'], task['revision'], COMMIT, 'approve')
    await dispatcher.drain_batch()
    assert len(repo.list_runs()) == 1


@pytest.mark.asyncio
async def test_pause_does_not_consume_ready_review(setup):
    repo, target, client, workflow, dispatcher = setup
    ready(repo)
    repo.set_pause('human:123', True)
    result = await dispatcher.drain_batch()
    assert result['paused'] is True and result['cursor'] == 3
    assert not client.commands and not dispatcher.receipts()
    repo.set_pause('human:123', False)
    assert (await dispatcher.drain_batch())['dispatched'] == 1


@pytest.mark.asyncio
async def test_two_instances_do_not_duplicate_same_review(setup):
    repo, target, client, workflow, dispatcher = setup
    ready(repo)
    entered, release = asyncio.Event(), asyncio.Event()
    original = client.submit_command
    async def blocking(command):
        if command.action == 'deploy_fixture':
            entered.set()
            await release.wait()
        return await original(command)
    client.submit_command = blocking
    first = asyncio.create_task(dispatcher.drain_batch())
    await entered.wait()
    other_repo = HubRepository(repo.path, execution_principal='executor:fixture')
    other = StagingDispatcher(other_repo, FixtureWorkflow(other_repo, client, [target]))
    try:
        assert (await other.drain_batch())['dispatched'] == 0
    finally:
        release.set()
        await first
    assert len(repo.list_runs()) == 1 and len(client.commands) == 2


class Crash(BaseException):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize('after_create', [False, True])
async def test_crash_run_creation_gap_recovers_exact_receipt(setup, after_create):
    repo, target, client, workflow, dispatcher = setup
    ready(repo)
    original = workflow.create_run
    def crashing(*args, **kwargs):
        if after_create:
            original(*args, **kwargs)
        raise Crash()
    workflow.create_run = crashing
    with pytest.raises(Crash):
        await dispatcher.drain_batch()
    receipt = dispatcher.receipts()[0]
    assert receipt['state'] == 'reserved' and receipt['needs_reconciliation']
    assert receipt['run_id'] is None and not client.commands
    workflow.create_run = original
    # Ordinary catchup never steals an uncertain reservation.
    await StagingDispatcher(repo, workflow).drain_batch()
    assert not client.commands
    with pytest.raises(HubError, match='dispatch_quiescence_required'):
        await dispatcher.reconcile(receipt['sequence'], expected_owner=receipt['owner'])
    recovered = await dispatcher.reconcile(receipt['sequence'], expected_owner=receipt['owner'], confirmed_quiescent=True)
    assert recovered['state'] == 'succeeded'
    assert len(repo.list_runs()) == 1 and len(client.commands) == 2


@pytest.mark.asyncio
async def test_unknown_external_effect_never_replays_on_reconcile(setup):
    repo, target, client, workflow, dispatcher = setup
    client.states['deploy_fixture'] = TimeoutError('secret endpoint')
    ready(repo)
    await dispatcher.drain_batch()
    receipt = dispatcher.receipts()[0]
    assert receipt['state'] == 'needs_reconciliation' and receipt['reason'] == 'held'
    assert len(client.commands) == 1
    await dispatcher.reconcile(receipt['sequence'], expected_owner=receipt['owner'], confirmed_quiescent=True)
    assert len(client.commands) == 1 and len(repo.list_runs()) == 1
    assert 'secret endpoint' not in str(dispatcher.receipts())


@pytest.mark.asyncio
async def test_rollback_and_new_review_obey_lifetime_budget(setup):
    repo, target, client, workflow, dispatcher = setup
    client.states['smoke_fixture'] = 'failed'
    task = ready(repo)
    for attempt in range(4):
        await dispatcher.drain_batch()
        if attempt < 3:
            assert dispatcher.receipts()[0]['state'] == 'rolled_back'
        task = repo.get_task(task['id'])
        if attempt < 3:
            task = repo.set_candidate('codex', task['id'], task['revision'], COMMIT)
            task = repo.review_task('claude', task['id'], task['revision'], COMMIT, 'approve')
    assert len(repo.list_runs()) == 3
    assert dispatcher.receipts()[0]['reason'] == 'repair_limit'
    assert repo.get_task(task['id'])['repair_count'] == 3


@pytest.mark.asyncio
async def test_manifest_change_before_recovery_never_binds_other_run(setup):
    repo, target, client, workflow, dispatcher = setup
    task = ready(repo)
    original = workflow.create_run
    workflow.create_run = lambda **kwargs: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        await dispatcher.drain_batch()
    workflow.create_run = original
    unrelated = workflow.create_run('codex', task['id'], task['revision'], DIGEST, target.target_id)
    receipt = dispatcher.receipts()[0]
    workflow.targets[target.target_id] = replace(target, rollback_commit='e' * 40)
    await dispatcher.reconcile(receipt['sequence'], expected_owner=receipt['owner'], confirmed_quiescent=True)
    newest = dispatcher.receipts()[0]
    assert newest['run_id'] is None and newest['reason'] == 'fixture_manifest_changed'
    assert repo.get_run(unrelated['id'])['status'] == 'pending' and not client.commands


@pytest.mark.asyncio
async def test_pause_after_reservation_stays_visible(setup):
    repo, target, client, workflow, dispatcher = setup
    ready(repo)
    original = workflow.create_run
    def pausing(**kwargs):
        repo.set_pause('human:123', True)
        return original(**kwargs)
    workflow.create_run = pausing
    await dispatcher.drain_batch()
    receipt = dispatcher.receipts()[0]
    assert receipt['state'] == 'deferred' and receipt['reason'] == 'paused'
    assert not repo.list_runs() and not client.commands
    workflow.create_run = original
    repo.set_pause('human:123', False)
    await dispatcher.drain_batch()
    assert dispatcher.receipts()[0]['state'] == 'succeeded'
    assert len(repo.list_runs()) == 1 and len(client.commands) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('smoke_state', ['succeeded', 'failed'])
async def test_known_target_contention_retries_after_terminal_release(setup, smoke_state):
    repo, target, client, workflow, dispatcher = setup
    first = ready(repo, key='first')
    held_run = workflow.create_run('codex', first['id'], first['revision'], DIGEST, target.target_id)
    ready(repo, key='second')
    await dispatcher.drain_batch()
    receipt = dispatcher.receipts()[0]
    assert receipt['state'] == 'deferred' and receipt['reason'] == 'active_run'
    assert len(repo.list_runs()) == 1 and not client.commands
    # Retry while still busy is bounded, no new run/fence/command identity.
    await dispatcher.drain_batch()
    assert dispatcher.receipts()[0]['state'] == 'deferred' and len(repo.list_runs()) == 1
    client.states['smoke_fixture'] = smoke_state
    await workflow.advance(held_run['id'])
    client.states.clear()
    await dispatcher.drain_batch()
    assert dispatcher.receipts()[0]['state'] == 'succeeded'
    assert len(repo.list_runs()) == 2
    await dispatcher.drain_batch()
    assert len(repo.list_runs()) == 2


@pytest.mark.asyncio
async def test_deferred_stale_review_does_not_launch(setup):
    repo, target, client, workflow, dispatcher = setup
    first = ready(repo, key='first')
    held_run = workflow.create_run('codex', first['id'], first['revision'], DIGEST, target.target_id)
    second = ready(repo, key='second')
    await dispatcher.drain_batch()
    repo.set_candidate('codex', second['id'], second['revision'], 'e' * 40)
    await workflow.advance(held_run['id'])
    await dispatcher.drain_batch()
    assert dispatcher.receipts()[0]['state'] == 'skipped'
    assert dispatcher.receipts()[0]['reason'] == 'stale_review'
    assert len(repo.list_runs()) == 1 and len(client.commands) == 2


@pytest.mark.asyncio
async def test_limit_one_catchup_no_busy_retry_hotloop_and_release_wakes(setup):
    repo, target, client, workflow, dispatcher = setup
    first = ready(repo, key='first')
    held_run = workflow.create_run('codex', first['id'], first['revision'], DIGEST, target.target_id)
    ready(repo, key='second')
    attempts = []
    original = workflow.create_run
    def counting(**kwargs):
        attempts.append(kwargs['client_request_id'])
        return original(**kwargs)
    workflow.create_run = counting
    for iteration in range(40):
        result = await dispatcher.drain_batch(limit=1)
        if not result['more']:
            break
    else:
        pytest.fail('busy retry consumed history without making progress')
    assert len(attempts) == 1 and dispatcher.receipts()[0]['state'] == 'deferred'
    # No new durable event: another invocation does not retry target contention.
    assert not (await dispatcher.drain_batch(limit=1))['more']
    assert len(attempts) == 1
    await workflow.advance(held_run['id'])
    for iteration in range(40):
        if not (await dispatcher.drain_batch(limit=1))['more']:
            break
    else:
        pytest.fail('release catchup did not finish')
    assert len(attempts) == 2 and dispatcher.receipts()[0]['state'] == 'succeeded'
    assert len(repo.list_runs()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['succeeded', 'rolled_back'])
async def test_release_between_busy_rejection_and_receipt_does_not_lose_wake(setup, terminal):
    repo, target, client, workflow, dispatcher = setup
    first = ready(repo, key='first')
    occupied = workflow.create_run('codex', first['id'], first['revision'], DIGEST, target.target_id)
    ready(repo, key='second')
    original = dispatcher._record
    released = False
    def releasing(reservation, state, **kwargs):
        nonlocal released
        if state == 'deferred' and not released:
            released = True
            run = occupied
            events = ['running', 'succeeded'] if terminal == 'succeeded' else ['running', 'failed', 'rolled_back']
            for event in events:
                run = repo.append_run_event(repo.execution_principal, run['id'], event,
                                            run['id'], run['fencing_generation'], run['revision'])
        return original(reservation, state, **kwargs)
    dispatcher._record = releasing
    # Exactly four initial task events pertask plus run/task-createdevents:
    # scan once; release occurs just before the deferred receipt is persisted.
    await dispatcher.drain_batch()
    assert released and dispatcher.receipts()[0]['state'] == 'deferred'
    assert len(repo.list_runs()) == 1
    # No additional write is needed to make this ready again.
    await dispatcher.drain_batch()
    assert dispatcher.receipts()[0]['state'] == 'succeeded'
    assert len(repo.list_runs()) == 2 and len(client.commands) == 2


@pytest.mark.asyncio
async def test_colliding_local_request_key_cannot_bind_other_fingerprint(setup):
    repo, target, client, workflow, dispatcher = setup
    task = ready(repo)
    original = workflow.create_run
    workflow.create_run = lambda **kwargs: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        await dispatcher.drain_batch()
    workflow.create_run = original
    receipt = dispatcher.receipts()[0]
    # Same key but a different manifest fingerprint; never adopt this run.
    unrelated = repo.create_run('codex', task['id'], task['revision'], DIGEST, target.target_id,
                                client_request_id='hub-dispatch:' + str(receipt['sequence']))
    recovered = await dispatcher.reconcile(receipt['sequence'], expected_owner=receipt['owner'], confirmed_quiescent=True)
    assert recovered['state'] == 'needs_reconciliation'
    assert recovered['reason'] == 'idempotency_conflict' and recovered['run_id'] is None
    assert repo.get_run(unrelated['id'])['status'] == 'pending' and not client.commands


@pytest.mark.asyncio
async def test_completed_run_receipt_recovers_even_after_new_candidate(setup):
    repo, target, client, workflow, dispatcher = setup
    task = ready(repo)
    original = dispatcher._record
    def crashing(reservation, state, **kwargs):
        if state == 'succeeded':
            raise Crash()
        return original(reservation, state, **kwargs)
    dispatcher._record = crashing
    with pytest.raises(Crash):
        await dispatcher.drain_batch()
    dispatcher._record = original
    receipt = dispatcher.receipts()[0]
    assert repo.get_run(receipt['run_id'])['status'] == 'succeeded'
    task = repo.get_task(task['id'])
    repo.set_candidate('codex', task['id'], task['revision'], 'e' * 40)
    recovered = await dispatcher.reconcile(receipt['sequence'], expected_owner=receipt['owner'], confirmed_quiescent=True)
    assert recovered['state'] == 'succeeded' and len(client.commands) == 2


@pytest.mark.asyncio
async def test_recovery_claim_cas_and_manifest_guards(setup):
    repo, target, client, workflow, dispatcher = setup
    client.states['deploy_fixture'] = TimeoutError()
    ready(repo)
    await dispatcher.drain_batch()
    receipt = dispatcher.receipts()[0]
    with pytest.raises(HubError, match='dispatch_reservation_conflict'):
        await dispatcher.reconcile(receipt['sequence'], expected_owner='not-owner', confirmed_quiescent=True)
    repo.set_pause('human:123', True)
    with pytest.raises(HubError, match='paused'):
        await dispatcher.reconcile(receipt['sequence'], expected_owner=receipt['owner'], confirmed_quiescent=True)
    assert len(client.commands) == 1


@pytest.mark.parametrize('limit', [True, 0, 501, '50'])
@pytest.mark.asyncio
async def test_invalid_limits(setup, limit):
    dispatcher = setup[-1]
    with pytest.raises(HubError, match='invalid_limit'):
        await dispatcher.drain_batch(limit)
    with pytest.raises(HubError, match='invalid_limit'):
        dispatcher.receipts(limit)


def test_repository_identity_required(setup, tmp_path):
    repo, target, client, workflow, dispatcher = setup
    with pytest.raises(ValueError, match='dispatcher_repository_identity_required'):
        StagingDispatcher(HubRepository(tmp_path / 'other.sqlite'), workflow)
