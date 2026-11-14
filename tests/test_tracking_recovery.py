import io
import json

import pytest

from agent_message_bus.agent_session_client import ClientError, _write
from engineering_hub import tracking_client, tracking_outbox

ORIGIN = 'https://hub.example'
FIRST = '11111111-1111-4111-8111-111111111111'
SECOND = '22222222-2222-4222-8222-222222222222'
TASK = '33333333-3333-4333-8333-333333333333'
OTHER = '44444444-4444-4444-8444-444444444444'


def response(record, task=TASK):
    return {'type': 'tracking_result', 'operation': record['action'], 'execution_authorization': False,
            'result': {'id': task, 'project': 'engineering-hub'}}


def setup(tmp_path):
    outbox = tmp_path / 'outbox'; outbox.mkdir(mode=0o700)
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    _write(profile / 'installation.private', {'origin': ORIGIN, 'installation_id': SECOND})
    args = {'project': 'engineering-hub', 'title': 'Synthetic', 'client_request_id': 'stable'}
    identity = tracking_outbox.stage(outbox, None, None, None, 'propose-draft', args)
    source = tracking_outbox.read(outbox, identity)
    tracking_outbox.bind(outbox, source, ORIGIN, FIRST)
    return outbox, profile, source


class Input:
    def __init__(self, value): self.buffer = io.BytesIO(json.dumps(value).encode())


def test_recovery_after_ambiguous_send_is_explicit_and_preserves_source(tmp_path, monkeypatch, capsys):
    outbox, profile, source = setup(tmp_path)
    original_binding = tracking_outbox.binding(outbox, source)
    base = ['--outbox-dir', str(outbox), '--profile-dir', str(profile)]
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': source['id']}))
    assert tracking_client.main(base + ['recover-staged']) == 0
    result = json.loads(capsys.readouterr().out)
    attempt = tracking_outbox.read(outbox, result['record_id'])
    assert not result['sent'] and attempt['version'] == 3 and attempt['arguments'] == source['arguments']
    assert attempt['installation_id'] == SECOND and attempt['expected_task_id'] is None
    calls = []
    async def send(args, arguments, *, binding, action):
        calls.append((binding, action))
        assert arguments == source['arguments']
        return response(attempt)
    monkeypatch.setattr(tracking_client, '_run', send)
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': attempt['id']}))
    assert tracking_client.main(base + ['--session', 'test', 'send-staged']) == 0
    capsys.readouterr()
    assert len(calls) == 1 and tracking_outbox.receipt(outbox, attempt)['task_id'] == TASK
    assert tracking_outbox.read(outbox, source['id']) == source
    assert tracking_outbox.binding(outbox, source) == original_binding
    assert tracking_outbox.receipt(outbox, source) is None


def test_recovery_rejects_changed_origin_v1_unbound_and_corruption(tmp_path):
    outbox, _, source = setup(tmp_path)
    with pytest.raises(ClientError):
        tracking_outbox.stage_recovery(outbox, source, 'https://other.example', SECOND)
    with pytest.raises(ClientError):
        tracking_outbox.stage_recovery(outbox, source, ORIGIN, FIRST)
    old_id = tracking_outbox.stage(outbox, None, None, None, 'propose', source['arguments'])
    with pytest.raises(ClientError):
        tracking_outbox.stage_recovery(outbox, tracking_outbox.read(outbox, old_id), ORIGIN, SECOND)
    unbound_id = tracking_outbox.stage(outbox, None, None, None, 'propose-draft',
        {'project': 'engineering-hub', 'title': 'Unbound', 'client_request_id': 'unbound'})
    with pytest.raises(ClientError):
        tracking_outbox.stage_recovery(outbox, tracking_outbox.read(outbox, unbound_id), ORIGIN, SECOND)
    (outbox / (source['id'] + '.accepted')).write_bytes(b'corrupt')
    with pytest.raises(Exception):
        tracking_outbox.stage_recovery(outbox, source, ORIGIN, SECOND)


def test_known_original_receipt_is_bound_and_conflicting_response_denied(tmp_path):
    outbox, _, source = setup(tmp_path)
    tracking_outbox.record_acceptance(outbox, source, response(source))
    attempt_id = tracking_outbox.stage_recovery(outbox, source, ORIGIN, SECOND)
    attempt = tracking_outbox.read(outbox, attempt_id)
    assert attempt['expected_task_id'] == TASK
    with pytest.raises(ClientError, match='tracking_recovery_task_mismatch'):
        tracking_outbox.record_acceptance(outbox, attempt, response(attempt, OTHER))
    assert tracking_outbox.receipt(outbox, attempt) is None
    tracking_outbox.record_acceptance(outbox, attempt, response(attempt))
    assert tracking_outbox.receipt(outbox, attempt)['task_id'] == TASK


def test_late_original_receipt_and_changed_binding_fail_closed(tmp_path):
    outbox, _, source = setup(tmp_path)
    attempt = tracking_outbox.read(outbox, tracking_outbox.stage_recovery(outbox, source, ORIGIN, SECOND))
    tracking_outbox.record_acceptance(outbox, source, response(source))
    with pytest.raises(ClientError, match='tracking_recovery_task_mismatch'):
        tracking_outbox.record_acceptance(outbox, attempt, response(attempt, OTHER))
    assert tracking_outbox.receipt(outbox, attempt) is None
    (outbox / (source['id'] + '.binding')).write_bytes(b'corrupt')
    with pytest.raises(Exception):
        tracking_outbox.validate_recovery(outbox, attempt)


def test_concurrent_recovery_stages_distinct_attempts_without_rebinding(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    outbox, _, source = setup(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        identities = list(executor.map(lambda _: tracking_outbox.stage_recovery(outbox, source, ORIGIN, SECOND), range(2)))
    assert len(set(identities)) == 2
    attempts = [tracking_outbox.read(outbox, identity) for identity in identities]
    assert all(attempt['arguments'] == source['arguments'] for attempt in attempts)
    assert tracking_outbox.binding(outbox, source)['installation_id'] == FIRST
    assert tracking_outbox.receipt(outbox, source) is None


def test_corrupt_original_blocks_recovery_send_before_network(tmp_path, monkeypatch, capsys):
    outbox, profile, source = setup(tmp_path)
    attempt = tracking_outbox.read(outbox, tracking_outbox.stage_recovery(outbox, source, ORIGIN, SECOND))
    (outbox / (source['id'] + '.binding')).write_bytes(b'corrupt')
    calls = []
    async def send(*args, **kwargs):
        calls.append(True)
        return response(attempt)
    monkeypatch.setattr(tracking_client, '_run', send)
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'record_id': attempt['id']}))
    assert tracking_client.main(['--outbox-dir', str(outbox), '--profile-dir', str(profile),
                                 '--session', 'test', 'send-staged']) == 1
    assert not calls and not capsys.readouterr().out
