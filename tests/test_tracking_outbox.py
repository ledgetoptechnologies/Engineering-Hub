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
    payload = {'task_id': '11111111-1111-4111-8111-111111111111', 'expected_revision': 2, 'client_request_id': 'review-stable',
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
        return {'type': 'tracking_result', 'operation': action, 'execution_authorization': False,
                'result': {'id': payload['task_id'], 'status': 'completion_verified'}}
    monkeypatch.setattr(tracking_client, '_run', send)
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': record_id}))
    assert tracking_client.main(argv + ['send-staged']) == 0
    assert calls == [(payload, record, 'review')]
    assert json.loads(capsys.readouterr().out)['result']['status'] == 'completion_verified'
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': record_id}))
    assert tracking_client.main(argv + ['status-staged']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['state'] == 'hub_accepted' and status['task_id'] == payload['task_id']
    assert not status['execution_authorization']


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


def test_receipt_is_immutable_bound_and_never_a_completion_claim(tmp_path):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    installation = 'b39e3ea5-567c-45cb-a184-01dd1c6352bb'
    task_id = '11111111-1111-4111-8111-111111111111'
    arguments = {'project': 'engineering-hub', 'title': 'Work', 'client_request_id': 'stable'}
    ident = tracking_outbox.stage(directory, 'https://hub.example', installation, None, 'propose', arguments)
    record = tracking_outbox.read(directory, ident)
    assert tracking_outbox.receipt(directory, record) is None
    response = {'type': 'tracking_result', 'operation': 'propose', 'execution_authorization': False,
                'result': {'id': task_id, 'project': 'engineering-hub', 'status': 'completion_verified'}}
    value = tracking_outbox.record_acceptance(directory, record, response)
    assert value['state'] == 'hub_accepted' and value['task_id'] == task_id
    assert 'status' not in value and not value['execution_authorization']
    assert tracking_outbox.read(directory, ident) == record
    assert tracking_outbox.receipt(directory, record) == value
    assert tracking_outbox.record_acceptance(directory, record, response) == value
    conflicting = {**response, 'result': {'id': '22222222-2222-4222-8222-222222222222', 'project': 'engineering-hub'}}
    with pytest.raises(ClientError, match='tracking_receipt_conflict'):
        tracking_outbox.record_acceptance(directory, record, conflicting)
    for modified in ({**record, 'origin': 'https://different.example'},
                     {**record, 'installation_id': 'different-installation'}):
        with pytest.raises(ClientError):
            tracking_outbox.receipt(directory, modified)
        with pytest.raises(ClientError, match='tracking_outbox_changed'):
            tracking_outbox.record_acceptance(directory, modified, response)


def test_receipt_rejects_invalid_response_and_wrong_target(tmp_path):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    task_id = '11111111-1111-4111-8111-111111111111'
    arguments = {'task_id': task_id, 'expected_revision': 1, 'client_request_id': 'stable',
                 'progress': 'Work', 'evidence_links': []}
    ident = tracking_outbox.stage(directory, 'https://hub.example', 'install', None, 'update', arguments)
    record = tracking_outbox.read(directory, ident)
    good = {'type': 'tracking_result', 'operation': 'update', 'execution_authorization': False,
            'result': {'id': task_id}}
    for bad in ({**good, 'operation': 'review'}, {**good, 'execution_authorization': True},
                {**good, 'result': {'id': '22222222-2222-4222-8222-222222222222'}},
                {**good, 'result': {'id': 'not-a-uuid'}}, {'error': 'denied'}):
        with pytest.raises(ClientError):
            tracking_outbox.record_acceptance(directory, record, bad)
        assert tracking_outbox.receipt(directory, record) is None
    assert tracking_outbox.record_acceptance(directory, record, good)['task_id'] == task_id


def test_concurrent_acceptance_and_flush_failure_fail_closed(tmp_path, monkeypatch):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    args = {'project': 'engineering-hub', 'title': 'Work', 'client_request_id': 'stable'}
    ident = tracking_outbox.stage(directory, 'https://hub.example', 'install', None, 'propose', args)
    record = tracking_outbox.read(directory, ident)
    response = {'type': 'tracking_result', 'operation': 'propose', 'execution_authorization': False,
                'result': {'id': '11111111-1111-4111-8111-111111111111'}}
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda _: tracking_outbox.record_acceptance(directory, record, response), range(8)))
    assert all(value == values[0] for value in values)

    second = tracking_outbox.stage(directory, 'https://hub.example', 'install', None, 'propose',
                                   {**args, 'client_request_id': 'other'})
    record2 = tracking_outbox.read(directory, second)
    original = tracking_outbox._sync_directory
    def fail(_directory): raise OSError('synthetic directory flush failure')
    monkeypatch.setattr(tracking_outbox, '_sync_directory', fail)
    with pytest.raises(OSError):
        tracking_outbox.record_acceptance(directory, record2, response)
    with pytest.raises(OSError):
        tracking_outbox.receipt(directory, record2)
    monkeypatch.setattr(tracking_outbox, '_sync_directory', original)
    assert tracking_outbox.receipt(directory, record2)['state'] == 'hub_accepted'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX no-follow semantics')
