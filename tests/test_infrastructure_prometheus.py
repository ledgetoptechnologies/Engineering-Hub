"""Synthetic HTTP transport and temporary SQLite; no actual probe/network."""
import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime,timedelta,timezone
import json
import sqlite3

import httpx
import pytest

from engineering_hub import infrastructure_prometheus as module
from engineering_hub.infrastructure import InfrastructureRegistry,InfrastructurePrincipal,CAPABILITIES
from engineering_hub.infrastructure_service import InfrastructureService
from engineering_hub.infrastructure_prometheus import PrometheusOrigin,TargetMapping,PrometheusCollector
from engineering_hub.repository import HubError


NOW=datetime(2026,10,26,tzinfo=timezone.utc)


@pytest.fixture
def env():
    clock=[NOW]
    registry=InfrastructureRegistry(sqlite3.connect(':memory:'),capabilities={
        InfrastructurePrincipal('human','human:1'):CAPABILITIES,
        InfrastructurePrincipal('agent','hermes'):{'read','probe_write'},
        InfrastructurePrincipal('agent','codex'):{'read'}},clock=lambda:clock[0])
    service=InfrastructureService(registry)
    for identity,address in [('fixture-a','192.0.2.10'),('fixture-b','2001:db8::20')]:
        service.upsert_device('human:1',dict(id=identity,name=identity,addresses=[address],role='Synthetic'),expected_revision=0)
    yield service,clock
    registry.close()


def mapping():return TargetMapping('node','192.0.2.10:9100','fixture-a','node-scrape','Prometheus scrape')


def target(*,job='node',instance='192.0.2.10:9100',health='up',stamp=None,interval='15s'):
    return dict(labels={'job':job,'instance':instance},health=health,lastScrape=stamp or NOW.isoformat(),
                scrapeInterval=interval,scrapeUrl='http://example.invalid/private-never-followed',
                lastError='private-exporter-error-never-published')


def response(targets=None,*,raw=None,status=200,headers=None):
    raw=json.dumps({'status':'success','data':{'activeTargets':targets or []}}).encode() if raw is None else raw
    return httpx.Response(status,headers={'content-type':'application/json',**(headers or {})},stream=httpx.ByteStream(raw))


def collector(service,handler,mappings=None):
    return PrometheusCollector(service,PrometheusOrigin('192.0.2.1',9090),mappings or [mapping()],
                               test_transport=httpx.MockTransport(handler))


@pytest.mark.parametrize('address,port',[('localhost',9090),('http://192.0.2.1',9090),('192.000.2.1',9090),
    ('2001:0db8::1',9090),('fe80::1%eth0',9090),('192.0.2.1',True),('192.0.2.1',0),('192.0.2.1',65536)])
def test_origin_is_canonical_literal_no_urls_or_hostnames(address,port):
    with pytest.raises(ValueError):PrometheusOrigin(address,port)


@pytest.mark.parametrize('instance',['host.example:9100','192.0.2.10:09100','192.0.2.10','http://192.0.2.10:9100',
    '2001:db8::20:9100','[2001:0db8::20]:9100','[fe80::1%eth0]:9100','192.0.2.10:99999'])
def test_mapping_requires_exact_canonical_instance(instance):
    with pytest.raises(ValueError):TargetMapping('node',instance,'fixture-a','scrape')


@pytest.mark.asyncio
async def test_fixed_request_only_no_proxy_redirect_scrapeurl_or_declared_changes(env,monkeypatch):
    service,_=env;requests=[];client_options=[]
    actual=module.httpx.AsyncClient
    def client(**kwargs):client_options.append(kwargs);return actual(**kwargs)
    monkeypatch.setattr(module.httpx,'AsyncClient',client)
    monkeypatch.setenv('HTTP_PROXY','http://private-token@example.invalid:9999')
    def handler(request):
        requests.append(request)
        assert str(request.url)=='http://192.0.2.1:9090/api/v1/targets?state=active'
        assert request.method=='GET' and 'authorization' not in request.headers and 'cookie' not in request.headers
        assert request.extensions['timeout']['connect']==2 and request.extensions['timeout']['read']==5
        return response([target()])
    before=service.registry.get_device(service.principal('human:1'),'fixture-a')
    result=await collector(service,handler).collect('hermes')
    assert result['recorded']==1 and result['state']=='collected' and len(requests)==1
    assert client_options[0]['trust_env'] is False and client_options[0]['follow_redirects'] is False
    assert service.registry.get_device(service.principal('human:1'),'fixture-a')==before
    observation=service.get_device('hermes','fixture-a')['device']['observations'][0]
    assert observation==dict(probe_id='node-scrape',source_label='Prometheus scrape',state='ok',
        observed_at=NOW.isoformat(),valid_until=(NOW+timedelta(seconds=45)).isoformat())
    assert not any('success' in key for key in observation)
    assert 'private-' not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('actor',['codex','claude','human:1','future-agent'])
