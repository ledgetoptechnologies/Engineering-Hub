"""Offline packaged settings-file startup and recreation; synthetic state only.

No host mounts/ports, network, registry pull, live identity, or TrueNAS access.
"""
import argparse
import json
import re
import uuid

from hub_release_smoke import run
from hub_unified_container_smoke import stop_owned, remove_owned_volume


SEED = r'''
import json, os
from pathlib import Path
from agent_message_bus.database import MessageRepository
from engineering_hub.repository import HubRepository
assert os.getuid() == os.getgid() == 568
root = Path('/var/lib/hub')
bus = root / 'agent-message-bus'
bus.mkdir()
(bus / 'files').mkdir()
(bus / 'audit.jsonl').touch()
repo = MessageRepository(bus / 'messages.sqlite3')
message = repo.insert(sender='hermes', recipient='codex', message_type='info',
                      body={'topic':'settings-smoke','fixture':'preserved'})
task = HubRepository(root / 'hub.sqlite3').create_task(
    'human:123', 'treedar', 'Synthetic settings persistence', 'settings-fixture')
settings = {'schema_version':1,'base_url':'https://hub.ledgetoptechnologies.com',
    'cloudflare_access':{'issuer':'https://synthetic.cloudflareaccess.com',
        'audience':'synthetic-settings-only','allowed_emails':['fixture@example.invalid']},
    'agent_allowed_hosts':['hub.ledgetoptechnologies.com','127.0.0.1:8770'],
    'agent_allowed_ips':['127.0.0.1']}
(root / 'settings.json').write_text(json.dumps(settings))
(root / '.settings-smoke.json').write_text(json.dumps({'message':message.id,'task':task}))
print(json.dumps({'seed':'passed'}))
'''


PROBE = r'''
import asyncio, json, os, sys, time
from pathlib import Path
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
assert os.getuid() == os.getgid() == 568
commit = sys.argv[1]
base = 'http://127.0.0.1:8770'
browser = 'http://127.0.0.1:8771'
headers = {'Host':'hub.ledgetoptechnologies.com'}
saved = json.loads(Path('/var/lib/hub/.settings-smoke.json').read_text())
BUS = {'send_message','get_send_receipt','presence_status','receive_messages','wait_for_messages',
 'claim_message','ack_message','reject_message','get_message','knowledge_put','knowledge_get',
 'knowledge_list','knowledge_search','semantic_search','semantic_status','file_put','file_get',
 'file_list','file_history','feedback_submit','feedback_get','feedback_list','feedback_update','bus_info'}
HUB = {'hub_overview','hub_task_get','hub_task_claim','hub_task_candidate','hub_task_review',
 'hub_decision_request','hub_run_list','hub_topic_peers','hub_topic_preflight'}

async def call(session, name, args=None):
    result = await session.call_tool(name, args or {})
    assert not result.isError, name
    return result.structuredContent if result.structuredContent is not None else json.loads(result.content[0].text)

async def main():
    async with httpx.AsyncClient(headers=headers, trust_env=False, timeout=2) as http:
        deadline = time.monotonic() + 25
        for endpoint in (base, browser):
            while True:
                try:
                    response = await http.get(endpoint + '/health')
                    health = response.json()
                    assert response.status_code == 200 and health['ok'] is True
                    assert health['build_commit'] == commit
                    assert health['bus']['push']['state'] == 'healthy'
                    break
                except (httpx.HTTPError, AssertionError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Settings profile readiness deadline expired')
                    await asyncio.sleep(.1)
        for value in ({}, {'Authorization':'Bearer invalid'}):
            assert (await http.get(base+'/api/messages',headers=value)).status_code == 401
        for path in ('/', '/index.html', '/assets/example.js', '/api/hub/v1/auth/login', '/api/hub/v1/overview'):
            for value in ({}, {'CF-Access-Jwt-Assertion':'forged'},
                          {'CF-Access-Authenticated-User-Email':'fixture@example.invalid'}):
                assert (await http.get(browser+path,headers=value)).status_code == 401
        assert (await http.get(browser+'/mcp')).status_code == 403
        assert (await http.get(browser+'/',headers={'Authorization':'Bearer '+os.environ['CODEX_TOKEN']})).status_code == 403
        for agent in ('hermes','codex','claude'):
            token = os.environ[agent.upper()+'_TOKEN']
            auth = {**headers,'Authorization':'Bearer '+token}
            async with httpx.AsyncClient(headers=auth,trust_env=False,timeout=httpx.Timeout(10,read=None)) as client:
                async with streamable_http_client(base+'/mcp',http_client=client) as (read,write,_):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        assert {tool.name for tool in (await session.list_tools()).tools} == BUS | HUB
                        await call(session,'hub_overview')
                        task = await call(session,'hub_task_get',{'task_id':saved['task']['id']})
                        assert task == saved['task'], 'Preserved Hub task changed'
                        message = await session.call_tool('get_message',{'message_id':saved['message']})
                        if agent == 'codex':
                            assert not message.isError
                            data = message.structuredContent if message.structuredContent is not None else json.loads(message.content[0].text)
                            assert data['id'] == saved['message'] and data['status'] == 'pending'
                            assert data['body'] == {'topic':'settings-smoke','fixture':'preserved'}
                        else:
                            assert message.isError, 'Foreign inbox was readable'
    listeners = {}
    for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
        fields = line.split()
        if fields[3] == '0A':
            address,port = fields[1].split(':')
            listeners.setdefault(int(port,16),set()).add(address)
    assert listeners.get(8770) == listeners.get(8771) == {'00000000'}
    assert 8765 not in listeners, 'Unexpected compatibility alias'
    for line in Path('/proc/net/tcp6').read_text().splitlines()[1:]:
        fields = line.split()
        assert not (fields[3] == '0A' and int(fields[1].split(':')[1],16) in (8765,8770,8771))
    print(json.dumps({'settings_profile':'passed','build_commit':commit,'authenticated_agents':3,
        'mcp_tools':33,'preserved_message_and_task':'passed','browser_unauthorized':'rejected',
        'agent_bind':'0.0.0.0:8770','browser_bind':'0.0.0.0:8771','alias':'none'}))

asyncio.run(asyncio.wait_for(main(),50))
'''


