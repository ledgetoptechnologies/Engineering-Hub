"""Owner-read machine metrics for operator-mapped Prometheus targets only.

Reads the configured literal-IP origin with a fixed set of instant queries per
exporter kind. Queries carry no interpolated labels: every result is filtered
in process to the exact configured (job, instance) mappings, so nothing from
the browser or from Prometheus selects what is read. One read issues the
targets request plus at most len(QUERIES) queries, all within one aggregate
deadline and one aggregate byte budget. Nothing is stored, scheduled or cached.
Missing or malformed samples are reported as null, never as zero.

Byte budget: every yielded body chunk is checked against the aggregate budget
before the per-response limit. Chunks fitting that budget count even when their
response is rejected; response bodies retained by the reader total at most
MAX_TOTAL_BYTES. The first chunk exceeding the aggregate budget aborts the group
and closes its streams. That rejected chunk has already been yielded by the
transport, so this is not a strict bound on received or buffered network bytes.

Freshness ('state', 'last_scrape') describes the target's last scrape, not the
age of each individual metric value.
"""
import asyncio
from datetime import datetime,timezone
import json
import math
import re
from types import MappingProxyType
from urllib.parse import urlencode

import httpx

from .infrastructure_prometheus import (EXPORTERS, MAX_TARGETS, TARGET_PATH, PrometheusOrigin, TargetMapping,
                                        _interval, _need, _targets, _unique)
from .infrastructure_service import InfrastructureService
from .repository import HubError


SCHEMA_VERSION=1
DEADLINE_SECONDS=10
MAX_QUERY_BYTES=1024*1024
MAX_TOTAL_BYTES=2*1024*1024
MAX_SERIES=4096
MAX_DISKS=16
MAX_GPUS=8
MAX_PROCESSES=10
LABELS=('host','role','env')

_NODE_FS='fstype!~"tmpfs|devtmpfs|overlay|squashfs|ramfs|nsfs|autofs|fuse\\\\..*",mountpoint!~"/run.*|/snap/.*|/boot/efi"'
# (kind, key, PromQL). Fixed text only; see module docstring.
QUERIES=(
    ('node','cpu','1 - avg by (job,instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))'),
    ('node','mem_total','node_memory_MemTotal_bytes'),
    ('node','mem_available','node_memory_MemAvailable_bytes'),
    ('node','uptime','time() - node_boot_time_seconds'),
    ('node','disk_size','max by (job,instance,mountpoint) (node_filesystem_size_bytes{'+_NODE_FS+'})'),
    ('node','disk_free','max by (job,instance,mountpoint) (node_filesystem_avail_bytes{'+_NODE_FS+'})'),
    ('windows','cpu','1 - avg by (job,instance) (rate(windows_cpu_time_seconds_total{mode="idle"}[5m]))'),
    ('windows','mem_total','windows_memory_physical_total_bytes'),
    ('windows','mem_available','windows_memory_available_bytes'),
    ('windows','uptime','time() - windows_system_boot_time_timestamp'),
    ('windows','disk_size','windows_logical_disk_size_bytes{volume=~"[A-Z]:"}'),
    ('windows','disk_free','windows_logical_disk_free_bytes{volume=~"[A-Z]:"}'),
    ('windows','process_memory','topk by (job,instance) (10, sum by (job,instance,process) (windows_process_working_set_bytes))'),
    ('windows','process_count','count by (job,instance,process) (windows_process_working_set_bytes)'),
    ('nvidia','gpu_util','DCGM_FI_DEV_GPU_UTIL'),
    ('nvidia','gpu_used','DCGM_FI_DEV_FB_USED'),
    ('nvidia','gpu_free','DCGM_FI_DEV_FB_FREE'),
    ('nvidia','gpu_temp','DCGM_FI_DEV_GPU_TEMP'),
)
_MIB=1024*1024


def _plain(value,maximum):
    return (type(value) is str and 0<len(value)<=maximum and value.strip()==value
            and all(ord(char)>=32 and ord(char)!=127 for char in value))


