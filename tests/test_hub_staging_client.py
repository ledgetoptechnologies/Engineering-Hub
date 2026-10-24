"""Synthetic files and MockTransport only: never connects to a staging host."""
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import gzip
import json
import os
import ssl
import stat
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import httpx
import pytest

from engineering_hub.staging_client import (
    StagingClient, StagingClientError, StagingNotAccepted, StagingConnectionConfig, load_optional_config,
)


TOKEN = 'synthetic-private-worker-token-never-real'
KINDS = ('workspace', 'build', 'test_run', 'preview')


@pytest.fixture
def config(tmp_path):
    secret = tmp_path / 'worker-token'
    secret.write_text(TOKEN)
    secret.chmod(0o600)
    return StagingConnectionConfig('http://127.0.0.1:8780', secret,
        enabled=True, admin_actors=frozenset({'human:1'}), projects={'demo': {'recipes': ['static']}})


def status():
    return {'schema': 'hub-staging.v1', 'active_command_id': None, 'projects': {'demo': ['static']}}


def operation(job_id=None, command_id=None, result=None, state='succeeded'):
    return dict(command_id=command_id or str(uuid4()), job_id=job_id,
                state=state, result={} if result is None else result)


@pytest.mark.asyncio
async def test_cleanup_operation_read_is_fixed_get_with_original_command_and_no_replay(config):
    command, resource = str(uuid4()), str(uuid4())
    original = operation(command_id=command, result={'deleted_resource_ids': [resource]})
    requests = []
    def respond(request):
        requests.append(request)
        assert request.method == 'GET' and request.url.path == '/v1/operations/' + command
        assert request.headers['authorization'] == 'Bearer ' + TOKEN
        assert not request.content
        return httpx.Response(200, json=original)
    client = StagingClient(config, test_transport=httpx.MockTransport(respond))
    try:
        assert await client.operation(command) == original
        assert len(requests) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [
    lambda command: httpx.Response(404, json={'error': 'operation_not_found'}),
    lambda command: httpx.Response(200, json=operation(command_id=str(uuid4()), result={'deleted_resource_ids': [str(uuid4())]})),
    lambda command: httpx.Response(200, json=operation(job_id=str(uuid4()), command_id=command)),
    lambda command: httpx.Response(200, json=operation(command_id=command, result={'preview_ready': True})),
    lambda command: httpx.Response(200, json=operation(command_id=command, result={})),
])
async def test_cleanup_operation_missing_wrong_command_or_job_shape_is_closed(config, response):
    command, requests = str(uuid4()), []
    def respond(request):
        requests.append(request)
        return response(command)
    client = StagingClient(config, test_transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(StagingClientError):
            await client.operation(command)
        assert len(requests) == 1 and requests[0].method == 'GET'
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cleanup_operation_invalid_id_never_reaches_transport(config):
    client = StagingClient(config, test_transport=httpx.MockTransport(lambda request: pytest.fail('network')))
    try:
        with pytest.raises(StagingClientError):
            await client.operation('../cleanup')
    finally:
        await client.aclose()


def cleanup_payload():
    return {'command_id': str(uuid4()), 'idempotency_key': 'synthetic-cleanup',
            'resources': [{'resource_id': str(uuid4()), 'expected_revision': 1}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('method,status,reason', [
    ('submit', 409, 'worker_busy_or_held'), ('submit', 409, 'worker_storage_capacity'),
    ('submit', 409, 'resource_conflict'), ('submit', 403, 'project_not_allowed'),
    ('submit', 403, 'recipe_not_allowed'), ('submit', 401, 'unauthorized'),
    ('submit', 403, 'peer_not_allowed'), ('cleanup', 409, 'worker_busy'),
    ('cleanup', 409, 'cleanup_revision_or_reference'), ('cleanup', 409, 'resource_job_not_terminal'),
    ('cleanup', 401, 'unauthorized'), ('cleanup', 403, 'peer_not_allowed'),
])
async def test_exact_known_post_rejection_is_safe_subtype_without_retry(config, method, status, reason):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={'error': reason})
    client = StagingClient(config, test_transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(StagingNotAccepted) as error:
            await getattr(client, method)(job() if method == 'submit' else cleanup_payload())
        assert error.value.reason == reason
        assert error.value.code == 'staging_worker_rejected' and error.value.status == 409
        assert str(error.value) == 'staging_worker_rejected'
        assert TOKEN not in repr(error.value)
        assert len(requests) == 1 and requests[0].method == 'POST'
    finally:
        await client.aclose()


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
async def test_conflict_unknown_wrong_status_or_wrong_route_remains_uncertain(config, method, status, body):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, json=body)
    client = StagingClient(config, test_transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(StagingClientError) as error:
            await getattr(client, method)(job() if method == 'submit' else cleanup_payload())
        assert type(error.value) is StagingClientError
        assert TOKEN not in str(error.value) and len(requests) == 1
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
async def test_rejection_subtype_requires_full_bounded_validated_private_response(config, response):
    requests = []
    def respond(request):
        requests.append(request)
        return response()
    client = StagingClient(config, test_transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(StagingClientError) as error:
            await client.submit(job())
        assert type(error.value) is StagingClientError
        assert TOKEN not in str(error.value) and 'foreign.invalid' not in str(error.value)
        assert len(requests) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_known_error_get_source_and_file_responses_remain_opaque(config):
    requests = []
    def respond(request):
        requests.append(request)
        if '/files/' in request.url.path:
            return httpx.Response(404, content=b'not found', headers={'content-type': 'text/html'})
        return httpx.Response(401, json={'error': 'unauthorized'})
    client = StagingClient(config, test_transport=httpx.MockTransport(respond))
    try:
        for invoke in (client.status, lambda: client.put_source(source()),
                       lambda: client.operation(str(uuid4())), lambda: client.file(str(uuid4()), 'index.html')):
            with pytest.raises(StagingClientError) as error:
                await invoke()
            assert type(error.value) is StagingClientError
        assert len(requests) == 4
    finally:
        await client.aclose()


def test_not_accepted_constructor_never_accepts_unvalidated_reason():
    for reason in (TOKEN, 'unknown_worker_error', {}, None):
        with pytest.raises(StagingClientError) as error:
            StagingNotAccepted(reason)
        assert type(error.value) is StagingClientError
        assert TOKEN not in str(error.value)


@pytest.mark.asyncio
async def test_plaintext_channel_policy_keeps_even_valid_rejection_opaque(config):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(409, json={'error': 'worker_busy_or_held'})
    client = StagingClient(config, test_transport=httpx.MockTransport(respond))
    assert client._definitive_rejections is True  # Explicit synthetic-only exception.
    # Exercise the exact gate used by real plaintext-loopback clients, without
    # opening a real connection or weakening the private credential reader.
    client._definitive_rejections = False
    try:
        with pytest.raises(StagingClientError) as error:
            await client.submit(job())
        assert type(error.value) is StagingClientError and len(requests) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_accepted_held_operation_receipt_never_becomes_not_accepted(config):
    payload = cleanup_payload()
    original = operation(command_id=payload['command_id'], state='held',
                         result={'error': 'cleanup_uncertain'})
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=original)
    client = StagingClient(config, test_transport=httpx.MockTransport(respond))
    try:
        assert await client.cleanup(payload) == original
        assert len(requests) == 1
    finally:
        await client.aclose()


def source():
    raw = b'Synthetic bytes; archive validation belongs to worker'
    return dict(source_id=str(uuid4()), project='demo', source_digest=hashlib.sha256(raw).hexdigest(),
                source_zip_base64=base64.b64encode(raw).decode())


def job():
    return dict(job_id=str(uuid4()), command_id=str(uuid4()), idempotency_key='fixture-submit',
        project='demo', recipe='static', source_commit='a' * 40, source_digest='b' * 64,
        source_id=str(uuid4()), resource_ids={kind: str(uuid4()) for kind in KINDS})


@pytest.mark.parametrize('origin', ['http://192.168.50.90:8780', 'https://8.8.8.8',
    'https://worker.example', 'https://192.168.50.90/path', 'https://u:p@192.168.50.90',
    'https://192.168.50.90?x=1', 'https://192.168.50.90#x', 'http://127.0.0.2',
    'https://0.0.0.0', 'https://224.0.0.1', 'https://[fe80::1%25eth0]'])
def test_origin_is_explicit_numeric_private_tls_or_exact_loopback_http(config, origin):
    with pytest.raises(StagingClientError):
        replace(config, origin=origin, ca_file=config.credential_file)


def test_secure_origin_requires_explicit_ca_and_admin_is_never_first_user(config):
    with pytest.raises(StagingClientError):
        replace(config, origin='https://192.168.50.90:8780')
    with pytest.raises(StagingClientError):
        replace(config, admin_actors=frozenset({'codex'}))
    with pytest.raises(StagingClientError):
        replace(config, auto_run=True, enabled=False)
    assert StagingConnectionConfig('http://[::1]:8780', config.credential_file).admin_actors == frozenset()
    assert str(config.credential_file) not in repr(config)
    assert TOKEN not in repr(config)


def test_optional_config_is_nonfatal_closed_and_does_not_read_credentials(tmp_path, config):
    target = tmp_path / 'staging-service.json'
    assert load_optional_config(target) == (None, {'state': 'not_configured', 'enabled': False})
    raw = dict(schema=1, origin='http://127.0.0.1:8780', credential_file=str(tmp_path / 'missing-token'),
               enabled=True, admin_actors=['human:1'], projects={'demo': {'recipes': ['static']}})
    target.write_text(json.dumps(raw)); target.chmod(0o600)
    loaded, safe = load_optional_config(target)
    assert loaded.credential_file == tmp_path / 'missing-token'
    assert safe == {'state': 'configured', 'enabled': True}
    assert loaded.admin_actors == frozenset({'human:1'})
    raw['unknown'] = TOKEN
    target.write_text(json.dumps(raw))
    loaded, safe = load_optional_config(target)
    assert loaded is None and safe == {'state': 'invalid', 'enabled': False}
    assert TOKEN not in str(safe) and str(target) not in str(safe)
    target.write_text('{"schema":1,"schema":1}')
    assert load_optional_config(target)[0] is None


def test_disabled_missing_secret_invalid_secret_and_symlink_deny_before_network(tmp_path, config):
    requests = []
    transport = httpx.MockTransport(lambda request: requests.append(request))
    for candidate in (replace(config, enabled=False), replace(config, credential_file=tmp_path / 'missing')):
        with pytest.raises(StagingClientError) as error:
            StagingClient(candidate, test_transport=transport)
        assert TOKEN not in str(error.value) and str(tmp_path) not in str(error.value)
    config.credential_file.write_text('short')
    with pytest.raises(StagingClientError):
        StagingClient(config, test_transport=transport)
    config.credential_file.write_text(TOKEN)
    link = tmp_path / 'link'
    try:
        link.symlink_to(config.credential_file)
    except OSError:
        if os.name == 'posix':
            raise
    else:
        with pytest.raises(StagingClientError):
            StagingClient(replace(config, credential_file=link), test_transport=transport)
    assert requests == []


@pytest.mark.skipif(os.name != 'posix', reason='Production POSIX file ownership/modes')
@pytest.mark.parametrize('mode', [0o644, 0o620, 0o660, 0o666])
def test_nonprivate_credential_mode_denied(config, mode):
    config.credential_file.chmod(mode)
    with pytest.raises(StagingClientError):
        StagingClient(config, test_transport=httpx.MockTransport(lambda request: pytest.fail('network')))


def test_https_uses_configured_ca_only(config, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Synthetic staging CA')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256()))
    ca = tmp_path / 'ca.pem'
    ca.write_bytes(certificate.public_bytes(serialization.Encoding.PEM)); ca.chmod(0o600)
    client = StagingClient(replace(config, origin='https://192.168.50.90:8780', ca_file=ca),
                           test_transport=httpx.MockTransport(lambda request: httpx.Response(200, json=status())))
    # Construction validates PEM without opening a network connection.
    assert client._client.is_closed is False
    import asyncio
    asyncio.run(client.aclose())
    ca.write_text('not a CA')
    with pytest.raises(StagingClientError):
        StagingClient(replace(config, origin='https://192.168.50.90', ca_file=ca),
                      test_transport=httpx.MockTransport(lambda request: pytest.fail('network')))


@pytest.mark.asyncio
async def test_owned_client_has_no_default_cookie_auth_redirect_or_environment_merging(config, monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://must-not-be-used.invalid')
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url == httpx.URL(config.origin + '/v1/status')
        assert request.headers['authorization'] == 'Bearer ' + TOKEN
        assert 'cookie' not in request.headers and 'cf-access-jwt-assertion' not in request.headers
        assert request.headers['accept-encoding'] == 'identity'
        return httpx.Response(200, json=status())
    client = StagingClient(config, test_transport=httpx.MockTransport(handle))
    client._client.cookies.set('human-session', 'should-never-merge')
    client._client.headers['X-Human'] = 'should-never-merge'
    assert await client.status() == status()
    assert 'x-human' not in requests[0].headers and len(requests) == 1
    assert not client._client.cookies
    await client.aclose()
    assert client._client.is_closed and client._token == ''
    with pytest.raises(StagingClientError):
        await client.status()


@pytest.mark.asyncio
async def test_source_submit_job_metadata_file_cleanup_match_fixed_worker_contract(config):
    payload, source_payload = job(), source()
    resource_id = payload['resource_ids']['preview']
    cleanup = dict(command_id=str(uuid4()), idempotency_key='cleanup',
                   resources=[{'resource_id': resource_id, 'expected_revision': 1}])
    seen = []
    def handle(request):
        seen.append((request.method, request.url.path))
        body = json.loads(request.content) if request.content else None
        if request.url.path == '/v1/sources':
            assert body == source_payload and request.extensions['timeout']['write'] == 30
            return httpx.Response(200, json={key: source_payload[key] for key in ('source_id', 'source_digest', 'project')} | {'size_bytes': 53})
        if request.url.path == '/v1/jobs':
            assert body == payload
            return httpx.Response(202, json=operation(payload['job_id'], payload['command_id'], state='accepted'))
        if request.url.path == '/v1/jobs/' + payload['job_id']:
            return httpx.Response(200, json=operation(payload['job_id'], payload['command_id']))
        if request.url.path.endswith('/metadata'):
            return httpx.Response(200, json=dict(id=resource_id, job_id=payload['job_id'], kind='preview',
                revision=1, state='ready', inode=10, manifest={'index.html': {'bytes': 4, 'sha256': 'c' * 64}}, bytes=4, refs=[]))
        if request.url.path.endswith('/files/index.html'):
            return httpx.Response(200, content=b'demo', headers={'content-type': 'text/html'})
        if request.url.path == '/v1/cleanup':
            assert body == cleanup
            return httpx.Response(200, json=operation(command_id=cleanup['command_id'],
                result={'deleted_resource_ids': [resource_id]}))
        pytest.fail('Unexpected route')
    client = StagingClient(config, test_transport=httpx.MockTransport(handle))
    try:
        assert (await client.put_source(source_payload))['source_id'] == source_payload['source_id']
        assert (await client.submit(payload))['state'] == 'accepted'
        assert (await client.job(payload['job_id']))['command_id'] == payload['command_id']
        assert (await client.resource(resource_id))['id'] == resource_id
        assert await client.file(resource_id, 'index.html') == (b'demo', 'text/html')
        assert (await client.cleanup(cleanup))['state'] == 'succeeded'
        assert len(seen) == 6
    finally:
        await client.aclose()


@pytest.mark.parametrize('response', [
    httpx.Response(302, headers={'location': 'https://evil.example'}),
    httpx.Response(401, text=TOKEN), httpx.Response(200, json=status(), headers={'set-cookie': 'secret=value'}),
    httpx.Response(200, content=gzip.compress(b'{}'), headers={'content-encoding': 'gzip'}),
    httpx.Response(200, content=b'{}', headers={'content-type': 'text/html'}),
    httpx.Response(200, content=b'{}', headers={'content-type': 'application/json', 'content-length': str(16 * 1024**2 + 1)}),
    httpx.Response(200, content=b'{"duplicate":1,"duplicate":2}', headers={'content-type': 'application/json'}),
    httpx.Response(200, content=b'{"secret":"' + TOKEN.encode() + b'"}', headers={'content-type': 'application/json'}),
], ids=['redirect', 'unauthorized', 'cookie', 'encoding', 'mime', 'declared-size', 'duplicate-json', 'token-reflection'])
@pytest.mark.asyncio
async def test_untrusted_response_is_opaque_and_never_retried(config, response):
    calls = []
    def handler(request):
        calls.append(request)
        return response
    client = StagingClient(config, test_transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(StagingClientError) as error:
            await client.status()
        assert TOKEN not in str(error.value) and 'evil.example' not in str(error.value)
        assert len(calls) == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize('bad', [{'schema': 'wrong', 'projects': {}, 'active_command_id': None},
    {'schema': 'hub-staging.v1', 'projects': {'demo': [{}]}, 'active_command_id': None},
    {'schema': 'hub-staging.v1', 'projects': {'demo': ['static']}, 'active_command_id': 'not-uuid'},
    {'schema': 'hub-staging.v1', 'projects': {'demo': ['static']}, 'active_command_id': None, 'secret': 'x'}])
@pytest.mark.asyncio
async def test_malformed_status_is_closed_and_sanitized(config, bad):
    client = StagingClient(config, test_transport=httpx.MockTransport(lambda request: httpx.Response(200, json=bad)))
    try:
        with pytest.raises(StagingClientError):
            await client.status()
    finally:
        await client.aclose()


@pytest.mark.parametrize('path', ['../index.html', '/index.html', 'http://evil/index.html',
    'a//b.js', 'a/./b.js', 'index.html?x=1', 'index.html#x', 'a\\b.js', 'x%2f.html', 'a\x00.html'])
@pytest.mark.asyncio
async def test_invalid_file_path_never_reaches_transport(config, path):
    client = StagingClient(config, test_transport=httpx.MockTransport(lambda request: pytest.fail('network')))
    try:
        with pytest.raises(StagingClientError):
            await client.file(str(uuid4()), path)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_input_schema_and_project_recipe_scope_deny_before_transport(config):
    client = StagingClient(config, test_transport=httpx.MockTransport(lambda request: pytest.fail('network')))
    try:
        for changes in ({'project': 'other'}, {'recipe': 'other'}, {'source_commit': 'main'},
                        {'extra': 'not-allowed'}, {'job_id': 'https://evil'}, {'resource_ids': {}}):
            with pytest.raises(StagingClientError):
                await client.submit(job() | changes)
        with pytest.raises(StagingClientError):
            await client.resource('../not-uuid')
        with pytest.raises(StagingClientError):
            await client.cleanup({'command_id': str(uuid4()), 'idempotency_key': 'x', 'resources': []})
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_transport_exception_and_mismatched_job_are_safe(config):
    def broken(request):
        raise httpx.ConnectError('SECRET token and private server path')
    client = StagingClient(config, test_transport=httpx.MockTransport(broken))
    try:
        with pytest.raises(StagingClientError) as error:
            await client.status()
        assert 'SECRET' not in str(error.value)
    finally:
        await client.aclose()
    client = StagingClient(config, test_transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=operation(str(uuid4())))))
    try:
        with pytest.raises(StagingClientError):
            await client.job(str(uuid4()))
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_streamed_size_is_bounded_even_without_declared_length(config):
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'x' * 128
            yield b'x' * 128
    client = StagingClient(config, test_transport=httpx.MockTransport(lambda request:
        httpx.Response(200, stream=Chunks(), headers={'content-type': 'text/html'})))
    client.MAX_RESPONSE_BYTES = 128
    try:
        with pytest.raises(StagingClientError):
            await client.file(str(uuid4()), 'index.html')
    finally:
        await client.aclose()


@pytest.mark.parametrize('change', ['unknown-field', 'wrong-id', 'wrong-kind', 'wrong-state',
                                    'boolean-revision', 'bad-hash', 'traversal-manifest', 'size-mismatch'])
@pytest.mark.asyncio
async def test_metadata_identity_manifest_and_shape_are_validated(config, change):
    identity = str(uuid4())
    metadata = dict(id=identity, job_id=str(uuid4()), kind='preview', revision=1,
        state='ready', inode=10, manifest={'index.html': {'bytes': 4, 'sha256': 'a' * 64}}, bytes=4, refs=[])
    if change == 'unknown-field':
        metadata['host_path'] = 'PRIVATE-worker-file'
    elif change == 'wrong-id':
        metadata['id'] = str(uuid4())
    elif change == 'wrong-kind':
        metadata['kind'] = ['preview']
    elif change == 'wrong-state':
        metadata['state'] = 'arbitrary'
    elif change == 'boolean-revision':
        metadata['revision'] = True
    elif change == 'bad-hash':
        metadata['manifest']['index.html']['sha256'] = 'not-a-digest'
    elif change == 'traversal-manifest':
        metadata['manifest']['../index.html'] = metadata['manifest'].pop('index.html')
    elif change == 'size-mismatch':
        metadata['bytes'] = 99
    client = StagingClient(config, test_transport=httpx.MockTransport(lambda request: httpx.Response(200, json=metadata)))
    try:
        with pytest.raises(StagingClientError) as error:
            await client.resource(identity)
        assert 'PRIVATE' not in str(error.value)
    finally:
        await client.aclose()