def test_receipt_symlink_is_never_followed(tmp_path):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    ident = tracking_outbox.stage(directory, 'https://hub.example', 'install', None, 'propose',
        {'project': 'engineering-hub', 'title': 'Work', 'client_request_id': 'stable'})
    record = tracking_outbox.read(directory, ident)
    (directory / (ident + '.accepted')).symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(OSError):
        tracking_outbox.receipt(directory, record)


def test_cli_lost_response_and_receipt_failure_remain_unconfirmed(tmp_path, monkeypatch, capsys):
    import io
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    outbox = tmp_path / 'outbox'; outbox.mkdir(mode=0o700)
    installation = 'b39e3ea5-567c-45cb-a184-01dd1c6352bb'
    task_id = '11111111-1111-4111-8111-111111111111'
    _write(profile / 'installation.private', {'origin': 'https://hub.example', 'installation_id': installation})
    arguments = {'project': 'engineering-hub', 'title': 'Work', 'client_request_id': 'stable'}
    ident = tracking_outbox.stage(outbox, 'https://hub.example', installation, None, 'propose', arguments)
    record = tracking_outbox.read(outbox, ident)
    class Input:
        def __init__(self, value): self.buffer = io.BytesIO(json.dumps(value).encode())
    argv = ['--profile-dir', str(profile), '--session', 'first', '--outbox-dir', str(outbox)]
    async def lost(*args, **kwargs): raise OSError('synthetic lost response')
    monkeypatch.setattr(tracking_client, '_run', lost)
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': ident}))
    assert tracking_client.main(argv + ['send-staged']) == 1
    assert 'same request id' in capsys.readouterr().err and tracking_outbox.receipt(outbox, record) is None
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': ident}))
    assert tracking_client.main(argv + ['status-staged']) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'unconfirmed'
    async def accepted(args, payload, *, binding, action):
        assert payload == arguments and binding == record and action == 'propose'
        return {'type': 'tracking_result', 'operation': action, 'execution_authorization': False,
                'result': {'id': task_id, 'project': 'engineering-hub'}}
    monkeypatch.setattr(tracking_client, '_run', accepted)
    original = tracking_outbox.os.link
    def fail_link(*args): raise OSError('synthetic local receipt failure')
    monkeypatch.setattr(tracking_outbox.os, 'link', fail_link)
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': ident}))
    assert tracking_client.main(argv + ['send-staged']) == 1
    assert capsys.readouterr().out == '' and tracking_outbox.receipt(outbox, record) is None
    monkeypatch.setattr(tracking_outbox.os, 'link', original)
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': ident}))
    assert tracking_client.main(argv + ['send-staged']) == 0
    assert json.loads(capsys.readouterr().out)['result']['id'] == task_id
    assert tracking_outbox.receipt(outbox, record)['task_id'] == task_id