def _number(text):
    """Prometheus sample text to a finite float, else None (never zero)."""
    if type(text) is not str or len(text)>64:
        return None
    try:value=float(text)
    except ValueError:return None
    return value if math.isfinite(value) else None


def _vector(raw):
    def finite(value):
        number=float(value);_need(math.isfinite(number));return number
    value=json.loads(raw,object_pairs_hook=_unique,parse_float=finite,parse_constant=lambda value:(_ for _ in ()).throw(ValueError()))
    _need(type(value) is dict and value.get('status')=='success' and type(value.get('data')) is dict)
    _need(value['data'].get('resultType')=='vector')
    result=value['data'].get('result')
    _need(type(result) is list and len(result)<=MAX_SERIES)
    samples=[]
    for item in result:
        _need(type(item) is dict and type(item.get('metric')) is dict and type(item.get('value')) is list)
        labels=item['metric']
        _need(len(labels)<=32 and all(type(key) is str and type(text) is str for key,text in labels.items()))
        _need(len(item['value'])==2)
        samples.append((labels,_number(item['value'][1])))
    return samples


class _Exhausted(Exception):
    """Aggregate budget spent; deliberately not caught by per-request handlers."""


class _Budget:
    def __init__(self):self.used=0

    def take(self,size):
        if self.used+size>MAX_TOTAL_BYTES:
            raise _Exhausted()
        self.used+=size


