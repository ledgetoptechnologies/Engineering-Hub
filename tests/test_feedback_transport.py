from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from agent_message_bus.app import create_app
from agent_message_bus.auth import AuthSettings
from agent_message_bus.mcp_server import build_mcp


@pytest.fixture
def app(tmp_path):
    return create_app(db_path=tmp_path / 'm.sqlite3', audit_path=tmp_path / 'audit.jsonl',
                      file_root=tmp_path / 'files', auth=AuthSettings('hermes-test', 'codex-test',
                      admin_token='admin-test', claude_token='claude-test'))


def headers(identity):
    return {'Authorization': f'Bearer {identity}-test'}


def context(identity):
    request = Request({'type': 'http', 'method': 'POST', 'scheme': 'http', 'server': ('test', 80),
                       'client': ('127.0.0.1', 1234), 'root_path': '', 'path': '/mcp', 'raw_path': b'/mcp',
                       'query_string': b'', 'headers': [(b'authorization', f'Bearer {identity}-test'.encode())],
                       'state': {}})
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


def payload(**overrides):
    return {'kind': 'bug', 'title': 'Harmless test bug', 'description': 'private-report-marker',
            'client_request_id': 'feedback-test-1', **overrides}


def test_rest_feedback_roundtrip_permissions_idempotency_and_audit(app, caplog):
    with TestClient(app) as client:
        first = client.post('/api/feedback', json=payload(), headers=headers('claude'))
        assert first.status_code == 201
        report = first.json()
        assert report['author'] == 'claude' and report['revision'] == 1
        ident = report['id']
        assert client.post('/api/feedback', json=payload(), headers=headers('claude')).json()['id'] == ident
        assert client.post('/api/feedback', json=payload(description='different'), headers=headers('claude')).status_code == 409
        assert client.get('/api/feedback', headers=headers('codex')).json()['items'][0]['id'] == ident
        forbidden = client.patch(f'/api/feedback/{ident}', json={'expected_revision': 1, 'status': 'resolved'}, headers=headers('codex'))
        assert forbidden.status_code == 403
        updated = client.patch(f'/api/feedback/{ident}', json={'expected_revision': 1, 'status': 'triaged', 'note': 'private-evidence-marker'}, headers=headers('hermes'))
        assert updated.status_code == 200 and updated.json()['revision'] == 2
        assert client.patch(f'/api/feedback/{ident}', json={'expected_revision': 1, 'status': 'resolved'}, headers=headers('claude')).status_code == 409
        page = client.get(f'/api/feedback/{ident}?history_limit=1', headers=headers('claude')).json()
        assert page['feedback']['revision'] == 2 and len(page['history']) == 1 and page['next_revision'] == 1
        page2 = client.get(f'/api/feedback/{ident}?after_revision=1&history_limit=1', headers=headers('codex')).json()
        assert page2['history'][0]['note'] == 'private-evidence-marker' and page2['next_revision'] is None
        assert client.get('/api/messages', headers=headers('hermes')).json()['messages'] == []
        health = client.get('/health').json()
        assert health['version'] == '0.5.1' and health['schema_version'] == 3
    logs = app.state.audit.path.read_text() + caplog.text
    assert 'private-report-marker' not in logs and 'private-evidence-marker' not in logs
    assert 'feedback_submit' in logs and 'feedback_update' in logs


def test_feedback_validation_auth_and_spoofing_are_safe(app):
    with TestClient(app) as client:
        for identity in ('wrong', 'admin'):
            assert client.post('/api/feedback', json=payload(), headers=headers(identity)).status_code == 401
        forged = client.post('/api/feedback', json=payload(actor='hermes', author='hermes'), headers=headers('claude'))
        assert forged.status_code == 422 and 'private-report-marker' not in forged.text
        bad = client.post('/api/feedback', json=payload(kind='private-invalid-enum-marker'), headers=headers('claude'))
        assert bad.status_code == 400 and 'private-invalid-enum-marker' not in bad.text
        assert client.get('/api/feedback?status=bad', headers=headers('codex')).status_code == 400
        assert client.get('/api/feedback?limit=101', headers=headers('hermes')).status_code == 422
        assert client.get('/api/feedback?limit=0', headers=headers('hermes')).status_code == 422


def test_mcp_feedback_identity_shared_read_and_cas(app):
    tools = {tool.name: tool for tool in build_mcp(app.state.service)._tool_manager.list_tools()}
    names = {'feedback_submit', 'feedback_get', 'feedback_list', 'feedback_update'}
    assert names <= tools.keys()
    for name in names:
        assert not {'actor', 'identity', 'sender', 'updater'} & tools[name].parameters['properties'].keys()
    report = tools['feedback_submit'].fn(**payload(), ctx=context('claude'))
    ident = report['id']
    assert report['author'] == 'claude'
    assert tools['feedback_list'].fn(status='open', ctx=context('codex'))['items'][0]['id'] == ident
    with pytest.raises(ValueError, match='feedback_forbidden'):
        tools['feedback_update'].fn(ident, 1, 'resolved', ctx=context('codex'))
    result = tools['feedback_update'].fn(ident, 1, 'planned', 'reviewed, not approved to execute', ctx=context('hermes'))
    assert result['revision'] == 2
    history = tools['feedback_get'].fn(ident, history_limit=1, ctx=context('codex'))
    assert history['next_revision'] == 1
    with pytest.raises(ValueError, match='feedback_conflict'):
        tools['feedback_update'].fn(ident, 1, 'resolved', ctx=context('claude'))
    with pytest.raises(Exception):
        tools['feedback_get'].fn(ident, ctx=context('wrong'))
    assert app.state.repository.list_for_recipient('hermes') == []


def test_feedback_history_snapshot_pagination_and_restart(app):
    repo = app.state.service.feedback
    report = repo.submit('codex', 'improvement', 'Test pagination', 'No production data')
    for revision in range(1, 6):
        repo.update(report.id, 'codex', revision, 'triaged', f'evidence-{revision}')
    current, first = repo.get_with_history(report.id, limit=2)
    assert current.revision == 6 and [x.revision for x in first] == [1, 2]
    assert [x.revision for x in repo.history(report.id, after_revision=2, limit=2)] == [3, 4]
    from agent_message_bus.feedback import FeedbackRepository
    restarted = FeedbackRepository(repo.path)
    assert restarted.get(report.id).revision == 6
    assert [x.revision for x in restarted.history(report.id, after_revision=4)] == [5, 6]