async def test_actual_agent_and_explicit_capability_before_network(env,actor):
    service,_=env;calls=[]
    def handler(request):calls.append(request);return response([target()])
    with pytest.raises(HubError,match='forbidden'):await collector(service,handler).collect(actor)
    assert calls==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['address','retired','missing'])
async def test_current_declared_address_device_and_retirement_before_network(env,change):
    service,_=env;calls=[]
    if change=='missing':maps=[TargetMapping('node','192.0.2.10:9100','absent','scrape')]
    else:
        maps=[mapping()]
        current=service.registry.get_device(service.principal('human:1'),'fixture-a')
        declaration={key:current[key] for key in ('id','name','addresses','role','hostname','network','vlan','retired')}
        declaration['addresses']=['192.0.2.11'] if change=='address' else declaration['addresses']
        declaration['retired']=change=='retired'
        service.upsert_device('human:1',declaration,expected_revision=1)
    def handler(request):calls.append(request);return response([target()])
    with pytest.raises(HubError):await collector(service,handler,maps).collect('hermes')
    assert calls==[]


@pytest.mark.asyncio
async def test_recheck_declaration_after_network_await_before_any_write(env):
    service,_=env
    async def handler(request):
        await asyncio.sleep(0)
        service.upsert_device('human:1',dict(id='fixture-a',name='Changed',addresses=['192.0.2.99'],role='Synthetic'),expected_revision=1)
        return response([target()])
    with pytest.raises(HubError,match='prometheus_mapping_invalid'):await collector(service,handler).collect('hermes')
    assert service.get_device('hermes','fixture-a')['device']['observations']==[]


@pytest.mark.asyncio
async def test_recheck_capability_after_await_before_write(env,monkeypatch):
    service,_=env;original=service.require;revoked=[False]
    def require(actor,action='read'):
        if revoked[0] and action=='probe_write':raise HubError('forbidden',403)
        return original(actor,action)
    monkeypatch.setattr(service,'require',require)
    async def handler(request):await asyncio.sleep(0);revoked[0]=True;return response([target()])
    with pytest.raises(HubError,match='forbidden'):await collector(service,handler).collect('hermes')
    assert service.get_device('hermes','fixture-a')['device']['observations']==[]


@pytest.mark.asyncio
async def test_repeated_older_future_and_ambiguous_duplicates_never_refresh_or_flip_healthy(env):
    service,clock=env;current=[target(health='down')]
    subject=collector(service,lambda request:response(current))
    assert (await subject.collect('hermes'))['recorded']==1
    before=service.get_device('hermes','fixture-a')['device']['observations'][0]
    current[0]=target(health='up')
    current[0]['scrapeInterval']='20m'  # Replay cannot extend prior freshness.
    assert (await subject.collect('hermes'))['unchanged']==1
    assert service.get_device('hermes','fixture-a')['device']['observations'][0]==before
    current[0]=target(stamp=(NOW-timedelta(seconds=1)).isoformat())
    assert (await subject.collect('hermes'))['unchanged']==1
    current[0]=target(stamp=(NOW+timedelta(minutes=1)).isoformat())
    assert (await subject.collect('hermes'))['invalid']==1
    clock[0]+=timedelta(seconds=60)
    current[:]=[target(health='up'),target(health='down')]
    result=await subject.collect('hermes')
    assert result['duplicate']==2 and result['recorded']==0
    assert service.get_device('hermes','fixture-a')['device']['observations'][0]['state']=='stale'


