"""Shared Hub listeners to real loopback worker TLS and journal; no Docker.

The worker backend copies synthetic bytes. This is integration evidence, not
host enrollment, isolation, deployment, rollback, or reboot acceptance.
"""
import json

import pytest

from engineering_hub import staging_module
from tests.test_shared_staging_application import shared_app
from tests.test_hub_unified_queued_pilot_service import rig, start, connections, submit, until, service, BASE
from tests.test_hub_staging_controller import payload
from tests.test_hub_staging_wire_integration import Wire, HTML


@pytest.mark.asyncio
async def test_shared_hub_to_worker_journal_reviewed_source_and_lost_ack(shared_app, tmp_path, monkeypatch):
    state = shared_app
    worker_root = tmp_path / 'disposable-worker'
    worker_root.mkdir()
    wire = Wire(worker_root)
    config_path = state.settings.staging_config_path
    config = json.loads(config_path.read_text())
    config.update(origin=wire.config.origin, credential_file=str(wire.config.credential_file),
                  ca_file=str(wire.config.ca_file), projects={'engineering-hub': {'recipes': ['static']}})
    config_path.write_text(json.dumps(config))
    # Use the actual network transport and the queue owned by this application.
    monkeypatch.setattr(service, 'prepare_shared_staging', staging_module.prepare_shared_staging)
    stop = task = None
    try:
        stop, task, addresses = await start(state)
        shared = state.core.state.shared_staging
        controller = shared.controller
        assert shared.status['state'] == 'active'
        assert controller.queue is shared.queue is state.core.state.work_queue
        controller.resources.set_policy(state.owner_actor, 'engineering-hub', 0,
                                        {'codex': ['read', 'build', 'test']})
        request = payload(controller.repo)
        async with connections(addresses, state) as (agent, _):
            response = await agent.post(BASE + '/staging/jobs', json=request)
            assert response.status_code == 202, response.text
            submission = response.json()
            repeated = await agent.post(BASE + '/staging/jobs', json=request)
            assert repeated.status_code == 202
            assert repeated.json()['job']['id'] == submission['job']['id']
        assert wire.posts('/v1/sources') == 1
        wire.drop_jobs = True
        held = await controller.run_once()
        assert held['job']['state'] == 'held'
        assert wire.posts() == 1
        async with connections(addresses, state) as (agent, browser):
            _, pilot = await submit(agent, browser, 'pilot-waits-for-modular-hold')
        assert controller.queue.get_job(pilot['id'])['machine_id'] == held['job']['machine_id']
        assert controller.queue.get_job(pilot['id'])['state'] == 'queued'
        assert await shared.bridge.run_once() is None
        assert controller.queue.get_job(pilot['id'])['state'] == 'queued'
        assert state.requests == []
        second = await controller.submit('codex', payload(controller.repo))
        assert await controller.run_once() is None
        assert controller.queue.get_job(second['job']['id'])['state'] == 'queued'
        assert wire.posts() == 1
        done = await controller.reconcile(submission['job']['id'])
        assert done['job']['state'] == 'succeeded'
        await until(lambda: controller.queue.get_job(pilot['id'])['state'] == 'succeeded')
        assert state.requests  # The pilot transport was reached only after the hold cleared.
        assert wire.posts() == 1
        receipt = wire.service.status(submission['job']['id'])
        assert receipt['state'] == 'succeeded'
        assert receipt['command_id'] == controller._read(job_id=submission['job']['id'])['command_id']
        assert receipt['result']['source_commit'] == request['source_commit']
        assert receipt['result']['source_digest'] == request['source_digest']
        assert receipt['result']['source_commit_verification'] == 'git-sha1-tree-v1'
        assert receipt['result']['source_binding'] == submission['source_binding']
        assert receipt['result']['source_binding']['commit_oid'] == request['source_commit']
        assert receipt['result']['resource_ids'] == submission['resource_ids']
        assert await controller.preview_file(submission['resource_ids']['preview'], 'index.html') == (HTML, 'text/html')
    finally:
        if stop is not None:
            stop.set()
        if task is not None:
            await task
        await wire.close()
