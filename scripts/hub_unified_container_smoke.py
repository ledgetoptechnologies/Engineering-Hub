"""Disposable synthetic legacy-to-unified cutover; never opens production state.

The legacy process is the candidate image's bus implementation, not a copy of
the currently deployed older image. This is not TrueNAS, reboot or agent-wake
acceptance. Only locally present, explicitly selected Hub artifacts are used.
"""
import argparse
import json
import re
import time
import uuid

from hub_release_smoke import run, owned_exists


PROBE = r'''
import asyncio, hashlib, json, os, sqlite3, sys, time
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

stage, commit = sys.argv[1:]
base = 'http://127.0.0.1:8770'
alias_base = 'http://127.0.0.1:8765'
host = 'hub.ledgetoptechnologies.com'
root = Path('/var/lib/hub')
manifest = root / '.unified-smoke.json'
tokens = {name: 'synthetic-unified-' + name + '-only' for name in ('hermes', 'codex', 'claude')}
assert os.getuid() == os.getgid() == 568
BUS = {'send_message','get_send_receipt','presence_status','receive_messages','wait_for_messages',
 'claim_message','ack_message','reject_message','get_message','knowledge_put','knowledge_get',
 'knowledge_list','knowledge_search','semantic_search','semantic_status','file_put','file_get',
 'file_list','file_history','feedback_submit','feedback_get','feedback_list','feedback_update','bus_info'}
HUB = {'hub_overview','hub_task_get','hub_task_claim','hub_task_candidate','hub_task_review',
 'hub_decision_request','hub_run_list','hub_topic_peers','hub_topic_preflight','hub_staging_task_propose',
 'hub_planning_list','hub_planning_get','hub_planning_put'}

def snapshot():
    def normalized(value):
        return {'blob': value.hex()} if isinstance(value, bytes) else value
    with sqlite3.connect('file:' + str(root / 'legacy/messages.sqlite3') + '?mode=ro', uri=True) as db:
        names = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        tables = {}
        for name in names:
            rows = [[normalized(v) for v in row] for row in db.execute('SELECT * FROM "' + name.replace('"','""') + '"')]
            encoded = sorted(json.dumps(row, separators=(',', ':'), ensure_ascii=False) for row in rows)
            tables[name] = {'rows': len(rows), 'sha256': hashlib.sha256(json.dumps(encoded).encode()).hexdigest()}
    files = {str(p.relative_to(root / 'legacy/files')): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted((root / 'legacy/files').rglob('*')) if p.is_file()}
    audit = (root / 'legacy/audit.jsonl').read_bytes()
    return {'tables': tables, 'files': files, 'audit_bytes': len(audit), 'audit_sha256': hashlib.sha256(audit).hexdigest()}

@asynccontextmanager
async def client(agent, endpoint=base):
    events = asyncio.Queue()
    async def incoming(message):
        if isinstance(message, types.ServerNotification):
            await events.put(message)
    async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + tokens[agent], 'Host': host},
                                  timeout=httpx.Timeout(10, read=None), trust_env=False) as http:
        async with streamable_http_client(endpoint + '/mcp', http_client=http) as (read, write, sid):
            async with ClientSession(read, write, message_handler=incoming) as session:
                initialized = await session.initialize()
                yield session, events, sid, initialized

async def tool(session, name, args=None):
    result = await session.call_tool(name, args or {})
    assert not result.isError, name
    return result.structuredContent if result.structuredContent is not None else json.loads(result.content[0].text)

async def main():
    async with httpx.AsyncClient(headers={'Host': host}, timeout=1, trust_env=False) as http:
        deadline = time.monotonic() + 20
        while True:
            try:
                response = await http.get(base + '/health')
                health = response.json()
                assert response.status_code == 200 and health['ok'] is True
                break
            except (httpx.HTTPError, AssertionError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Bounded synthetic server readiness expired')
                await asyncio.sleep(.1)
        if stage != 'seed':
            assert health['build_commit'] == commit and health['bus']['push']['state'] == 'healthy'
            alias_response = await http.get(alias_base + '/health')
            alias_health = alias_response.json()
            assert alias_response.status_code == 200 and alias_health['ok'] is True
            assert alias_health['build_commit'] == health['build_commit'] == commit
            assert alias_health['service'] == health['service'] == 'engineering-hub-control'
            assert alias_health['bus']['push']['state'] == 'healthy'
            # Health does not append an audit event. Compare before any new
            # MCP canary operation, which legitimately appends new records.
            saved = json.loads(manifest.read_text())
            assert snapshot() == saved['snapshot'], 'Legacy row/file/audit preservation failed'
        else:
            saved = {}

    # On unified stages Hermes uses the legacy compatibility port while Codex
    # owns a primary-port session and native subscription. Both must be one bus.
    hermes_endpoint = base if stage == 'seed' else alias_base
    async with client('hermes', endpoint=hermes_endpoint) as (hermes, _, _, _), client('codex') as (codex, events, sid, initialized):
        assert {t.name for t in (await codex.list_tools()).tools} == (BUS if stage == 'seed' else BUS | HUB)
        if stage == 'seed':
            args = {'recipient':'codex','message_type':'info','body':{'topic':'unified-cutover'},'client_request_id':'pending-key'}
            pending = await tool(hermes, 'send_message', args)
            assert (await tool(hermes, 'send_message', args))['id'] == pending['id']
            claimed = await tool(hermes, 'send_message', {**args,'client_request_id':'claimed-key'})
            assert (await tool(codex, 'claim_message', {'message_id':claimed['id']}))['status'] == 'claimed'
            saved.update(pending=pending['id'], claimed=claimed['id'])
            doc = {'project':'engineering-hub','path':'cutover.md','title':'Cutover fixture','content':'first','expected_version':0}
            await tool(hermes, 'knowledge_put', doc)
            await tool(codex, 'knowledge_put', {**doc,'content':'second','expected_version':1})
            file = {'project':'engineering-hub','path':'fixture.txt','content':'old','expected_version':0}
            await tool(hermes, 'file_put', file)
            await tool(codex, 'file_put', {**file,'content':'new','expected_version':1})
            feedback = await tool(hermes, 'feedback_submit', {'kind':'info','title':'Synthetic cutover',
                                  'description':'Disposable fixture only','project':'engineering-hub','client_request_id':'feedback-key'})
            saved['feedback'] = feedback['id']
        else:
            assert initialized.capabilities.resources.subscribe is True
            await tool(codex, 'hub_overview')
            receipt = await tool(hermes, 'get_send_receipt', {'client_request_id':'pending-key'})
            assert receipt['message_id'] == saved['pending']
            retry = await tool(hermes, 'send_message', {'recipient':'codex','message_type':'info',
                'body':{'topic':'unified-cutover'},'client_request_id':'pending-key'})
            assert retry['id'] == saved['pending']
            expected = 'pending' if stage == 'cutover' else 'acknowledged'
            assert (await tool(codex, 'get_message', {'message_id':saved['pending']}))['status'] == expected
            if stage == 'cutover':
                await tool(codex, 'claim_message', {'message_id':saved['pending']})
                await tool(codex, 'ack_message', {'message_id':saved['pending']})
                await tool(codex, 'ack_message', {'message_id':saved['claimed']})
            doc = await tool(codex,'knowledge_get',{'project':'engineering-hub','path':'cutover.md'})
            assert doc['content'] == 'second' and doc['version'] == 2
            history = await tool(codex,'file_history',{'project':'engineering-hub','path':'fixture.txt'})
            assert {item['version'] for item in history['versions']} == {1,2}
            old = await tool(codex,'file_get',{'project':'engineering-hub','path':'fixture.txt','version':1})
            assert old['content'] == 'old'
            assert (await tool(codex,'feedback_get',{'feedback_id':saved['feedback']}))['feedback']['id'] == saved['feedback']
            semantic = await tool(codex,'semantic_status')
            assert isinstance(semantic,dict)
            assert (await hermes.call_tool('get_message',{'message_id':saved['pending']})).isError
            async with httpx.AsyncClient(headers={'Host':host}, trust_env=False) as http:
                # Reuse the primary session on the alias, not just a second
                # authenticated initialization. Independent MCP managers cannot
                # satisfy this shared-session continuity check.
                rpc = {'jsonrpc':'2.0','id':1,'method':'tools/list'}
                same_session_headers = {'Mcp-Session-Id':sid(),
                    'Authorization':'Bearer '+tokens['codex'],
                    'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-06-18'}
                alias_tools = await http.post(alias_base+'/mcp',headers=same_session_headers,json=rpc)
                assert alias_tools.status_code == 200
                if alias_tools.headers['content-type'].startswith('text/event-stream'):
                    data = [line[6:] for line in alias_tools.text.splitlines() if line.startswith('data: ')]
                    assert len(data) == 1
                    alias_result = json.loads(data[0])
                else:
                    alias_result = alias_tools.json()
                assert {item['name'] for item in alias_result['result']['tools']} == BUS | HUB
                foreign_session_headers = {**same_session_headers,'Authorization':'Bearer '+tokens['hermes']}
                for endpoint in (base, alias_base):
                    assert (await http.post(endpoint+'/mcp',headers=foreign_session_headers,json=rpc)).status_code == 404
                assert (await http.get(base+'/api/messages')).status_code == 401
                assert (await http.get(base+'/api/messages',headers={'Authorization':'Bearer invalid'})).status_code == 401
            await codex.subscribe_resource(AnyUrl('agentbus://inbox/codex'))
            resources = await codex.read_resource(AnyUrl('agentbus://inbox/codex?after=0'))
            page = json.loads(resources.contents[0].text)
            assert saved['pending'] in {item['id'] for item in page['messages']}
            after = page['next_cursor']
            canary = await tool(hermes,'send_message',{'recipient':'codex','message_type':'info',
                       'body':{'topic':'native-cutover-'+stage},'client_request_id':'canary-'+stage})
            event = await asyncio.wait_for(events.get(),3)
            assert str(event.root.params.uri) == 'agentbus://inbox/codex'
            next_page = json.loads((await codex.read_resource(AnyUrl('agentbus://inbox/codex?after='+str(after)))).contents[0].text)
            assert canary['id'] in {item['id'] for item in next_page['messages']}
            with_foreign = False
            try:
                await hermes.read_resource(AnyUrl('agentbus://inbox/codex?after=0'))
            except McpError:
                with_foreign = True
            assert with_foreign
            await codex.unsubscribe_resource(AnyUrl('agentbus://inbox/codex'))
    # Session shutdown completes first; no client is left active at the
    # host-side graceful stop. Save a new baseline for recreation checks.
    saved['snapshot'] = snapshot()
    manifest.write_text(json.dumps(saved,sort_keys=True))
    print(json.dumps({'stage':stage,'preservation':'passed','tools':24 if stage=='seed' else 37,
                      'agent_ports':[8770] if stage=='seed' else [8770,8765],
                      'shared_session_across_agent_ports':'not_applicable' if stage=='seed' else 'passed',
                      'native_notification_route':'not_applicable' if stage=='seed' else 'alias_to_primary',
                      'table_count':len(saved['snapshot']['tables'])}))

asyncio.run(main())
'''


