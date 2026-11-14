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


def test_cli_stages_and_explicitly_sends_review(tmp_path, monkeypatch, capsys):
    import io
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    outbox = tmp_path / 'outbox'; outbox.mkdir(mode=0o700)
    installation = 'b39e3ea5-567c-45cb-a184-01dd1c6352bb'
    _write(profile / 'installation.private', {'origin': 'https://hub.example', 'installation_id': installation})
    payload = {'task_id': 'task', 'expected_revision': 2, 'client_request_id': 'review-stable',
               'verdict': 'verify', 'note': 'reviewed', 'evidence_links': [{'label': 'e', 'url': 'https://example.com'}]}
    class Input:
        def __init__(self, value): self.buffer = io.BytesIO(json.dumps(value).encode())
    argv = ['--profile-dir', str(profile), '--session', 'first', '--outbox-dir', str(outbox)]
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'action': 'review', 'arguments': payload}))
    assert tracking_client.main(argv + ['stage']) == 0
    record_id = json.loads(capsys.readouterr().out)['record_id']
    record = tracking_outbox.read(outbox, record_id)
    assert record['action'] == 'review' and record['arguments'] == payload
    calls = []
    async def send(args, arguments, *, binding, action):
        calls.append((arguments, binding, action))
        return {'type': 'tracking_result', 'execution_authorization': False, 'result': {'status': 'completion_verified'}}
    monkeypatch.setattr(tracking_client, '_run', send)
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': record_id}))
    assert tracking_client.main(argv + ['send-staged']) == 0
    assert calls == [(payload, record, 'review')]
    assert json.loads(capsys.readouterr().out)['result']['status'] == 'completion_verified'


def test_cli_stages_claim_withdrawal_without_sending(tmp_path, monkeypatch, capsys):
    import io
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    outbox = tmp_path / 'outbox'; outbox.mkdir(mode=0o700)
    installation = 'b39e3ea5-567c-45cb-a184-01dd1c6352bb'
    _write(profile / 'installation.private', {'origin': 'https://hub.example', 'installation_id': installation})
    payload = {'task_id': 'task', 'expected_revision': 4, 'expected_claim_revision': 3,
               'client_request_id': 'withdraw-stable', 'reason': 'Incomplete'}
    class Input:
        buffer = io.BytesIO(json.dumps({'action': 'withdraw-claim', 'arguments': payload}).encode())
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input())
    argv = ['--profile-dir', str(profile), '--session', 'first', '--outbox-dir', str(outbox)]
    assert tracking_client.main(argv + ['stage']) == 0
    result = json.loads(capsys.readouterr().out)
    assert not result['sent'] and not result['execution_authorization']
    record = tracking_outbox.read(outbox, result['record_id'])
    assert record['action'] == 'withdraw-claim' and record['arguments'] == payload


def test_reference_pagination_recovers_large_outbox_without_opening_or_deleting(tmp_path, monkeypatch):
    import uuid
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    ids = [str(uuid.UUID(int=i)) for i in range(1, 1038)]
    for identity in reversed(ids):
        (directory / (identity + '.tracking')).touch()
    (directory / 'not-a-uuid.tracking').touch()
    (directory / 'discarded.partial').touch()
    # Even a UUID-shaped symlink is only a name during enumeration: reading it
    # is separately guarded and never occurs while listing.
    symlink_id = str(uuid.UUID(int=2000))
    if os.name != 'nt':
        (directory / (symlink_id + '.tracking')).symlink_to(tmp_path / 'outside')
        ids.append(symlink_id)
    monkeypatch.setattr(tracking_outbox, 'read', lambda *a: (_ for _ in ()).throw(AssertionError('opened a record')))
    with pytest.raises(ClientError, match='tracking_outbox_too_many'):
        tracking_outbox.references(directory)  # Original internal bounded API.
    seen = []; after = None
    while True:
        page = tracking_outbox.reference_page(directory, limit=37, after=after)
        assert len(page['record_ids']) <= 37
        seen.extend(page['record_ids'])
        after = page['next_after']
        if after is None:
            break
        assert after == page['record_ids'][-1]
    assert seen == sorted(ids) and len(set(seen)) == len(ids)
    assert all((directory / (identity + '.tracking')).exists() for identity in ids if identity != symlink_id)
    for bad in [0, 1001, True, '10']:
        with pytest.raises(ClientError):
            tracking_outbox.reference_page(directory, limit=bad)
    for bad in ['invalid', '../bad', 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA']:
        with pytest.raises(ClientError):
            tracking_outbox.reference_page(directory, after=bad)


def test_cli_list_staged_legacy_shape_and_explicit_cursor(tmp_path, monkeypatch, capsys):
    import io
    import uuid
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    outbox = tmp_path / 'outbox'; outbox.mkdir(mode=0o700)
    ids = [str(uuid.UUID(int=i)) for i in (1, 2, 3)]
    for identity in ids:
        (outbox / (identity + '.tracking')).touch()
    class Input:
        def __init__(self, value): self.buffer = io.BytesIO(json.dumps(value).encode())
    argv = ['--profile-dir', str(profile), '--session', 'first', '--outbox-dir', str(outbox), 'list-staged']
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({}))
    assert tracking_client.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {'type': 'tracking_outbox_references', 'record_ids': ids}
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'limit': 2}))
    assert tracking_client.main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first['record_ids'] == ids[:2] and first['next_after'] == ids[1]
    # A later insertion before the cursor requires a fresh scan from start.
    older = str(uuid.UUID(int=0)); (outbox / (older + '.tracking')).touch()
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'limit': 2, 'after': first['next_after']}))
    assert tracking_client.main(argv) == 0
    last = json.loads(capsys.readouterr().out)
    assert last['record_ids'] == ids[2:] and last['next_after'] is None
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'limit': 2}))
    assert tracking_client.main(argv) == 0
    assert json.loads(capsys.readouterr().out)['record_ids'][0] == older
