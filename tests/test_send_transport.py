from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from agent_message_bus.app import create_app
from agent_message_bus.auth import AuthSettings
from agent_message_bus.mcp_server import build_mcp


@pytest.fixture
def app(tmp_path):
    return create_app(db_path=tmp_path / 'messages.sqlite3', audit_path=tmp_path / 'audit.jsonl',
                      file_root=tmp_path / 'files', auth=AuthSettings(
                          'hermes-test', 'codex-test', claude_token='claude-test', admin_token='admin-test'))


def headers(agent):
    return {'Authorization': f'Bearer {agent}-test'}


def context(agent):
    request = Request({'type': 'http', 'method': 'POST', 'scheme': 'http', 'server': ('test', 80),
                       'client': ('127.0.0.1', 1234), 'root_path': '', 'path': '/mcp', 'raw_path': b'/mcp',
                       'query_string': b'', 'headers': [(b'authorization', f'Bearer {agent}-test'.encode())],
                       'state': {}})
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


def payload(**overrides):
    return {'recipient': 'codex', 'message_type': 'INFO', 'body': {'text': 'private-body-marker'},
            'client_request_id': 'private-key-marker', **overrides}


def test_rest_keyed_replay_does_not_create_second_delivery(app):
    with TestClient(app) as client:
        first = client.post('/api/messages', json=payload(), headers=headers('hermes'))
        assert first.status_code == 201
        replay = client.post('/api/messages', json=payload(), headers=headers('hermes'))
        assert replay.status_code == 201
        assert replay.json() == first.json()
        messages, cursor = app.state.repository.list_after('codex', after=0, limit=100)
        assert len(messages) == 1 and cursor == messages[0]['seq']


def test_rest_sender_receipt_is_private_metadata_without_inbox_access(app, caplog):
    with TestClient(app) as client:
        message = client.post('/api/messages', json=payload(), headers=headers('hermes')).json()
        url = '/api/message-receipts?client_request_id=private-key-marker'
        receipt = client.get(url, headers=headers('hermes'))
        assert receipt.status_code == 200
        data = receipt.json()
        assert set(data) == {'message_id', 'sender', 'recipient', 'created_at', 'seq', 'status', 'retained'}
        assert data['message_id'] == message['id'] and data['status'] == 'pending' and data['retained']
        assert client.get(f"/api/messages/{message['id']}", headers=headers('hermes')).status_code == 404
        for other in ('codex', 'claude'):
            assert client.get(url, headers=headers(other)).status_code == 404
            assert client.get(url + '&sender=hermes', headers=headers(other)).status_code == 404
        for other in ('admin', 'wrong'):
            assert client.get(url, headers=headers(other)).status_code == 401
        client.post(f"/api/messages/{message['id']}/claim", headers=headers('codex')).raise_for_status()
        client.post(f"/api/messages/{message['id']}/ack", headers=headers('codex')).raise_for_status()
        assert client.get(url, headers=headers('hermes')).json()['status'] == 'acknowledged'
    logs = app.state.audit.path.read_text() + caplog.text
    assert 'private-key-marker' not in logs and 'private-body-marker' not in logs


def test_rest_conflict_and_pruned_replay_have_distinct_errors(app):
    with TestClient(app) as client:
        message = client.post('/api/messages', json=payload(), headers=headers('hermes')).json()
        conflict = client.post('/api/messages', json=payload(body={'text': 'changed'}), headers=headers('hermes'))
        assert conflict.status_code == 409
        with app.state.repository.connect() as conn:
            conn.execute('DELETE FROM messages WHERE id=?', (message['id'],))
        expired = client.post('/api/messages', json=payload(), headers=headers('hermes'))
        assert expired.status_code == 410
        receipt = client.get('/api/message-receipts?client_request_id=private-key-marker', headers=headers('hermes')).json()
        assert receipt['status'] == 'pruned' and not receipt['retained']
        assert receipt['message_id'] == message['id']
        assert app.state.repository.list_for_recipient('codex') == []
        assert 'private-key-marker' not in conflict.text + expired.text


def test_mcp_keyed_send_and_receipt_use_authenticated_sender(app):
    tools = {tool.name: tool for tool in build_mcp(app.state.service)._tool_manager.list_tools()}
    assert 'client_request_id' in tools['send_message'].parameters['properties']
    assert 'client_request_id' not in tools['send_message'].parameters['required']
    assert 'get_send_receipt' in tools
    assert not {'sender', 'identity', 'actor'} & tools['get_send_receipt'].parameters['properties'].keys()
    send = tools['send_message'].fn
    first = send(**payload(), ctx=context('hermes'))
    assert send(**payload(), ctx=context('hermes')) == first
    receipt = tools['get_send_receipt'].fn('private-key-marker', ctx=context('hermes'))
    assert receipt['message_id'] == first['id']
    assert 'body' not in receipt and 'client_request_id' not in receipt
    with pytest.raises(ValueError, match='send receipt not found'):
        tools['get_send_receipt'].fn('private-key-marker', ctx=context('codex'))
    with pytest.raises(ValueError, match='send_conflict'):
        send(**payload(priority='high'), ctx=context('hermes'))
    with pytest.raises(Exception):
        tools['get_send_receipt'].fn('private-key-marker', ctx=context('admin'))


def test_send_audit_distinguishes_replays_without_logging_request_keys(app):
    import json
    with TestClient(app) as client:
        for _ in range(2):
            client.post('/api/messages', json=payload(), headers=headers('hermes')).raise_for_status()
    events = [json.loads(line) for line in app.state.audit.path.read_text().splitlines()]
    sends = [event for event in events if event['operation'] == 'send']
    assert [event['result'] for event in sends] == ['ok', 'replayed']
    assert sends[0]['message_id'] == sends[1]['message_id']
    assert 'private-key-marker' not in app.state.audit.path.read_text()
