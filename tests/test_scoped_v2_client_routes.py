"""Synthetic transport only; no deployed receiver or security fence is implied."""
from dataclasses import replace
import base64
import json
import os
from uuid import uuid4

import httpx
import pytest

from engineering_hub.staging_client import StagingClient, StagingClientError, StagingNotAccepted, load_optional_config
from hub_staging_service.source_provenance import verify
from tests.test_hub_staging_client import config, job
from tests.test_source_evidence import make_source

PROFILE = {'project': 'demo', 'recipe': 'static', 'generation': 1, 'digest': 'sha256:' + 'c' * 64}


@pytest.mark.asyncio
async def test_scoped_reads_and_status_never_use_legacy_routes(config):
    seen = []
    job, command = str(uuid4()), str(uuid4())
    def respond(request):
        seen.append((request.method, request.url.path))
        if request.url.path == '/v2/status':
            return httpx.Response(200, json={'schema': 'hub-staging.v2', 'active_command_id': None,
                'projects': {'demo': ['static']}, 'profile_protocol': 'hub-staging-profile.v2',
                'profile_protocol_ready': False})
        return httpx.Response(200, json={'command_id': command, 'job_id': job, 'state': 'running', 'result': {}})
    client = StagingClient(replace(config, protocol_mode='scoped-v2'), test_transport=httpx.MockTransport(respond))
    try:
        assert (await client.status())['schema'] == 'hub-staging.v2'
        assert (await client.job(job))['job_id'] == job
        with pytest.raises(StagingClientError):
            await client.runtime()
        with pytest.raises(StagingClientError):
            await client._legacy_put_source({})
    finally:
        await client.aclose()
    assert seen == [('GET', '/v2/status'), ('GET', '/v2/jobs/' + job)]


