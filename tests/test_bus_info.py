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
    assert overview['version']=='0.6.0'
    assert overview['tool_count']==len(tools)==27
    assert overview['schema_version']==5
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


@pytest.mark.asyncio
async def test_bus_info_presence_recovery_and_selected_conversation_help(tmp_path):
    app=create_app(db_path=tmp_path/'info.sqlite3',audit_path=tmp_path/'audit.jsonl',
                   file_root=tmp_path/'files',auth=AuthSettings('hermes-test','codex-test',claude_token='claude-test'),
                   push_events_operational=False)
    server=build_mcp(app.state.service,push_publisher=app.state.push_publisher)
    tools={t.name:t for t in server._tool_manager.list_tools()}
    info=tools['bus_info'].fn
    presence=await info(topic='presence',ctx=context('hermes'))
    assert presence['presence']['scope']=='this_server_process'
    assert all(not row['subscribed'] for row in presence['presence']['agents'])
    assert presence['examples'][0]['tool']=='presence_status'
    assert {item['tool'] for item in presence['examples'] if 'tool' in item} == {
        'presence_status', 'topic_presence_join', 'topic_presence_status', 'topic_presence_leave'
    }
    assert presence['features']['per_topic_presence'] is True
    assert all(name in presence['help'] for name in (
        'topic_presence_join', 'topic_presence_status', 'topic_presence_leave',
    ))
    assert 'not ACLs' in presence['help']
    recovery=await info(topic='recovery',ctx=context('codex'))
    assert recovery['examples'][0]['tool']=='get_send_receipt'
    assert 'same key' in recovery['help']
    messaging=await info(topic='messaging',ctx=context('claude'))
    assert 'client_request_id' in messaging['examples'][0]['arguments']
    worker=await info(topic='worker',ctx=context('hermes'))
    assert 'owner-selected' in worker['help']
    assert 'Use only the explicitly selected dedicated mcp-worker profile' not in worker['help']
    assert all(not app.state.repository.list_for_recipient(agent) for agent in ('hermes','codex','claude'))
