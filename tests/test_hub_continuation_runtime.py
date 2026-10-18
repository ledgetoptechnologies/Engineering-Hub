"""Event-runtime continuation using Linux ownership and simulated host receipts.

Feed is an injected EventBroker, not the installed Linux native watcher. These
tests do not prove Docker effects, service supervision or real-host reboot.
"""
import asyncio
import sys

import pytest

from engineering_hub.autonomy import StagingRuntime
from engineering_hub.dispatcher import StagingDispatcher
from tests.test_hub_autonomy import Feed
from tests.test_hub_container_pilot import reviewed_task
from tests.test_hub_pilot_recovery import RecoveryClient, rig


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux pilot ownership')


async def settle(predicate):
    async def wait():
        for _ in range(10000):
            if predicate():
                return
            await asyncio.sleep(0)
        pytest.fail('Runtime did not reach the required bounded test condition')
    await asyncio.wait_for(wait(), timeout=5)


def head(repo):
    with repo._connection() as db:
        return db.execute('SELECT COALESCE(MAX(sequence),0) FROM hub_outbox').fetchone()[0]


def state(dispatcher):
    rows = dispatcher.receipts()
    return rows[0]['state'] if rows else None


def check_identity(current, original_run, commands):
    runs = current.repo.list_runs()
    assert len(runs) == 1
    final = runs[0]
    for key in ('id', 'task_id', 'fencing_generation', 'execution_manifest_digest', 'artifact_digest'):
        assert final[key] == original_run[key]
    assert final['status'] == 'succeeded'
    assert current.repo.get_task(final['task_id'])['repair_count'] == 0
    assert len(commands) == len({command.command_id for command in commands}) == 2
    assert [command.action for command in commands] == ['deploy_container', 'smoke_container']
    assert all(command.run_id == final['id']
               and command.fencing_generation == final['fencing_generation']
               and command.manifest_digest == final['execution_manifest_digest'] for command in commands)


@pytest.mark.asyncio
async def test_runtime_pointer_unpause_continues_accepted_run_without_manual_dispatch(rig):
    current = rig.restart()
    dispatcher = StagingDispatcher(current.repo, current.workflow)
    class PausingClient(RecoveryClient):
        async def submit_command(self, command):
            response = await super().submit_command(command)
            if command.action == 'deploy_container':
                current.repo.set_pause('human:1234', True)
            return response
    current.client = current.workflow.client = PausingClient()
    reviewed_task(current.repo)
    feed = Feed()
    runtime = StagingRuntime(dispatcher, feed)
    try:
        await runtime.start()
        await settle(lambda: state(dispatcher) == 'needs_reconciliation'
                     and runtime.last_result is not None and runtime.last_result['paused'])
        assert runtime.status['running'] and feed.broker.subscriber_count == 1
        assert current.repo.get_paused() is True
        original = list(current.client.submissions)
        assert [command.action for command in original] == ['deploy_container']
        row = dispatcher.receipts()[0]
        run = current.repo.get_run(row['run_id'])
        current.client = current.workflow.client = RecoveryClient()
        current.repo.set_pause('human:1234', False)
        feed.broker.publish(head(current.repo))
        await settle(lambda: state(dispatcher) == 'succeeded'
                     and runtime.last_result is not None and not runtime.last_result['more'])
        assert runtime.status['state'] == 'active'
        assert current.client.inspections == original
        assert [command.action for command in current.client.submissions] == ['smoke_container']
        assert runtime.last_result['examined'] <= 1
        check_identity(current, run, original + current.client.submissions)
    finally:
        await asyncio.wait_for(runtime.stop(), timeout=5)
        current.owner.close()
    assert feed.broker.subscriber_count == 0 and runtime.status['state'] == 'stopped'


@pytest.mark.asyncio
async def test_runtime_startup_recovers_original_receipt_after_owner_loss(rig):
    previous = rig.restart()
    dispatcher = StagingDispatcher(previous.repo, previous.workflow)
    class CrashClient(RecoveryClient):
        async def submit_command(self, command):
            response = await super().submit_command(command)
            if command.action == 'deploy_container':
                previous.owner.close()
            return response
    previous.client = previous.workflow.client = CrashClient()
    reviewed_task(previous.repo)
    old_feed = Feed()
    old_runtime = StagingRuntime(dispatcher, old_feed)
    try:
        await old_runtime.start()
        await settle(lambda: old_runtime.status['state'] == 'failed'
                     and not old_runtime.status['running'])
        original = list(previous.client.submissions)
        assert [command.action for command in original] == ['deploy_container']
        row = dispatcher.receipts()[0]
        run = previous.repo.get_run(row['run_id'])
    finally:
        await asyncio.wait_for(old_runtime.stop(), timeout=5)
        previous.owner.close()
    assert old_feed.broker.subscriber_count == 0

    current = rig.restart()
    dispatcher = StagingDispatcher(current.repo, current.workflow)
    feed = Feed()
    runtime = StagingRuntime(dispatcher, feed)
    try:
        # No published pointer is needed: subscription startup snapshot drives
        # durable catchup, including fair recovery/outbox limit-one steps.
        await runtime.start()
        await settle(lambda: state(dispatcher) == 'succeeded'
                     and runtime.last_result is not None and not runtime.last_result['more'])
        assert current.client.inspections == original
        assert [command.action for command in current.client.submissions] == ['smoke_container']
        assert runtime.status['state'] == 'active' and runtime.status['running']
        assert runtime.last_result['examined'] <= 1
        check_identity(current, run, original + current.client.submissions)
    finally:
        await asyncio.wait_for(runtime.stop(), timeout=5)
        current.owner.close()
    assert feed.broker.subscriber_count == 0 and runtime.status['state'] == 'stopped'