@pytest.mark.skipif(os.name == 'nt', reason='draft binding requires qualified POSIX directory fsync')
def test_profile_free_draft_bind_and_send_is_explicit_and_one_time(tmp_path, monkeypatch, capsys):
    import io
    outbox = tmp_path / 'outbox'; outbox.mkdir(mode=0o700)
    first = tmp_path / 'first'; first.mkdir(mode=0o700)
    other = tmp_path / 'other'; other.mkdir(mode=0o700)
    install_a = '11111111-1111-4111-8111-111111111111'
    install_b = '22222222-2222-4222-8222-222222222222'
    task_id = '33333333-3333-4333-8333-333333333333'
    _write(first / 'installation.private', {'origin': 'https://hub.example', 'installation_id': install_a})
    _write(other / 'installation.private', {'origin': 'https://hub.example', 'installation_id': install_b})
    payload = {'project': 'engineering-hub', 'title': 'Remember this', 'client_request_id': 'stable-draft'}
    class Input:
        def __init__(self, value): self.buffer = io.BytesIO(json.dumps(value).encode())
    base = ['--outbox-dir', str(outbox)]
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'action': 'propose', 'arguments': payload}))
    assert tracking_client.main(base + ['draft']) == 0
    draft = json.loads(capsys.readouterr().out)
    assert draft['state'] == 'unbound' and not draft['sent']
    ident = draft['record_id']; record = tracking_outbox.read(outbox, ident)
    assert tracking_outbox.is_draft(record) and {k: v for k, v in record['arguments'].items() if k != 'intake_id'} == payload
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': ident}))
    assert tracking_client.main(base + ['status-staged']) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'unbound'
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': ident}))
    assert tracking_client.main(base + ['send-staged']) == 1
    assert capsys.readouterr().out == ''
    assert tracking_outbox.binding(outbox, record) is None

    bind_args = base + ['--profile-dir', str(first), 'bind-staged']
    for _ in range(2):
        monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': ident}))
        assert tracking_client.main(bind_args) == 0
        assert not json.loads(capsys.readouterr().out)['sent']
    bound = tracking_outbox.binding(outbox, record)
    assert bound['installation_id'] == install_a and bound['origin'] == 'https://hub.example'
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': ident}))
    assert tracking_client.main(base + ['--profile-dir', str(other), 'bind-staged']) == 1
    capsys.readouterr()
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': ident}))
    assert tracking_client.main(base + ['status-staged']) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'unconfirmed'
    async def send(args, arguments, *, binding, action):
        assert binding == bound and arguments == record['arguments'] and action == 'propose-draft'
        return {'type': 'tracking_result', 'operation': action, 'execution_authorization': False,
                'result': {'id': task_id, 'project': payload['project']}}
    monkeypatch.setattr(tracking_client, '_run', send)
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': ident}))
    assert tracking_client.main(base + ['--profile-dir', str(first), '--session', 'session', 'send-staged']) == 0
    capsys.readouterr()
    receipt = tracking_outbox.receipt(outbox, record)
    assert receipt['installation_id'] == install_a and receipt['task_id'] == task_id and receipt['binding_sha256']
    assert tracking_outbox.read(outbox, ident) == record
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': ident}))
    assert tracking_client.main(base + ['status-staged']) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'hub_accepted'
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': ident}))
    assert tracking_client.main(base + ['--profile-dir', str(other), 'bind-staged']) == 1
    capsys.readouterr()


@pytest.mark.skipif(os.name == 'nt', reason='draft binding requires qualified POSIX directory fsync')
def test_draft_binding_first_wins_under_concurrent_different_installations(tmp_path):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    ident = tracking_outbox.stage(directory, None, None, None, 'propose',
        {'project': 'engineering-hub', 'title': 'Work', 'client_request_id': 'stable'})
    record = tracking_outbox.read(directory, ident)
    ids = ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222']
    def attempt(value):
        try: return tracking_outbox.bind(directory, record, 'https://hub.example', value)['installation_id']
        except ClientError as error: return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ids))
    winners = [value for value in results if value in ids]
    assert len(winners) == 1 and 'tracking_outbox_already_bound' in results
    assert tracking_outbox.binding(directory, record)['installation_id'] == winners[0]
    assert tracking_outbox.bind(directory, record, 'https://hub.example', winners[0])['installation_id'] == winners[0]
    assert tracking_outbox.read(directory, ident) == record


