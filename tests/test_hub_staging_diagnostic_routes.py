"""Real app/session/public-private gateway; synthetic Access and worker I/O.

No external identity revocation, UDS deployment, worker execution or staging
host acceptance is claimed. Async gates exercise revocation between reads.
"""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.cloudflare_access import CloudflareIdentity
from engineering_hub.private_gateway import PublicBrowserGateway, PrivateBrowserGateway, browser_route_allowed
from engineering_hub.staging_client import StagingClient
from engineering_hub.staging_module import StagingModule
from tests.test_hub_frontends import Feed
from tests.test_hub_staging_diagnostics import diagnostic_env, controller_env, config


BASE='/api/hub/v1/staging/diagnostics/'
HUMAN='human:cf:'+'a'*64


@pytest_asyncio.fixture
async def routed(diagnostic_env,monkeypatch):
    controller,worker,resources,repo,old_client,identity,data,_,_=diagnostic_env
    requests=[];hooks={};now=[time.time()]
    async def worker_read(request):
        requests.append((request.method,request.url.path))
        key='metadata' if request.url.path.endswith('/metadata') else 'file'
        if hooks.get(key):await hooks[key]()
        if key=='metadata':return httpx.Response(200,json=worker.facts[identity])
        return httpx.Response(200,content=data,headers={'content-type':'application/octet-stream'})
    wire=StagingClient(old_client.config,test_transport=httpx.MockTransport(worker_read))
    previous=controller.client;controller.client=wire
    module=StagingModule(controller=controller,client=wire,config=wire.config,status={'state':'ready'})
    monkeypatch.setattr('engineering_hub.staging_module.compose_staging',lambda *a,**k:module)
    access_identity=CloudflareIdentity('synthetic-human','operator@example.com',HUMAN,int(time.time())+3600)
    class Checker:
        async def verify(self,token):
            assert token=='synthetic-access'
            return access_identity
    monkeypatch.setattr('engineering_hub.cloudflare_access.CloudflareAccessVerifier',lambda _:Checker())
    monkeypatch.setattr('engineering_hub.ingress.CloudflareAccessVerifier',lambda _:Checker())
    auth=AuthSettings('fixture-hermes','fixture-codex',claude_token='fixture-claude')
    auth_config=AuthConfig('https://hub.example',human_auth_mode='cloudflare_access',
        cloudflare_issuer='https://fixture-team.cloudflareaccess.com',cloudflare_audience='fixture-hub',
        cloudflare_allowed_emails=('operator@example.com',))
    core=create_app(Settings(repo.path,auth,auth_config),repository=repo,work_queue=controller.queue,event_feed=Feed())
    sessions=core.state.sessions;sessions.clock=lambda:now[0]
    original=sessions.create_cloudflare_session(auth_config,access_identity)
    alternate=sessions.create_cloudflare_session(auth_config,access_identity)
    policy=resources.get_policy('human:1','engineering-hub')
    grants=dict(policy['grants']);grants[HUMAN]=['read']
    resources.set_policy('human:1','engineering-hub',policy['revision'],grants)
    receiver=PrivateBrowserGateway(core,agent_auth=auth,cloudflare_config=auth_config.cloudflare_config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=receiver)) as private:
        gateway=PublicBrowserGateway(private)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway),base_url=auth_config.origin) as browser, \
                   httpx.AsyncClient(transport=httpx.ASGITransport(app=core),base_url=auth_config.origin) as direct:
            for client in (browser,direct):
                client.headers['Cf-Access-Jwt-Assertion']='synthetic-access'
                client.cookies.set(auth_config.cookie_name,original['cookie_token'])
            state=SimpleNamespace(core=core,browser=browser,direct=direct,resources=resources,repo=repo,
                worker=worker,wire=wire,identity=identity,data=data,requests=requests,hooks=hooks,now=now,
                sessions=sessions,original=original,alternate=alternate,auth_config=auth_config,module=module)
            try:yield state
            finally:
                controller.client=previous
                await wire.aclose()


@pytest.mark.asyncio
async def test_real_app_gateway_downloads_exact_attachment_without_csrf(routed):
    state=routed;url=BASE+state.identity
    listed=await state.browser.get(url)
    assert listed.status_code==200,listed.text
    assert set(listed.json()['files'])=={'test/report.html'}
    response=await state.browser.get(url+'/files/test/report.html')
    assert response.status_code==200 and response.content==state.data
    assert response.headers['content-type']=='application/octet-stream'
    assert response.headers['content-disposition']=='attachment; filename="report.html"'
    assert response.headers['x-content-type-options']=='nosniff'
    assert response.headers['content-security-policy'].startswith('sandbox;')
    assert response.headers['cache-control']=='no-store' and response.headers['referrer-policy']=='no-referrer'
    assert all(method=='GET' for method,_ in state.requests) and state.worker.posts==1
    assert 'x-hub-csrf' not in state.browser.headers and 'origin' not in state.browser.headers