def smoke(image, commit):
    if not isinstance(image, str) or not re.fullmatch(
            r'ghcr\.io/ledgetoptechnologies/engineering-hub-control:(?:build-)[a-z0-9-]+|'
            r'ghcr\.io/ledgetoptechnologies/engineering-hub-control@sha256:[a-f0-9]{64}', image):
        raise ValueError('Expected the explicit Hub release artifact')
    if not isinstance(commit, str) or not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('Expected exact source commit')
    image_id = run('docker','image','inspect','--format','{{.Id}}',image)
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id):
        raise RuntimeError('Exact locally present immutable image identity required')
    owner = uuid.uuid4().hex
    volume = 'hub-settings-state-' + owner
    names, reports = [], []

    def command(name):
        return ['docker','run','--pull','never','--name',name,'--label','hub.release-owner='+owner,
            '--network','none','--user','568:568','--read-only','--cap-drop','ALL',
            '--security-opt','no-new-privileges:true','--memory','512m','--memory-swap','512m',
            '--cpus','1','--pids-limit','128','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777',
            '--mount','type=volume,source='+volume+',target=/var/lib/hub']
    try:
        run('docker','volume','create','--label','hub.release-owner='+owner,volume)
        seed = 'hub-settings-' + owner + '-seed'
        names.append(seed)
        assert json.loads(run(*command(seed),'--entrypoint','python',image_id,'-c',SEED))['seed'] == 'passed'
        stop_owned(seed,owner)
        names.remove(seed)
        # Initial startup plus two complete process recreations over one volume.
        for iteration in range(3):
            name = 'hub-settings-' + owner + '-' + str(iteration)
            names.append(name)
            args = command(name)
            args.insert(2,'-d')
            for agent in ('HERMES','CODEX','CLAUDE','ADMIN'):
                args.extend(['-e',agent+'_TOKEN=synthetic-settings-'+agent.lower()+'-only'])
            run(*args,image_id,'--config','/var/lib/hub/settings.json')
            actual = json.loads(run('docker','container','inspect','--format','{{json .}}',name))
            profile = actual['HostConfig']
            assert actual['Config']['User'] == '568:568'
            assert actual['Config']['Labels']['hub.release-owner'] == owner
            assert profile['NetworkMode'] == 'none' and not profile.get('PortBindings')
            assert not profile.get('Binds') and not profile['Privileged']
            assert profile['ReadonlyRootfs'] is True
            assert len(actual['Mounts']) == 1 and actual['Mounts'][0]['Type'] == 'volume'
            assert actual['Mounts'][0]['Name'] == volume and actual['Mounts'][0]['Destination'] == '/var/lib/hub'
            report = json.loads(run('docker','exec',name,'python','-c',PROBE,commit,timeout=60))
            assert report['settings_profile'] == 'passed' and report['build_commit'] == commit
            reports.append(report)
            stop_owned(name,owner)
            names.remove(name)
        return {'source_commit':commit,'resolved_image_id':image_id,'settings_profile':'passed',
            'stages':reports,'recreations':2,'network':'none','host_mounts':False,'host_ports':False,
            'production_state':'never_opened','truenas':'not_deployed','human_sign_in':'not_tested',
            'agent_wake':'not_tested'}
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
