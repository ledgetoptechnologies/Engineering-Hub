import json

import pytest
from agent_message_bus.app import create_app
from agent_message_bus.auth import AuthSettings
from agent_message_bus.mcp_server import build_mcp
from tests.test_feedback_transport import context


@pytest.mark.asyncio
async def test_bus_info_live_catalog_help_and_authentication(tmp_path):
    app=create_app(db_path=tmp_path/'info.sqlite3',audit_path=tmp_path/'audit.jsonl',
                   file_root=tmp_path/'files',auth=AuthSettings('hermes-test','codex-test',claude_token='claude-test'),
                   push_events_operational=False)
    server=build_mcp(app.state.service,push_publisher=app.state.push_publisher)
    tools={t.name:t for t in server._tool_manager.list_tools()}
    info=tools['bus_info'].fn
    overview=await info(ctx=context('codex'))
    assert overview['version']=='0.5.2'
    assert overview['tool_count']==len(tools)==22
    assert set(overview['tools'])==set(tools)
    assert overview['native_push']['subscriptions_enabled'] is False
    assert overview['native_push']['inbox_uri']=='agentbus://inbox/codex'
    assert 'hermes-test' not in json.dumps(overview)
    commands=await info(topic='commands',ctx=context('hermes'))
    assert {t['name'] for t in commands['commands']}==set(tools)
    assert all('input_schema' in t for t in commands['commands'])
    assert 'sender' not in next(t for t in commands['commands'] if t['name']=='send_message')['input_schema']['properties']
    feedback=await info(topic='feedback',ctx=context('claude'))
    assert feedback['examples'][0]['tool']=='feedback_submit'
    assert not app.state.service.feedback.list().items
    with pytest.raises(Exception):await info(ctx=context('wrong'))
    with pytest.raises(ValueError,match='unknown help topic'):await info(topic='untrusted-invalid-topic',ctx=context('hermes'))
