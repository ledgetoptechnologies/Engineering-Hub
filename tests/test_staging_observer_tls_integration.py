"""Opt-in two-image observer TLS integration, no published ports or live host."""
import base64
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

import pytest

NGINX = 'engineering-hub-backend-qa:node24-vitest-nginx-20261030-v1'
HUB = 'ghcr.io/ledgetoptechnologies/engineering-hub-control:codex-local-core-2ed3351-20261030'

PROXY_SETUP = r'''
import base64,pathlib,subprocess
r=pathlib.Path('/shared'); r.chmod(0o755)
subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(r/'key'),
 '-out',str(r/'ca'),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],
 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(r/'wrong-key'),
 '-out',str(r/'wrong-ca'),'-days','1','-subj','/CN=wrong-synthetic-ca'],
 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
t=base64.b64decode(TEMPLATE).decode()
for k,v in {'__STAGING_BIND_IP__':'127.0.0.1','__TLS_PORT__':'19443',
 '__CERTIFICATE_CHAIN_PATH__':str(r/'ca'),'__CERTIFICATE_KEY_PATH__':str(r/'key'),
 '__APPROVED_NAS_IP__':'127.0.0.1','__OBSERVER_PORT__':'18781'}.items(): t=t.replace(k,v)
(r/'http.conf').write_text(t)
(r/'nginx.conf').write_text('pid /shared/nginx.pid; error_log /dev/null info; events { worker_connections 32; } '
 'http { client_body_temp_path /tmp/body; proxy_temp_path /tmp/proxy; fastcgi_temp_path /tmp/fastcgi; '
 'uwsgi_temp_path /tmp/uwsgi; scgi_temp_path /tmp/scgi; include /shared/http.conf; }')
subprocess.run(['nginx','-c','/shared/nginx.conf'],check=True)
'''

