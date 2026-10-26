"""Capability-gated MCP metadata tools; no host fetch, probe or execution."""
from .infrastructure_api import MaintenanceInput, ResolutionInput, ProbeInput
from .infrastructure_service import InfrastructureService
from .repository import HubError
from pydantic import ValidationError


def _validated(model,payload):
    try:
        return model.model_validate(payload).model_dump()
    except ValidationError:
        raise HubError('invalid_infrastructure_input') from None


def register_infrastructure_tools(mcp,*,service,identity,invoke):
    from mcp.server.fastmcp import Context
    if service is not None and type(service) is not InfrastructureService:
        raise TypeError('Explicit InfrastructureService required')

    def authorized(ctx,action='read'):
        actor=identity(ctx)
        InfrastructureService.principal(actor)
        if service is None:
            raise HubError('infrastructure_not_configured',503)
        service.require(actor,action)
        return actor

    @mcp.tool()
    def hub_infrastructure_list(ctx:Context,include_retired:bool=False)->dict:
        """Read explicitly permitted declared devices and timestamped observations."""
        actor=authorized(ctx)
        return invoke(lambda:service.list_devices(actor,include_retired=include_retired))

    @mcp.tool()
    def hub_infrastructure_get(device_id:str,ctx:Context)->dict:
        """Read one permitted device with bounded maintenance history."""
        actor=authorized(ctx)
        return invoke(lambda:service.get_device(actor,device_id))

    @mcp.tool()
    def hub_infrastructure_maintenance_open(device_id:str,payload:dict,ctx:Context)->dict:
        """Record maintenance under an explicit grant; expected end never resolves it."""
        actor=authorized(ctx,'maintenance_write')
        return invoke(lambda:service.append_maintenance(actor,device_id,**_validated(MaintenanceInput,payload)))

    @mcp.tool()
    def hub_infrastructure_maintenance_resolve(maintenance_id:str,payload:dict,ctx:Context)->dict:
        """Explicitly resolve maintenance using its expected revision."""
        actor=authorized(ctx,'maintenance_write')
        return invoke(lambda:service.resolve_maintenance(actor,maintenance_id,**_validated(ResolutionInput,payload)))

    @mcp.tool()
    def hub_infrastructure_probe_record(device_id:str,payload:dict,ctx:Context)->dict:
        """Record actual source probe timestamps under probe_write; performs no probe."""
        actor=authorized(ctx,'probe_write')
        return invoke(lambda:service.record_probe(actor,device_id,**_validated(ProbeInput,payload)))
