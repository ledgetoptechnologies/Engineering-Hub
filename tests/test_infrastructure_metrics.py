"""Synthetic Prometheus transport and temporary SQLite; no actual network."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sqlite3
import sys
from types import MappingProxyType
from urllib.parse import parse_qs,urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from starlette.responses import JSONResponse

from engineering_hub import infrastructure_metrics as module
from engineering_hub.infrastructure import InfrastructureRegistry,InfrastructurePrincipal,CAPABILITIES
from engineering_hub.infrastructure_api import attach_infrastructure_routes
from engineering_hub.infrastructure_metrics import MetricsReader,QUERIES
from engineering_hub.infrastructure_module import _settings
from engineering_hub.infrastructure_prometheus import PrometheusOrigin,TargetMapping,TARGET_PATH
from engineering_hub.infrastructure_service import InfrastructureService
from engineering_hub.repository import HubError


NOW=datetime(2026,10,29,17,0,tzinfo=timezone.utc)
FIXTURE=Path(__file__).parents[1]/'hub-ui/src/fixtures/infrastructure-metrics-v1.json'
MAPPINGS=[TargetMapping('windows','192.0.2.51:9182','desk-pc','windows-scrape',exporter='windows'),
          TargetMapping('node','192.0.2.88:9100','gpu-box','node-scrape',exporter='node'),
          TargetMapping('nvidia-gpu','192.0.2.88:9400','gpu-box','gpu-scrape',exporter='nvidia'),
          TargetMapping('node','192.0.2.86:9100','field-pi','node-scrape',exporter='node'),
          TargetMapping('legacy','192.0.2.90:9100','old-box','legacy-scrape')]


@pytest.fixture
def service():
    registry=InfrastructureRegistry(sqlite3.connect(':memory:',check_same_thread=False),capabilities={
        InfrastructurePrincipal('human','human:1'):CAPABILITIES,
        InfrastructurePrincipal('human','human:2'):{'read'},
        InfrastructurePrincipal('agent','hermes'):{'read','metrics_read','probe_write'},
        InfrastructurePrincipal('agent','codex'):{'read'}},clock=lambda:NOW)
    yield InfrastructureService(registry)
    registry.close()


def target(job,instance,*,health='up',ago=10,interval='15s',labels=None):
    return dict(labels={'job':job,'instance':instance,**(labels or {})},health=health,
                lastScrape=(NOW-timedelta(seconds=ago)).isoformat().replace('+00:00','.123456789Z'),
                scrapeInterval=interval,scrapeUrl='http://example.invalid/never-followed',lastError='private-error')


def sample(value,**labels):return {'metric':labels,'value':[NOW.timestamp(),value]}


W=dict(job='windows',instance='192.0.2.51:9182')
N=dict(job='node',instance='192.0.2.88:9100')
G=dict(job='nvidia-gpu',instance='192.0.2.88:9400')
OTHER=dict(job='windows',instance='192.0.2.99:9182')  # Unmapped: must never appear.
SERIES={
    'cpu':[sample('0.25',**W),sample('0.5',**N),sample('0.9',**OTHER)],
    'mem_total':[sample('68719476736',**W),sample('34359738368',**N)],
    'mem_available':[sample('25769803776',**W),sample('NaN',**N)],
    'uptime':[sample('86400.6',**W),sample('3600',**N)],
    'disk_size':[sample('4000000000000',**W,volume='C:'),sample('500000000000',**N,mountpoint='/'),
                 sample('1000',**N,mountpoint='/data'),sample('1',**OTHER,volume='C:')],
    'disk_free':[sample('353000000000',**W,volume='C:'),sample('125000000000',**N,mountpoint='/'),
                 sample('2000',**N,mountpoint='/data')],
    'process_memory':[sample('4000000000',**W,process='msedge'),sample('2500000000',**W,process='node'),
                      sample('9',**OTHER,process='secret-unmapped')],
    'process_count':[sample('57',**W,process='msedge'),sample('22',**W,process='node'),sample('3',**W,process='svchost')],
    'gpu_util':[sample('35',**G,gpu='0',modelName='Synthetic GPU A'),sample('0',**G,gpu='1',modelName='Synthetic GPU B')],
    'gpu_used':[sample('4096',**G,gpu='0',modelName='Synthetic GPU A')],
    'gpu_free':[sample('12288',**G,gpu='0',modelName='Synthetic GPU A')],
    'gpu_temp':[sample('61',**G,gpu='0',modelName='Synthetic GPU A'),sample('+Inf',**G,gpu='1',modelName='Synthetic GPU B')],
}
TARGETS=[target('windows','192.0.2.51:9182',labels={'host':'desk','role':'workstation','env':'personal','secret':'x'}),
         target('node','192.0.2.88:9100',labels={'host':'gpu-box','role':'processing','env':'production'}),
         target('nvidia-gpu','192.0.2.88:9400',labels={'host':'gpu-box','role':'processing','env':'production'}),
         target('node','192.0.2.86:9100',health='down',ago=600),
         target('windows','192.0.2.99:9182')]


def respond(payload,status=200,headers=None):
    return httpx.Response(status,headers={'content-type':'application/json',**(headers or {})},
                          stream=httpx.ByteStream(json.dumps(payload).encode()))


def handler_for(*,fail=(),targets=TARGETS,requests=None):
    by_query={promql:(kind,key) for kind,key,promql in QUERIES}
    def handler(request):
        if requests is not None:requests.append(str(request.url))
        assert request.method=='GET' and request.url.host=='192.0.2.1' and request.url.port==9090
        path=request.url.raw_path.decode()
        if path==TARGET_PATH:
            if 'targets' in fail:return httpx.Response(503)
            return respond({'status':'success','data':{'activeTargets':targets}})
        parts=urlsplit(path);assert parts.path=='/api/v1/query'
        query=parse_qs(parts.query,strict_parsing=True);assert list(query)==['query'] and len(query['query'])==1
        kind,key=by_query[query['query'][0]]
        if key in fail:return httpx.Response(500)
        series=[item for item in SERIES.get(key,[]) if item['metric']['job']=={'windows':'windows','node':'node','nvidia':'nvidia-gpu'}[kind]]
        return respond({'status':'success','data':{'resultType':'vector','result':series}})
    return handler


def reader(service,handler,mappings=MAPPINGS):
    return MetricsReader(service,PrometheusOrigin('192.0.2.1',9090),mappings,
                         test_transport=httpx.MockTransport(handler),clock=lambda:NOW)


@pytest.mark.asyncio
async def test_frozen_fixture_and_exact_mapped_filtering(service):
    requests=[]
    result=await reader(service,handler_for(requests=requests)).read('human:1')
    assert result==json.loads(FIXTURE.read_text(encoding='utf-8'))
    text=json.dumps(result)
    for private in ('192.0.2.99','secret-unmapped','private-error','never-followed','"secret"'):
        assert private not in text
    # The targets request plus exactly one request per fixed query of a mapped kind; nothing else.
    allowed={TARGET_PATH}|{'/api/v1/query?'+module.urlencode({'query':promql}) for _,_,promql in QUERIES}
    paths=[httpx.URL(url).raw_path.decode() for url in requests]
    assert len(paths)==len(set(paths))==1+len(QUERIES) and set(paths)==allowed


@pytest.mark.asyncio
async def test_only_mapped_exporter_kinds_are_queried(service):
    requests=[]
    await reader(service,handler_for(requests=requests),[MAPPINGS[0]]).read('human:1')
    assert len(requests)==1+sum(kind=='windows' for kind,_,_ in QUERIES)
    requests.clear()
    await reader(service,handler_for(requests=requests),[MAPPINGS[4]]).read('human:1')
    assert len(requests)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('actor',['human:2','hermes','codex','claude'])
async def test_metrics_need_human_metrics_read_before_any_request(service,actor):
    requests=[]
    with pytest.raises(HubError) as error:
        await reader(service,handler_for(requests=requests)).read(actor)
    assert error.value.status==403 and requests==[]


@pytest.mark.asyncio
async def test_source_outage_is_unknown_not_down(service):
    result=await reader(service,handler_for(fail=('targets',))).read('human:1')
    assert result['source']=={'status':'unreachable'}
    assert all(item['up'] is None and item['state']=='unknown' and item['last_scrape'] is None for item in result['devices'])


@pytest.mark.asyncio
async def test_one_failed_query_is_partial_and_null_never_zero(service):
    result=await reader(service,handler_for(fail=('mem_total','process_count'))).read('human:1')
    assert result['source']=={'status':'partial'}
    desk=result['devices'][0]
    # Available memory alone can't be judged, so it is withheld with its missing total.
    assert desk['mem_total_bytes'] is None and desk['mem_available_bytes'] is None
    assert desk['cpu_busy_ratio']==0.25 and desk['disks']
    assert [row['count'] for row in desk['top_processes']]==[None,None]


@pytest.mark.asyncio
async def test_aggregate_deadline_bounds_every_request(service,monkeypatch):
    monkeypatch.setattr(module,'DEADLINE_SECONDS',0.2)
    started=asyncio.get_running_loop().time()
    async def slow(request):
        await asyncio.sleep(5);return respond({})
    result=await MetricsReader(service,PrometheusOrigin('192.0.2.1',9090),MAPPINGS,
        test_transport=httpx.MockTransport(slow),clock=lambda:NOW).read('human:1')
    assert asyncio.get_running_loop().time()-started<2
    assert result['source']=={'status':'unreachable'}


class Stream(httpx.AsyncByteStream):
    """Endless body in fixed chunks; records how much was read and whether it was closed."""
    def __init__(self,log,chunk=1024):self.log,self.chunk=log,chunk
    async def __aiter__(self):
        while True:
            self.log['read']+=self.chunk
            yield b' '*self.chunk
            await asyncio.sleep(0)
    async def aclose(self):self.log['closed']+=1


@pytest.mark.asyncio
async def test_budget_exhaustion_aborts_every_stream_together(service,monkeypatch):
    monkeypatch.setattr(module,'MAX_TOTAL_BYTES',64*1024)
    log={'read':0,'closed':0};streams=[]
    def handler(request):
        stream=Stream(log);streams.append(stream)
        return httpx.Response(200,headers={'content-type':'application/json'},stream=stream)
    result=await reader(service,handler).read('human:1')
    assert result['source']=={'status':'unreachable'}
    assert all(item['up'] is None and item['state']=='unknown' for item in result['devices'])
    # Retained bytes never pass the budget; consumption exceeds it by at most one chunk per open stream.
    assert log['read']<=64*1024+1024*len(streams)
    assert log['closed']==len(streams)==1+len(QUERIES)


def test_budget_retains_nothing_past_the_limit(monkeypatch):
    monkeypatch.setattr(module,'MAX_TOTAL_BYTES',10)
    budget=module._Budget();budget.take(10)
    with pytest.raises(module._Exhausted):budget.take(1)
    assert budget.used==10


@pytest.mark.asyncio
async def test_per_response_limit_rejects_only_that_response(service,monkeypatch):
    monkeypatch.setattr(module,'MAX_QUERY_BYTES',10)
    result=await reader(service,handler_for()).read('human:1')
    assert result['source']=={'status':'unreachable'}


@pytest.mark.asyncio
async def test_freshness_is_judged_when_readings_arrive(service):
    # One clock reading, taken after the network: a second call would raise StopIteration.
    times=iter([NOW+timedelta(seconds=5)])
    def handler(request):
        if request.url.raw_path.decode()==TARGET_PATH:
            # Scraped two seconds after the read began: not in the future by completion time.
            later=[{**TARGETS[0],'lastScrape':(NOW+timedelta(seconds=2)).isoformat().replace('+00:00','Z')}]
            return respond({'status':'success','data':{'activeTargets':later}})
        return handler_for()(request)
    result=await MetricsReader(service,PrometheusOrigin('192.0.2.1',9090),[MAPPINGS[0]],
        test_transport=httpx.MockTransport(handler),clock=lambda:next(times)).read('human:1')
    desk=result['devices'][0]
    assert desk['state']=='fresh' and desk['last_scrape']=='2026-10-29T17:00:02Z'
    assert result['generated_at']=='2026-10-29T17:00:05Z'


@pytest.mark.asyncio
async def test_huge_and_inconsistent_values_are_dropped_not_crashing(service,monkeypatch):
    huge=str(2**60)
    series={**SERIES,'disk_size':[sample(huge,**W,volume='C:'),sample('100',**W,volume='D:')],
            'disk_free':[sample('50',**W,volume='C:'),sample(huge,**W,volume='D:')],
            'process_memory':[sample(huge,**W,process='giant'),sample('5',**W,process='small')],
            'mem_total':[sample('100',**W)],'mem_available':[sample('200',**W)]}
    monkeypatch.setattr(sys.modules[__name__],'SERIES',series)
    result=await reader(service,handler_for(),[MAPPINGS[0]]).read('human:1')
    desk=result['devices'][0]
    assert desk['disks']==[{'mount':'D:','size_bytes':100,'free_bytes':None}]
    assert desk['top_processes']==[{'name':'small','count':None,'working_set_bytes':5}]
    assert desk['mem_total_bytes']==100 and desk['mem_available_bytes'] is None


@pytest.mark.asyncio
async def test_capability_revoked_during_read_returns_nothing(service):
    principal=InfrastructurePrincipal('human','human:1')
    base=handler_for()
    def handler(request):
        # Policy replaced mid-read (as a reload would): metrics_read is gone.
        service.registry.capabilities=MappingProxyType({principal:frozenset({'read'})})
        return base(request)
    with pytest.raises(HubError) as error:
        await reader(service,handler).read('human:1')
    assert error.value.status==403


@pytest.mark.asyncio
async def test_external_cancellation_closes_every_opened_stream_and_propagates(service):
    log={'read':0,'closed':0};opened=[];expected=1+len(QUERIES);everyone=asyncio.Event()
    class Slow(Stream):
        async def __aiter__(self):
            opened.append(self)
            if len(opened)==expected:everyone.set()
            await asyncio.sleep(30)
            yield b''
    def handler(request):
        return httpx.Response(200,headers={'content-type':'application/json'},stream=Slow(log))
    task=asyncio.create_task(reader(service,handler).read('human:1'))
    await asyncio.wait_for(everyone.wait(),5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert len(opened)==expected and log['closed']==expected


@pytest.mark.asyncio
async def test_chunks_rejected_by_the_per_response_limit_are_still_charged(service,monkeypatch):
    budgets=[]
    class Spy(module._Budget):
        def __init__(self):super().__init__();budgets.append(self)
    monkeypatch.setattr(module,'_Budget',Spy)
    monkeypatch.setattr(module,'MAX_QUERY_BYTES',1024)
    log={'read':0,'closed':0}
    def handler(request):
        return httpx.Response(200,headers={'content-type':'application/json'},stream=Stream(log))
    # Windows mapping: the targets request plus every windows query, each overflowing repeatedly.
    result=await reader(service,handler,[MAPPINGS[0]]).read('human:1')
    assert result['source']=={'status':'unreachable'}
    # Each stream delivered one accepted chunk and one rejected chunk; both were charged.
    streams=1+sum(kind=='windows' for kind,_,_ in QUERIES)
    assert budgets[0].used==log['read']==2*1024*streams


@pytest.mark.asyncio
@pytest.mark.parametrize('payload',[{'status':'error'},{'status':'success','data':{'resultType':'matrix','result':[]}},
    {'status':'success','data':{'resultType':'vector','result':[{'metric':{'job':1},'value':[0,'1']}]}},
    {'status':'success','data':{'resultType':'vector','result':[{'metric':{},'value':[0]}]}}])
async def test_malformed_query_responses_are_rejected(service,payload):
    def handler(request):
        if request.url.raw_path.decode()==TARGET_PATH:
            return respond({'status':'success','data':{'activeTargets':TARGETS}})
        return respond(payload)
    result=await reader(service,handler).read('human:1')
    assert result['source']=={'status':'partial'}
    assert all(item['cpu_busy_ratio'] is None and item['disks']==[] and item['gpus']==[] for item in result['devices'])


def test_mapping_exporter_kinds_are_closed():
    with pytest.raises(ValueError):TargetMapping('node','192.0.2.1:9100','a','b',exporter='snmp')
    assert TargetMapping('node','192.0.2.1:9100','a','b').exporter is None


def settings(tmp_path,principals,mapping=None):
    path=tmp_path/'infrastructure.json'
    value={'schema_version':1,'enabled':True,'principals':principals}
    if mapping is not None:
        value['prometheus']={'address':'192.0.2.1','port':9090,'mappings':[mapping]}
    path.write_text(json.dumps(value));path.chmod(0o600)
    return path


def test_config_rejects_metrics_read_on_agents_and_accepts_exporter(tmp_path):
    with pytest.raises(ValueError):
        _settings(settings(tmp_path,[{'kind':'agent','identity':'hermes','capabilities':['read','metrics_read']}]))
    with pytest.raises(ValueError):
        _settings(settings(tmp_path,[{'kind':'human','identity':'human:1','capabilities':['metrics_read']}]))
    enabled,grants,prometheus=_settings(settings(tmp_path,
        [{'kind':'human','identity':'human:1','capabilities':['read','metrics_read']}],
        {'job':'windows','instance':'192.0.2.51:9182','device_id':'desk-pc','probe_id':'windows-scrape','exporter':'windows'}))
    assert enabled and grants[InfrastructurePrincipal('human','human:1')]==frozenset({'read','metrics_read'})
    assert prometheus[1][0].exporter=='windows'
    with pytest.raises(ValueError):
        _settings(settings(tmp_path,[],{'job':'node','instance':'192.0.2.1:9100','device_id':'a','probe_id':'b','exporter':'x'}))


def api(service,metrics):
    def actor(request,mutation=False):
        bearer=request.headers.get('authorization')
        if bearer:return {'Bearer hermes-token':'hermes'}[bearer]
        identity=request.headers.get('x-test-human')
        if not identity:raise HubError('unauthorized',401)
        if mutation:raise HubError('csrf_required',403)
        return identity
    app=FastAPI()
    @app.exception_handler(HubError)
    async def error(request,exc):return JSONResponse({'detail':{'code':exc.code}},status_code=exc.status)
    attach_infrastructure_routes(app,service,actor,metrics=metrics)
    return TestClient(app)


PATH='/api/hub/v1/infrastructure/metrics'


def test_http_route_owner_read_without_csrf_and_closed_errors(service):
    with api(service,reader(service,handler_for())) as client:
        response=client.get(PATH,headers={'X-Test-Human':'human:1'})
        assert response.status_code==200 and response.json()['schema_version']==1
        assert client.get(PATH,headers={'X-Test-Human':'human:2'}).status_code==403
        assert client.get(PATH,headers={'Authorization':'Bearer hermes-token'}).status_code==403
        assert client.get(PATH).status_code==401
        assert client.get(PATH+'?query=up',headers={'X-Test-Human':'human:1'}).status_code==400
    with api(service,None) as client:
        missing=client.get(PATH,headers={'X-Test-Human':'human:1'})
        assert missing.status_code==404 and missing.json()=={'detail':{'code':'infrastructure_metrics_not_configured'}}
        assert client.get(PATH,headers={'Authorization':'Bearer hermes-token'}).status_code==403
    other=InfrastructureService(InfrastructureRegistry(sqlite3.connect(':memory:'),capabilities={}))
    with pytest.raises(TypeError):attach_infrastructure_routes(FastAPI(),other,lambda request:None,metrics=reader(service,handler_for()))


@pytest.mark.asyncio
async def test_oversized_process_count_keeps_the_process_with_unknown_count(service,monkeypatch):
    series={**SERIES,'process_memory':[sample('4000000000',**W,process='msedge')],
            'process_count':[sample(str(2**53),**W,process='msedge')]}
    monkeypatch.setattr(sys.modules[__name__],'SERIES',series)
    result=await reader(service,handler_for(),[MAPPINGS[0]]).read('human:1')
    assert result['devices'][0]['top_processes']==[{'name':'msedge','count':None,'working_set_bytes':4000000000}]


@pytest.mark.asyncio
@pytest.mark.parametrize('health',[[],{},None,7,'UP','unknown'])
async def test_malformed_health_on_a_matching_target_is_unknown(service,health):
    def handler(request):
        if request.url.raw_path.decode()==TARGET_PATH:
            return respond({'status':'success','data':{'activeTargets':[{**TARGETS[0],'health':health}]}})
        return handler_for()(request)
    result=await reader(service,handler,[MAPPINGS[0]]).read('human:1')
    desk=result['devices'][0]
    # Scrape time is still real, so freshness is reported; up is unknown, never false.
    assert desk['up'] is None and desk['state']=='fresh' and result['source']=={'status':'ok'}
