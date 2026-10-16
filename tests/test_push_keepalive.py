"""Server-originated SSE comments keep a quiet stream alive without inbox reads."""
import asyncio

import httpx
import pytest
from sse_starlette.sse import EventSourceResponse

from agent_message_bus.app import create_app
from tests.test_native_push_events import TOKENS, loopback


@pytest.mark.asyncio
async def test_real_idle_get_stream_emits_default_keepalive_without_sqlite_reads(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_BUS_ALLOWED_HOSTS', '127.0.0.1:*')
    app = create_app(db_path=tmp_path/'keepalive.sqlite3', audit_path=tmp_path/'audit.jsonl',
                     file_root=tmp_path/'files', auth=TOKENS, push_events_operational=True)
    assert EventSourceResponse.DEFAULT_PING_INTERVAL == 15
    async with loopback(app) as base_url:
        async with httpx.AsyncClient(timeout=22) as client:
            headers = {'Authorization':'Bearer '+TOKENS.codex_token,
                       'Accept':'application/json, text/event-stream'}
            initialized = await client.post(base_url+'/mcp', headers=headers,
                json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{
                    'protocolVersion':'2025-11-25','capabilities':{},
                    'clientInfo':{'name':'keepalive-fixture','version':'1'}}})
            assert initialized.status_code == 200
            headers['Mcp-Session-Id'] = initialized.headers['mcp-session-id']
            headers['MCP-Protocol-Version'] = '2025-11-25'
            response = await client.post(base_url+'/mcp', headers=headers,
                json={'jsonrpc':'2.0','method':'notifications/initialized'})
            assert response.status_code == 202
            source = app.state.push_event_source
            await asyncio.to_thread(source.flush_pending)
            baseline = source.stats['snapshot_reads']
            async with client.stream('GET',base_url+'/mcp',headers=headers) as stream:
                assert stream.status_code == 200
                async with asyncio.timeout(22):
                    async for line in stream.aiter_lines():
                        if line.startswith(': ping'):
                            break
                    else:
                        pytest.fail('quiet MCP SSE stream ended without a keepalive')
            assert source.stats['snapshot_reads'] == baseline
            deleted = await client.delete(base_url+'/mcp',headers=headers)
            assert deleted.status_code == 200