def stop_owned(name, owner):
    """Graceful stop only; never force-remove a still-running application."""
    if not owned_exists('container', name, owner):
        return
    running = run('docker', 'container', 'inspect', '--format', '{{.State.Running}}', name)
    if running == 'true':
        run('docker', 'kill', '--signal', 'SIGTERM', name)
        deadline = time.monotonic() + 35
        while run('docker', 'container', 'inspect', '--format', '{{.State.Running}}', name) == 'true':
            if time.monotonic() >= deadline:
                raise RuntimeError('Owned fixture has not drained; retained without force-removal')
            time.sleep(.2)
    elif running != 'false':
        raise RuntimeError('Owned fixture state unavailable; retained')
    # Verify again after the bounded drain. Never call the older fixture helper
    # whose container-removal branch uses rm -f, even for a stopped object.
    if not owned_exists('container', name, owner):
        return
    if run('docker', 'container', 'inspect', '--format', '{{.State.Running}}', name) != 'false':
        raise RuntimeError('Owned fixture is not confirmed stopped; retained')
    run('docker', 'container', 'rm', name)
    if owned_exists('container', name, owner):
        raise RuntimeError('Owned stopped fixture remains after removal')


def remove_owned_volume(name, owner):
    if owned_exists('volume', name, owner):
        run('docker', 'volume', 'rm', name)
        if owned_exists('volume', name, owner):
            raise RuntimeError('Owned fixture volume remains after removal')


