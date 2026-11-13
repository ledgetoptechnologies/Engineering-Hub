import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_message_bus.agent_session_client import ClientError, _write
from engineering_hub import tracking_client, tracking_outbox


def test_immutable_private_envelope_and_concurrent_staging(tmp_path):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    arguments = {'project': 'engineering-hub', 'title': 'synthetic', 'client_request_id': 'stable-1'}
    def one(_):
        return tracking_outbox.stage(directory, 'https://hub.example', 'install', None, 'propose', arguments)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(one, range(12)))
    assert len(set(ids)) == 12
    assert tracking_outbox.references(directory) == sorted(ids)
    for record_id in ids:
        record = tracking_outbox.read(directory, record_id)
        assert record['arguments'] == arguments
        assert record['installation_id'] == 'install'
        if os.name != 'nt':
            assert (directory / (record_id + '.tracking')).stat().st_mode & 0o077 == 0
    with pytest.raises(ClientError):
        tracking_outbox.read(directory, '../bad')


def test_interrupted_publication_never_lists_partial_record(tmp_path, monkeypatch):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    def fail(*args):
        raise OSError('synthetic disk error')
    monkeypatch.setattr(tracking_outbox.os, 'link', fail)
    with pytest.raises(OSError):
        tracking_outbox.stage(directory, 'https://hub.example', 'install', None, 'propose', {'client_request_id': 'x'})
    assert tracking_outbox.references(directory) == []


@pytest.mark.skipif(os.name == 'nt', reason='directory fsync is POSIX-only')
def test_directory_flush_failure_keeps_published_record_for_explicit_recovery(tmp_path, monkeypatch):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    original = tracking_outbox.os.fsync
    calls = 0
    def flush(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('synthetic directory flush failure')
        original(fd)
    monkeypatch.setattr(tracking_outbox.os, 'fsync', flush)
    with pytest.raises(OSError):
        tracking_outbox.stage(directory, 'https://hub.example', 'install', None, 'propose', {'client_request_id': 'x'})
    ids = tracking_outbox.references(directory)
    assert len(ids) == 1 and tracking_outbox.read(directory, ids[0])['arguments']['client_request_id'] == 'x'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission and no-follow semantics')
def test_rejects_shared_directory_and_symlink_record(tmp_path):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o755); directory.chmod(0o755)
    with pytest.raises(ClientError):
        tracking_outbox.references(directory)
    directory.chmod(0o700)
    ident = 'e8e7d83a-b402-4953-b043-f8642a90a2f7'
    (directory / (ident + '.tracking')).symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(OSError):
        tracking_outbox.read(directory, ident)


def test_staging_without_session_and_send_binding(tmp_path, monkeypatch, capsys):
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    outbox = tmp_path / 'outbox'; outbox.mkdir(mode=0o700)
    installation = 'b39e3ea5-567c-45cb-a184-01dd1c6352bb'
    _write(profile / 'installation.private', {'origin': 'https://hub.example', 'installation_id': installation})
    arguments = {'project': 'engineering-hub', 'title': 'synthetic', 'client_request_id': 'stable-1'}
    raw = json.dumps({'action': 'propose', 'arguments': arguments}).encode()
    class Input:
        buffer = __import__('io').BytesIO(raw)
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input())
    assert tracking_client.main(['--profile-dir', str(profile), '--session', 'not-yet-created', '--outbox-dir', str(outbox), 'stage']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['sent'] is False
    record = tracking_outbox.read(outbox, result['record_id'])
    assert record['installation_id'] == installation and record['arguments'] == arguments
    async def no_network(*args, **kwargs):
        raise AssertionError('must reject mismatched installation before transport')
    monkeypatch.setattr(tracking_client, '_profile', lambda *args: ('https://hub.example', 'token', 'other', 'session', 9999999999))
    monkeypatch.setattr(tracking_client.httpx, 'AsyncClient', no_network)
    params = SimpleNamespace(profile_dir=profile, session='new', private_vpn=False, action='send-staged')
    with pytest.raises(ClientError, match='binding_mismatch'):
        asyncio.run(tracking_client._run(params, arguments, binding=record, action='propose'))
