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
            check('live_bus_info_catalog_version_and_features',info['version']=='0.6.0' and info['tool_count']==24 and info['native_push']['subscriptions_enabled'] is True)
            check('live_bus_info_includes_own_command_schema',any(t['name']=='bus_info' and 'input_schema' in t for t in info['commands']))
            send_args={'recipient':'codex','message_type':'INFO','body':{'private':'container-private-fixture'},'client_request_id':'native-key-'+ident}
            first=payload(await hermes.call_tool('send_message',send_args))
            notice=await asyncio.wait_for(events.get(),5)
            check('actual_recipient_uri_notification',notice['method']=='notifications/resources/updated' and notice['params']['uri']==str(uri))
            wire=json.dumps(notice)
            check('notification_has_no_body_or_message_id','container-private-fixture' not in wire and first['id'] not in wire)
            page=payload(await codex.read_resource(AnyUrl(f'{uri}?after={head}&limit=100')))
            check('push_followed_by_authoritative_cursor_read',len(page['messages'])==1 and page['messages'][0]['id']==first['id'])
            cursor=page['next_cursor']
            presence=payload(await hermes.call_tool('presence_status',{}))
            peer=next(row for row in presence['agents'] if row['agent']=='codex')
            check('presence_intersects_actual_sse_and_subscription',peer['subscribed'] and peer['active_streams']>=1 and peer['inbox_subscriptions']==1)
            check('presence_has_no_session_or_token_details',all(value not in json.dumps(presence) for value in [*tokens.values(),'session_id','thread_id']))
            replay=payload(await hermes.call_tool('send_message',send_args))
            check('keyed_replay_returns_identical_message',replay==first)
            receipt=payload(await hermes.call_tool('get_send_receipt',{'client_request_id':send_args['client_request_id']}))
            check('sender_private_receipt_matches_original_sequence',receipt['message_id']==first['id'] and receipt['seq']==cursor and 'body' not in receipt and 'fingerprint' not in receipt)
            forbidden=await codex.call_tool('get_send_receipt',{'client_request_id':send_args['client_request_id']})
            check('recipient_cannot_lookup_sender_receipt',forbidden.isError)
            conflict=await hermes.call_tool('send_message',{**send_args,'priority':'urgent'})
            check('changed_keyed_payload_conflicts',conflict.isError and 'send_conflict' in str(conflict.content))
            replay_page=payload(await codex.read_resource(AnyUrl(f'{uri}?after={cursor}&limit=100')))
            check('replay_does_not_advance_delivery_sequence',replay_page['messages']==[] and replay_page['next_cursor']==cursor)
            try:
                await asyncio.wait_for(events.get(),0.3)
            except asyncio.TimeoutError:
                duplicate_notice=False
            else:
                duplicate_notice=True
            check('replay_emits_no_second_resource_notification',not duplicate_notice)
            async with httpx.AsyncClient(timeout=30) as http:
                private_receipt=await http.get(url+'/api/message-receipts',headers={'Authorization':'Bearer '+tokens['hermes']},params={'client_request_id':send_args['client_request_id']})
                check('real_rest_receipt_query_is_private_metadata',private_receipt.status_code==200 and private_receipt.json()['message_id']==first['id'] and 'body' not in private_receipt.json())
                args={**send_args,'client_request_id':'concurrent-key-'+ident}
                results=await asyncio.gather(*(http.post(url+'/api/messages',headers={'Authorization':'Bearer '+tokens['hermes']},json=args) for _ in range(50)))
                check('fifty_real_http_writers_converge',all(r.status_code==201 for r in results) and len({r.json()['id'] for r in results})==1)
            await asyncio.wait_for(events.get(),5)
            concurrent_page=payload(await codex.read_resource(AnyUrl(f'{uri}?after={cursor}&limit=100')))
            check('concurrent_writers_create_one_durable_delivery',len(concurrent_page['messages'])==1 and concurrent_page['messages'][0]['id']==results[0].json()['id'])
            cursor=concurrent_page['next_cursor']
            # Send a complete real HTTP request, but deliberately never consume its
            # response. The independent recipient notification proves the commit.
            lost_args={**send_args,'client_request_id':'discarded-response-'+ident}
            encoded=json.dumps(lost_args).encode('utf-8')
            _reader,writer=await asyncio.open_connection('127.0.0.1',port)
            request=(f'POST /api/messages HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
                     f'Authorization: Bearer {tokens["hermes"]}\r\nContent-Type: application/json\r\n'
                     f'Content-Length: {len(encoded)}\r\nConnection: close\r\n\r\n').encode('ascii')+encoded
            try:
                writer.write(request);await writer.drain()
                await asyncio.wait_for(events.get(),5)
            finally:
                writer.close();await writer.wait_closed()
            lost_page=payload(await codex.read_resource(AnyUrl(f'{uri}?after={cursor}&limit=100')))
            check('commit_proven_without_sender_consuming_http_response',len(lost_page['messages'])==1)
            lost_id=lost_page['messages'][0]['id'];cursor=lost_page['next_cursor']
            await codex.unsubscribe_resource(uri)
            unbound=payload(await hermes.call_tool('presence_status',{}))
            peer=next(row for row in unbound['agents'] if row['agent']=='codex')
            check('unsubscribe_not_confused_with_stream_disconnect',not peer['subscribed'] and peer['inbox_subscriptions']==0 and peer['active_streams']>=1)
        async with httpx.AsyncClient(timeout=10) as http:
            closed=(await http.get(url+'/api/presence',headers={'Authorization':'Bearer '+tokens['hermes']})).json()
            check('closed_sdk_sessions_are_not_reported_active',all(row['active_streams']==0 and not row['subscribed'] for row in closed['agents']))
            check('unauthenticated_presence_rejected',(await http.get(url+'/api/presence')).status_code==401)
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
        async with client('hermes') as hermes:
            receipt=payload(await hermes.call_tool('get_send_receipt',{'client_request_id':send_args['client_request_id']}))
            check('receipt_survives_actual_container_restart',receipt['message_id']==first['id'] and receipt['retained'])
            replay=payload(await hermes.call_tool('send_message',send_args))
            check('post_restart_retry_recovers_original_commit',replay['id']==first['id'])
            lost_receipt=payload(await hermes.call_tool('get_send_receipt',{'client_request_id':lost_args['client_request_id']}))
            recovered=payload(await hermes.call_tool('send_message',lost_args))
            check('discarded_response_reconciled_after_restart',lost_receipt['message_id']==lost_id and recovered['id']==lost_id)
            missing=await hermes.call_tool('get_send_receipt',{'client_request_id':'unseen-key-'+ident})
            check('missing_sender_receipt_does_not_disclose_payload',missing.isError and 'container-private-fixture' not in str(missing.content))
        logs=await asyncio.to_thread(helper.run,['docker','logs',container],timeout=30)
        wire_logs=logs.stdout+logs.stderr
        check('keyed_send_errors_and_replays_do_not_log_keys_or_bodies',all(value not in wire_logs for value in [*tokens.values(),send_args['client_request_id'],lost_args['client_request_id'],'container-private-fixture']))
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
