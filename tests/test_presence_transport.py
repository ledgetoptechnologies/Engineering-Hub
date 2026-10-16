import json

import pytest
from fastapi.testclient import TestClient

from agent_message_bus.mcp_server import build_mcp
from tests.test_send_transport import app, context, headers


def assert_snapshot(snapshot):
    assert snapshot['scope'] == 'this_server_process'
    assert isinstance(snapshot['native_push_available'], bool)
    assert {row['agent'] for row in snapshot['agents']} == {'hermes', 'codex', 'claude'}
    for row in snapshot['agents']:
        assert row['active_streams'] == 0
        assert row['inbox_subscriptions'] == 0 and row['subscribed'] is False
        assert row['connected_since'] is None and row['subscribed_since'] is None
    wire = json.dumps(snapshot)
    for forbidden in ('hermes-test', 'codex-test', 'claude-test', 'session_id', 'thread_id', 'client_ip'):
        assert forbidden not in wire


def test_rest_presence_is_authenticated_read_only_transport_metadata(app):
    with TestClient(app) as client:
        for agent in ('hermes', 'codex', 'claude'):
            response = client.get('/api/presence', headers=headers(agent))
            assert response.status_code == 200
            assert_snapshot(response.json())
        for agent in ('wrong', 'admin'):
            assert client.get('/api/presence', headers=headers(agent)).status_code == 401
        assert app.state.repository.list_for_recipient('hermes') == []


@pytest.mark.asyncio
async def test_mcp_presence_exposes_no_identity_or_session_selector(app):
    tools = {tool.name: tool for tool in build_mcp(app.state.service)._tool_manager.list_tools()}
    assert 'presence_status' in tools
    assert tools['presence_status'].parameters['properties'] == {}
    assert_snapshot(await tools['presence_status'].fn(ctx=context('hermes')))
    with pytest.raises(Exception):
        await tools['presence_status'].fn(ctx=context('wrong'))
