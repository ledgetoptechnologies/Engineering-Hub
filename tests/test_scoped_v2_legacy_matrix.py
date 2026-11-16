"""Preserve old client security-case coverage for scoped mode; MockTransport only.

The historical definitive rejections are intentionally uncertain in scoped
mode until the original receiver/epoch and first-dispatch fence are proven.
These tests are not evidence of a live receiver or execution fence.
"""
import base64
from dataclasses import replace
import json
from uuid import uuid4

import httpx
import pytest

from engineering_hub.staging_client import StagingClient, StagingClientError, StagingNotAccepted
from hub_staging_service.source_provenance import verify
from hub_staging_service.core import recipe
from hub_staging_service.modular_staging_profile import from_worker_recipe, profile_digest
from hub_staging_service.profile_binding import checked_recipe_snapshot
from tests.test_hub_staging_client import TOKEN, cleanup_payload, config, job, operation
from tests.test_source_evidence import make_source
from tests.test_staging_service_core import profile as worker_profile


PROFILE = {'project': 'demo', 'recipe': 'static', 'generation': 1, 'digest': 'sha256:' + 'c' * 64}


def verified_job(source=None):
    """Consistent source, tree proof and worker-recipe snapshot; still no server admission."""
    source = make_source() if source is None else source
    binding = verify(base64.b64decode(source['source_zip_base64']),
                     source['source_commit_object_base64'], source['source_commit'])
    effective = recipe(worker_profile())
    reviewed = {'project':'demo', 'recipe':'static', 'generation':1,
                'digest':profile_digest(from_worker_recipe(effective))}
    checked_recipe_snapshot('demo', 'static', reviewed, effective)
    payload = job()
    payload.update(source_id=source['source_id'], source_digest=source['source_digest'],
                   source_commit=source['source_commit'], expected_source_binding=binding,
                   reviewed_profile=reviewed)
    return source, payload


@pytest.mark.asyncio
@pytest.mark.parametrize('status,reason', [
    (409, 'worker_busy'), (409, 'cleanup_revision_or_reference'),
    (409, 'resource_job_not_terminal'), (401, 'unauthorized'), (403, 'peer_not_allowed'),
])
@pytest.mark.parametrize('scoped', [False, True])
async def test_cleanup_definitive_contract_distinguishes_legacy_and_scoped(config, status, reason, scoped):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={'error': reason})
    client = StagingClient(replace(config, protocol_mode='scoped-v2' if scoped else 'legacy'),
                           test_transport=httpx.MockTransport(respond))
    try:
        expected = StagingClientError if scoped else StagingNotAccepted
        with pytest.raises(expected) as error:
            await client.cleanup(cleanup_payload())
        assert type(error.value) is expected
        if not scoped:
            assert error.value.reason == reason
    finally:
        await client.aclose()
    assert len(requests) == 1 and requests[0].url.path == ('/v2/cleanup' if scoped else '/v1/cleanup')


