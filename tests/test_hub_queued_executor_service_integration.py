"""Real TCP + protected UDS service integration, not Docker/host acceptance.

The controller, host adapter, SQLite ledgers, signed synthetic Access assertion,
independent review, native queue feed, and HTTP clients are real implementations.
Build/provenance, baseline, state, daemon, and health providers are deliberately
synthetic. This neither authorizes host enrollment nor proves real deployment,
container namespace isolation, reboot recovery, or exclusion of outside writers.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import sqlite3
import sys
import threading

import pytest

if sys.platform != 'linux':
    pytest.skip('Real Linux private owners and protected Unix socket', allow_module_level=True)

import httpx

from engineering_hub.container_pilot import ContainerPilotTarget
from engineering_hub.container_pilot_client import ContainerPilotClient, ContainerPilotRequest
from engineering_hub.private_browser_socket import create_private_browser_client
from engineering_hub.split_server import serve_public_browser
from tests.test_hub_container_executor_service import (
    owned as host_bundle, start as start_host, origin, TOKEN,
    assert_closed as assert_host_closed, assert_port_released,
)
from tests.test_hub_queued_pilot_service import (
    setup as coordinator, start as start_coordinator, agent_client,
    human_task, assert_closed as assert_coordinator_closed,
)
from tests.test_hub_gateway_queued_pilot import checked, BASE, ORIGIN
from tests.test_hub_queued_pilot import ACTION, MACHINE


@asynccontextmanager
async def services(control, host):
    host_stop = host_task = control_stop = control_task = public_stop = public_task = None
    public_client = None
    try:
        host_stop, host_task, host_address = await start_host(host)
        # Use the host fixture's complete canonical manifest. Its built image
        # config identity is not the separate coordinator unit fixture's image.
        control.manifest = host.manifest
        control.target = ContainerPilotTarget.from_manifest(host.manifest)
        control.allowed_task_id = None

        def synthetic_operator_binding(actor, action, environment, target):
            # Explicit test-only enrollment, not proof of real host approval,
            # provider authenticity, or independent machine reconciliation.
            return (actor == 'codex' and action == ACTION and environment == 'staging'
                and control.settings.staging_machine_id == MACHINE
                and control.core.state.work_queue.actions[action].machine_id == MACHINE
                and control.allowed_task_id is not None
                and target.task_id == control.allowed_task_id
                and target.source_commit == host.manifest.candidate.source_commit
                and target.artifact_digest == host.manifest.candidate.image_config_id)

        def client_factory():
            client = ContainerPilotClient(origin(host_address), TOKEN)
            original_close = client.aclose
            client.close_count = 0
            async def close():
                client.close_count += 1
                control.events.append('client-close')
                await original_close()
            client.aclose = close
            control.clients.append(client)
            return client
        control.client_factory = client_factory
        control_stop, control_task, agent_address = await start_coordinator(control,
            target_verifier=synthetic_operator_binding)
        public_stop = asyncio.Event()
        ready = asyncio.get_running_loop().create_future()
        def public_factory():
            nonlocal public_client
            public_client = create_private_browser_client(control.endpoint)
            return public_client
        public_task = asyncio.create_task(serve_public_browser(public_factory,
            browser_port=0, stop_event=public_stop, install_signals=False,
            on_ready=lambda address: ready.set_result(address)))
        browser_address = await asyncio.wait_for(ready, 8)
        yield dict(host_address=host_address, agent_address=agent_address,
            browser_address=browser_address, control_stop=control_stop,
            control_task=control_task, host_task=host_task)
    finally:
        # The gateway drains first; the coordinator retains its RPC client
        # until accepted remote work drains. The executor remains last owner.
        failures = []
        for stop, task in ((public_stop, public_task), (control_stop, control_task), (host_stop, host_task)):
            if stop is not None:
                stop.set()
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), 10)
                except asyncio.CancelledError:
                    if not task.done():
                        failures.append(RuntimeError('Integration cleanup was interrupted'))
                except Exception as error:
                    failures.append(error)
        # Never abandon a shielded owner merely because a bounded observation
        # expired. Backend barriers in these tests are always released first.
        pending = [task for task in (public_task, control_task, host_task)
                   if task is not None and not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if public_client is not None:
            assert public_client.is_closed
        if failures:
            raise ExceptionGroup('Integration service cleanup failed', failures)


def browser_client(address, control):
    return httpx.AsyncClient(base_url=origin(address), trust_env=False, follow_redirects=False,
        headers={'Host': 'hub.example', 'Cf-Access-Jwt-Assertion': control.assertion})


async def review_and_submit(agent, task, manifest, key):
    commit = manifest.candidate.source_commit
    image = manifest.candidate.image_config_id
    path = BASE + '/tasks/' + task['id']
    task = checked(await agent.post(path + '/claim', json={'expected_revision': task['revision']}))
    task = checked(await agent.post(path + '/candidate', json={
        'expected_revision': task['revision'], 'commit': commit}))
    denied = await agent.post(path + '/review', json={
        'expected_revision': task['revision'], 'commit': commit, 'verdict': 'approve'})
    assert denied.status_code == 403
    task = checked(await agent.post(path + '/review',
        headers={'Authorization': 'Bearer test-claude'}, json={
            'expected_revision': task['revision'], 'commit': commit, 'verdict': 'approve'}))
    assert task['owner'] == 'codex' and task['review']['actor'] == 'claude'
    assert task['review']['commit'] == commit
    job = checked(await agent.post(BASE + '/work/jobs', json={
        'request_id': key, 'action_key': ACTION, 'environment': 'staging',
        'target': {'task_id': task['id'], 'source_commit': commit, 'artifact_digest': image}}), 201)
    return task, job


async def completed_runtime(control):
    runtime = control.core.state.queued_pilot_runtime
    async with asyncio.timeout(15):
        while runtime.status['last_job'] is None:
            await asyncio.sleep(0.001)
    return runtime


def controller_receipts(control, run_id):
    with control.core.state.repository._connection() as db:
        rows = db.execute('SELECT action,payload,state,response_json FROM hub_container_pilot_commands '
                          'WHERE run_id=? ORDER BY rowid', (run_id,)).fetchall()
    return [(row['action'], ContainerPilotRequest.model_validate(json.loads(row['payload'])),
             row['state'], json.loads(row['response_json'])) for row in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize('rollback', [False, True], ids=['healthy', 'failed-candidate-healthy-rollback'])
async def test_actual_controller_to_owned_executor_http_full_workflow(coordinator, host_bundle, rollback):
    control, host = coordinator, host_bundle
    host.failed_probe = rollback
    async with services(control, host) as running:
        async with (browser_client(running['browser_address'], control) as browser,
                    agent_client(running['agent_address']) as agent):
            task = await human_task(control, browser, 'actual-service-task')
            control.allowed_task_id = task['id']
            task, job = await review_and_submit(agent, task, host.manifest, 'actual-service-work')
            runtime = await completed_runtime(control)
            assert runtime.status['last_job'] == {'id': job['id'], 'state': 'failed' if rollback else 'succeeded'}
            assert control.core.state.event_feed.available
            seen = checked(await browser.get(BASE + '/work/jobs/' + job['id']))
            assert seen['state'] == ('failed' if rollback else 'succeeded')
            repo = control.core.state.repository
            run = repo.list_runs()[0]
            assert run['task_id'] == task['id']
            assert run['artifact_digest'] == host.manifest.candidate.image_config_id
            assert run['status'] == ('rolled_back' if rollback else 'succeeded')
            visible_runs = checked(await browser.get(BASE + '/runs'))['items']
            assert len(visible_runs) == 1 and visible_runs[0]['id'] == run['id']
            assert visible_runs[0]['status'] == run['status']
            visible_task = checked(await browser.get(BASE + '/tasks/' + task['id']))
            assert visible_task['review']['actor'] == 'claude'
            assert visible_task['candidate_commit'] == host.manifest.candidate.source_commit
            receipts = controller_receipts(control, run['id'])
            phases = ['deploy_container', 'smoke_container']
            if rollback:
                phases.extend(['rollback_container', 'smoke_container_rollback'])
            assert [action for action, *_ in receipts] == phases
            events = list(host.backend.events)
            async with ContainerPilotClient(origin(running['host_address']), TOKEN) as inspector:
                for action, command, state, stored in receipts:
                    original = await inspector.get_command(command)
                    assert original.model_dump() == stored
                    assert original.command_id == command.command_id and not original.replayed
                    assert state == original.state
            assert host.backend.events == events
            assert events.count('create') == events.count('start') == (2 if rollback else 1)
            assert events.count('probe') == (2 if rollback else 1)
            assert host.adapter.ledger.target_snapshot()['state'] == 'active'
            with repo._connection() as db:
                assert db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?',
                                  (MACHINE,)).fetchone()[0] is None
                assert tuple(db.execute('SELECT state,run_id FROM hub_queued_pilot_jobs WHERE job_id=?',
                                  (job['id'],)).fetchone()) == ('mapped', run['id'])
                assert db.execute('SELECT state FROM hub_work_jobs WHERE id=?',
                                  (job['id'],)).fetchone()[0] == seen['state']
            assert host.provider_closes == 0 and control.clients[0].close_count == 0
            assert (await browser.get(BASE + '/tasks',
                headers={'Authorization': 'Bearer test-codex'})).status_code == 403
    assert_coordinator_closed(control)
    assert_host_closed(host)
    assert not control.endpoint.exists()
    assert_port_released(running['host_address'])
    # Reopen only the host receipt reader after both owners stop. This does
    # not submit or resume work and cannot add a synthetic backend effect.
    from hub_container_executor.adapter import HostAdapter
    reopened = HostAdapter(host.path, host.manifest)
    try:
        assert all(reopened.get(command.command_id).model_dump() == stored
                   for _, command, _, stored in receipts)
        assert host.backend.events == events
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_coordinator_cancellation_drains_actual_remote_phase_without_post_replay(coordinator, host_bundle):
    control, host = coordinator, host_bundle
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    def block_backend():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), 'Integration backend release was not signalled'
    host.block = block_backend
    async with services(control, host) as running:
        try:
            async with (browser_client(running['browser_address'], control) as browser,
                        agent_client(running['agent_address']) as agent):
                task = await human_task(control, browser, 'drained-service-task')
                control.allowed_task_id = task['id']
                _, job = await review_and_submit(agent, task, host.manifest, 'drained-service-work')
                await asyncio.wait_for(entered.wait(), 8)
                serving = running['control_task']
                serving.cancel()
                async with asyncio.timeout(8):
                    while control.core.state.queued_pilot_runtime.admission_allowed():
                        await asyncio.sleep(0.001)
                serving.cancel()
                assert not serving.done()
                assert control.clients[0].close_count == control.owners[0].close_count == 0
                control.owners[0].assert_active(control.settings.database)
                assert host.provider_closes == 0 and not host.adapter._closed
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(serving, 10)
                assert_coordinator_closed(control)
                assert host.provider_closes == 0 and not host.adapter._closed

                # Read the stopped controller's durable result, not a new
                # controller or an attempted resumption of its held work.
                database = sqlite3.connect(control.settings.database.as_uri() + '?mode=ro', uri=True)
                try:
                    assert database.execute('SELECT state FROM hub_work_jobs WHERE id=?',
                                            (job['id'],)).fetchone()[0] == 'held'
                    assert database.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?',
                                            (MACHINE,)).fetchone()[0] == job['id']
                    rows = database.execute('SELECT payload,response_json FROM hub_container_pilot_commands').fetchall()
                finally:
                    database.close()
                assert len(rows) == 1
                command = ContainerPilotRequest.model_validate(json.loads(rows[0][0]))
                assert command.action == 'deploy_container'
                stored = json.loads(rows[0][1])
                assert stored['state'] == 'succeeded'
                before = list(host.backend.events)
                async with ContainerPilotClient(origin(running['host_address']), TOKEN) as inspector:
                    assert (await inspector.get_command(command)).model_dump() == stored
                assert host.backend.events == before
                assert before.count('create') == before.count('start') == 1
                assert before.count('probe') == 0
        finally:
            # Release accepted backend work before services.__aexit__ starts
            # draining owners, even if an intermediate assertion fails.
            release.set()
    assert_host_closed(host)
    assert not control.endpoint.exists()
