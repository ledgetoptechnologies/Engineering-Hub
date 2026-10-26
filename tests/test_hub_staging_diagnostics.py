"""Canonical Hub queue/ledger with synthetic worker + real client MockTransport."""
import asyncio
import hashlib
import json

import httpx
import pytest
import pytest_asyncio

from engineering_hub.repository import HubError
from engineering_hub.staging_client import StagingClient, StagingClientError
from engineering_hub.staging_diagnostics import diagnostic_manifest, diagnostic_download, MAX_DOWNLOAD_BYTES
from tests.test_hub_staging_controller import env as controller_env, payload
from tests.test_hub_staging_client import config


@pytest_asyncio.fixture
async def diagnostic_env(controller_env,config):
    controller,worker,resources,_,repo=controller_env
    worker.fail=True;original=worker.submit
    data=b'<script>source-controlled HTML must download, never execute</script>'
    async def submitted(request):
        receipt=await original(request);identity=request['resource_ids']['test_run']
        fact=worker.facts[identity]
        fact.update(manifest={'test/report.html':{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}},bytes=len(data))
        receipt['result']['diagnostic']={'resource_id':identity,'bytes':len(data),'stage':'test',
            'manifest_sha256':hashlib.sha256(json.dumps(fact['manifest'],sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'scope':'original_engine_owned_writers_exporters_volumes_absent'}
        return receipt
    worker.submit=submitted
    accepted=await controller.submit('codex',payload());finished=await controller.run_once()
    assert finished['job']['state']=='failed'
    identity=accepted['resource_ids']['test_run'];requests=[];behavior={}
    async def response(request):
        requests.append((request.method,request.url.path))
        if request.url.path.endswith('/metadata'):
            return httpx.Response(200,json=worker.facts[identity])
        if behavior.get('revoke'):
            resources.set_policy('human:1','engineering-hub',1,{'codex':['read','build','test']})
        if behavior.get('before_download'):
            behavior['before_download']()
        body=behavior.get('bytes',data)
        return httpx.Response(200,content=body,headers={'content-type':behavior.get('media','application/octet-stream')})
    client=StagingClient(config,test_transport=httpx.MockTransport(response))
    try:yield controller,worker,resources,repo,client,identity,data,requests,behavior
    finally:await client.aclose()


@pytest.mark.asyncio
async def test_failed_test_run_digest_is_registered_without_build_or_preview_publish(diagnostic_env):
    controller,worker,resources,repo,client,identity,data,requests,behavior=diagnostic_env
    resource=resources.get_resource('human:1',identity)
    assert resource['kind']=='test_run' and resource['state']=='failed' and resource['artifact_digest'].startswith('sha256:')
    siblings=resources.list_resources('human:1','engineering-hub')['items']
    assert all(item['state']=='failed' for item in siblings)
    assert all(item['artifact_digest'] is None and item['preview_path'] is None
               for item in siblings if item['kind']!='test_run')
    listed=await diagnostic_manifest(resources,client,'human:1',identity,session_alive=lambda:True)
    assert listed['files']==worker.facts[identity]['manifest']
    response=await diagnostic_download(resources,client,'human:1',identity,'test/report.html',session_alive=lambda:True)
    assert response.body==data and response.media_type=='application/octet-stream'
    assert response.headers['content-disposition']=='attachment; filename="report.html"'
    assert response.headers['x-content-type-options']=='nosniff' and 'sandbox' in response.headers['content-security-policy']
    assert 'no-store' in response.headers['cache-control']
    assert requests[-1][0]=='GET' and '/diagnostics/'+listed['manifest_sha256']+'/' in requests[-1][1]
    assert worker.posts==1


@pytest.mark.asyncio
@pytest.mark.parametrize('path',['../journal.sqlite3','/etc/passwd','test/../report.html','test//report.html','https://other.invalid/report','test/%2e%2e/report'])
async def test_download_traversal_or_external_path_rejected_before_worker_read(diagnostic_env,path):
    _,_,resources,_,client,identity,_,requests,_=diagnostic_env
    with pytest.raises(HubError):
        await diagnostic_download(resources,client,'human:1',identity,path,session_alive=lambda:True)
    assert requests==[]


@pytest.mark.asyncio
async def test_revoked_project_read_prevents_any_worker_read(diagnostic_env):
    _,_,resources,_,client,identity,_,requests,_=diagnostic_env
    resources.set_policy('human:1','engineering-hub',1,{'codex':['read','build','test']})
    with pytest.raises(HubError):await diagnostic_manifest(resources,client,'human:1',identity,session_alive=lambda:True)
    assert requests==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['revoke','session','bytes','mime','revision'])
async def test_mid_read_revocation_or_changed_bytes_never_returns_attachment(diagnostic_env,change):
    _,_,resources,repo,client,identity,_,requests,behavior=diagnostic_env
    alive=[True]
    if change=='revoke':behavior['revoke']=True
    elif change=='session':behavior['before_download']=lambda:alive.__setitem__(0,False)
    elif change=='bytes':behavior['bytes']=b'tampered'
    elif change=='mime':behavior['media']='text/html'
    else:
        def altered():
            with repo._transaction() as db:
                row=db.execute('SELECT data FROM hub_entities WHERE id=?',(identity,)).fetchone()
                item=json.loads(row[0]);item['revision']+=1
                db.execute('UPDATE hub_entities SET data=? WHERE id=?',(json.dumps(item),identity))
        behavior['before_download']=altered
    with pytest.raises(HubError):
        await diagnostic_download(resources,client,'human:1',identity,'test/report.html',session_alive=lambda:alive[0])
    assert requests[-1][0]=='GET'


@pytest.mark.asyncio
async def test_oversized_manifest_entry_denied_before_file_request(diagnostic_env):
    _,worker,resources,repo,client,identity,_,requests,_=diagnostic_env
    fact=worker.facts[identity];fact['manifest']['test/report.html']['bytes']=MAX_DOWNLOAD_BYTES+1;fact['bytes']=MAX_DOWNLOAD_BYTES+1
    digest='sha256:'+hashlib.sha256(json.dumps(fact['manifest'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with repo._transaction() as db:
        row=db.execute('SELECT data FROM hub_entities WHERE id=?',(identity,)).fetchone();item=json.loads(row[0])
        item.update(artifact_digest=digest,size_bytes=fact['bytes'])
        db.execute('UPDATE hub_entities SET data=? WHERE id=?',(json.dumps(item),identity))
    with pytest.raises(HubError):await diagnostic_download(resources,client,'human:1',identity,'test/report.html',session_alive=lambda:True)
    assert len(requests)==1 and requests[0][1].endswith('/metadata')


@pytest.mark.asyncio
async def test_no_live_session_or_agent_actor_cannot_read(diagnostic_env):
    _,_,resources,_,client,identity,_,requests,_=diagnostic_env
    for actor,alive in [('human:1',False),('codex',True)]:
        with pytest.raises(HubError):await diagnostic_manifest(resources,client,actor,identity,session_alive=lambda:alive)
    assert requests==[]
