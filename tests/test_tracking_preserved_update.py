import io
import json

from agent_message_bus.agent_session_client import ClientError, _write
from engineering_hub import tracking_client, tracking_outbox
from tests.test_session_hub_bridge import PROJECT, call, env, pair


def test_update_stages_before_failed_send_and_can_be_inspected(tmp_path, monkeypatch, capsys):
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    outbox = tmp_path / 'outbox'; outbox.mkdir(mode=0o700)
    installation = 'b39e3ea5-567c-45cb-a184-01dd1c6352bb'
    _write(profile / 'installation.private', {'origin': 'https://hub.example', 'installation_id': installation})
    payload = {'task_id': '11111111-1111-4111-8111-111111111111', 'expected_revision': 2,
               'client_request_id': 'stable', 'progress': 'Work remains', 'evidence_links': []}
    monkeypatch.setattr(tracking_client.sys, 'stdin', type('Input', (), {'buffer': io.BytesIO(json.dumps(payload).encode())})())
    async def fail(args, arguments, *, binding, action):
        assert binding['arguments'] == payload and binding['installation_id'] == installation
        assert tracking_outbox.read(outbox, binding['id']) == binding
        raise ClientError('synthetic_pending_claim')
    monkeypatch.setattr(tracking_client, '_run', fail)
    args = ['--profile-dir', str(profile), '--session', 'first', '--outbox-dir', str(outbox)]
    assert tracking_client.main(args + ['update']) == 1
    output = capsys.readouterr()
    status = json.loads(output.out)
    assert status['state'] == 'unconfirmed' and 'Work remains' not in output.out + output.err
    assert tracking_outbox.read(outbox, status['record_id'])['arguments'] == payload
    assert tracking_outbox.receipt(outbox, tracking_outbox.read(outbox, status['record_id'])) is None


def test_update_receipt_and_staging_failure_precedes_network(tmp_path, monkeypatch, capsys):
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    outbox = tmp_path / 'outbox'; outbox.mkdir(mode=0o700)
    _write(profile / 'installation.private', {'origin': 'https://hub.example',
           'installation_id': 'b39e3ea5-567c-45cb-a184-01dd1c6352bb'})
    payload = {'task_id': '11111111-1111-4111-8111-111111111111', 'expected_revision': 2,
               'client_request_id': 'stable', 'progress': 'Progress', 'evidence_links': []}
    def input_payload():
        monkeypatch.setattr(tracking_client.sys, 'stdin', type('Input', (), {'buffer': io.BytesIO(json.dumps(payload).encode())})())
    calls = []
    async def send(args, arguments, *, binding, action):
        calls.append(binding['id'])
        return {'type': 'tracking_result', 'operation': action, 'execution_authorization': False,
                'result': {'id': payload['task_id'], 'status': 'open'}}
    monkeypatch.setattr(tracking_client, '_run', send)
    args = ['--profile-dir', str(profile), '--session', 'first', '--outbox-dir', str(outbox), 'update']
    input_payload()
    assert tracking_client.main(args) == 0
    record_id = json.loads(capsys.readouterr().out)['outbox_record_id']
    record = tracking_outbox.read(outbox, record_id)
    assert calls == [record_id] and tracking_outbox.receipt(outbox, record)['task_id'] == payload['task_id']
    monkeypatch.setattr(tracking_outbox, 'stage', lambda *args: (_ for _ in ()).throw(OSError('disk failed')))
    input_payload()
    assert tracking_client.main(args) == 1
    assert calls == [record_id]


def test_published_record_id_returned_if_read_fails(tmp_path, monkeypatch, capsys):
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    outbox = tmp_path / 'outbox'; outbox.mkdir(mode=0o700)
    _write(profile / 'installation.private', {'origin': 'https://hub.example',
           'installation_id': 'b39e3ea5-567c-45cb-a184-01dd1c6352bb'})
    payload = {'task_id': '11111111-1111-4111-8111-111111111111', 'expected_revision': 2,
               'client_request_id': 'stable', 'progress': 'Progress', 'evidence_links': []}
    monkeypatch.setattr(tracking_client.sys, 'stdin', type('Input', (), {'buffer': io.BytesIO(json.dumps(payload).encode())})())
    original_read = tracking_outbox.read
    monkeypatch.setattr(tracking_outbox, 'read', lambda *args: (_ for _ in ()).throw(OSError('read failed')))
    assert tracking_client.main(['--profile-dir', str(profile), '--session', 'first', '--outbox-dir', str(outbox), 'update']) == 1
    record_id = json.loads(capsys.readouterr().out)['record_id']
    assert original_read(outbox, record_id)['arguments'] == payload


