"""Real loopback TLS; synthetic credentials and non-Docker backend only."""
import ipaddress
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from hub_staging_service import __main__ as worker_http
from hub_staging_service import core
from hub_staging_service.__main__ import handler
from hub_staging_service.core import Service
from tests.test_hub_staging_wire_integration import Wire
from tests.test_scoped_v2_combined_wire import approved, enable_synthetic_only_gates, pytestmark
from tests.test_source_evidence import make_source


async def prepared(wire):
    submitted = await wire.controller.submit('codex', approved(wire))
    result = await wire.controller.run_once()
    wire.service.wait()
    if result['job']['state'] != 'succeeded':
        assert (await wire.controller.reconcile(submitted['job']['id']))['job']['state'] == 'succeeded'
    return submitted


@pytest.mark.asyncio
async def test_foreign_and_legacy_cannot_read_or_clean_scoped_results(tmp_path):
    wire = Wire(tmp_path, scoped=True)
    try:
        submitted = await prepared(wire)
        job, preview = submitted['job']['id'], submitted['resource_ids']['preview']
        preview_revision = wire.service.metadata(preview)['revision']
        async with httpx.AsyncClient(verify=str(wire.config.ca_file), trust_env=False) as client:
            def auth(token): return {'Authorization': 'Bearer ' + token}
            for token, prefix in ((wire.other_token, '/v2'), (wire.legacy_token, '/v1')):
                for path in (f'{prefix}/jobs/{job}', f'{prefix}/resources/{preview}/metadata',
                             f'{prefix}/resources/{preview}/files/index.html'):
                    response = await client.get(wire.config.origin + path, headers=auth(token))
                    assert response.status_code == 404
                denied = await client.post(wire.config.origin + prefix + '/cleanup', headers=auth(token), json={
                    'command_id': str(uuid4()), 'idempotency_key': 'not-owner',
                    'resources': [{'resource_id': preview, 'expected_revision': preview_revision}]})
                assert denied.status_code == 404
                assert wire.service.db.execute('SELECT count(*) FROM operations WHERE job_id IS NULL').fetchone()[0] == 0
            # Global runtime may be unavailable, but must not disclose scoped IDs if served.
            runtime = await client.get(wire.config.origin + '/v1/runtime', headers=auth(wire.legacy_token))
            assert runtime.status_code != 200 or (job not in runtime.text and preview not in runtime.text)
        assert wire.service.metadata(preview)['state'] == 'ready'
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_partial_lifecycle_interlock_denies_scoped_effects(tmp_path, monkeypatch):
    wire = Wire(tmp_path, scoped=True)
    try:
        submitted = await prepared(wire)
        preview = submitted['resource_ids']['preview']
        monkeypatch.setattr(worker_http, '_SCOPED_LIFECYCLE_READY', False)
        async with httpx.AsyncClient(verify=str(wire.config.ca_file), trust_env=False) as client:
            token = wire.config.credential_file.read_text().strip()
            response = await client.post(wire.config.origin + '/v2/cleanup',
                headers={'Authorization': 'Bearer ' + token}, json={'command_id': str(uuid4()),
                    'idempotency_key': 'disabled', 'resources': [{'resource_id': preview, 'expected_revision': 2}]})
            assert response.status_code == 503
        assert wire.service.metadata(preview)['state'] == 'ready'
    finally:
        await wire.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('module,name', [
    (worker_http, '_SCOPED_LIFECYCLE_READY'), (core, '_PROFILE_PROTOCOL_READY'),
    (core, '_SOURCE_ATTEMPT_ENABLED'), (core, '_SOURCE_EVIDENCE_ENABLED'),
])
async def test_any_missing_interlock_blocks_source_before_effect(tmp_path, monkeypatch, module, name):
    wire = Wire(tmp_path, scoped=True)
    try:
        monkeypatch.setattr(module, name, False)
        source = make_source(); source['project'] = 'engineering-hub'
        async with httpx.AsyncClient(verify=str(wire.config.ca_file), trust_env=False) as client:
            token = wire.config.credential_file.read_text().strip()
            headers = {'Authorization': 'Bearer ' + token}
            status = await client.get(wire.config.origin + '/v2/status', headers=headers)
            assert status.status_code == 200 and status.json()['profile_protocol_ready'] is False
            response = await client.post(wire.config.origin + '/v2/source-attempts', headers=headers, json=source)
            assert response.status_code == 503 and response.json() == {'error': 'v2_lifecycle_unavailable'}
        assert wire.service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0
        assert not (wire.service.sources / (source['source_id'] + '.zip')).exists()
        table = wire.service.db.execute("SELECT 1 FROM sqlite_master WHERE name='source_attempts'").fetchone()
        if table:
            assert wire.service.db.execute('SELECT count(*) FROM source_attempts').fetchone()[0] == 0
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_scoped_cleanup_exact_retry_returns_original_outcome(tmp_path):
    wire = Wire(tmp_path, scoped=True)
    try:
        submitted = await prepared(wire)
        preview = submitted['resource_ids']['preview']
        revision = wire.service.metadata(preview)['revision']
        payload = {'command_id': str(uuid4()), 'idempotency_key': 'exact-cleanup',
                   'resources': [{'resource_id': preview, 'expected_revision': revision}]}
        async with httpx.AsyncClient(verify=str(wire.config.ca_file), trust_env=False) as client:
            token = wire.config.credential_file.read_text().strip()
            headers = {'Authorization': 'Bearer ' + token}
            first = await client.post(wire.config.origin + '/v2/cleanup', headers=headers, json=payload)
            retry = await client.post(wire.config.origin + '/v2/cleanup', headers=headers, json=payload)
            assert first.status_code == retry.status_code == 200
            assert first.json() == retry.json()
            assert first.json()['result']['deleted_resource_ids'] == [preview]
        assert wire.service.metadata(preview)['state'] == 'deleted'
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_scoped_cleanup_lost_ack_recovers_after_worker_restart_with_private_terminal_receipts(tmp_path):
    wire = Wire(tmp_path, scoped=True, terminal=True)
    try:
        submitted = await prepared(wire)
        preview = submitted['resource_ids']['preview']
        wire.now[0] += 32 * 86400
        plan = wire.resources.cleanup_plan('codex', 'engineering-hub', kinds=['preview'])
        wire.drop_cleanup = True
        assert (await wire.controller.cleanup_apply('codex', plan['plan_id'], plan['digest']))['failed']
        assert wire.posts('/v2/cleanup') == 1
        cleanup = wire.service.db.execute("SELECT command_id FROM operations WHERE job_id IS NULL").fetchone()
        assert cleanup is not None
        command = cleanup['command_id']
        receipt = wire.service.terminal_evidence(command)
        assert receipt['state'] == 'observed' and receipt['authority'] == 'evidence_only'
        assert receipt['terminal']['outcome'] == 'succeeded'
        assert wire.service.metadata(preview)['state'] == 'deleted'
        await wire.controller.close()
        # Close and reopen the real temporary worker journal and private receipts;
        # a replacement handler on the same test TLS listener receives subsequent GETs.
        wire.service.close()
        wire.service = Service(tmp_path / 'worker', wire.worker_projects, wire.backend,
            capacity_bytes=128 * 1024**2, terminal_receipts=wire.receipt_config)
        assert wire.service._receipt_fault is None
        token = wire.config.credential_file.read_text().strip()
        base = handler(wire.service, wire.legacy_token, {ipaddress.ip_address('127.0.0.1')},
            v2_credentials=(('alice', token, frozenset({'engineering-hub'}),
                             {'engineering-hub': frozenset({'static'})}),))
        class Counted(base):
            def authorize(self):
                identity = super().authorize()
                with wire.guard:
                    wire.counts[(self.command, self.path)] += 1
                    wire.received.append((self.command, self.path))
                return identity
        wire.server.RequestHandlerClass = Counted
        wire.new_controller()
        assert (await wire.controller.cleanup_apply('codex', plan['plan_id'], plan['digest']))['removed'] == [preview]
        assert wire.posts('/v2/cleanup') == 1
        assert ('GET', '/v2/operations/' + command) in wire.received
        receipt = wire.service.terminal_evidence(command)
        assert receipt['state'] == 'observed' and receipt['authority'] == 'evidence_only'
        assert receipt['terminal']['outcome'] == 'succeeded'
    finally:
        await wire.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('projects,recipes', [(frozenset(), {}),
                                                (frozenset({'engineering-hub'}), {})])
