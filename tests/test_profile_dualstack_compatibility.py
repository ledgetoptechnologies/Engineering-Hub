"""Synthetic loopback tests of v1 preservation and explicit v2 admission."""
import uuid

import pytest

from engineering_hub.repository import HubError
from engineering_hub.staging_client import StagingClientError
from tests.test_hub_staging_wire_integration import Wire, request


@pytest.mark.asyncio
async def test_v1_remains_usable_with_v2_disabled(tmp_path, monkeypatch):
    from engineering_hub import staging_controller
    from hub_staging_service import core
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', False)
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', False)
    wire = Wire(tmp_path)
    try:
        payload = request(wire.repo)
        task = wire.repo.create_task('human:1', 'engineering-hub', 'v1 fixture', str(uuid.uuid4()))
        task = wire.repo.claim_task('codex', task['id'], task['revision'])
        task = wire.repo.set_candidate('codex', task['id'], task['revision'], payload['source_commit'])
        task = wire.repo.review_task('claude', task['id'], task['revision'], payload['source_commit'], 'approve')
        payload.update(review_task_id=task['id'], review_task_revision=task['revision'])
        assert (await wire.controller.client.status())['schema'] == 'hub-staging.v1'
        assert (await wire.controller.client.status_v2())['profile_protocol_ready'] is False
        queued = await wire.controller.submit('codex', payload)
        finished = await wire.controller.run_once()
        assert finished['job']['state'] == 'succeeded'
        assert wire.posts('/v1/jobs') == 1 and wire.posts('/v2/jobs') == 0
        assert (await wire.controller.submit('codex', payload))['job']['id'] == queued['job']['id']
        assert wire.posts('/v1/jobs') == 1
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_v1_queued_before_v2_enable_keeps_its_original_protocol(tmp_path, monkeypatch):
    from engineering_hub import staging_controller
    from hub_staging_service import core
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', False)
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', False)
    wire = Wire(tmp_path)
    try:
        payload = request(wire.repo)
        task = wire.repo.create_task('human:1', 'engineering-hub', 'queued v1 fixture', str(uuid.uuid4()))
        task = wire.repo.claim_task('codex', task['id'], task['revision'])
        task = wire.repo.set_candidate('codex', task['id'], task['revision'], payload['source_commit'])
        task = wire.repo.review_task('claude', task['id'], task['revision'], payload['source_commit'], 'approve')
        payload.update(review_task_id=task['id'], review_task_revision=task['revision'])
        queued = await wire.controller.submit('codex', payload)
        monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
        monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
        async def no_v2_status():
            raise AssertionError('legacy retry must not contact v2')
        monkeypatch.setattr(wire.controller.client, 'status_v2', no_v2_status)
        assert (await wire.controller.submit('codex', payload))['job']['id'] == queued['job']['id']
        finished = await wire.controller.run_once()
        assert finished['job']['id'] == queued['job']['id'] and finished['job']['state'] == 'succeeded'
        assert wire.posts('/v1/jobs') == 1 and wire.posts('/v2/jobs') == 0
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_v2_payload_cannot_fall_back_to_v1_endpoint(tmp_path, monkeypatch):
    from engineering_hub import staging_controller
    from hub_staging_service import core
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', False)
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', False)
    wire = Wire(tmp_path)
    try:
        from tests.test_hub_staging_client import source
        payload = source()
        payload['project'] = 'engineering-hub'
        with pytest.raises(StagingClientError):
            await wire.controller.client._request('POST', '/v1/sources', payload)
        assert wire.service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0
        with pytest.raises(StagingClientError):
            await wire.controller.client.put_source(payload)
        assert wire.service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0
    finally:
        await wire.close()
