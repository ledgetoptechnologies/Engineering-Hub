#!/usr/bin/env python3
"""Real disposable Docker native-push/restart acceptance; never touches production."""
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import uuid
from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

logging.disable(logging.CRITICAL)
REPO = Path(__file__).resolve().parents[1]

async def main(image):
    spec = importlib.util.spec_from_file_location('push_fixture_helpers', REPO/'scripts/live_messaging_smoke.py')
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    spec.loader.exec_module(helper)
    helper.IMAGE = image
    ident = uuid.uuid4().hex[:12]
    root = REPO.parent/('.native-push-container-'+ident)
    data = root/'data'
    root.mkdir(mode=0o700);data.mkdir(mode=0o777);os.chmod(data,0o777)
    container = 'agent-bus-push-'+ident
    tokens = {name:secrets.token_urlsafe(32) for name in ('hermes','codex','claude','admin')}
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    evidence={'checks':[], 'production_changed':False, 'host_worker_wake_claimed':False,'image':image}
    created=False
    def check(name,condition):
        evidence['checks'].append({'name':name,'passed':bool(condition)})
        if not condition:raise AssertionError(name)
    def payload(result):
        if hasattr(result,'contents'):
            return json.loads(next(c.text for c in result.contents if hasattr(c,'text')))
        assert not result.isError
        return result.structuredContent or json.loads(next(c.text for c in result.content if hasattr(c,'text')))
    @asynccontextmanager
    async def client(agent,queue=None):
        async def receive(message):
            if isinstance(message,types.ServerNotification) and isinstance(message.root,types.ResourceUpdatedNotification):
                if queue is not None:queue.put_nowait(message.root.model_dump(mode='json'))
        async with httpx.AsyncClient(headers={'Authorization':'Bearer '+tokens[agent]}, timeout=httpx.Timeout(30, read=60)) as transport:
            async with streamable_http_client(url+'/mcp', http_client=transport) as (read,write,_):
                async with ClientSession(read,write,message_handler=receive) as session:
                    initialized=await session.initialize()
                    check('native_subscription_capability_'+agent,initialized.capabilities.resources.subscribe is True)
                    yield session
    try:
        helper.create_container(container,data,port,tokens,ident);created=True
        check('uid568_readonly_single_bind_loopback',helper.container_metadata(container,ident,data,port)['passed'])
        health=await asyncio.to_thread(helper.wait_healthy,container,url)
        check('native_push_healthy_in_container',health['push']['state']=='healthy' and health['push']['subscriptions_enabled'])
        events=asyncio.Queue();uri=AnyUrl('agentbus://inbox/codex')
        async with client('codex',events) as codex,client('hermes') as hermes:
            await codex.subscribe_resource(uri)
            head=payload(await hermes.call_tool('wait_for_messages',{'since_now':True,'timeout':0}))['next_cursor']
            try:
                await hermes.read_resource(AnyUrl('agentbus://inbox/hermes?after=999999&limit=3'))
            except McpError as exc:
                check('future_cursor_rejected_in_real_container', 'after exceeds current delivery sequence head' in str(exc))
            else:
                check('future_cursor_rejected_in_real_container', False)
            info=payload(await hermes.call_tool('bus_info',{'topic':'commands'}))
            check('live_bus_info_catalog_version_and_features',info['version']=='0.5.2' and info['tool_count']==22 and info['native_push']['subscriptions_enabled'] is True)
            check('live_bus_info_includes_own_command_schema',any(t['name']=='bus_info' and 'input_schema' in t for t in info['commands']))
            first=payload(await hermes.call_tool('send_message',{'recipient':'codex','message_type':'INFO','body':{'private':'container-private-fixture'}}))
            notice=await asyncio.wait_for(events.get(),5)
            check('actual_recipient_uri_notification',notice['method']=='notifications/resources/updated' and notice['params']['uri']==str(uri))
            wire=json.dumps(notice)
            check('notification_has_no_body_or_message_id','container-private-fixture' not in wire and first['id'] not in wire)
            page=payload(await codex.read_resource(AnyUrl(f'{uri}?after={head}&limit=100')))
            check('push_followed_by_authoritative_cursor_read',len(page['messages'])==1 and page['messages'][0]['id']==first['id'])
            cursor=page['next_cursor']
            await codex.unsubscribe_resource(uri)
        headers={'Authorization':'Bearer '+tokens['codex'],'Accept':'application/json, text/event-stream'}
        async with httpx.AsyncClient(timeout=25) as http:
            response=await http.post(url+'/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'restart-keepalive-fixture','version':'1'}}})
            check('raw_session_initialized',response.status_code==200)
            headers['Mcp-Session-Id']=response.headers['mcp-session-id'];headers['MCP-Protocol-Version']='2025-11-25'
            response=await http.post(url+'/mcp',headers=headers,json={'jsonrpc':'2.0','method':'notifications/initialized'})
            check('raw_session_ready',response.status_code==202)
            async with http.stream('GET',url+'/mcp',headers=headers) as stream:
                keepalive=False
                async with asyncio.timeout(22):
                    async for line in stream.aiter_lines():
                        if line.startswith(': ping'):
                            keepalive=True;break
                check('real_container_server_originated_keepalive',keepalive)
            response=await http.post(url+'/api/messages',headers={'Authorization':'Bearer '+tokens['hermes']},json={'recipient':'codex','message_type':'INFO','body':{'subject':'offline reconnect fixture'}})
            check('message_committed_while_recipient_disconnected',response.status_code==201)
            missed=response.json()['id']
            result=await asyncio.to_thread(helper.run,['docker','restart',container],timeout=60)
            check('actual_owned_container_restart',result.returncode==0)
            health=await asyncio.to_thread(helper.wait_healthy,container,url)
            check('native_source_ready_after_restart',health['push']['state']=='healthy')
            stale=await http.post(url+'/mcp',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'ping'})
            check('pre_restart_session_rejected',stale.status_code==404)
        events=asyncio.Queue()
        async with client('codex',events) as codex:
            await codex.subscribe_resource(uri)
            page=payload(await codex.read_resource(AnyUrl(f'{uri}?after={cursor}&limit=100')))
            check('fresh_initialize_subscribe_saved_cursor_catches_up',[m['id'] for m in page['messages']]==[missed])
            await codex.unsubscribe_resource(uri)
        evidence['result']='passed'
    except BaseException as exc:
        evidence['result']='failed';evidence['error']=type(exc).__name__+': '+str(exc)[:300]
        raise
    finally:
        if created:
            # Only the fixture with this exact ownership label may be removed.
            valid=helper.container_metadata(container,ident,data,port)['passed']
            if valid:
                prepared,_=helper.prepare_fixture_permissions(container)
                result=helper.run(['docker','rm','-f',container],timeout=60)
                evidence['container_removed']=result.returncode==0
                if prepared and result.returncode==0:
                    shutil.rmtree(root);evidence['fixture_removed']=True
        path=REPO.parent/('live-native-push-evidence-'+ident+'.json')
        path.write_text(json.dumps(evidence,indent=2)+'\n')
        print(json.dumps({'result':evidence.get('result'),'passed':sum(c['passed'] for c in evidence['checks']),'failed':sum(not c['passed'] for c in evidence['checks']),'evidence':str(path),'cleanup':{k:evidence.get(k,False) for k in ['container_removed','fixture_removed']}}),flush=True)

if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('usage: live_push_smoke.py EXISTING_TEST_IMAGE')
    asyncio.run(main(sys.argv[1]))