@pytest.mark.skipif(os.name == 'nt', reason='draft binding requires qualified POSIX directory fsync')
def test_draft_ambiguous_send_never_rebinds_or_sends_as_another_installation(tmp_path, monkeypatch, capsys):
    import io
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    first = tmp_path / 'first'; first.mkdir(mode=0o700)
    other = tmp_path / 'other'; other.mkdir(mode=0o700)
    a = '11111111-1111-4111-8111-111111111111'; b = '22222222-2222-4222-8222-222222222222'
    _write(first / 'installation.private', {'origin': 'https://hub.example', 'installation_id': a})
    _write(other / 'installation.private', {'origin': 'https://hub.example', 'installation_id': b})
    arguments = {'project': 'engineering-hub', 'title': 'Work', 'client_request_id': 'stable-key'}
    ident = tracking_outbox.stage(directory, None, None, None, 'propose', arguments)
    record = tracking_outbox.read(directory, ident)
    bound = tracking_outbox.bind(directory, record, 'https://hub.example', a)
    class Input:
        def __init__(self): self.buffer = io.BytesIO(json.dumps({'record_id': ident}).encode())
    base = ['--outbox-dir', str(directory)]
    original_run = tracking_client._run
    calls = []
    async def ambiguous(args, payload, *, binding, action):
        calls.append((payload, binding, action))
        raise OSError('response lost after possible commit')
    monkeypatch.setattr(tracking_client, '_run', ambiguous)
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input())
    assert tracking_client.main(base + ['--profile-dir', str(first), '--session', 'first', 'send-staged']) == 1
    capsys.readouterr()
    assert calls == [(arguments, bound, 'propose')] and tracking_outbox.receipt(directory, record) is None
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input())
    assert tracking_client.main(base + ['--profile-dir', str(other), 'bind-staged']) == 1
    capsys.readouterr()
    monkeypatch.setattr(tracking_client, '_run', original_run)
    monkeypatch.setattr(tracking_client, '_profile', lambda *_: ('https://hub.example', 'synthetic-token', b,
                                                                'session-id', 9999999999))
    def network_forbidden(*args, **kwargs): raise AssertionError('wrong installation reached network')
    monkeypatch.setattr(tracking_client.httpx, 'AsyncClient', network_forbidden)
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input())
    assert tracking_client.main(base + ['--profile-dir', str(other), '--session', 'other', 'send-staged']) == 1
    capsys.readouterr()
    assert len(calls) == 1 and tracking_outbox.read(directory, ident) == record
    assert tracking_outbox.binding(directory, record) == bound and record['arguments'] == arguments
    assert tracking_outbox.receipt(directory, record) is None


@pytest.mark.skipif(os.name == 'nt', reason='draft binding requires qualified POSIX directory fsync')
def test_draft_binding_publication_failure_and_postlink_flush_recovery(tmp_path, monkeypatch):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    ident = tracking_outbox.stage(directory, None, None, None, 'propose',
        {'project': 'engineering-hub', 'title': 'Work', 'client_request_id': 'stable'})
    record = tracking_outbox.read(directory, ident); installation = '11111111-1111-4111-8111-111111111111'
    original_link = tracking_outbox.os.link
    monkeypatch.setattr(tracking_outbox.os, 'link', lambda *_: (_ for _ in ()).throw(OSError('publication failed')))
    with pytest.raises(OSError): tracking_outbox.bind(directory, record, 'https://hub.example', installation)
    assert tracking_outbox.binding(directory, record) is None and tracking_outbox.read(directory, ident) == record
    monkeypatch.setattr(tracking_outbox.os, 'link', original_link)
    original_sync = tracking_outbox._sync_directory
    monkeypatch.setattr(tracking_outbox, '_sync_directory', lambda *_: (_ for _ in ()).throw(OSError('flush failed')))
    with pytest.raises(OSError): tracking_outbox.bind(directory, record, 'https://hub.example', installation)
    with pytest.raises(OSError): tracking_outbox.binding(directory, record)
    monkeypatch.setattr(tracking_outbox, '_sync_directory', original_sync)
    assert tracking_outbox.binding(directory, record)['installation_id'] == installation
    with pytest.raises(ClientError, match='tracking_outbox_already_bound'):
        tracking_outbox.bind(directory, record, 'https://other.example', installation)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX no-follow semantics')
def test_draft_binding_symlink_is_never_followed(tmp_path):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    ident = tracking_outbox.stage(directory, None, None, None, 'propose',
        {'project': 'engineering-hub', 'title': 'Work', 'client_request_id': 'stable'})
    record = tracking_outbox.read(directory, ident)
    (directory / (ident + '.binding')).symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(OSError): tracking_outbox.binding(directory, record)


def test_draft_binding_without_qualified_durability_is_denied(tmp_path, monkeypatch):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    ident = tracking_outbox.stage(directory, None, None, None, 'propose',
        {'project': 'engineering-hub', 'title': 'Work', 'client_request_id': 'stable'})
    record = tracking_outbox.read(directory, ident)
    monkeypatch.setattr(tracking_outbox, '_draft_binding_supported', lambda: False)
    with pytest.raises(ClientError, match='tracking_binding_durability_unavailable'):
        tracking_outbox.bind(directory, record, 'https://hub.example', '11111111-1111-4111-8111-111111111111')
    with pytest.raises(ClientError, match='tracking_binding_durability_unavailable'):
        tracking_outbox.bound_for_send(directory, record)
    assert tracking_outbox.binding(directory, record) is None and tracking_outbox.read(directory, ident) == record