CLIENT_TEST = r'''
import asyncio,datetime,http.client,ipaddress,json,os,pathlib,socket,ssl,subprocess,sys,time
import httpx
from engineering_hub.staging_client import StagingConnectionConfig
from engineering_hub.staging_observer import StagingObserver
from hub_staging_service.__main__ import BoundedServer
from hub_staging_service.observer import handler
from hub_staging_service.runtime_inventory import _empty,_source,_timestamp,publish_host
r=pathlib.Path('/shared');token=r/'token'; token.write_text('synthetic-test-token-only'); token.chmod(0o600)
now=datetime.datetime.now(datetime.timezone.utc)
host=_empty(now);host['sources']=[_source('host','observed',_timestamp(now))]
publish_host(host,r/'snapshot.json')
observer_token=r/'observer-token'; observer_token.write_text('synthetic-test-token-only')
os.chown(observer_token,12345,12345); observer_token.chmod(0o600)
assert not pathlib.Path('/var/run/docker.sock').exists()
def drop_identity():
 os.setgroups([]);os.setgid(12345);os.setuid(12345)
command=[sys.executable,'-m','hub_staging_service.observer','--snapshot',str(r/'snapshot.json'),
 '--token-file',str(observer_token),'--port','18781']
for mode in (0o644,0o000):
 observer_token.chmod(mode)
 failed=subprocess.run(command,preexec_fn=drop_identity,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
 assert failed.returncode != 0
observer_token.chmod(0o600)
link=r/'observer-hardlink';os.link(observer_token,link)
assert subprocess.run(command,preexec_fn=drop_identity,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5).returncode != 0
link.unlink()
os.chown(observer_token,12346,12345)
assert subprocess.run(command,preexec_fn=drop_identity,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5).returncode != 0
os.chown(observer_token,12345,12345)
real=r/'observer-real-token';observer_token.rename(real);observer_token.symlink_to(real.name)
assert subprocess.run(command,preexec_fn=drop_identity,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5).returncode != 0
observer_token.unlink();real.rename(observer_token)
def drop_with_group():
 os.setgroups([12346]);os.setgid(12345);os.setuid(12345)
assert subprocess.run(command,preexec_fn=drop_with_group,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5).returncode != 0
observer=subprocess.Popen(command,preexec_fn=drop_identity,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
for _ in range(60):
 try: socket.create_connection(('127.0.0.1',18781),timeout=.1).close();break
 except OSError: time.sleep(.05)
else:
 observer.terminate();observer.wait(timeout=5)
 raise AssertionError('unprivileged observer failed to start')
try:
 status=pathlib.Path('/proc')/str(observer.pid)/'status'
 fields=dict(line.split(':',1) for line in status.read_text().splitlines() if ':' in line)
 assert fields['Uid'].split()[:3] == ['12345']*3
 assert fields['Gid'].split()[:3] == ['12345']*3
 assert fields['Groups'].strip() == ''
 assert not pathlib.Path('/var/run/docker.sock').exists()
except BaseException:
 observer.terminate();observer.wait(timeout=5)
 raise
async def run():
 config=StagingConnectionConfig(origin='https://127.0.0.1:19443',credential_file=token,ca_file=r/'ca',admin_actors=frozenset({'human:1'}),enabled=True)
 client=StagingObserver(config)
 try:
  result=await client.runtime('human:1');assert result['state']=='ready' and result['snapshot']['sources'][1]['state']=='observed'
  host['observed_at']=_timestamp(now-datetime.timedelta(seconds=100));host['sources'][0]['observed_at']=host['observed_at']
  (r/'snapshot.json').write_text(json.dumps(host)); (r/'snapshot.json').chmod(0o644)
  result=await client.runtime('human:1');assert result['snapshot']['sources'][1]['state']=='stale' and not result['snapshot']['containers']
  (r/'snapshot.json').chmod(0o666)
  result=await client.runtime('human:1');assert result['snapshot']['sources'][1]['state']=='unavailable' and not result['snapshot']['containers']
  (r/'snapshot.json').chmod(0o644)
  r.chmod(0o777)
  result=await client.runtime('human:1');assert result['snapshot']['sources'][1]['state']=='unavailable'
  r.chmod(0o755)
 finally: await client.close()
 wrong_token=r/'wrong-token';wrong_token.write_text('wrong-synthetic-token');wrong_token.chmod(0o600)
 for credential,ca in ((token,r/'wrong-ca'),(wrong_token,r/'ca')):
  bad_config=StagingConnectionConfig(origin='https://127.0.0.1:19443',credential_file=credential,
   ca_file=ca,admin_actors=frozenset({'human:1'}),enabled=True)
  bad=StagingObserver(bad_config)
  try:
   assert (await bad.runtime('human:1')) == {'configured':True,'state':'unavailable','snapshot':None}
  finally: await bad.close()
 context=ssl.create_default_context(cafile=str(r/'ca'))
 async with httpx.AsyncClient(verify=context,trust_env=False,timeout=3) as raw:
  assert (await raw.get('https://127.0.0.1:19443/v1/runtime',headers={'Authorization':'Bearer wrong-synthetic-token'})).status_code==401
  assert (await raw.post('https://127.0.0.1:19443/v1/jobs',headers={'Authorization':'Bearer synthetic-test-token-only'})).status_code==404
  assert (await raw.post('https://127.0.0.1:19443/v1/runtime',headers={'Authorization':'Bearer synthetic-test-token-only'})).status_code==404
 try:
  async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(r/'wrong-ca')),trust_env=False,timeout=3) as wrong:
   await wrong.get('https://127.0.0.1:19443/v1/runtime')
  raise AssertionError('untrusted CA accepted')
 except httpx.ConnectError as error:
  assert 'CERTIFICATE_VERIFY_FAILED' in str(error)
 ctx=ssl.create_default_context(cafile=str(r/'ca'))
 with ctx.wrap_socket(socket.create_connection(('127.0.0.1',19443),source_address=('127.0.0.2',0),timeout=3),server_hostname='127.0.0.1') as s:
  s.sendall(b'GET /v1/runtime HTTP/1.1\r\nHost: localhost\r\nX-Forwarded-For: 127.0.0.1\r\nConnection: close\r\n\r\n')
  assert b' 403 ' in s.recv(4096).split(b'\r\n')[0]
try:
 asyncio.run(run())
 from agent_message_bus.auth import AuthSettings
 from engineering_hub.app import Settings, create_app
 from engineering_hub.auth import AuthConfig
 from fastapi.testclient import TestClient
 (r/'staging-observer.json').write_text(json.dumps({'schema':1,'enabled':True,'origin':'https://127.0.0.1:19443',
  'credential_file':str(token),'ca_file':str(r/'ca'),'admin_actors':['human:42']}))
 (r/'staging-observer.json').chmod(0o600)
 settings=Settings(r/'hub.sqlite3',AuthSettings('synthetic-hermes','synthetic-codex',claude_token='synthetic-claude'),
  AuthConfig('https://hub.test',beau_github_id=42))
 app=create_app(settings)
 with TestClient(app,base_url=settings.auth_config.origin) as web:
  assert web.get('/api/hub/v1/staging/runtime').status_code==401
  non_admin=app.state.sessions.create_session(43)
  web.cookies.set(settings.auth_config.cookie_name,non_admin['cookie_token'])
  assert web.get('/api/hub/v1/staging/runtime').status_code==403
  session=app.state.sessions.create_session(42)
  web.cookies.set(settings.auth_config.cookie_name,session['cookie_token'])
  response=web.get('/api/hub/v1/staging/runtime')
  assert response.status_code==200, response.text
  assert response.json()['snapshot']['sources'][1]['state']=='stale'
 print('real observer and hub client TLS checks passed')
finally:
 observer.terminate();observer.wait(timeout=5)
'''