async def test_removed_project_or_recipe_denies_original_scoped_job(tmp_path, projects, recipes):
    wire = Wire(tmp_path, scoped=True)
    try:
        submitted = await prepared(wire)
        token = wire.config.credential_file.read_text().strip()
        wire.server.RequestHandlerClass = handler(wire.service, wire.legacy_token,
            {ipaddress.ip_address('127.0.0.1')}, v2_credentials=(('alice', token, projects, recipes),))
        async with httpx.AsyncClient(verify=str(wire.config.ca_file), trust_env=False) as client:
            response = await client.get(wire.config.origin + '/v2/jobs/' + submitted['job']['id'],
                                        headers={'Authorization': 'Bearer ' + token})
            assert response.status_code == 404
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_queued_profile_after_mode_revert_is_held_without_post_or_replay(tmp_path):
    wire = Wire(tmp_path, scoped=True)
    try:
        submitted = await wire.controller.submit('codex', approved(wire))
        original_id = submitted['job']['id']
        await wire.controller.close()
        scoped_config = wire.config
        wire.config = replace(scoped_config, protocol_mode='legacy')
        wire.new_controller()
        before = list(wire.received)
        held = await wire.controller.run_once()
        assert held['job']['id'] == original_id and held['job']['state'] == 'held'
        assert held['phase'] == 'held' and wire.received == before and wire.posts('/v2/jobs') == 0
        await wire.controller.close()
        wire.config = scoped_config
        wire.new_controller()
        assert await wire.controller.run_once() is None
        assert wire.controller.queue.get_job(original_id)['state'] == 'held'
        assert wire.posts('/v2/jobs') == 0
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_same_source_id_cannot_substitute_reviewed_profile_tuple(tmp_path):
    from tests.test_staging_profile_task_binding import confirmation
    wire = Wire(tmp_path, scoped=True)
    try:
        command = approved(wire)
        submitted = await wire.controller.submit('codex', command)
        original_profile = confirmation(wire.resources.get_profile('human:1', 'engineering-hub', 'static'))
        source_id = submitted['source_id']
        source = {key: command[key] for key in ('project', 'source_digest', 'source_zip_base64',
                                                  'source_commit', 'source_commit_object_base64')}
        source.update(source_id=source_id, reviewed_profile=original_profile)
        before = tuple(wire.service.db.execute('SELECT * FROM source_attempts WHERE source_id=?', (source_id,)).fetchone())
        async with httpx.AsyncClient(verify=str(wire.config.ca_file), trust_env=False) as client:
            token = wire.config.credential_file.read_text().strip()
            headers = {'Authorization': 'Bearer ' + token}
            for changed in (original_profile, dict(original_profile, generation=original_profile['generation'] + 1),
                            dict(original_profile, digest='sha256:' + '0' * 64),
                            dict(original_profile, recipe='other')):
                response = await client.post(wire.config.origin + '/v2/source-attempts', headers=headers,
                                             json={**source, 'reviewed_profile': changed})
                assert 400 <= response.status_code < 500
                assert tuple(wire.service.db.execute('SELECT * FROM source_attempts WHERE source_id=?',
                                                     (source_id,)).fetchone()) == before
        assert wire.service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 1
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_job_cannot_substitute_profile_for_already_deposited_source(tmp_path):
    wire = Wire(tmp_path, scoped=True)
    try:
        submitted = await wire.controller.submit('codex', approved(wire))
        item = wire.controller._read(job_id=submitted['job']['id'])
        original = {'job_id':item['job_id'], 'command_id':item['command_id'], 'idempotency_key':item['command_id'],
                    'project':item['project'], 'recipe':item['recipe'], 'source_commit':item['source_commit'],
                    'source_digest':item['source_digest'], 'source_id':item['source_id'],
                    'resource_ids':item['resource_ids'], 'require_source_binding':True,
                    'expected_source_binding':item['source_binding'], 'reviewed_profile':item['reviewed_profile']}
        before_attempt = tuple(wire.service.db.execute('SELECT * FROM source_attempts WHERE source_id=?',
                                                       (item['source_id'],)).fetchone())
        before_source = tuple(wire.service.db.execute('SELECT * FROM sources WHERE id=?',
                                                      (item['source_id'],)).fetchone())
        original_bytes = (wire.service.sources / (item['source_id'] + '.zip')).read_bytes()
        async with httpx.AsyncClient(verify=str(wire.config.ca_file), trust_env=False) as client:
            token = wire.config.credential_file.read_text().strip()
            headers = {'Authorization': 'Bearer ' + token}
            for profile in (dict(item['reviewed_profile'], generation=item['reviewed_profile']['generation'] + 1),
                            dict(item['reviewed_profile'], digest='sha256:' + '0' * 64),
                            dict(item['reviewed_profile'], recipe='other')):
                response = await client.post(wire.config.origin + '/v2/jobs', headers=headers,
                                             json={**original, 'reviewed_profile': profile})
                assert 400 <= response.status_code < 500
                assert wire.service.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
                assert tuple(wire.service.db.execute('SELECT * FROM source_attempts WHERE source_id=?',
                                                     (item['source_id'],)).fetchone()) == before_attempt
                assert tuple(wire.service.db.execute('SELECT * FROM sources WHERE id=?',
                                                     (item['source_id'],)).fetchone()) == before_source
                assert (wire.service.sources / (item['source_id'] + '.zip')).read_bytes() == original_bytes
                assert not wire.backend.entered.is_set()
    finally:
        await wire.close()