class MetricsReader:
    """On-demand summary for a human holding metrics_read; no agent path."""

    def __init__(self,service,origin,mappings,*,test_transport=None,clock=None):
        _need(type(service) is InfrastructureService and type(origin) is PrometheusOrigin)
        _need(type(mappings) in (tuple,list) and 1<=len(mappings)<=MAX_TARGETS)
        _need(all(type(item) is TargetMapping for item in mappings))
        _need(len({(item.job,item.instance) for item in mappings})==len(mappings))
        _need(test_transport is None or type(test_transport) is httpx.MockTransport)
        self.service,self.origin,self._transport=service,origin,test_transport
        self.mappings=tuple(sorted(mappings,key=lambda item:(item.device_id,item.probe_id)))
        self._mapping=MappingProxyType({(item.job,item.instance):item for item in self.mappings})
        self._clock=clock or (lambda:datetime.now(timezone.utc))
        self._kinds=frozenset(item.exporter for item in self.mappings if item.exporter is not None)

    def _admit(self,actor):
        principal=self.service.require(actor,'metrics_read')
        if principal.kind!='human':raise HubError('forbidden',403)

    async def _get(self,client,path,budget):
        async with client.stream('GET',self.origin.url+path) as response:
            _need(response.status_code==200)
            _need(response.headers.get('content-type','').split(';',1)[0].strip().lower()=='application/json')
            _need(response.headers.get('content-encoding','identity').lower()=='identity')
            sizes=response.headers.get_list('content-length')
            _need(len(sizes)<=1 and (not sizes or len(sizes[0])<=9 and sizes[0].isdigit()
                  and int(sizes[0])<=MAX_QUERY_BYTES))
            result=bytearray()
            async for part in response.aiter_raw():
                # Charge every received chunk first, so a response rejected for its own size
                # still counts toward the aggregate budget.
                budget.take(len(part))
                _need(len(result)+len(part)<=MAX_QUERY_BYTES)
                result.extend(part)
            return bytes(result)

    async def _query(self,client,promql,budget):
        try:
            return _vector(await self._get(client,'/api/v1/query?'+urlencode({'query':promql}),budget))
        except (httpx.HTTPError,ValueError,TypeError,RecursionError,UnicodeError):
            return None

    async def read(self,actor):
        self._admit(actor)
        budget=_Budget()
        queries=[item for item in QUERIES if item[0] in self._kinds]
        targets=None;results={}
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                async with httpx.AsyncClient(trust_env=False,follow_redirects=False,transport=self._transport,
                        timeout=httpx.Timeout(5,connect=2,write=2,pool=2),headers={
                            'Accept':'application/json','Accept-Encoding':'identity'}) as client:
                    async def fetch_targets():
                        try:return _targets(await self._get(client,TARGET_PATH,budget))
                        except (httpx.HTTPError,ValueError,TypeError,RecursionError,UnicodeError):return None
                    exhausted=False
                    try:
                        async with asyncio.TaskGroup() as group:
                            pending=[group.create_task(fetch_targets())]+[
                                group.create_task(self._query(client,q,budget)) for _,_,q in queries]
                    except* _Exhausted:
                        exhausted=True
            if not exhausted:
                targets=pending[0].result()
                results={(kind,key):task.result() for (kind,key,_),task in zip(queries,pending[1:])}
        except TimeoutError:
            targets=None;results={}
        # Recheck after the network: a revoked capability gets nothing back.
        self._admit(actor)
        # Judge freshness when the readings arrived, so a scrape during the read isn't "future".
        now=self._clock()
        if targets is None:
            status='unreachable'
        elif all(value is not None for value in results.values()) and len(results)==len(queries):
            status='ok'
        else:
            status='partial'
        return {'schema_version':SCHEMA_VERSION,'generated_at':_iso(now),'source':{'status':status},
                'devices':[self._device(mapping,targets,results,now) for mapping in self.mappings]}

    def _samples(self,results,mapping,key):
        values=results.get((mapping.exporter,key))
        if values is None:
            return None
        return [(labels,value) for labels,value in values
                if labels.get('job')==mapping.job and labels.get('instance')==mapping.instance]

    def _single(self,results,mapping,key,low=0.0,high=None):
        samples=self._samples(results,mapping,key)
        if not samples or len(samples)!=1:
            return None
        value=samples[0][1]
        if value is None or value<low or (high is not None and value>high):
            return None
        return value

    def _device(self,mapping,targets,results,now):
        found=[] if targets is None else [item for item in targets if type(item.get('labels')) is dict
                                          and item['labels'].get('job')==mapping.job
                                          and item['labels'].get('instance')==mapping.instance]
        target=found[0] if len(found)==1 else None
        up=None;last=None;state='unknown';labels={}
        if target is not None:
            health=target.get('health')
            # Malformed health (a list, object or unknown word) is unknown, never a crash or "down".
            up={'up':True,'down':False}.get(health) if type(health) is str else None
            last=_scrape_time(target.get('lastScrape'),now)
            try:interval=_interval(target.get('scrapeInterval'))
            except (ValueError,TypeError):interval=None
            if last is not None and interval is not None:
                state='fresh' if now-last<=3*interval else 'stale'
            labels={key:target['labels'][key] for key in LABELS if _plain(target['labels'].get(key),64)}
        item={'device_id':mapping.device_id,'probe_id':mapping.probe_id,'job':mapping.job,
              'instance':mapping.instance,'exporter':mapping.exporter,'labels':labels,'up':up,
              'last_scrape':_iso(last) if last is not None else None,'state':state,
              'cpu_busy_ratio':None,'mem_total_bytes':None,'mem_available_bytes':None,'uptime_seconds':None,
              'disks':[],'gpus':[],'top_processes':[]}
        if mapping.exporter in ('node','windows'):
            cpu=self._single(results,mapping,'cpu',-0.05,1.05)
            item['cpu_busy_ratio']=None if cpu is None else min(max(cpu,0.0),1.0)
            total=_bytes(self._single(results,mapping,'mem_total'))
            available=_bytes(self._single(results,mapping,'mem_available'))
            # An inconsistent pair would show negative use; report the available side as missing.
            item['mem_total_bytes']=total
            item['mem_available_bytes']=None if total is None or available is None or available>total else available
            item['uptime_seconds']=_whole(self._single(results,mapping,'uptime'))
            item['disks']=self._disks(results,mapping)
        if mapping.exporter=='windows':
            item['top_processes']=self._processes(results,mapping)
        if mapping.exporter=='nvidia':
            item['gpus']=self._gpus(results,mapping)
        return item

    def _disks(self,results,mapping):
        label='volume' if mapping.exporter=='windows' else 'mountpoint'
        def by(key):
            values={}
            for labels,value in self._samples(results,mapping,key) or []:
                name=labels.get(label)
                if _plain(name,128) and value is not None:
                    values.setdefault(name,[]).append(_bytes(value))
            return {name:found[0] for name,found in values.items() if len(found)==1 and found[0] is not None}
        sizes,free=by('disk_size'),by('disk_free')
        # Size and free are separate instant queries. A stale or inconsistent
        # free sample must not hide an independently valid size observation.
        disks=[]
        for name,size in sizes.items():
            if size<=0:continue
            available=free.get(name)
            disks.append({'mount':name,'size_bytes':size,
                          'free_bytes':available if available is None or available<=size else None})
        return sorted(disks,key=lambda disk:disk['mount'])[:MAX_DISKS]

    def _processes(self,results,mapping):
        counts={}
        for labels,value in self._samples(results,mapping,'process_count') or []:
            name=labels.get('process')
            if _plain(name,128) and value is not None and 1<=value<2**53 and value==int(value):
                counts[name]=None if name in counts else int(value)
        top={}
        for labels,value in self._samples(results,mapping,'process_memory') or []:
            name=labels.get('process')
            if _plain(name,128) and value is not None:
                top[name]=None if name in top else _bytes(value)
        rows=[{'name':name,'count':counts.get(name),'working_set_bytes':value}
              for name,value in top.items() if value is not None]
        return sorted(rows,key=lambda row:(-row['working_set_bytes'],row['name']))[:MAX_PROCESSES]

    def _gpus(self,results,mapping):
        def by(key):
            values={}
            for labels,value in self._samples(results,mapping,key) or []:
                gpu=labels.get('gpu')
                if type(gpu) is str and re.fullmatch(r'[0-9]{1,2}',gpu) and value is not None and 0<=value<2**53:
                    values.setdefault(gpu,[]).append((labels,value))
            return {gpu:found[0] for gpu,found in values.items() if len(found)==1}
        util,used,free,temp=by('gpu_util'),by('gpu_used'),by('gpu_free'),by('gpu_temp')
        gpus=[]
        for gpu in sorted(set(util)|set(used)|set(free)|set(temp),key=int)[:MAX_GPUS]:
            labels=next(found[0] for found in (util.get(gpu),used.get(gpu),free.get(gpu),temp.get(gpu)) if found)
            used_mib=used[gpu][1] if gpu in used else None
            free_mib=free[gpu][1] if gpu in free else None
            ratio=util[gpu][1]/100 if gpu in util and util[gpu][1]<=100 else None
            gpus.append({'index':int(gpu),'model':labels.get('modelName') if _plain(labels.get('modelName'),100) else None,
                         'util_ratio':ratio,'mem_used_bytes':_bytes(None if used_mib is None else used_mib*_MIB),
                         'mem_total_bytes':_bytes(None if used_mib is None or free_mib is None else (used_mib+free_mib)*_MIB),
                         'temperature_c':temp[gpu][1] if gpu in temp and temp[gpu][1]<=150 else None})
        return gpus


def _bytes(value):
    return int(value) if value is not None and 0<=value<2**53 else None


def _whole(value):
    return int(value) if value is not None and 0<=value<2**53 else None


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00','Z')


def _scrape_time(stamp,now):
    if type(stamp) is not str or len(stamp)>40 or not re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})',stamp):
        return None
    try:
        observed=datetime.fromisoformat(re.sub(r'(\.[0-9]{6})[0-9]+',r'\1',stamp).replace('Z','+00:00'))
    except ValueError:
        return None
    observed=observed.astimezone(timezone.utc)
    # Prometheus reports year-1 zero time before the first scrape.
    return observed if observed.year>=1970 and observed<=now else None
