"""MCP adapters use authenticated actors and the same real registry projection."""
from datetime import datetime,timedelta,timezone
import sqlite3

import pytest

from engineering_hub.infrastructure import InfrastructureRegistry,InfrastructurePrincipal,CAPABILITIES
from engineering_hub.infrastructure_service import InfrastructureService
from engineering_hub.infrastructure_tools import register_infrastructure_tools
from engineering_hub.repository import HubError


class Tools:
    def __init__(self):self.tools={}
    def tool(self):
        def registered(fn):self.tools[fn.__name__]=fn;return fn
        return registered


@pytest.fixture
def env():
    clock=[datetime(2026,10,26,tzinfo=timezone.utc)]
    registry=InfrastructureRegistry(sqlite3.connect(':memory:'),capabilities={
        InfrastructurePrincipal('human','human:1'):CAPABILITIES,
        InfrastructurePrincipal('agent','hermes'):{'read','maintenance_write','probe_write'},
        InfrastructurePrincipal('agent','codex'):{'read'}},clock=lambda:clock[0])
    service=InfrastructureService(registry)
    service.upsert_device('human:1',dict(id='staging',name='Staging',addresses=[],role='Fixture'),expected_revision=0)
    tools=Tools()
    register_infrastructure_tools(tools,service=service,identity=lambda ctx:ctx,invoke=lambda fn:fn())
    yield tools.tools,service,clock
    registry.close()


def test_tools_same_capabilities_and_device_projection(env):
    tools,service,_=env
    assert set(tools)=={'hub_infrastructure_list','hub_infrastructure_get','hub_infrastructure_maintenance_open',
                        'hub_infrastructure_maintenance_resolve','hub_infrastructure_probe_record'}
    assert tools['hub_infrastructure_list']('codex')==service.list_devices('codex')
    assert tools['hub_infrastructure_list']('codex')['capabilities']=={'device_write':False,'maintenance_write':False}
    assert tools['hub_infrastructure_get']('staging','hermes')==service.get_device('hermes','staging')
    assert not any('device_write' in name or 'upsert' in name for name in tools)


@pytest.mark.parametrize('actor',['claude','human:2','future-agent','human:cf:invalid'])
def test_unknown_and_ungranted_actors_fail_before_device_existence_or_payload(env,actor):
    tools,*_=env
    with pytest.raises(HubError,match='forbidden'):tools['hub_infrastructure_get']('missing',actor)
    with pytest.raises(HubError,match='forbidden'):
        tools['hub_infrastructure_probe_record']('missing',{'not':'a probe','private-secret':'must not reflect'},actor)


def test_agent_maintenance_attribution_idempotency_and_revision(env):
    tools,_,_=env
    payload={'reason':'Planned\nFixture','expected_end':None,'client_request_id':'test-note'}
    item=tools['hub_infrastructure_maintenance_open']('staging',payload,'hermes')
    assert item['opened_by']=={'kind':'agent','identity':'hermes'}
    assert tools['hub_infrastructure_maintenance_open']('staging',payload,'hermes')==item
    with pytest.raises(HubError,match='forbidden'):
        tools['hub_infrastructure_maintenance_resolve'](item['id'],{'expected_revision':1,'resolution':'done'},'codex')
    resolved=tools['hub_infrastructure_maintenance_resolve'](item['id'],{'expected_revision':1,'resolution':'done'},'hermes')
    assert resolved['state']=='resolved'
    with pytest.raises(HubError,match='revision_conflict'):
        tools['hub_infrastructure_maintenance_resolve'](item['id'],{'expected_revision':1,'resolution':'again'},'hermes')


def test_probe_tool_explicit_grant_preserves_real_source_and_times(env):
    tools,_,clock=env
    payload=dict(probe_id='exporter',source_label='Registered exporter',status='healthy',
                 observed_at=clock[0].isoformat(),valid_until=(clock[0]+timedelta(minutes=1)).isoformat())
    with pytest.raises(HubError,match='forbidden'):
        tools['hub_infrastructure_probe_record']('staging',payload,'codex')
    result=tools['hub_infrastructure_probe_record']('staging',payload,'hermes')
    observation=result['device']['observations'][0]
    assert observation=={'probe_id':'exporter','source_label':'Registered exporter','state':'ok',
                         'observed_at':payload['observed_at'],'valid_until':payload['valid_until']}
    clock[0]+=timedelta(minutes=2)
    aged=tools['hub_infrastructure_get']('staging','codex')['device']['observations'][0]
    assert aged['state']=='stale' and aged['observed_at']==payload['observed_at']


def test_tool_inputs_closed_sanitized_and_no_actor_or_urls(env):
    tools,_,clock=env
    bad={'reason':'fixture','client_request_id':'key','actor':'human:1-private-secret'}
    with pytest.raises(HubError,match='^invalid_infrastructure_input$') as error:
        tools['hub_infrastructure_maintenance_open']('staging',bad,'hermes')
    assert 'private-secret' not in str(error.value)
    with pytest.raises(HubError,match='invalid_infrastructure_input'):
        tools['hub_infrastructure_probe_record']('staging',dict(probe_id='http',status='healthy',
            observed_at=clock[0].isoformat(),valid_until=(clock[0]+timedelta(minutes=1)).isoformat(),
            url='http://example.invalid/never-fetched'),'hermes')


def test_disabled_tools_still_validate_canonical_actor_then_unavailable():
    tools=Tools();register_infrastructure_tools(tools,service=None,identity=lambda ctx:ctx,invoke=lambda fn:fn())
    with pytest.raises(HubError,match='infrastructure_not_configured'):tools.tools['hub_infrastructure_list']('codex')
    with pytest.raises(HubError,match='forbidden'):tools.tools['hub_infrastructure_list']('future')
