"""Private synthetic handoffs only; no network, NAS or live credentials."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest

SPEC = importlib.util.spec_from_file_location('install_staging_connection',
    Path(__file__).parents[1] / 'scripts' / 'install_staging_connection.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.fixture(scope='module')
def ca():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic-installer-ca')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1).not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256()))
    return certificate.public_bytes(serialization.Encoding.PEM).decode('ascii')


@pytest.fixture
def handoff(tmp_path, ca):
    return {'schema': installer.SCHEMA, 'worker_token': 'synthetic-test-token-never-live', 'ca_pem': ca,
        'connection': {'schema': 1, 'enabled': True, 'auto_run': False,
            'origin': 'https://192.168.50.90:9443',
            'credential_file': str(tmp_path / 'staging-private' / 'worker.token'),
            'ca_file': str(tmp_path / 'staging-private' / 'worker-ca.pem'),
            'admin_actors': ['human:42'], 'preview_origin': None,
            'projects': {'engineering-hub': {'recipes': ['static-check']}}}}


def encoded(handoff):
    raw = json.dumps(handoff).encode()
    return raw, hashlib.sha256(raw).hexdigest()


def test_validates_real_certificate_and_fixed_paths(tmp_path, handoff):
    raw, digest = encoded(handoff)
    config, token, certificate, enabled = installer.validate_bundle(raw, digest, tmp_path)
    assert json.loads(config) == handoff['connection'] and enabled
    assert token == b'synthetic-test-token-never-live\n'
    assert certificate.decode() == handoff['ca_pem']
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('mutate', [
    lambda b: b.update(unknown='refused'),
    lambda b: b['connection'].update(extra='refused'),
    lambda b: b['connection'].update(credential_file='/var/lib/hub/settings.json'),
    lambda b: b['connection'].update(ca_file='/var/lib/hub/hub.sqlite3'),
    lambda b: b['connection'].update(origin='https://example.com'),
    lambda b: b['connection'].update(admin_actors=[]),
    lambda b: b.update(worker_token='short'),
    lambda b: b.update(ca_pem='-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----\n'),
    lambda b: b.update(ca_pem=b['ca_pem'] + '-----BEGIN PRIVATE KEY-----\nrefused'),
])
def test_invalid_handoff_has_no_effects(tmp_path, handoff, mutate):
    mutate(handoff)
    with pytest.raises(installer.InstallError): installer.validate_bundle(*encoded(handoff), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_digest_checked_before_json_or_target_access(tmp_path):
    with pytest.raises(installer.InstallError, match='bundle_digest_or_size'):
        installer.validate_bundle(b'not json and not trusted', '0' * 64, tmp_path)
    assert list(tmp_path.iterdir()) == []


POSIX = pytest.mark.skipif(os.name != 'posix', reason='Production dirFD/mode transaction requires Linux')


@POSIX
def test_first_install_permissions_and_existing_state_preserved(tmp_path, handoff):
    sentinels = {'settings.json': b'existing-settings', 'hub.sqlite3': b'existing-ledger'}
    for name, content in sentinels.items(): (tmp_path / name).write_bytes(content)
    (tmp_path / 'agent-message-bus').mkdir()
    (tmp_path / 'agent-message-bus' / 'messages.sqlite3').write_bytes(b'existing-bus')
    result = installer.install(*encoded(handoff), tmp_path)
    assert result == {'installed': True, 'restart_required': True, 'enabled': True,
        'existing_hub_state_changed': False, 'handoff_bundle_retained': True}
    for name, content in sentinels.items(): assert (tmp_path / name).read_bytes() == content
    assert (tmp_path / 'agent-message-bus' / 'messages.sqlite3').read_bytes() == b'existing-bus'
    private = tmp_path / 'staging-private'
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    for path in (private / 'worker.token', private / 'worker-ca.pem', tmp_path / 'staging-service.json'):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_uid == os.geteuid() and path.stat().st_nlink == 1
    assert not (tmp_path / installer.TEMP_CONFIG).exists()
    from engineering_hub.staging_client import load_optional_config
    parsed, status = load_optional_config(tmp_path / 'staging-service.json')
    assert parsed is not None and parsed.enabled and status['state'] == 'configured'


@POSIX
@pytest.mark.parametrize('name,kind', [('staging-service.json', 'file'), ('staging-private', 'dir'),
    ('staging-private', 'symlink'), (installer.TEMP_CONFIG, 'file')])
def test_existing_or_unknown_target_is_never_adopted(tmp_path, handoff, name, kind):
    path = tmp_path / name
    if kind == 'file': path.write_bytes(b'older-state')
    elif kind == 'dir': path.mkdir(); (path / 'unknown').write_bytes(b'keep')
    else: path.symlink_to('/nonexistent-synthetic')
    with pytest.raises(installer.InstallError, match='existing_staging_target_refused'):
        installer.install(*encoded(handoff), tmp_path)
    if kind == 'file': assert path.read_bytes() == b'older-state'
    elif kind == 'dir': assert (path / 'unknown').read_bytes() == b'keep'
    else: assert path.is_symlink()
    assert not (tmp_path / 'staging-service.json').exists() or name == 'staging-service.json'


@POSIX
@pytest.mark.parametrize('fail_file', ['worker.token', 'worker-ca.pem', installer.TEMP_CONFIG])
def test_partial_writes_remove_only_this_attempt(tmp_path, handoff, monkeypatch, fail_file):
    (tmp_path / 'hub.sqlite3').write_bytes(b'untouched')
    original = installer.exclusive_file
    def fail(directory, name, data, owned):
        original(directory, name, data, owned)
        if name == fail_file: raise OSError('synthetic-write-failure')
    monkeypatch.setattr(installer, 'exclusive_file', fail)
    with pytest.raises(installer.InstallError, match='installation_failed'):
        installer.install(*encoded(handoff), tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['hub.sqlite3']
    assert (tmp_path / 'hub.sqlite3').read_bytes() == b'untouched'


@POSIX
def test_publish_race_does_not_delete_foreign_config(tmp_path, handoff, monkeypatch):
    original = os.link
    def race(source, target, **kwargs):
        (tmp_path / 'staging-service.json').write_bytes(b'foreign-config')
        return original(source, target, **kwargs)
    monkeypatch.setattr(installer.os, 'link', race)
    with pytest.raises(installer.InstallError): installer.install(*encoded(handoff), tmp_path)
    assert (tmp_path / 'staging-service.json').read_bytes() == b'foreign-config'
    assert not (tmp_path / 'staging-private').exists()


@POSIX
def test_actual_partial_write_is_rolled_back(tmp_path, handoff, monkeypatch):
    original = os.write
    count = 0
    def partial(fd, data):
        nonlocal count
        count += 1
        if count == 1: return original(fd, data[:3])
        raise OSError('synthetic-full-volume')
    monkeypatch.setattr(installer.os, 'write', partial)
    with pytest.raises(installer.InstallError): installer.install(*encoded(handoff), tmp_path)
    assert list(tmp_path.iterdir()) == []


@POSIX
def test_failed_first_private_open_removes_empty_directory_and_allows_retry(tmp_path, handoff, monkeypatch):
    original = os.open
    failed = False
    def fail_first_private(path, flags, *args, **kwargs):
        nonlocal failed
        if path == installer.PRIVATE and not failed:
            failed = True
            raise OSError('synthetic-fd-exhaustion')
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(installer.os, 'open', fail_first_private)
    with pytest.raises(installer.InstallError, match='installation_failed'):
        installer.install(*encoded(handoff), tmp_path)
    assert failed and list(tmp_path.iterdir()) == []
    assert installer.install(*encoded(handoff), tmp_path)['installed']


@POSIX
def test_restrictive_umask_refuses_and_removes_owned_empty_directory(tmp_path, handoff):
    previous = os.umask(0o700)
    try:
        with pytest.raises(installer.InstallError, match='new_private_directory_identity'):
            installer.install(*encoded(handoff), tmp_path)
    finally:
        os.umask(previous)
    assert list(tmp_path.iterdir()) == []
    assert installer.install(*encoded(handoff), tmp_path)['installed']


@POSIX
def test_symlink_state_root_cannot_redirect_install(tmp_path, handoff):
    alias = tmp_path / 'alias'; alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(installer.InstallError, match='existing_unaliased_state_root_required'):
        installer.install(*encoded(handoff), alias)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['alias']


@POSIX
def test_failure_after_link_unpublishes_only_our_config(tmp_path, handoff, monkeypatch):
    original = os.unlink
    def fail(name, **kwargs):
        if name == installer.TEMP_CONFIG and not getattr(fail, 'done', False):
            fail.done = True
            raise OSError('synthetic-after-publish')
        return original(name, **kwargs)
    monkeypatch.setattr(installer.os, 'unlink', fail)
    with pytest.raises(installer.InstallError): installer.install(*encoded(handoff), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cli_failure_never_prints_token_or_certificate(tmp_path, handoff, monkeypatch, capsys):
    raw, digest = encoded(handoff)
    path = tmp_path / 'handoff.json'; path.write_bytes(raw)
    monkeypatch.setattr(installer.os, 'geteuid', lambda: 0, raising=False)
    assert installer.main(['--bundle', str(path), '--expected-sha256', digest, '--state-root', str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert handoff['worker_token'] not in output and handoff['ca_pem'] not in output
    assert 'nas_hub_uid_568_required' in output