@pytest.mark.asyncio
async def test_missing_unmatched_unknown_and_separate_ipv6_probe(env):
    service,_=env
    maps=[mapping(),TargetMapping('node','[2001:db8::20]:9100','fixture-b','node-v6')]
    targets=[target(job='unconfigured-secret-job'),target(job='node',instance='[2001:db8::20]:9100',health='unknown')]
    result=await collector(service,lambda request:response(targets),maps).collect('hermes')
    assert result['recorded']==1 and result['ignored']==1 and result['missing']==1
    assert 'unconfigured-secret-job' not in json.dumps(result)
    assert service.get_device('hermes','fixture-a')['device']['observations']==[]
    assert service.get_device('hermes','fixture-b')['device']['observations'][0]['state']=='unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[
    {'status':302,'headers':{'location':'http://example.invalid/never-followed'}},
    {'status':500},{'raw':b'{"status":"success","status":"success"}'},
    {'raw':b'{"status":NaN}'},
    {'raw':b'{"status":"success","ignored":1e9999,"data":{"activeTargets":[]}}'},
    {'raw':b'x'* (module.MAX_RESPONSE_BYTES+1)},
    {'headers':{'content-encoding':'gzip'}},{'headers':{'content-type':'text/html'}},
    {'headers':{'content-length':str(module.MAX_RESPONSE_BYTES+1)}},
    {'targets':[target()]*201},
])
async def test_invalid_responses_fail_bounded_without_overwriting_prior(env,bad):
    service,clock=env
    service.record_probe('hermes','fixture-a','unhealthy',probe_id='node-scrape',observed_at=NOW.isoformat(),
                         valid_until=(NOW+timedelta(seconds=45)).isoformat())
    before=service.get_device('hermes','fixture-a')['device']['observations']
    calls=[]
    def handler(request):calls.append(request);return response(**bad)
    result=await collector(service,handler).collect('hermes')
    assert result['state']=='unavailable' and result['recorded']==0 and len(calls)==1
    assert service.get_device('hermes','fixture-a')['device']['observations']==before


@pytest.mark.asyncio
@pytest.mark.parametrize('interval',['0s','-1s','1.5s','21m','1s1m','9999999h','bad',None])
async def test_invalid_or_unbounded_intervals_do_not_invent_ttl(env,interval):
    service,_=env
    result=await collector(service,lambda request:response([target(interval=interval)])).collect('hermes')
    assert result['invalid']==1 and result['recorded']==0


@pytest.mark.asyncio
async def test_valid_compound_interval_ttl_is_from_actual_scrape_not_fetch_time(env):
    service,clock=env;clock[0]+=timedelta(minutes=2)
    result=await collector(service,lambda request:response([target(interval='1m30s')])).collect('hermes')
    assert result['recorded']==1
    item=service.get_device('hermes','fixture-a')['device']['observations'][0]
    assert item['observed_at']==NOW.isoformat() and item['valid_until']==(NOW+timedelta(seconds=270)).isoformat()


@pytest.mark.asyncio
async def test_absolute_deadline_cancels_progressing_body_and_closes_response(env,monkeypatch):
    service,_=env;closed=[]
    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            for part in [b'{',b'"status"',b':"success"}']:
                await asyncio.sleep(.02);yield part
        async def aclose(self):closed.append(True)
    monkeypatch.setattr(module,'FETCH_DEADLINE_SECONDS',.03)  # synthetic shortened deadline
    subject=collector(service,lambda request:httpx.Response(200,headers={'content-type':'application/json'},stream=Drip()))
    result=await subject.collect('hermes')
    assert result['failure']=='deadline_exceeded' and result['recorded']==0 and closed==[True]


def test_trusted_origin_and_mappings_are_immutable_and_disjoint(env):
    service,_=env;subject=collector(service,lambda request:response())
    with pytest.raises(FrozenInstanceError):subject.origin=PrometheusOrigin('192.0.2.2',9090)
    with pytest.raises(FrozenInstanceError):subject.mappings[0].instance='192.0.2.99:9100'
    with pytest.raises(ValueError):collector(service,lambda request:response(),[mapping(),mapping()])
    with pytest.raises(ValueError):collector(service,lambda request:response(),[
        mapping(),TargetMapping('different','192.0.2.10:9200','fixture-a','node-scrape')])


@pytest.mark.asyncio
async def test_network_failure_and_missing_target_only_age_prior_observation(env):
    service,clock=env
    subject=collector(service,lambda request:response([target(health='down')]))
    await subject.collect('hermes')
    before=service.get_device('hermes','fixture-a')['device']['observations'][0]
    clock[0]+=timedelta(seconds=60)
    def failure(request):raise httpx.ConnectError('private-origin-token-never-reflected')
    result=await collector(service,failure).collect('hermes')
    assert result['state']=='unavailable' and 'private-' not in json.dumps(result)
    missing=await collector(service,lambda request:response([])).collect('hermes')
    assert missing['missing']==1 and missing['recorded']==0
    after=service.get_device('hermes','fixture-a')['device']['observations'][0]
    assert after['state']=='stale' and after['valid_until']==before['valid_until'] and after['observed_at']==before['observed_at']
