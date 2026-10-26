"""HTTP over a real dedicated registry; fixture is generated semantically."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.responses import JSONResponse

from engineering_hub import infrastructure as module
from engineering_hub.infrastructure import InfrastructureRegistry,InfrastructurePrincipal,CAPABILITIES
from engineering_hub.infrastructure_api import attach_infrastructure_routes
from engineering_hub.infrastructure_service import InfrastructureService
from engineering_hub.repository import HubError


BASE='/api/hub/v1/infrastructure'
CF='human:cf:'+'a'*64
HUMAN={'X-Test-Human':'human:1','X-Test-CSRF':'ok'}
AGENT={'Authorization':'Bearer hermes-token'}


def draft():
    return dict(name='Fixture device',hostname=None,addresses=['192.0.2.1'],network='192.0.2.0/24',
                vlan=None,role='Fixture',retired=False,expected_revision=0)


@pytest.fixture
def env():
    clock=[datetime.fromisoformat('2026-10-25T12:00:00+00:00')]
    changes=[]
    registry=InfrastructureRegistry(sqlite3.connect(':memory:',check_same_thread=False),capabilities={
        InfrastructurePrincipal('human','human:1'):CAPABILITIES,
        InfrastructurePrincipal('human',CF):{'read','maintenance_write'},
        InfrastructurePrincipal('agent','hermes'):{'read','maintenance_write','probe_write'},
        InfrastructurePrincipal('agent','codex'):{'read'}},clock=lambda:clock[0])
    service=InfrastructureService(registry,on_change=changes.append)
    calls=[]
    def actor(request,mutation=False):
        calls.append(mutation)
        bearer=request.headers.get('authorization')
        if bearer:
            if bearer not in ('Bearer hermes-token','Bearer codex-token','Bearer denied-token'):
                raise HubError('unauthorized',401)
            return {'Bearer hermes-token':'hermes','Bearer codex-token':'codex','Bearer denied-token':'claude'}[bearer]
        identity=request.headers.get('x-test-human')
        if not identity:raise HubError('unauthorized',401)
        if mutation and request.headers.get('x-test-csrf')!='ok':raise HubError('csrf_required',403)
        return identity
    app=FastAPI()
    @app.exception_handler(HubError)
    async def error(request,exc):return JSONResponse({'detail':{'code':exc.code}},status_code=exc.status)
    attach_infrastructure_routes(app,service,actor)
    with TestClient(app) as client:yield client,service,clock,changes,calls
    registry.close()


def test_exact_fixture_semantics_from_real_registry_and_http(env,monkeypatch):
    client,service,clock,_,_=env
    fixture=json.loads((Path(__file__).parents[1]/'hub-ui/src/fixtures/infrastructure-v1.json').read_text())
    build,camera=fixture['list']['items']
    ids=iter([build['open_maintenance']['id'],fixture['detail']['maintenance']['items'][0]['id']])
    monkeypatch.setattr(module,'uuid',SimpleNamespace(UUID=uuid.UUID,uuid4=lambda:uuid.UUID(next(ids))))
    fields=('id','name','hostname','addresses','network','vlan','role','retired')
    for item in (build,camera):
        clock[0]=datetime.fromisoformat(item['updated_at'])
        declared={key:item[key] for key in fields}
        for revision in range(item['revision']):service.upsert_device('human:1',declared,expected_revision=revision)
    opened=build['open_maintenance'];clock[0]=datetime.fromisoformat(opened['opened_at'])
    assert service.append_maintenance('hermes',build['id'],opened['reason'],'build-note',expected_end=opened['expected_end'])==opened
    resolved=fixture['detail']['maintenance']['items'][0];clock[0]=datetime.fromisoformat(resolved['opened_at'])
    entry=service.append_maintenance(CF,camera['id'],resolved['reason'],'camera-note',expected_end=resolved['expected_end'])
    clock[0]=datetime.fromisoformat(resolved['resolved_at'])
    assert service.resolve_maintenance('hermes',entry['id'],expected_revision=1,resolution=resolved['resolution'])==resolved
    clock[0]=datetime.fromisoformat('2026-10-25T12:00:00+00:00')
    for probe in build['observations']:
        service.record_probe('hermes',build['id'],{'ok':'healthy','failed':'unhealthy'}[probe['state']],
            probe_id=probe['probe_id'],source_label=probe['source_label'],observed_at=probe['observed_at'],valid_until=probe['valid_until'])
    # Exact semantic DTO equality, not merely matching keys. All times use UTC
    # ISO+00:00 in this fixture, so no timestamp representation normalization.
    assert client.get(BASE+'/devices',headers=HUMAN).json()==fixture['list']
    assert client.get(BASE+'/devices/camera-hub',headers={'X-Test-Human':CF}).json()==fixture['detail']


def test_http_mutations_cas_idempotency_csrf_and_callbacks(env):
    client,service,_,changes,_=env
    created=client.put(BASE+'/devices/staging',headers=HUMAN,json=draft())
    assert created.status_code==200,created.text
    assert created.json()['observations']==[] and created.json()['revision']==1
    assert changes==[{'kind':'infrastructure.changed','device_id':'staging'}]
    assert client.put(BASE+'/devices/staging',headers=AGENT,json=draft()).status_code==403
    assert client.put(BASE+'/devices/staging',headers={'X-Test-Human':'human:1'},json=draft()).status_code==403
    assert client.put(BASE+'/devices/staging',headers=HUMAN,json=draft()).status_code==409
    payload={'reason':'Planned\nFixture maintenance','expected_end':None,'client_request_id':'request-1'}
    opened=client.post(BASE+'/devices/staging/maintenance',headers=AGENT,json=payload)
    assert opened.status_code==200
    assert opened.json()['opened_by']=={'kind':'agent','identity':'hermes'}
    assert client.post(BASE+'/devices/staging/maintenance',headers=AGENT,json=payload).json()==opened.json()
    changed={**payload,'reason':'Changed'}
    assert client.post(BASE+'/devices/staging/maintenance',headers=AGENT,json=changed).status_code==409
    path=BASE+'/maintenance/'+opened.json()['id']+'/resolve'
    resolved=client.post(path,headers=HUMAN,json={'expected_revision':1,'resolution':'Verified\nResolved'})
    assert resolved.status_code==200 and resolved.json()['resolved_by']['identity']=='human:1'
    assert client.post(path,headers=HUMAN,json={'expected_revision':1,'resolution':'Again'}).status_code==409


@pytest.mark.parametrize('headers,code',[({},401),({'Authorization':'Bearer denied-token'},403),
    ({'Authorization':'Bearer codex-token'},403),({'X-Test-Human':'human:1'},403)])
def test_auth_and_capability_before_body_or_missing_resource(env,headers,code):
    client,*_=env
    response=client.post(BASE+'/devices/does-not-exist/maintenance',headers={**headers,'Content-Type':'application/json'},content=b'bad'+b'x'*20000)
    assert response.status_code==code,response.text


def test_duplicate_malformed_bearer_never_cookie_fallback(env):
    client,_,_,_,calls=env
    for auth in [[('Authorization','Bearer hermes-token'),('Authorization','Bearer codex-token')],
                 [('Authorization','Basic secret')],[('Authorization','Bearer bad token')]]:
        calls.clear()
        response=client.get(BASE+'/devices',headers=[*auth,('X-Test-Human','human:1')])
        assert response.status_code==401 and calls==[]


@pytest.mark.parametrize('body,status',[(b'{"name":"private-secret","name":"duplicate"}',400),
    (b'{"actor":"spoofed-private-secret"}',400),(b'null',400),(b'{"expected_revision":true}',400),
    (b'x'*16385,413)])
def test_bounded_closed_sanitized_inputs(env,body,status):
    client,*_=env
    response=client.put(BASE+'/devices/staging',headers={**HUMAN,'Content-Type':'application/json'},content=body)
    assert response.status_code==status,response.text
    assert 'private-secret' not in response.text


def test_stream_limit_unknown_query_and_no_probe_http_write(env):
    client,*_=env
    response=client.put(BASE+'/devices/staging',headers={**HUMAN,'Content-Type':'application/json'},
                        content=iter([b'x'*10000,b'x'*10000]))
    assert response.status_code==413
    assert client.get(BASE+'/devices?include_retired=true&include_retired=false',headers=HUMAN).status_code==400
    assert client.get(BASE+'/devices?url=http://example.invalid',headers=HUMAN).status_code==400
    assert client.post(BASE+'/devices/staging/probes',headers=HUMAN,json={}).status_code==404


def test_unconfigured_and_invalid_callback_actor_are_closed():
    app=FastAPI()
    @app.exception_handler(HubError)
    async def error(request,exc):return JSONResponse({'detail':{'code':exc.code}},status_code=exc.status)
    attach_infrastructure_routes(app,None,lambda request,mutation=False:'human:1')
    with TestClient(app) as client:assert client.get(BASE+'/devices').status_code==503
    with pytest.raises(HubError):InfrastructureService.principal('agent:future')
    with pytest.raises(HubError):InfrastructureService.principal(['codex'])


def test_invalidation_failure_does_not_reverse_committed_write(env):
    _,service,*_=env
    def failed(event):raise RuntimeError('private details must not escape')
    service.on_change=failed
    item=draft();item.pop('expected_revision');item['id']='test-device'
    assert service.upsert_device('human:1',item,expected_revision=0)['revision']==1
    assert service.invalidation_failed is True
    assert service.get_device('human:1','test-device')['device']['revision']==1