def test_accepted_lost_response_retry_uses_original_immutable_request(tmp_path, monkeypatch, capsys):
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    outbox = tmp_path / 'outbox'; outbox.mkdir(mode=0o700)
    _write(profile / 'installation.private', {'origin': 'https://hub.example',
           'installation_id': 'b39e3ea5-567c-45cb-a184-01dd1c6352bb'})
    payload = {'task_id': '11111111-1111-4111-8111-111111111111', 'expected_revision': 2,
               'client_request_id': 'stable', 'progress': 'Progress', 'evidence_links': []}
    calls, committed = [], []
    async def send(args, arguments, *, binding, action):
        assert arguments == payload and binding['arguments'] == payload and action == 'update'
        calls.append(arguments.copy())
        if not committed:
            committed.append(arguments.copy())
            raise OSError('response lost after acceptance')
        return {'type': 'tracking_result', 'operation': 'update', 'execution_authorization': False,
                'result': {'id': payload['task_id'], 'status': 'open'}}
    monkeypatch.setattr(tracking_client, '_run', send)
    argv = ['--profile-dir', str(profile), '--session', 'first', '--outbox-dir', str(outbox)]
    monkeypatch.setattr(tracking_client.sys, 'stdin', type('Input', (), {'buffer': io.BytesIO(json.dumps(payload).encode())})())
    assert tracking_client.main(argv + ['update']) == 1
    record_id = json.loads(capsys.readouterr().out)['record_id']
    monkeypatch.setattr(tracking_client.sys, 'stdin', type('Input', (), {'buffer': io.BytesIO(json.dumps({'record_id': record_id}).encode())})())
    assert tracking_client.main(argv + ['send-staged']) == 0
    assert calls == [payload, payload] and committed == [payload]
    record = tracking_outbox.read(outbox, record_id)
    assert tracking_outbox.receipt(outbox, record)['task_id'] == payload['task_id']


def test_pending_rejection_preserves_original_across_independent_reopen(tmp_path, monkeypatch, capsys, env):
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    outbox = tmp_path / 'outbox'; outbox.mkdir(mode=0o700)
    _write(profile / 'installation.private', {'origin': 'https://hub.example',
           'installation_id': 'b39e3ea5-567c-45cb-a184-01dd1c6352bb'})
    _, claimant, _ = pair(env, name='pending-owner', caps=('tracking.update',))
    _, reviewer, _ = pair(env, name='pending-reviewer', family='claude', caps=('tracking.update', 'tracking.review'))
    repo = env.repo
    task = repo.propose_tracking_task('codex', PROJECT, 'Work', 'pending-task')
    proof = [{'label': 'proof', 'url': 'https://example.com/proof'}]
    claim = call(env, claimant, 'hub_tracking_task_update', lambda actor: repo.update_tracking_task(
        actor, task['id'], 1, 'pending-claim', progress='Done', evidence_links=proof, completion_claim=True))
    payload = {'task_id': task['id'], 'expected_revision': claim['revision'], 'client_request_id': 'stable',
               'progress': 'Work remains', 'evidence_links': []}
    async def rejected(args, arguments, *, binding, action):
        assert arguments == payload and binding['arguments'] == payload
        raise ClientError('synthetic_pending_rejection')
    monkeypatch.setattr(tracking_client, '_run', rejected)
    monkeypatch.setattr(tracking_client.sys, 'stdin', type('Input', (), {'buffer': io.BytesIO(json.dumps(payload).encode())})())
    argv = ['--profile-dir', str(profile), '--session', 'first', '--outbox-dir', str(outbox)]
    assert tracking_client.main(argv + ['update']) == 1
    record_id = json.loads(capsys.readouterr().out)['record_id']
    reopened = call(env, reviewer, 'hub_tracking_task_review', lambda actor: repo.review_tracking_task(
        actor, task['id'], claim['revision'], 'independent-reopen', verdict='reopen', note='Work remains', evidence_links=[]))
    assert reopened['status'] == 'open' and reopened['revision'] != payload['expected_revision']
    assert tracking_outbox.read(outbox, record_id)['arguments'] == payload
    assert tracking_outbox.receipt(outbox, tracking_outbox.read(outbox, record_id)) is None