@pytest.mark.asyncio
@pytest.mark.parametrize('method,status,body', [
    ('submit', 409, {'error': 'idempotency_conflict'}),
    ('submit', 409, {'error': 'job_conflict'}),
    ('submit', 409, {'error': 'execution_uncertain'}),
    ('submit', 500, {'error': 'worker_busy_or_held'}),
    ('submit', 400, {'error': 'worker_busy_or_held'}),
    ('submit', 409, {'error': 'worker_busy'}),
    ('cleanup', 409, {'error': 'worker_busy_or_held'}),
    ('cleanup', 409, {'error': 'cleanup_uncertain'}),
    ('cleanup', 409, {'error': 'idempotency_conflict'}),
    ('cleanup', 409, {'error': 'resource_conflict'}),
    ('cleanup', 409, {'error': 'worker_busy', 'private': TOKEN}),
    ('cleanup', 409, {'error': []}),
])
async def test_scoped_unknown_wrong_status_or_wrong_route_is_uncertain(config, method, status, body):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, json=body)
    client = StagingClient(replace(config, protocol_mode='scoped-v2'), test_transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(StagingClientError) as error:
            await getattr(client, method)(verified_job()[1] if method == 'submit' else cleanup_payload())
        assert type(error.value) is StagingClientError
        assert TOKEN not in str(error.value) and len(requests) == 1
        assert requests[0].url.path == ('/v2/jobs' if method == 'submit' else '/v2/cleanup')
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [
    lambda: httpx.Response(409, json={'error': 'worker_busy_or_held'}, headers={'set-cookie': 'session=x'}),
    lambda: httpx.Response(409, json={'error': 'worker_busy_or_held'}, headers={'location': 'https://foreign.invalid'}),
    lambda: httpx.Response(409, json={'error': 'worker_busy_or_held'}, headers={'content-type': 'text/html'}),
    lambda: httpx.Response(409, content=b'{"error":"worker_busy_or_held","error":"worker_busy_or_held"}', headers={'content-type': 'application/json'}),
    lambda: httpx.Response(409, content=b'{"error":"worker_busy_or_held"', headers={'content-type': 'application/json'}),
    lambda: httpx.Response(409, json={'error': 'worker_busy_or_held'}, headers={'content-length': '1'}),
    lambda: httpx.Response(409, json={'error': 'worker_busy_or_held'}, headers={'content-length': str(16 * 1024**2 + 1)}),
    lambda: httpx.Response(409, json={'error': 'worker_busy_or_held'}, headers={'content-encoding': 'gzip'}),
    lambda: httpx.Response(409, json={'error': 'worker_busy_or_held'}, headers={'x-reflected-token': TOKEN}),
    lambda: httpx.Response(409, json={'error': TOKEN}),
    lambda: httpx.Response(302, json={'error': 'worker_busy_or_held'}, headers={'location': 'https://foreign.invalid'}),
])
async def test_scoped_rejection_response_is_bounded_opaque_and_never_retried(config, response):
    requests = []
    def respond(request):
        requests.append(request)
        return response()
    client = StagingClient(replace(config, protocol_mode='scoped-v2'), test_transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(StagingClientError) as error:
            await client.submit(verified_job()[1])
        assert type(error.value) is StagingClientError
        assert TOKEN not in str(error.value) and 'foreign.invalid' not in str(error.value)
        assert len(requests) == 1 and requests[0].url.path == '/v2/jobs'
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_scoped_get_source_file_known_errors_remain_opaque(config):
    requests = []
    def respond(request):
        requests.append(request)
        if '/files/' in request.url.path:
            return httpx.Response(404, content=b'not found', headers={'content-type': 'text/html'})
        return httpx.Response(401, json={'error': 'unauthorized'})
    client = StagingClient(replace(config, protocol_mode='scoped-v2'), test_transport=httpx.MockTransport(respond))
    source = dict(make_source(), reviewed_profile=PROFILE, profile_protocol='hub-staging-profile.v2')
    try:
        for invoke in (client.status, lambda: client.put_source(source),
                       lambda: client.operation(str(uuid4())), lambda: client.file(str(uuid4()), 'index.html')):
            with pytest.raises(StagingClientError) as error:
                await invoke()
            assert type(error.value) is StagingClientError
        assert len(requests) == 4
        assert all(request.url.path.startswith('/v2/') for request in requests)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_scoped_plaintext_channel_policy_keeps_valid_rejection_opaque(config):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(409, json={'error': 'worker_busy_or_held'})
    client = StagingClient(replace(config, protocol_mode='scoped-v2'), test_transport=httpx.MockTransport(respond))
    client._definitive_rejections = False
    try:
        with pytest.raises(StagingClientError) as error:
            await client.submit(verified_job()[1])
        assert type(error.value) is StagingClientError and len(requests) == 1
        assert requests[0].url.path == '/v2/jobs'
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_scoped_source_submit_read_file_cleanup_fixed_contract(config):
    raw_source, payload = verified_job()
    source = dict(raw_source, reviewed_profile=payload['reviewed_profile'], profile_protocol='hub-staging-profile.v2')
    binding = payload['expected_source_binding']
    size = len(base64.b64decode(source['source_zip_base64']))
    resource_id = payload['resource_ids']['preview']
    cleanup = dict(command_id=str(uuid4()), idempotency_key='cleanup',
                   resources=[{'resource_id': resource_id, 'expected_revision': 1}])
    seen = []
    def respond(request):
        seen.append((request.method, request.url.path))
        body = json.loads(request.content) if request.content else None
        if request.url.path == '/v2/source-attempts':
            assert body == {key: value for key, value in source.items() if key != 'profile_protocol'}
            assert request.extensions['timeout']['write'] == 30
            return httpx.Response(200, json={key: source[key] for key in ('source_id', 'source_digest', 'project', 'reviewed_profile')}
                | {'size_bytes': size, 'state':'historical_observation', 'authority':'observation_only', 'source_binding':binding})
        if request.url.path == '/v2/jobs':
            assert body == payload
            return httpx.Response(202, json=operation(payload['job_id'], payload['command_id'], state='accepted'))
        if request.url.path == '/v2/jobs/' + payload['job_id']:
            return httpx.Response(200, json=operation(payload['job_id'], payload['command_id']))
        if request.url.path == '/v2/resources/' + resource_id + '/metadata':
            return httpx.Response(200, json=dict(id=resource_id, job_id=payload['job_id'], kind='preview',
                revision=1, state='ready', inode=10, manifest={'index.html': {'bytes': 4, 'sha256': 'c' * 64}}, bytes=4, refs=[]))
        if request.url.path == '/v2/resources/' + resource_id + '/files/index.html':
            return httpx.Response(200, content=b'demo', headers={'content-type':'text/html'})
        if request.url.path == '/v2/cleanup':
            assert body == cleanup
            return httpx.Response(200, json=operation(command_id=cleanup['command_id'], result={'deleted_resource_ids':[resource_id]}))
        pytest.fail('Unexpected route')
    client = StagingClient(replace(config, protocol_mode='scoped-v2'), test_transport=httpx.MockTransport(respond))
    try:
        assert (await client.put_source(source))['source_id'] == source['source_id']
        assert (await client.submit(payload))['state'] == 'accepted'
        assert (await client.job(payload['job_id']))['command_id'] == payload['command_id']
        assert (await client.resource(resource_id))['id'] == resource_id
        assert await client.file(resource_id, 'index.html') == (b'demo', 'text/html')
        assert (await client.cleanup(cleanup))['state'] == 'succeeded'
        assert len(seen) == 6 and all(path.startswith('/v2/') for _, path in seen)
    finally:
        await client.aclose()
