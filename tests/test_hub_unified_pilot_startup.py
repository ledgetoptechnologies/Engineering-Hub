"""Explicit selector parsing, protected files and TLS; no NAS configuration."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import sys
from uuid import uuid4

import httpx
import pytest
from tests.manual_session_fixtures import synthetic_reconciliation
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from engineering_hub import unified_pilot_startup as startup
from engineering_hub.container_pilot_client import ContainerPilotClient
from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError
from engineering_hub.repository import HubRepository
from tests.test_hub_container_pilot import pilot_manifest


def run_owned_loop(coroutine):
    # Explicit ownership: Runner closes only its new loop, without replacing or
    # clearing pytest-asyncio's ambient policy loop (and orphaning its sockets).
    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        return runner.run(coroutine)


def test_owned_runner_preserves_ambient_policy_loop():
    previous_policy = asyncio.get_event_loop_policy()
    policy = asyncio.DefaultEventLoopPolicy()
    retained = policy.new_event_loop()
    policy.set_event_loop(retained)
    asyncio.set_event_loop_policy(policy)
    try:
        assert run_owned_loop(asyncio.sleep(0, result='done')) == 'done'
        assert policy.get_event_loop() is retained and not retained.is_closed()
    finally:
        retained.close()
        policy.set_event_loop(None)
        asyncio.set_event_loop_policy(previous_policy)


TASK = '00000000-0000-4000-8000-000000000001'
LINUX = pytest.mark.skipif(sys.platform != 'linux', reason='Current-UID Linux file/ownership guards')


def profile_document():
    return {'schema': startup.SCHEMA, 'manifest': json.loads(pilot_manifest().canonical_bytes()),
        'machine_id': 'staging-pc', 'action_key': startup.ACTION, 'allowed_actors': ['codex'],
        'allowed_task_ids': [TASK], 'executor': {'https_origin': 'https://executor.example:9443',
            'ca_file': '/operator/ca.pem', 'ca_sha256': 'sha256:' + 'a' * 64,
            'credential_file': '/operator/secret/token', 'credential_sha256': 'sha256:' + 'b' * 64},
        'supervisor_stop_grace_seconds': 2000}


def encode(document):
    return json.dumps(document, sort_keys=True, separators=(',', ':')).encode()


def certificate(name='localhost'):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM), key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def test_pure_profile_is_closed_and_exact_scope_without_secret_resolution(monkeypatch):
    monkeypatch.setattr(startup, 'read_protected_file', lambda *_a, **_k: pytest.fail('File read'))
    parsed = startup.parse_profile(encode(profile_document()))
    assert parsed.machine_id == 'staging-pc' and parsed.actors == {'codex'} and parsed.task_ids == {TASK}
    assert parsed.manifest.target_id == 'engineering-hub:staging'
    assert 'credential' not in repr(parsed)


@pytest.mark.parametrize('change', ['extra', 'inline-token', 'duplicate', 'http', 'url-user',
    'url-path', 'ca-unpinned', 'secret-unpinned', 'bad-task', 'duplicate-task', 'bad-actor',
    'production', 'bad-machine', 'short-grace', 'float'])
def test_invalid_profile_denied_without_private_fragments(change):
    data = profile_document()
    if change == 'extra': data['unexpected-private'] = 'private-token-value'
    elif change == 'inline-token': data['executor']['token'] = 'private-token-value'
    elif change == 'http': data['executor']['https_origin'] = 'http://executor.example'
    elif change == 'url-user': data['executor']['https_origin'] = 'https://private-token-value@executor.example'
    elif change == 'url-path': data['executor']['https_origin'] += '/commands'
    elif change == 'ca-unpinned': data['executor']['ca_sha256'] = 'unknown'
    elif change == 'secret-unpinned': data['executor']['credential_sha256'] = 'unknown'
    elif change == 'bad-task': data['allowed_task_ids'] = ['invalid']
    elif change == 'duplicate-task': data['allowed_task_ids'] = [TASK, TASK]
    elif change == 'bad-actor': data['allowed_actors'] = ['human:42']
    elif change == 'production': data['action_key'] = 'engineering-hub:production'
    elif change == 'bad-machine': data['machine_id'] = 'production-pc'
    elif change == 'short-grace': data['supervisor_stop_grace_seconds'] = 30
    elif change == 'float': data['supervisor_stop_grace_seconds'] = 2000.0
    raw = encode(data)
    if change == 'duplicate': raw = raw[:-1] + b',"schema":"private-token-value"}'
    with pytest.raises(startup.UnifiedPilotStartupError) as error:
        startup.parse_profile(raw)
    assert str(error.value) == startup.ERROR


def test_verified_https_uses_captured_ca_and_ignores_all_environment_trust(monkeypatch, tmp_path):
    ca, _ = certificate()
    keylog = tmp_path / 'keylog'
    for key, value in {'SSLKEYLOGFILE': str(keylog), 'SSL_CERT_FILE': '/private/evil.pem',
        'SSL_CERT_DIR': '/private/evil', 'HTTPS_PROXY': 'http://private:9999',
        'ALL_PROXY': 'http://private:9999'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ssl, 'create_default_context', lambda *a, **k: pytest.fail('Default trust discovery'))
    captured = {}
    real_transport = httpx.AsyncHTTPTransport
    def transport(**kwargs):
        captured.update(kwargs)
        return real_transport(**kwargs)
    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', transport)
    client = ContainerPilotClient.for_verified_https('https://executor.example:9443', 'private-test-token', ca)
    try:
        context = captured['verify']
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
        assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
        assert context.keylog_filename is None and len(context.get_ca_certs()) == 1
        assert captured['retries'] == 0 and captured['trust_env'] is False
        assert not keylog.exists() and 'private-test-token' not in repr(client)
    finally:
        run_owned_loop(client.aclose())


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['match', 'wrong-ca', 'wrong-host'])
async def test_real_tls_certificate_chain_and_hostname(case, tmp_path):
    ca, key = certificate()
    cert_path, key_path = tmp_path / 'certificate.pem', tmp_path / 'key.pem'
    cert_path.write_bytes(ca)
    key_path.write_bytes(key)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    reached = []
    async def handler(reader, writer):
        try:
            await reader.readuntil(b'\r\n\r\n')
            reached.append(True)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    listener = await asyncio.start_server(handler, '127.0.0.1', 0, ssl=server_context)
    port = listener.sockets[0].getsockname()[1]
    hostname = '127.0.0.1' if case == 'wrong-host' else 'localhost'
    trusted = certificate('other-ca')[0] if case == 'wrong-ca' else ca
    client = ContainerPilotClient.for_verified_https(f'https://{hostname}:{port}', 'synthetic-token', trusted)
    # Exercise the exact fixed transport created above, not a separate TLS fixture.
    private_client = client._ContainerPilotClient__client
    try:
        if case == 'match':
            assert (await private_client.get('/health')).status_code == 200
            assert reached == [True]
        else:
            with pytest.raises(httpx.ConnectError):
                await private_client.get('/health')
            assert reached == []
    finally:
        await client.aclose()
        listener.close()
        await listener.wait_closed()


@pytest.fixture
def installed(tmp_path, monkeypatch):
    if sys.platform != 'linux': pytest.skip('Protected installed configuration requires Linux')
    hub = tmp_path / 'hub'
    hub.mkdir(mode=0o700)
    operator = tmp_path / 'operator'
    operator.mkdir(mode=0o700)
    secrets = operator / 'secrets'
    secrets.mkdir(mode=0o700)
    ca, _ = certificate()
    token = b'synthetic-executor-token'
    ca_path, token_path = operator / 'ca.pem', secrets / 'token'
    ca_path.write_bytes(ca); ca_path.chmod(0o644)
    token_path.write_bytes(token); token_path.chmod(0o600)
    document = profile_document()
    document['executor'].update(ca_file=str(ca_path), ca_sha256='sha256:' + hashlib.sha256(ca).hexdigest(),
        credential_file=str(token_path), credential_sha256='sha256:' + hashlib.sha256(token).hexdigest())
    profile = operator / 'pilot.json'
    profile.write_bytes(encode(document)); profile.chmod(0o600)
    config = hub / 'settings.json'
    config.write_text(json.dumps({'schema_version': 1, 'base_url': 'https://hub.example',
        'cloudflare_access': {'issuer': 'https://team.cloudflareaccess.com', 'audience': 'synthetic-hub',
                              'allowed_emails': ['operator@example.com']},
        'agent_allowed_hosts': ['192.168.50.80:8770'], 'agent_allowed_ips': ['127.0.0.1']}))
    for key, value in {'CODEX_TOKEN': 'synthetic-codex', 'HERMES_TOKEN': 'synthetic-hermes',
        'CLAUDE_TOKEN': 'synthetic-claude'}.items(): monkeypatch.setenv(key, value)
    return dict(config=config, profile=profile, document=document, ca=ca, token=token, token_path=token_path,
                ca_path=ca_path, database=hub / 'hub.sqlite3')


def test_validate_never_resolves_environment_credentials_owner_ca_or_network(installed, monkeypatch, capsys):
    from engineering_hub.app import Settings
    monkeypatch.setattr(Settings, 'from_env', lambda *_: pytest.fail('Token resolution'))
    monkeypatch.setattr(startup, 'PilotOwner', lambda *_a, **_k: pytest.fail('Owner construction'))
    original = startup.read_protected_file
    def read(path, **kwargs):
        assert Path(path) == installed['profile']
        return original(path, **kwargs)
    monkeypatch.setattr(startup, 'read_protected_file', read)
    startup.main(['validate', '--config', str(installed['config']), '--pilot-config', str(installed['profile'])])
    assert json.loads(capsys.readouterr().out)['state'] == 'validated_not_activated'
    assert not installed['database'].exists()


@LINUX
@pytest.mark.parametrize('case', ['missing-both', 'missing-db', 'missing-lock', 'unenrolled'])
def test_require_existing_never_creates_or_adopts_state(tmp_path, case):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    path = directory / 'hub.db'
    if case in {'missing-db', 'missing-lock'}:
        owner = PilotOwner(path, allow_ephemeral_test_storage=True)
        owner.close()
        (path if case == 'missing-db' else path.with_name(path.name + '.pilot-owner.lock')).unlink()
    elif case == 'unenrolled':
        HubRepository(path)
    before = {entry.name: entry.read_bytes() for entry in directory.iterdir()}
    with pytest.raises(PilotOwnershipError):
        PilotOwner(path, allow_ephemeral_test_storage=True, require_existing=True)
    assert {entry.name: entry.read_bytes() for entry in directory.iterdir()} == before


@LINUX
def test_require_existing_reopen_preserves_records_and_uses_current_uid(tmp_path):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    path = directory / 'hub.db'
    with PilotOwner(path, allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(path, pilot_owner=owner)
        item = repo.create_task('human:42', 'engineering-hub', 'Existing task', 'fixture')
        epoch = owner.epoch
    with PilotOwner(path, allow_ephemeral_test_storage=True, require_existing=True) as owner:
        assert owner.previous_epoch == epoch and owner.epoch != epoch
        assert HubRepository(path, pilot_owner=owner).get_task(item['id']) == item
        assert path.stat().st_uid == os.getuid()


@LINUX
def test_require_existing_uses_no_create_flags_and_deleted_database_is_not_recreated(tmp_path, monkeypatch):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    path = directory / 'hub.db'
    with PilotOwner(path, allow_ephemeral_test_storage=True):
        pass
    original_open = os.open
    def checked_open(name, flags, *args, **kwargs):
        assert not flags & os.O_CREAT
        return original_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', checked_open)
    with PilotOwner(path, allow_ephemeral_test_storage=True, require_existing=True):
        pass
    import engineering_hub.pilot_ownership as ownership
    original_connect = ownership.sqlite3.connect
    def delete_before_connect(database, *args, **kwargs):
        assert 'mode=rw' in database
        path.unlink()
        return original_connect(database, *args, **kwargs)
    monkeypatch.setattr(ownership.sqlite3, 'connect', delete_before_connect)
    with pytest.raises(PilotOwnershipError):
        PilotOwner(path, allow_ephemeral_test_storage=True, require_existing=True)
    assert not path.exists()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'permissions', 'digest', 'file-root', 'db-collision', 'ca-collision', 'agent-token'])
def test_secret_file_guards_and_no_token_echo(installed, monkeypatch, kind):
    from engineering_hub import unified_queued_pilot_service as serving
    monkeypatch.setattr(serving, 'serve_owned_unified_queued_pilot', lambda *_a, **_k: pytest.fail('Activated'))
    # Explicit fixture-only enrollment; actual serve must require existing state.
    owner = PilotOwner(installed['database'], allow_ephemeral_test_storage=True)
    owner.close()
    original_owner = startup.PilotOwner
    def open_owner(path, **kwargs):
        return original_owner(path, allow_ephemeral_test_storage=True, **kwargs)
    monkeypatch.setattr(startup, 'PilotOwner', open_owner)
    path = installed['token_path']
    document = installed['document']
    if kind == 'symlink':
        alias = path.with_name('alias'); alias.symlink_to(path)
        document['executor']['credential_file'] = str(alias)
    elif kind == 'hardlink':
        os.link(path, path.with_name('alias'))
    elif kind == 'permissions': path.chmod(0o644)
    elif kind == 'digest': document['executor']['credential_sha256'] = 'sha256:' + 'f' * 64
    elif kind == 'file-root': document['executor']['credential_file'] = str(installed['database'].parent / 'agent-message-bus/files/private-token')
    elif kind == 'db-collision': document['executor']['credential_file'] = str(installed['database'])
    elif kind == 'ca-collision': document['executor']['credential_file'] = str(installed['ca_path'])
    else:
        path.write_bytes(b'synthetic-codex')
        document['executor']['credential_sha256'] = 'sha256:' + hashlib.sha256(b'synthetic-codex').hexdigest()
    installed['profile'].write_bytes(encode(document))
    with pytest.raises(startup.UnifiedPilotStartupError) as error:
        run_owned_loop(startup.serve_configuration(installed['config'], installed['profile'], reconciliation_factory=synthetic_reconciliation))
    assert 'synthetic-executor-token' not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('layout', ['external', 'private-hub-child'])
async def test_selector_preserves_settings_scope_tls_and_closes_pretransfer_owner(installed, monkeypatch, layout):
    from engineering_hub import unified_queued_pilot_service as serving
    from engineering_hub.work_queue import WorkTarget
    owner = PilotOwner(installed['database'], allow_ephemeral_test_storage=True)
    owner.close()
    if layout == 'private-hub-child':
        secret_root = installed['database'].parent / 'executor-secrets'
        secret_root.mkdir(mode=0o700)
        token_path = secret_root / 'token'
        token_path.write_bytes(installed['token']); token_path.chmod(0o600)
        installed['document']['executor']['credential_file'] = str(token_path)
        installed['profile'].write_bytes(encode(installed['document']))
    opened = []
    original_owner = startup.PilotOwner
    def open_owner(path, **kwargs):
        assert kwargs == {'require_existing': True}
        result = original_owner(path, allow_ephemeral_test_storage=True, **kwargs)
        opened.append(result)
        return result
    monkeypatch.setattr(startup, 'PilotOwner', open_owner)
    async def fake_serve(settings, manifest, factory, **kwargs):
        assert settings.database == installed['database']
        assert settings.agent_auth.codex_token == 'synthetic-codex'
        assert kwargs['agent_port'] == 8770 and kwargs['browser_port'] == 8771
        assert kwargs['agent_host'] == kwargs['browser_host'] == '0.0.0.0'
        assert kwargs['bus_paths']['AGENT_BUS_DB'] == installed['database'].parent / 'agent-message-bus/messages.sqlite3'
        veto = kwargs['target_verifier']
        target = WorkTarget(TASK, manifest.candidate.source_commit, manifest.candidate.image_config_id)
        assert veto('codex', startup.ACTION, 'staging', target)
        assert not veto('hermes', startup.ACTION, 'staging', target)
        assert not veto('codex', startup.ACTION, 'production', target)
        assert not veto('codex', startup.ACTION, 'staging', WorkTarget(str(uuid4()), target.source_commit, target.artifact_digest))
        # Replacing a pathname after capture must not alter the trusted CA.
        # A later path reopen would now fail parsing instead of creating client.
        installed['ca_path'].write_bytes(b'invalid-replacement')
        client = factory()
        try:
            assert type(client) is ContainerPilotClient
        finally:
            await client.aclose()
        raise RuntimeError('Synthetic early bind failure')
    monkeypatch.setattr(serving, 'serve_owned_unified_queued_pilot', fake_serve)
    with pytest.raises(startup.UnifiedPilotStartupError):
        await startup.serve_configuration(installed['config'], installed['profile'], reconciliation_factory=synthetic_reconciliation)
    assert len(opened) == 1
    with pytest.raises(PilotOwnershipError): opened[0].assert_active(installed['database'])
