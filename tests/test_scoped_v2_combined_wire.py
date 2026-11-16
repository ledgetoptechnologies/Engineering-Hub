"""Loopback TLS with fake backend and TEST-ONLY gates, never a staging test."""
import json
import os

import pytest

from engineering_hub import staging_controller
from engineering_hub.repository import HubError
from engineering_hub.staging_client import StagingClientError
from hub_staging_service import core
from hub_staging_service import __main__ as worker_http
from tests.test_hub_staging_wire_integration import Wire, request
from tests.test_staging_profile_task_binding import confirmation

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Synthetic credential file permissions require POSIX')


@pytest.fixture(autouse=True)
def enable_synthetic_only_gates(monkeypatch):
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    monkeypatch.setattr(core, '_SOURCE_EVIDENCE_ENABLED', True)
    monkeypatch.setattr(worker_http, '_SCOPED_LIFECYCLE_READY', True)


def approved(wire):
    enrolled = wire.resources.get_profile('human:1', 'engineering-hub', 'static')
    command = request(wire.repo)
    task = wire.repo.get_task(command['review_task_id'])
    candidate = wire.repo.set_candidate('codex', task['id'], task['revision'], command['source_commit'],
                                       {'recipe': 'static', 'generation': 1})
    reviewed = wire.repo.review_task('claude', task['id'], candidate['revision'], command['source_commit'],
                                     'approve', confirmation(enrolled))
    command['review_task_revision'] = reviewed['revision']
    return command


@pytest.mark.asyncio
async def test_scoped_job_and_cleanup_lost_acks_reconcile_original_ids(tmp_path):
    wire = Wire(tmp_path, scoped=True)
    try:
        command = approved(wire)
        submitted = await wire.controller.submit('codex', command)
        expected_profile = confirmation(wire.resources.get_profile('human:1', 'engineering-hub', 'static'))
        observed = await wire.controller.client.observe_source_attempt(submitted['source_id'], 'engineering-hub',
            expected_profile, command['source_digest'], submitted['source_binding'])
        evidence = await wire.controller.client.observe_source_evidence(submitted['source_id'], 'engineering-hub',
            expected_profile, command['source_digest'], submitted['source_binding'])
        assert observed['state'] == 'historical_observation' and observed['reviewed_profile'] == expected_profile
        assert evidence['state'] == 'present' and evidence['reviewed_profile'] == expected_profile
        wire.drop_jobs = True
        held = await wire.controller.run_once()
        assert held['job']['state'] == 'held' and wire.posts('/v2/jobs') == 1
        wire.service.wait()
        assert (await wire.controller.reconcile(submitted['job']['id']))['job']['state'] == 'succeeded'
        assert wire.posts('/v2/jobs') == 1
        preview = submitted['resource_ids']['preview']
        assert (await wire.controller.preview_file(preview, 'index.html'))[0]
        wire.now[0] += 32 * 86400
        plan = wire.resources.cleanup_plan('codex', 'engineering-hub', kinds=['preview'])
        wire.drop_cleanup = True
        assert (await wire.controller.cleanup_apply('codex', plan['plan_id'], plan['digest']))['failed']
        await wire.controller.close()
        wire.new_controller()
        assert (await wire.controller.cleanup_apply('codex', plan['plan_id'], plan['digest']))['removed'] == [preview]
        assert wire.posts('/v2/cleanup') == 1
        assert any(method == 'GET' and path.startswith('/v2/operations/') for method, path in wire.received)
        assert not any(path.startswith('/v1/') for _, path in wire.received)
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_scoped_lost_source_ack_is_held_and_never_reposted_to_replacement(tmp_path):
    original = Wire(tmp_path, scoped=True)
    replacement = None
    original_client = original.controller.client
    try:
        payload = approved(original)
        original.drop_sources = True
        with pytest.raises(StagingClientError):
            await original.controller.submit('codex', payload)
        with original.repo._connection() as db:
            item = json.loads(db.execute('SELECT data FROM hub_staging_submissions WHERE actor=? AND request_id=?',
                                         ('codex', payload['request_id'])).fetchone()[0])
        assert item['phase'] == 'deposit_unknown' and item['job_id'] is None
        assert original.service.db.execute('SELECT id FROM sources').fetchone()[0] == item['source_id']
        assert original.posts('/v2/source-attempts') == 1 and original.posts('/v2/jobs') == 0
        root = tmp_path / 'replacement'; root.mkdir()
        replacement = Wire(root, scoped=True)
        original.controller.client = replacement.controller.client
        with pytest.raises(HubError, match='staging_source_receipt_uncertain'):
            await original.controller.submit('codex', payload)
        assert replacement.received == [] and original.posts('/v2/source-attempts') == 1
        assert replacement.service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0
    finally:
        original.controller.client = original_client
        if replacement is not None: await replacement.close()
        await original.close()
