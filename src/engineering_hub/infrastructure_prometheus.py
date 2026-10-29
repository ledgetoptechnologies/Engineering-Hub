"""On-demand, fixed-origin Prometheus scrape observations; never host discovery.

Owner supplies immutable literal-IP origin and exact job/instance mappings.
Only /api/v1/targets?state=active is read. scrapeUrl, discovered labels and
response URLs never select network destinations or declared device facts.
Failure/missing/replayed samples leave prior observations to expire normally.
No scheduler, listener, grants, device creation or last-success claim exists.
Importing/composing this adapter attaches no startup or MCP scheduling hook;
monitoring occurs only when a trusted caller explicitly awaits collect(actor).
The10-second deadline bounds asynchronous fetch, with cooperative checks during
bounded parsing/processing. It is not a hard wall-clock bound on synchronous
registry locks, filesystem guards or SQLite writes; writes are never abandoned
in background threads to manufacture such a guarantee.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
import ipaddress
import json
import math
import re
from types import MappingProxyType

import httpx

from .infrastructure_service import InfrastructureService
from .repository import HubError


MAX_RESPONSE_BYTES=2*1024*1024
MAX_TARGETS=200
FETCH_DEADLINE_SECONDS=10
MAX_INTERVAL_SECONDS=1200  # TTL is exactly3*interval, never more than1hour.
TARGET_PATH='/api/v1/targets?state=active'
EXPORTERS=('node','windows','nvidia')


def _need(condition):
    if not condition:raise ValueError('Invalid trusted Prometheus configuration or response')


def _literal(value):
    _need(type(value) is str and '%' not in value)
    address=ipaddress.ip_address(value)
    _need(str(address)==value)
    return address


def _port(value):
    _need(type(value) is int and 1<=value<=65535)
    return value


def _instance(value):
    _need(type(value) is str and len(value)<=64)
    if value.startswith('['):
        match=re.fullmatch(r'\[([^\]]+)\]:([0-9]{1,5})',value)
        _need(match is not None)
        address=_literal(match[1]);_need(address.version==6)
    else:
        match=re.fullmatch(r'([^:]+):([0-9]{1,5})',value)
        _need(match is not None)
        address=_literal(match[1]);_need(address.version==4)
    port=_port(int(match[2]));_need(str(port)==match[2])
    return str(address)


def _plain(value,maximum):
    _need(type(value) is str and 0<len(value)<=maximum and value.strip()
          and all(ord(char)>=32 and ord(char)!=127 for char in value))
    value.encode('utf-8')


@dataclass(frozen=True)
class PrometheusOrigin:
    address:str
    port:int

    def __post_init__(self):
        _literal(self.address);_port(self.port)

    @property
    def url(self):
        address='['+self.address+']' if ':' in self.address else self.address
        return f'http://{address}:{self.port}'


@dataclass(frozen=True)
class TargetMapping:
    job:str
    instance:str
    device_id:str
    probe_id:str
    source_label:str='Prometheus scrape'
    # Selects the fixed metric query set; None means up/down only.
    exporter:str|None=None

    def __post_init__(self):
        _plain(self.job,128);_instance(self.instance);_plain(self.source_label,100)
        _need(self.exporter is None or self.exporter in EXPORTERS)
        for value in (self.device_id,self.probe_id):
            _need(type(value) is str and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',value))


def _unique(pairs):
    result={}
    for key,value in pairs:
        _need(key not in result)
        result[key]=value
    return result


def _targets(raw):
    _need(type(raw) is bytes and len(raw)<=MAX_RESPONSE_BYTES)
    def finite(value):
        number=float(value);_need(math.isfinite(number));return number
    value=json.loads(raw,object_pairs_hook=_unique,parse_float=finite,
                     parse_constant=lambda value:(_ for _ in ()).throw(ValueError()))
    _need(type(value) is dict and value.get('status')=='success' and type(value.get('data')) is dict)
    targets=value['data'].get('activeTargets')
    _need(type(targets) is list and len(targets)<=MAX_TARGETS and all(type(item) is dict for item in targets))
    return targets


def _interval(value):
    _need(type(value) is str and 0<len(value)<=32)
    parts=list(re.finditer(r'([0-9]{1,7})(ms|s|m|h)',value))
    _need(''.join(match[0] for match in parts)==value)
    units={'h':(3,3600000),'m':(2,60000),'s':(1,1000),'ms':(0,1)}
    ranks=[units[match[2]][0] for match in parts]
    _need(ranks==sorted(set(ranks),reverse=True))
    milliseconds=sum(int(match[1])*units[match[2]][1] for match in parts)
    _need(0<milliseconds<=MAX_INTERVAL_SECONDS*1000)
    return timedelta(milliseconds=milliseconds)


def _observation(target,now):
    status=target.get('health')
    _need(type(status) is str and status in ('up','down','unknown'))
    stamp=target.get('lastScrape')
    _need(type(stamp) is str and len(stamp)<=40 and re.fullmatch(
        r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})',stamp))
    observed=datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone(timezone.utc)
    _need(observed.year>=1970 and observed<=now)  # Prometheus zero time is no scrape.
    valid=observed+3*_interval(target.get('scrapeInterval'))
    return {'status':{'up':'healthy','down':'unhealthy','unknown':'unknown'}[status],
            'observed_at':observed.isoformat(),'valid_until':valid.isoformat()}


@dataclass(frozen=True,init=False)
class PrometheusCollector:
    service:object
    origin:PrometheusOrigin
    mappings:tuple
    _mapping:object
    _transport:object

    def __init__(self,service,origin,mappings,*,test_transport=None):
        _need(type(service) is InfrastructureService and type(origin) is PrometheusOrigin)
        _need(type(mappings) in (tuple,list) and 1<=len(mappings)<=MAX_TARGETS)
        _need(all(type(mapping) is TargetMapping for mapping in mappings))
        _need(len({(mapping.job,mapping.instance) for mapping in mappings})==len(mappings))
        _need(len({(mapping.device_id,mapping.probe_id) for mapping in mappings})==len(mappings))
        _need(all(sum(other.device_id==mapping.device_id for other in mappings)<=16 for mapping in mappings))
        _need(test_transport is None or type(test_transport) is httpx.MockTransport)
        object.__setattr__(self,'service',service);object.__setattr__(self,'origin',origin)
        object.__setattr__(self,'mappings',tuple(mappings))
        object.__setattr__(self,'_mapping',MappingProxyType({(item.job,item.instance):item for item in mappings}))
        object.__setattr__(self,'_transport',test_transport)

    def _admit(self,actor):
        principal=self.service.require(actor,'probe_write')
        if principal.kind!='agent':raise HubError('forbidden',403)
        for mapping in self.mappings:
            device=self.service.get_device(actor,mapping.device_id)['device']
            if device['retired'] or _instance(mapping.instance) not in device['addresses']:
                raise HubError('prometheus_mapping_invalid',409)

    async def _fetch(self):
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,transport=self._transport,
                timeout=httpx.Timeout(5,connect=2,write=2,pool=2),headers={
                    'Accept':'application/json','Accept-Encoding':'identity'}) as client:
            async with client.stream('GET',self.origin.url+TARGET_PATH) as response:
                _need(response.status_code==200)
                _need(response.headers.get('content-type','').split(';',1)[0].strip().lower()=='application/json')
                _need(response.headers.get('content-encoding','identity').lower()=='identity')
                sizes=response.headers.get_list('content-length')
                _need(len(sizes)<=1 and (not sizes or len(sizes[0])<=9 and sizes[0].isdigit()
                      and int(sizes[0])<=MAX_RESPONSE_BYTES))
                result=bytearray()
                async for part in response.aiter_raw():
                    _need(len(result)+len(part)<=MAX_RESPONSE_BYTES)
                    result.extend(part)
                return bytes(result)

    async def collect(self,actor):
        # Actual trusted principal and every current declaration are checked
        # before the first network operation; no implicit internal agent.
        self._admit(actor)
        summary={'state':'collected','recorded':0,'unchanged':0,'ignored':0,'invalid':0,
                 'duplicate':0,'missing':0,'failure':None}
        loop=asyncio.get_running_loop();deadline=loop.time()+FETCH_DEADLINE_SECONDS
        try:
            async with asyncio.timeout_at(deadline):
                raw=await self._fetch()
                targets=_targets(raw)
            _need(loop.time()<deadline)
        except TimeoutError:
            return {**summary,'state':'unavailable','failure':'deadline_exceeded'}
        except (httpx.HTTPError,ValueError,TypeError,RecursionError,UnicodeError):
            return {**summary,'state':'unavailable','failure':'response_unavailable'}
        grouped={}
        for target in targets:
            labels=target.get('labels')
            key=(labels.get('job'),labels.get('instance')) if type(labels) is dict else None
            if key is None or any(type(value) is not str for value in key) or key not in self._mapping:
                summary['ignored']+=1;continue
            grouped.setdefault(key,[]).append(target)
        summary['missing']=len(self.mappings)-len(grouped)
        # No await between re-admission, declaration checks and writes. The
        # registry lock also excludes concurrent HTTP declaration mutations.
        with self.service.registry.lock:
            self._admit(actor)
            for key,items in grouped.items():
                if len(items)!=1:
                    summary['duplicate']+=len(items);continue
                if loop.time()>=deadline:
                    return {**summary,'state':'unavailable','failure':'deadline_exceeded'}
                try:observation=_observation(items[0],self.service.registry._now())
                except (ValueError,TypeError,OverflowError):
                    summary['invalid']+=1;continue
                mapping=self._mapping[key]
                try:
                    self.service.record_probe(actor,mapping.device_id,**observation,
                        probe_id=mapping.probe_id,source_label=mapping.source_label)
                    summary['recorded']+=1
                except HubError as exc:
                    if exc.code=='probe_observation_out_of_order':summary['unchanged']+=1
                    else:raise
        return summary
