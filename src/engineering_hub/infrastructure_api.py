"""Closed HTTP inputs; authenticate and authorize before parsing any body."""
import json

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .infrastructure_service import InfrastructureService
from .repository import HubError
from .work_queue_api import _authorization


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)


class DeviceInput(Closed):
    name: str = Field(min_length=1,max_length=100)
    hostname: str | None = Field(default=None,max_length=253)
    metrics_host: str | None = Field(default=None,max_length=64)
    addresses: list[str] = Field(max_length=16)
    network: str | None = Field(default=None,max_length=64)
    vlan: int | None = Field(default=None,ge=1,le=4094)
    role: str = Field(min_length=1,max_length=100)
    retired: bool = False
    expected_revision: int = Field(ge=0,le=2**53-1)


class MaintenanceInput(Closed):
    reason: str = Field(min_length=1,max_length=1000)
    expected_end: str | None = Field(default=None,max_length=40)
    client_request_id: str = Field(min_length=1,max_length=128)


class ResolutionInput(Closed):
    expected_revision: int = Field(ge=1,le=2**53-1)
    resolution: str = Field(min_length=1,max_length=1000)


class ProbeInput(Closed):
    probe_id: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,63}$')
    source_label: str | None = Field(default=None,max_length=100)
    status: str = Field(pattern=r'^(healthy|unhealthy|unknown)$')
    observed_at: str = Field(max_length=40)
    valid_until: str = Field(max_length=40)


def _unique(pairs):
    value = {}
    for key,item in pairs:
        if key in value:
            raise HubError('invalid_infrastructure_json')
        value[key] = item
    return value


async def _payload(request,model,maximum=16384):
    sizes=request.headers.getlist('content-length')
    if len(sizes)>1 or sizes and (not sizes[0].isdigit() or len(sizes[0])>8 or int(sizes[0])>maximum):
        raise HubError('infrastructure_request_too_large',413)
    if request.headers.get('content-type','').split(';',1)[0].strip().lower()!='application/json':
        raise HubError('infrastructure_json_required',415)
    body=bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>maximum:
            raise HubError('infrastructure_request_too_large',413)
        body.extend(chunk)
    try:
        value=json.loads(body,object_pairs_hook=_unique,
                         parse_constant=lambda value:(_ for _ in ()).throw(ValueError()))
        return model.model_validate(value)
    except (ValidationError,ValueError,UnicodeError,RecursionError):
        # No reflected inputs, validation contexts, raw bodies or private data.
        raise HubError('invalid_infrastructure_input') from None


def attach_infrastructure_routes(app,service,actor,*,metrics=None,metrics_identity=None,grafana_host_url=None):
    if service is not None and type(service) is not InfrastructureService:
        raise TypeError('Explicit InfrastructureService required')
    if metrics is not None and (service is None or metrics.service is not service):
        raise TypeError('Metrics must read through the attached InfrastructureService')

    def authorized(request,action='read'):
        _authorization(request)
        identity=actor(request,mutation=True) if action not in ('read','metrics_read') else actor(request)
        InfrastructureService.principal(identity)
        if service is None:
            raise HubError('infrastructure_not_configured',503)
        service.require(identity,action)
        return identity

    @app.get('/api/hub/v1/infrastructure/devices')
    def devices(request:Request):
        identity=authorized(request)
        values=request.query_params.getlist('include_retired')
        if len(values)>1 or values and values[0] not in ('true','false') or set(request.query_params)-{'include_retired'}:
            raise HubError('invalid_infrastructure_filter')
        return service.list_devices(identity,include_retired=values==['true'])

    @app.get('/api/hub/v1/infrastructure/metrics')
    async def machine_metrics(request:Request):
        # Same 503/403 order as other reads; then 404 while Prometheus metrics are unconfigured.
        identity=metrics_identity(request) if metrics_identity is not None else authorized(request,'metrics_read')
        if request.query_params:
            raise HubError('invalid_infrastructure_filter')
        if metrics is None:
            raise HubError('infrastructure_metrics_not_configured',404)
        return await metrics.read(identity)

    @app.get('/api/hub/v1/infrastructure/metrics/history')
    async def machine_metric_history(request:Request):
        identity=metrics_identity(request) if metrics_identity is not None else authorized(request,'metrics_read')
        params=request.query_params
        if set(params)!={'host','range'} or any(len(params.getlist(name))!=1 for name in params):
            raise HubError('invalid_infrastructure_filter')
        if metrics is None or not hasattr(metrics,'history'):
            raise HubError('infrastructure_metrics_not_configured',404)
        return await metrics.history(identity,params['host'],params['range'])

    @app.get('/api/hub/v1/infrastructure/metrics/links')
    def machine_metric_links(request:Request):
        # Owner's browser only: the link opens the owner's Grafana, so agents and
        # bearer callers never receive the template, whatever it contains.
        if request.headers.getlist('authorization'):
            raise HubError('forbidden',403)
        identity=metrics_identity(request) if metrics_identity is not None else authorized(request,'metrics_read')
        if metrics_identity is None and InfrastructureService.principal(identity).kind!='human':
            raise HubError('forbidden',403)
        if request.query_params:
            raise HubError('invalid_infrastructure_filter')
        if metrics is None:
            raise HubError('infrastructure_metrics_not_configured',404)
        return {'schema_version':1,'grafana_host_url':grafana_host_url}

    @app.get('/api/hub/v1/infrastructure/devices/{device_id}')
    def detail(device_id:str,request:Request):
        identity=authorized(request)
        return service.get_device(identity,device_id)

    @app.put('/api/hub/v1/infrastructure/devices/{device_id}')
    async def save(device_id:str,request:Request):
        identity=authorized(request,'device_write')
        payload=(await _payload(request,DeviceInput)).model_dump()
        revision=payload.pop('expected_revision')
        return service.upsert_device(identity,{'id':device_id,**payload},expected_revision=revision)

    @app.post('/api/hub/v1/infrastructure/devices/{device_id}/maintenance')
    async def open_maintenance(device_id:str,request:Request):
        identity=authorized(request,'maintenance_write')
        payload=await _payload(request,MaintenanceInput)
        return service.append_maintenance(identity,device_id,**payload.model_dump())

    @app.post('/api/hub/v1/infrastructure/maintenance/{maintenance_id}/resolve')
    async def resolve(maintenance_id:str,request:Request):
        identity=authorized(request,'maintenance_write')
        payload=await _payload(request,ResolutionInput)
        return service.resolve_maintenance(identity,maintenance_id,**payload.model_dump())