def test_unsupported_host_never_sends_bound_draft_and_legacy_still_works(tmp_path, monkeypatch, capsys):
    import io
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    installation = '11111111-1111-4111-8111-111111111111'
    task_id = '22222222-2222-4222-8222-222222222222'
    _write(profile / 'installation.private', {'origin': 'https://hub.example', 'installation_id': installation})
    payload = {'project': 'engineering-hub', 'title': 'Work', 'client_request_id': 'stable'}
    ident = tracking_outbox.stage(directory, None, None, None, 'propose', payload)
    draft = tracking_outbox.read(directory, ident)
    if os.name == 'nt':
        # A copied/pre-existing sidecar must not bypass the native Windows gate.
        assert not tracking_outbox._draft_binding_supported()
        _write(directory / (ident + '.binding'), tracking_outbox._binding_value(draft, 'https://hub.example', installation))
    else:
        tracking_outbox.bind(directory, draft, 'https://hub.example', installation)
    legacy_id = tracking_outbox.stage(directory, 'https://hub.example', installation, None, 'propose',
                                      {**payload, 'client_request_id': 'legacy'})
    class Input:
        def __init__(self, identity): self.buffer = io.BytesIO(json.dumps({'record_id': identity}).encode())
    calls = []
    async def send(args, arguments, *, binding, action):
        calls.append((arguments, binding, action))
        return {'type': 'tracking_result', 'operation': action, 'execution_authorization': False,
                'result': {'id': task_id, 'project': 'engineering-hub'}}
    monkeypatch.setattr(tracking_client, '_run', send)
    monkeypatch.setattr(tracking_outbox, '_draft_binding_supported', lambda: False)
    base = ['--profile-dir', str(profile), '--session', 'first', '--outbox-dir', str(directory)]
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input(ident))
    assert tracking_client.main(base + ['send-staged']) == 1
    denied = capsys.readouterr()
    assert denied.out == '' and 'tracking_draft_binding_unavailable_on_this_platform' in denied.err and not calls
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input(ident))
    assert tracking_client.main(base + ['status-staged']) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'unconfirmed'
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input(legacy_id))
    assert tracking_client.main(base + ['send-staged']) == 0
    assert json.loads(capsys.readouterr().out)['result']['id'] == task_id and len(calls) == 1


def test_profile_free_draft_and_status_need_no_installation(tmp_path, monkeypatch, capsys):
    import io
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    class Input:
        def __init__(self, value): self.buffer = io.BytesIO(json.dumps(value).encode())
    payload = {'project': 'engineering-hub', 'title': 'Remember', 'client_request_id': 'stable'}
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'action': 'propose', 'arguments': payload}))
    assert tracking_client.main(['--outbox-dir', str(directory), 'draft']) == 0
    ident = json.loads(capsys.readouterr().out)['record_id']
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': ident}))
    assert tracking_client.main(['--outbox-dir', str(directory), 'status-staged']) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'unbound'
    assert {k: v for k, v in tracking_outbox.read(directory, ident)['arguments'].items() if k != 'intake_id'} == payload


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


def test_v2_draft_binds_when_v1_durability_is_unavailable(tmp_path, monkeypatch):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    args = {'project': 'engineering-hub', 'title': 'synthetic', 'client_request_id': 'stable-v2'}
    new_id = tracking_outbox.stage(directory, None, None, None, 'propose-draft', args)
    new = tracking_outbox.read(directory, new_id)
    old_id = tracking_outbox.stage(directory, None, None, None, 'propose', args)
    old = tracking_outbox.read(directory, old_id)
    assert new['version'] == 2 and len(new['arguments']['intake_id']) == 43
    assert old['version'] == 1 and 'intake_id' not in old['arguments']
    monkeypatch.setattr(tracking_outbox, '_draft_binding_supported', lambda: False)
    installation = 'b39e3ea5-567c-45cb-a184-01dd1c6352bb'
    with pytest.raises(ClientError, match='tracking_binding_durability_unavailable'):
        tracking_outbox.bind(directory, old, 'https://hub.example', installation)
    tracking_outbox.bind(directory, new, 'https://hub.example', installation)
    assert tracking_outbox.bound_for_send(directory, new)['installation_id'] == installation