@pytest.mark.skipif(os.environ.get('EH_TEST_NGINX_PROXY') != '1', reason='explicit opt-in required')
def test_isolated_real_observer_tls():
    docker = shutil.which('docker')
    if not docker: pytest.skip('Docker unavailable')
    def call(args, **kwargs):
        return subprocess.run([docker, *args], capture_output=True, text=True, timeout=30, **kwargs)
    ids=[]
    try:
        for tag in (NGINX,HUB):
            result=call(['image','inspect','--format','{{.Id}}',tag])
            if result.returncode: pytest.skip('Docker runtime or required cached image unavailable')
            identity=result.stdout.strip()
            assert re.fullmatch(r'sha256:[0-9a-f]{64}',identity)
            ids.append(identity)
    except (OSError,subprocess.TimeoutExpired): pytest.skip('Docker runtime unavailable')
    print('images:',*ids,flush=True)
    suffix=uuid.uuid4().hex;volume='eh-observer-fixture-'+suffix; name='eh-observer-net-'+suffix
    client='eh-observer-client-'+suffix
    try:
        assert call(['volume','create',volume]).returncode==0
        result=call(['run','-d','--name',name,'--pull=never','--network','none','--user','0',
            '--memory','128m','--cpus','0.5','--pids-limit','32','--mount',f'type=volume,src={volume},dst=/shared',
            '--entrypoint','sleep',ids[0],'120'])
        assert result.returncode==0,result.stderr
        template=base64.b64encode(Path('deployment/staging-observer/nginx-http.conf').read_bytes()).decode()
        result=call(['exec','-i',name,'python3','-'],input='TEMPLATE='+repr(template)+'\n'+PROXY_SETUP)
        assert result.returncode==0,result.stderr
        src=str(Path('src').resolve())
        result=call(['run','--rm','-i','--name',client,'--pull=never','--network','container:'+name,'--user','0',
            '--memory','256m','--cpus','0.5','--pids-limit','64','--mount',f'type=volume,src={volume},dst=/shared',
            '--mount',f'type=bind,src={src},dst=/repo/src,readonly','--env','PYTHONPATH=/repo/src',
            '--entrypoint','python',ids[1],'-'],input=CLIENT_TEST)
        assert result.returncode==0,result.stderr
        assert 'TLS checks passed' in result.stdout
    finally:
        original=sys.exception(); issues=[]
        for kind,item in [('container',client),('container',name),('volume',volume)]:
            try:
                removal=call(['rm','-f',item] if kind=='container' else ['volume','rm',item])
                check=call([kind,'inspect',item])
                if check.returncode==0 or not any(x in check.stderr.lower() for x in ('no such object','no such container','no such volume')):
                    issues.append(kind+' cleanup unverified')
                elif removal.returncode and not any(x in removal.stderr.lower() for x in ('no such object','no such container','no such volume')):
                    issues.append(kind+' removal failed')
            except (OSError,subprocess.TimeoutExpired): issues.append(kind+' cleanup unavailable')
        if issues:
            if original: original.add_note('Cleanup failure: '+', '.join(issues))
            else: pytest.fail('Cleanup failure: '+', '.join(issues))