@pytest.mark.asyncio
async def test_scoped_cleanup_uncertainty_reconciles_only_original_command(config):
    seen = []
    command, resource = str(uuid4()), str(uuid4())
    def respond(request):
        seen.append((request.method, request.url.path))
        if request.method == 'POST':
            raise httpx.ReadError('response lost', request=request)
        return httpx.Response(200, json={'command_id': command, 'job_id': None, 'state': 'succeeded',
                                         'result': {'deleted_resource_ids': [resource]}})
    client = StagingClient(replace(config, protocol_mode='scoped-v2'), test_transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(StagingClientError):
            await client.cleanup({'command_id': command, 'idempotency_key': 'one',
                                  'resources': [{'resource_id': resource, 'expected_revision': 1}]})
        assert (await client.operation(command))['command_id'] == command
    finally:
        await client.aclose()
    assert seen == [('POST', '/v2/cleanup'), ('GET', '/v2/operations/' + command)]


@pytest.mark.asyncio
async def test_scoped_v2_failure_never_falls_back_or_claims_definitive_rejection(config):
    seen = []
    def respond(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(409, json={'error': 'active_command'})
    client = StagingClient(replace(config, protocol_mode='scoped-v2'), test_transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(StagingClientError) as exc:
            await client._request('POST', '/v2/jobs', {})
        assert type(exc.value) is StagingClientError
    finally:
        await client.aclose()
    assert seen == [('POST', '/v2/jobs')]


@pytest.mark.asyncio
@pytest.mark.parametrize('status,reason', [
    (409, 'worker_busy_or_held'), (409, 'worker_storage_capacity'),
    (409, 'resource_conflict'), (403, 'project_not_allowed'),
    (403, 'recipe_not_allowed'), (401, 'unauthorized'), (403, 'peer_not_allowed'),
])
async def test_scoped_legacy_definitive_rejection_inventory_remains_uncertain(config, status, reason):
    """Conservative interim result; not parity with the old definitive outcomes."""
    from tests.test_scoped_v2_legacy_matrix import verified_job
    _, payload = verified_job()
    seen = []
    def respond(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(status, json={'error': reason})
    client = StagingClient(replace(config, protocol_mode='scoped-v2'), test_transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(StagingClientError) as exc:
            await client.submit(payload)
        assert type(exc.value) is StagingClientError
    finally:
        await client.aclose()
    assert seen == [('POST', '/v2/jobs')]


@pytest.mark.asyncio
@pytest.mark.parametrize('status,reason', [
    (409, 'worker_busy_or_held'), (409, 'worker_storage_capacity'),
    (409, 'resource_conflict'), (403, 'project_not_allowed'),
    (403, 'recipe_not_allowed'), (401, 'unauthorized'), (403, 'peer_not_allowed'),
])
async def test_actual_legacy_v1_submit_preserves_definitive_rejections(config, status, reason):
    seen = []
    def respond(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(status, json={'error': reason})
    client = StagingClient(config, test_transport=httpx.MockTransport(respond))
    payload = job(); del payload['reviewed_profile']
    try:
        with pytest.raises(StagingNotAccepted) as exc:
            await client.submit(payload)
        assert exc.value.reason == reason
    finally:
        await client.aclose()
    assert seen == [('POST', '/v1/jobs')]


def test_invalid_protocol_mode_fails_config_validation(config):
    with pytest.raises(StagingClientError):
        replace(config, protocol_mode='automatic')


def test_scoped_config_loads_explicitly_and_rejects_bad_values(tmp_path, config):
    target = tmp_path / 'configuration.json'
    data = {'schema': 1, 'origin': config.origin, 'credential_file': str(config.credential_file),
            'enabled': True, 'projects': {'demo': {'recipes': ['static']}}}
    def load():
        target.write_text(json.dumps(data))
        target.chmod(0o600)
        return load_optional_config(target)
    assert load()[0].protocol_mode == 'legacy'
    data['protocol_mode'] = 'scoped-v2'
    assert load()[0].protocol_mode == 'scoped-v2'
    for invalid in ('auto', True, None):
        data['protocol_mode'] = invalid
        assert load() == (None, {'state': 'invalid', 'enabled': False})
    data['protocol_mode'] = 'scoped-v2'
    data['origin'] = 'http://evil.example/path'
    assert load() == (None, {'state': 'invalid', 'enabled': False})


def test_scoped_client_rejects_malformed_credential_before_transport(tmp_path, config):
    token_file = tmp_path / 'bad-token'
    token_file.write_text('malformed token with spaces')
    token_file.chmod(0o600)
    calls = []
    with pytest.raises(StagingClientError):
        StagingClient(replace(config, protocol_mode='scoped-v2', credential_file=token_file),
                      test_transport=httpx.MockTransport(lambda request: calls.append(request)))
    assert calls == []


@pytest.mark.asyncio
async def test_scoped_observer_runtime_is_unavailable_without_legacy_network(config):
    from engineering_hub.staging_observer import StagingObserver
    seen = []
    observer = StagingObserver(replace(config, protocol_mode='scoped-v2'),
        test_transport=httpx.MockTransport(lambda request: seen.append(request)))
    try:
        assert await observer.runtime('human:1') == {'configured': True, 'state': 'unavailable', 'snapshot': None}
    finally:
        await observer.close()
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', [None, 'size', 'digest', 'binding', 'profile_generation', 'profile_digest'])
async def test_scoped_deposit_converts_exact_payload_and_validates_receipt(config, damage):
    source = make_source()
    source['profile_protocol'] = 'hub-staging-profile.v2'
    source['reviewed_profile'] = PROFILE.copy()
    raw = base64.b64decode(source['source_zip_base64'])
    binding = verify(raw, source['source_commit_object_base64'], source['source_commit'])
    result = {'source_id': source['source_id'], 'source_digest': source['source_digest'],
              'size_bytes': len(raw), 'project': 'demo', 'source_binding': binding, 'reviewed_profile': PROFILE.copy(),
              'state': 'historical_observation', 'authority': 'observation_only'}
    if damage == 'size': result['size_bytes'] += 1
    if damage == 'digest': result['source_digest'] = '0' * 64
    if damage == 'binding': result['source_binding'] = dict(binding, commit_oid='0' * 40)
    if damage == 'profile_generation': result['reviewed_profile']['generation'] = 2
    if damage == 'profile_digest': result['reviewed_profile']['digest'] = 'sha256:' + 'd' * 64
    seen = []
    def respond(request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=result)
    client = StagingClient(replace(config, protocol_mode='scoped-v2'), test_transport=httpx.MockTransport(respond))
    try:
        if damage:
            with pytest.raises(StagingClientError): await client.put_source(source)
        else:
            assert (await client.put_source(source)) == result
    finally:
        await client.aclose()
    assert seen == [('POST', '/v2/source-attempts', {k: v for k, v in source.items() if k != 'profile_protocol'})]


@pytest.mark.asyncio
async def test_scoped_deposit_post_commit_lost_response_is_not_reposted(config):
    source = dict(make_source(), profile_protocol='hub-staging-profile.v2', reviewed_profile=PROFILE.copy())
    committed = []
    def respond(request):
        committed.append((request.url.path, json.loads(request.content)))
        raise httpx.ReadError('synthetic response lost after commit', request=request)
    client = StagingClient(replace(config, protocol_mode='scoped-v2'), test_transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(StagingClientError): await client.put_source(source)
    finally:
        await client.aclose()
    assert len(committed) == 1 and committed[0][0] == '/v2/source-attempts'
    assert committed[0][1]['source_id'] == source['source_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['attempt', 'evidence'])
@pytest.mark.parametrize('damage', [None, 'generation', 'digest', 'recipe'])
async def test_scoped_source_observation_requires_exact_profile_and_original_binding(config, kind, damage):
    source = make_source()
    raw = base64.b64decode(source['source_zip_base64'])
    binding = verify(raw, source['source_commit_object_base64'], source['source_commit'])
    common = {'authority':'observation_only', 'source_id':source['source_id'], 'project':'demo',
              'source_digest':source['source_digest'], 'source_binding':binding, 'reviewed_profile':PROFILE.copy()}
    if kind == 'attempt':
        result = {'state':'historical_observation', **common}
    else:
        result = {'state':'present', 'binding_assurance':'journal_match_tree_checked', 'size_bytes':len(raw), **common}
    if damage == 'generation': result['reviewed_profile']['generation'] = 2
    if damage == 'digest': result['reviewed_profile']['digest'] = 'sha256:' + 'd' * 64
    if damage == 'recipe': result['reviewed_profile']['recipe'] = 'other'
    seen = []
    def respond(request):
        seen.append((request.method, request.url.path, dict(request.url.params)))
        return httpx.Response(200, json=result)
    client = StagingClient(replace(config, protocol_mode='scoped-v2'), test_transport=httpx.MockTransport(respond))
    try:
        observe = client.observe_source_attempt if kind == 'attempt' else client.observe_source_evidence
        if damage:
            with pytest.raises(StagingClientError): await observe(source['source_id'], 'demo', PROFILE, source['source_digest'], binding)
        else:
            assert await observe(source['source_id'], 'demo', PROFILE, source['source_digest'], binding) == result
    finally:
        await client.aclose()
    assert len(seen) == 1 and seen[0][0] == 'GET'
    assert seen[0][1] == '/v2/source-' + ('attempts/' if kind == 'attempt' else 'evidence/') + source['source_id']
    assert seen[0][2]['project'] == 'demo' and seen[0][2]['recipe'] == 'static'


@pytest.mark.asyncio
async def test_scoped_source_missing_recipe_grant_rejected_before_request(config):
    seen = []
    source = dict(make_source(), profile_protocol='hub-staging-profile.v2',
                  reviewed_profile=dict(PROFILE, recipe='other'))
    client = StagingClient(replace(config, protocol_mode='scoped-v2'),
                           test_transport=httpx.MockTransport(lambda request: seen.append(request)))
    try:
        with pytest.raises(StagingClientError): await client.put_source(source)
    finally:
        await client.aclose()
    assert seen == []


@pytest.mark.asyncio
async def test_legacy_mode_never_sends_profile_effect_to_old_ready_receiver(config):
    sent = []
    def respond(request):
        sent.append(request.url.path)
        return httpx.Response(200, json={'schema':'hub-staging.v2', 'active_command_id':None,
            'projects': {'demo':['static']}, 'profile_protocol':'hub-staging-profile.v2', 'profile_protocol_ready':True})
    client = StagingClient(config, test_transport=httpx.MockTransport(respond))
    try:
        assert (await client.status_v2())['profile_protocol_ready'] is True
        with pytest.raises(StagingClientError): await client.put_source(dict(make_source(), profile_protocol='hub-staging-profile.v2', reviewed_profile=PROFILE.copy()))
        with pytest.raises(StagingClientError): await client._submit_v2({})
    finally:
        await client.aclose()
    assert sent == ['/v2/status']


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != 'posix', reason='synthetic private credential fixture requires POSIX')
async def test_controller_denies_legacy_profile_before_creating_deposit_intent(tmp_path, monkeypatch):
    from engineering_hub import staging_controller
    from engineering_hub.repository import HubError
    from tests.test_hub_staging_wire_integration import Wire, request
    from tests.test_staging_profile_task_binding import confirmation
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    wire = Wire(tmp_path)
    try:
        enrolled = wire.resources.get_profile('human:1', 'engineering-hub', 'static')
        command = request(wire.repo)
        task = wire.repo.get_task(command['review_task_id'])
        candidate = wire.repo.set_candidate('codex', task['id'], task['revision'], command['source_commit'],
                                            {'recipe': 'static', 'generation': 1})
        reviewed = wire.repo.review_task('claude', task['id'], candidate['revision'], command['source_commit'],
                                         'approve', confirmation(enrolled))
        command['review_task_revision'] = reviewed['revision']
        with pytest.raises(HubError, match='staging_profile_protocol_unavailable'):
            await wire.controller.submit('codex', command)
        with wire.repo._connection() as db:
            assert db.execute('SELECT COUNT(*) FROM hub_staging_submissions').fetchone()[0] == 0
        assert not any(path.startswith('/v2/') for _, path in wire.received)
    finally:
        await wire.close()