@pytest.mark.asyncio
@pytest.mark.parametrize('authorization',[[('Authorization','Bearer fixture-codex')],
    [('Authorization','')],[('Authorization','malformed')],
    [('Authorization','Bearer fixture-codex'),('Authorization','Bearer fixture-claude')]])
async def test_agent_or_malformed_bearer_never_falls_through_human_cookie(routed,authorization):
    state=routed
    for client in (state.browser,state.direct):
        response=await client.get(BASE+state.identity,headers=authorization)
        assert response.status_code in (401,403)
    assert state.requests==[]


@pytest.mark.asyncio
async def test_missing_configuration_and_forbidden_project_are_opaque(routed,monkeypatch):
    state=routed
    policy=state.resources.get_policy('human:1','engineering-hub');grants=dict(policy['grants']);grants.pop(HUMAN)
    state.resources.set_policy('human:1','engineering-hub',policy['revision'],grants)
    denied=await state.browser.get(BASE+state.identity)
    assert denied.status_code==403 and state.requests==[]
    # Construct a separate real app whose optional staging config is absent.
    module=StagingModule(status={'state':'not_configured'})
    monkeypatch.setattr('engineering_hub.staging_module.compose_staging',lambda *a,**k:module)
    missing=create_app(Settings(state.repo.path,state.core.state.settings.agent_auth,state.auth_config),repository=state.repo,event_feed=Feed())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=missing),base_url=state.auth_config.origin) as client:
        response=await client.get(BASE+state.identity,headers={'Cf-Access-Jwt-Assertion':'synthetic-access',
            'Cookie':state.auth_config.cookie_name+'='+state.original['cookie_token']})
        assert response.status_code==503 and response.json()['detail']['code']=='staging_not_configured'
    assert state.requests==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['session','project','expiry','enrollment'])
async def test_revocation_between_real_app_awaits_never_returns_report(routed,change):
    state=routed;entered=asyncio.Event();release=asyncio.Event()
    async def gate():entered.set();await release.wait()
    state.hooks['file' if change=='project' else 'metadata']=gate
    if change=='expiry':
        binding=state.sessions.preview_session_binding(HUMAN,state.original['cookie_token'])
        with state.sessions._db() as db:
            db.execute('UPDATE auth_access_sessions SET expires=? WHERE token_hash=?',(state.now[0]+10,binding[0]))
    path=BASE+state.identity+('/files/test/report.html' if change=='project' else '')
    request=asyncio.create_task(state.browser.get(path))
    try:
        await asyncio.wait_for(entered.wait(),3)
        if change=='session':
            state.sessions.delete_cloudflare_session(state.original['cookie_token'])
            assert state.sessions.preview_session_binding(HUMAN,state.alternate['cookie_token']) is not None
        elif change=='project':
            policy=state.resources.get_policy('human:1','engineering-hub');grants=dict(policy['grants']);grants.pop(HUMAN)
            state.resources.set_policy('human:1','engineering-hub',policy['revision'],grants)
        elif change=='expiry':
            # Even extending the DB row cannot extend the original read's bound
            # expiry. The current human session remains otherwise valid.
            with state.sessions._db() as db:
                db.execute('UPDATE auth_access_sessions SET expires=? WHERE token_hash=?',(state.now[0]+300,binding[0]))
            state.now[0]+=11
        else:
            with state.sessions._db() as db:db.execute('DELETE FROM auth_access_identities WHERE actor=?',(HUMAN,))
        release.set();response=await asyncio.wait_for(request,3)
        assert response.status_code==403 and state.data not in response.content
        assert 'content-disposition' not in response.headers
    finally:
        release.set()
        if not request.done():await asyncio.wait_for(request,3)


@pytest.mark.parametrize('path',[
    '/files/../secret','/files//report','/files/%2e%2e/secret','/files/https://other/report',
    '/files/a\\b','/files/','/files/'+'a'*513,'/other'])
def test_gateway_diagnostic_paths_are_exact_and_bounded(path):
    identity='01234567-89ab-cdef-0123-456789abcdef'
    assert not browser_route_allowed('GET',BASE+identity+path)
    assert not browser_route_allowed('POST',BASE+identity)
    assert not browser_route_allowed('GET',BASE+'not-a-uuid')


@pytest.mark.asyncio
@pytest.mark.parametrize('size,expected',[(8*1024**2,200),(8*1024**2+1,502)])
async def test_gateway_attachment_budget_is_exact_and_other_routes_keep_general_limit(size,expected):
    body=b'x'*size
    async def upstream(request):
        return httpx.Response(200,content=body,headers={'content-type':'application/octet-stream',
            'content-disposition':'attachment; filename="report.bin"'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as private:
        gateway=PublicBrowserGateway(private)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway),base_url='https://hub.example') as browser:
            response=await browser.get(BASE+'01234567-89ab-cdef-0123-456789abcdef/files/report.bin')
            assert response.status_code==expected
            if expected==200:
                assert response.content==body and response.headers['content-disposition']=='attachment; filename="report.bin"'
            general=await browser.get('/api/hub/v1/staging/status')
            assert general.status_code==502