def smoke(image, commit):
    if not re.fullmatch(r'ghcr\.io/ledgetoptechnologies/engineering-hub-control:(?:build-)[a-z0-9-]+|ghcr\.io/ledgetoptechnologies/engineering-hub-control@sha256:[a-f0-9]{64}', image):
        raise ValueError('Expected the explicit Hub release artifact')
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('Expected exact source commit')
    image_id = run('docker', 'image', 'inspect', '--format', '{{.Id}}', image)
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id):
        raise RuntimeError('Exact locally present immutable image identity required')
    owner = uuid.uuid4().hex
    volume = 'hub-unified-state-' + owner
    names, reports = [], []
    try:
        run('docker','volume','create','--label','hub.release-owner='+owner,volume)
        for stage in ('seed','cutover','recreate'):
            name = 'hub-unified-'+owner+'-'+stage
            names.append(name)
            env = {
                'AGENT_BUS_DB':'/var/lib/hub/legacy/messages.sqlite3',
                'AGENT_BUS_AUDIT':'/var/lib/hub/legacy/audit.jsonl',
                'AGENT_BUS_FILE_ROOT':'/var/lib/hub/legacy/files',
                'AGENT_BUS_ALLOWED_HOSTS':'hub.ledgetoptechnologies.com',
                'HUB_DB':'/var/lib/hub/hub.sqlite3',
                'HUB_BASE_URL':'https://hub.ledgetoptechnologies.com',
                'HUB_HUMAN_AUTH_MODE':'cloudflare_access',
                'HUB_CF_ACCESS_ISSUER':'https://synthetic.cloudflareaccess.com',
                'HUB_CF_ACCESS_AUDIENCE':'synthetic-unified-only',
                'HUB_CF_ACCESS_ALLOWED_EMAILS':'fixture@example.invalid',
                'HUB_PRIVATE_BROWSER_SOCKET':'/tmp/hub-browser.sock',
                'HERMES_TOKEN':'synthetic-unified-hermes-only',
                'CODEX_TOKEN':'synthetic-unified-codex-only',
                'CLAUDE_TOKEN':'synthetic-unified-claude-only',
            }
            command = ['docker','run','-d','--pull','never','--name',name,'--label','hub.release-owner='+owner,
                '--network','none','--user','568:568','--read-only','--cap-drop','ALL','--entrypoint','python',
                '--security-opt','no-new-privileges:true','--memory','512m','--memory-swap','512m',
                '--cpus','1','--pids-limit','128','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777',
                '--mount','type=volume,source='+volume+',target=/var/lib/hub']
            for key,value in env.items():
                command.extend(['-e',key+'='+value])
            command.append(image_id)
            if stage == 'seed':
                command.extend(['-m','uvicorn','agent_message_bus.app:app','--host','127.0.0.1',
                                '--port','8770','--no-access-log','--no-proxy-headers'])
            else:
                command.extend(['-m','engineering_hub','--split-ingress','--unified-mcp',
                                '--host','127.0.0.1','--port','8770','--browser-port','8771',
                                '--agent-alias-port','8765'])
            run(*command)
            report = json.loads(run('docker','exec',name,'python','-c',PROBE,stage,commit,timeout=55))
            assert report['stage'] == stage and report['preservation'] == 'passed'
            if stage != 'seed':
                assert report['agent_ports'] == [8770,8765]
                assert report['shared_session_across_agent_ports'] == 'passed'
                assert report['native_notification_route'] == 'alias_to_primary'
            reports.append(report)
            stop_owned(name,owner)
            names.remove(name)
        return {'source_commit':commit,'resolved_image_id':image_id,'synthetic_legacy_cutover':'passed','stages':reports,
                'network':'none','native_notifications':'passed','named_volume_recreation':'passed',
                'compatibility_alias':{'agent_ports':[8770,8765], 'shared_stateful_session':'passed',
                                       'native_notification_route':'alias_to_primary'},
                'semantic':'status_only_no_model_download_or_embedding_test',
                'production_state':'never_opened','legacy_version':'candidate_bus_not_deployed_old_image',
                'truenas':'not_deployed','reboot':'not_tested','agent_wake':'not_tested'}
    finally:
        for name in names:
            stop_owned(name,owner)
        remove_owned_volume(volume,owner)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--commit',required=True)
    args = parser.parse_args()
    print(json.dumps(smoke(args.image,args.commit)))
