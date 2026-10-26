"""Standalone temporary SQLite tests; no network, probes or production DB."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import uuid

import pytest

from engineering_hub.infrastructure import (InfrastructureRegistry, InfrastructurePrincipal, CAPABILITIES,
    MAX_PROBES_PER_DEVICE,MAX_MAINTENANCE_PER_DEVICE,MAX_EVENTS_PER_DEVICE,MAX_PROBE_IDENTITIES,MAX_PROBE_SAMPLES)
from engineering_hub.repository import HubError


OWNER=InfrastructurePrincipal('human','owner-authenticated')
AGENT=InfrastructurePrincipal('agent','assistant-authenticated')
READER=InfrastructurePrincipal('human','reader-authenticated')
NOW=datetime(2026,10,26,tzinfo=timezone.utc)


def device(identity='sample-device',**changes):
    return dict(id=identity,name='Sample device',hostname='sample.example',addresses=['192.0.2.10'],
                network='192.0.2.0/24',vlan=10,role='test fixture',retired=False,**changes)


@pytest.fixture
def registry():
    clock=[NOW]
    registry=InfrastructureRegistry(sqlite3.connect(':memory:'), capabilities={
        OWNER:CAPABILITIES,AGENT:{'read','maintenance_write','probe_write'},READER:{'read'}},clock=lambda:clock[0])
    registry.upsert_device(OWNER,device(),expected_revision=0)
    yield registry,clock
    registry.close()


def test_closed_by_default_and_future_principals_denied():
    registry=InfrastructureRegistry(sqlite3.connect(':memory:'))
    try:
        for principal in [OWNER,AGENT,InfrastructurePrincipal('robot','future'),{'kind':'human','identity':'owner-authenticated'}]:
            with pytest.raises(HubError,match='forbidden'):registry.list_devices(principal)
    finally:registry.close()
    with pytest.raises(HubError):
        InfrastructureRegistry(sqlite3.connect(':memory:'),capabilities={InfrastructurePrincipal('future','x'):{'read'}})


def test_permissions_explicit_owner_only_devices_and_read_required():
    registry=InfrastructureRegistry(sqlite3.connect(':memory:'),capabilities={
        AGENT:CAPABILITIES,OWNER:{'device_write','maintenance_write','probe_write'}})
    try:
        with pytest.raises(HubError,match='forbidden'):registry.upsert_device(AGENT,device(),expected_revision=0)
        with pytest.raises(HubError,match='forbidden'):registry.upsert_device(OWNER,device(),expected_revision=0)
        with pytest.raises(HubError,match='forbidden'):registry.append_maintenance(OWNER,'sample-device','reason','key')
        with pytest.raises(HubError,match='forbidden'):registry.record_probe(OWNER,'sample-device','healthy',
            observed_at=NOW.isoformat(),valid_until=(NOW+timedelta(minutes=1)).isoformat())
    finally:registry.close()


def test_device_stable_slug_cas_and_facts_never_overwritten_by_probe(registry):
    store,clock=registry
    before=store.get_device(READER,'sample-device')
    assert before['revision']==1 and before['id']=='sample-device'
    changed=device();changed['name']='Updated declared name'
    updated=store.upsert_device(OWNER,changed,expected_revision=1)
    assert updated['revision']==2 and updated['created_at']==before['created_at']
    with pytest.raises(HubError,match='revision_conflict'):store.upsert_device(OWNER,changed,expected_revision=1)
    store.record_probe(AGENT,'sample-device','unhealthy',observed_at=NOW.isoformat(),valid_until=(NOW+timedelta(minutes=1)).isoformat())
    assert store.get_device(READER,'sample-device')==updated
    with pytest.raises(HubError,match='forbidden'):store.upsert_device(AGENT,changed,expected_revision=2)


@pytest.mark.parametrize('changes',[
    {'id':'Other_Device'},{'addresses':['http://example.invalid']},{'addresses':['192.0.2.10','192.0.2.10']},
    {'addresses':['fe80::1%eth0']},{'addresses':['198.51.100.1']},{'network':'192.0.2.1/24'},
    {'vlan':True},{'vlan':4095},{'retired':'false'},{'hostname':'bad host'},
    {'hostname':'-bad.example'},{'role':'x'*101},{'name':'bad\x00text'},{'actor':'spoofed'}])
def test_strict_declared_schema(registry,changes):
    store,_=registry
    item=device();item.update(changes)
    with pytest.raises(HubError):store.upsert_device(OWNER,item,expected_revision=1)
    assert store.get_device(READER,'sample-device')['revision']==1


def test_device_limit_counts_retired_and_ipv6_is_literal(registry):
    store,_=registry
    for number in range(255):
        item=device('fixture-'+str(number));item.update(addresses=[],network=None,retired=True)
        store.upsert_device(OWNER,item,expected_revision=0)
    assert len(store.list_devices(READER)['items'])==1
    assert len(store.list_devices(READER,include_retired=True)['items'])==256
    with pytest.raises(HubError,match='device_limit'):store.upsert_device(OWNER,device('overflow'),expected_revision=0)
    item=device();item.update(addresses=['2001:db8::1'],network='2001:db8::/64')
    assert store.upsert_device(OWNER,item,expected_revision=1)['addresses']==['2001:db8::1']


def test_maintenance_idempotency_original_result_one_open_and_cas(registry):
    store,clock=registry
    opened=store.append_maintenance(AGENT,'sample-device','Planned fixture repair','original-request',
                                    expected_end=(NOW+timedelta(minutes=5)).isoformat())
    assert opened['opened_by']=={'kind':'agent','identity':AGENT.identity}
    assert store.append_maintenance(AGENT,'sample-device','Planned fixture repair','original-request',
                                    expected_end=(NOW+timedelta(minutes=5)).isoformat())==opened
    with pytest.raises(HubError,match='idempotency_conflict'):
        store.append_maintenance(AGENT,'sample-device','different','original-request')
    with pytest.raises(HubError,match='maintenance_already_open'):
        store.append_maintenance(OWNER,'sample-device','another','other-request')
    clock[0]+=timedelta(days=2)
    assert store.maintenance_history(READER,'sample-device')['items'][0]['state']=='open'
    with pytest.raises(HubError,match='revision_conflict'):
        store.resolve_maintenance(OWNER,opened['id'],expected_revision=0,resolution='done')
    resolved=store.resolve_maintenance(OWNER,opened['id'],expected_revision=1,resolution='Checked and resolved')
    assert resolved['revision']==2 and resolved['resolved_by']['kind']=='human'
    with pytest.raises(HubError,match='revision_conflict'):
        store.resolve_maintenance(OWNER,opened['id'],expected_revision=1,resolution='again')
    assert store.append_maintenance(AGENT,'sample-device','Planned fixture repair','original-request',
                                    expected_end=(NOW+timedelta(minutes=5)).isoformat())==opened
    events=list(store.db.execute('SELECT action FROM infra_events'))
    assert [row[0] for row in events]==['device.upserted','maintenance.opened','maintenance.resolved']


def test_retirement_preserves_history_and_allows_existing_open_resolution(registry):
    store,_=registry
    opened=store.append_maintenance(AGENT,'sample-device','repair','key')
    item=device();item['retired']=True
    store.upsert_device(OWNER,item,expected_revision=1)
    assert store.list_devices(READER)['items']==[]
    assert store.get_device(READER,'sample-device')['retired'] is True
    with pytest.raises(HubError,match='device_retired'):store.append_maintenance(AGENT,'sample-device','new','new-key')
    with pytest.raises(HubError,match='device_retired'):store.record_probe(AGENT,'sample-device','healthy',
        observed_at=NOW.isoformat(),valid_until=(NOW+timedelta(minutes=1)).isoformat())
    store.resolve_maintenance(OWNER,opened['id'],expected_revision=1,resolution='Resolved after retirement')
    assert store.maintenance_history(READER,'sample-device')['items'][0]['state']=='resolved'


def test_bounded_history_reports_truncation_without_deletion(registry):
    store,_=registry
    for number in range(51):
        item=store.append_maintenance(AGENT,'sample-device','fixture',str(number))
        store.resolve_maintenance(OWNER,item['id'],expected_revision=1,resolution='fixture complete')
    history=store.maintenance_history(READER,'sample-device')
    assert len(history['items'])==50 and history['truncated'] is True
    assert store.db.execute('SELECT count(*) FROM infra_maintenance').fetchone()[0]==51
    assert len(store.maintenance_history(READER,'sample-device',limit=1)['items'])==1
    with pytest.raises(HubError):store.maintenance_history(READER,'sample-device',limit=51)


def test_probe_freshness_reads_do_not_advance_observations_or_write(registry):
    store,clock=registry
    assert store.probe_state(READER,'sample-device')['status']=='unknown'
    observed=NOW.isoformat();valid=(NOW+timedelta(minutes=1)).isoformat()
    store.record_probe(AGENT,'sample-device','healthy',observed_at=observed,valid_until=valid)
    changes=store.db.total_changes
    fresh=store.probe_state(READER,'sample-device')
    clock[0]+=timedelta(minutes=1)
    stale=store.probe_state(READER,'sample-device')
    assert fresh['status']=='healthy' and stale['status']=='stale'
    assert fresh['observed_at']==stale['observed_at']==observed
    assert fresh['valid_until']==stale['valid_until']==valid
    assert store.db.total_changes==changes
    with pytest.raises(HubError,match='invalid_infrastructure_time'):
        store.record_probe(AGENT,'sample-device','healthy',observed_at=(clock[0]+timedelta(days=1)).isoformat(),
                           valid_until=(clock[0]+timedelta(days=2)).isoformat())
    clock[0]+=timedelta(days=1)
    assert store.probe_state(READER,'sample-device')['status']=='stale'


@pytest.mark.parametrize('value',['bad','2026-10-26T00:00:00',None,True])
def test_invalid_probe_times_cannot_produce_healthy(registry,value):
    store,_=registry
    with pytest.raises(HubError):store.record_probe(AGENT,'sample-device','healthy',
        observed_at=value,valid_until=(NOW+timedelta(minutes=1)).isoformat())
    store.db.execute('INSERT INTO infra_probes(device_id,principal,data) VALUES(?,?,?)',
                     ('sample-device','{}',json.dumps({'status':'healthy','observed_at':value,'valid_until':'bad'})))
    store.db.commit()
    assert store.probe_state(READER,'sample-device')['status']=='unknown'


def test_foreign_database_refused_without_mutations():
    db=sqlite3.connect(':memory:');db.execute('CREATE TABLE hub_tasks(id TEXT)');db.commit()
    with pytest.raises(HubError,match='dedicated_infrastructure_database_required'):
        InfrastructureRegistry(db,capabilities={OWNER:CAPABILITIES})
    assert [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]==['hub_tasks']
    db.close()


def test_sqlite_transaction_failure_rolls_back_maintenance_and_idempotency(registry):
    store,_=registry
    store.db.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON infra_events BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):store.append_maintenance(AGENT,'sample-device','reason','failed-key')
    assert store.db.execute('SELECT count(*) FROM infra_maintenance').fetchone()[0]==0
    assert store.db.execute('SELECT count(*) FROM infra_requests').fetchone()[0]==0


def test_durable_reopen_preserves_declared_facts_and_journal(tmp_path):
    path=tmp_path/'standalone.sqlite3'
    store=InfrastructureRegistry(sqlite3.connect(path),capabilities={OWNER:CAPABILITIES},clock=lambda:NOW)
    store.upsert_device(OWNER,device('stable-device'),expected_revision=0)
    opened=store.append_maintenance(OWNER,'stable-device','maintenance','key')
    store.close()
    reopened=InfrastructureRegistry(sqlite3.connect(path),capabilities={OWNER:CAPABILITIES},clock=lambda:NOW)
    try:
        assert reopened.get_device(OWNER,'stable-device')['revision']==1
        assert reopened.maintenance_history(OWNER,'stable-device')['items']==[opened]
        assert reopened.append_maintenance(OWNER,'stable-device','maintenance','key')==opened
    finally:reopened.close()


def test_revision_types_identifier_immutability_and_plaintext_boundaries(registry):
    store,_=registry
    with pytest.raises(HubError,match='invalid_revision'):
        store.upsert_device(OWNER,device(),expected_revision=True)
    renamed=device('renamed-device')
    with pytest.raises(HubError,match='revision_conflict'):
        store.upsert_device(OWNER,renamed,expected_revision=1)
    assert [item['id'] for item in store.list_devices(READER)['items']]==['sample-device']
    for reason in ['x'*1001,'control\x00text','']:
        with pytest.raises(HubError):store.append_maintenance(AGENT,'sample-device',reason,'key')
    opened=store.append_maintenance(AGENT,'sample-device','x'*1000,'key')
    with pytest.raises(HubError,match='invalid_maintenance_id'):
        store.resolve_maintenance(OWNER,'not-a-uuid',expected_revision=1,resolution='done')
    assert store.resolve_maintenance(OWNER,opened['id'],expected_revision=1,resolution='x'*1000)['state']=='resolved'


def test_policy_snapshot_not_mutable_via_original_allowlist():
    actions={'read'};policy={READER:actions}
    store=InfrastructureRegistry(sqlite3.connect(':memory:'),capabilities=policy)
    actions.add('device_write');policy[READER]=CAPABILITIES
    try:
        with pytest.raises(HubError,match='forbidden'):store.upsert_device(READER,device(),expected_revision=0)
        with pytest.raises(TypeError):store.capabilities[READER]=CAPABILITIES
    finally:store.close()


def test_probe_delayed_healthy_cannot_replace_newer_unhealthy_and_sources_are_distinct(registry):
    store,clock=registry
    clock[0]+=timedelta(minutes=10)
    store.record_probe(AGENT,'sample-device','unhealthy',probe_id='http',source_label='HTTP exporter',
        observed_at=clock[0].isoformat(),valid_until=(clock[0]+timedelta(minutes=5)).isoformat())
    with pytest.raises(HubError,match='probe_observation_out_of_order'):
        store.record_probe(AGENT,'sample-device','healthy',probe_id='http',source_label='HTTP exporter',
            observed_at=NOW.isoformat(),valid_until=(clock[0]+timedelta(minutes=5)).isoformat())
    store.record_probe(AGENT,'sample-device','healthy',probe_id='ping',source_label='ICMP exporter',
        observed_at=clock[0].isoformat(),valid_until=(clock[0]+timedelta(minutes=5)).isoformat())
    assert store.probe_state(READER,'sample-device',probe_id='http')['status']=='unhealthy'
    assert store.probe_state(READER,'sample-device',probe_id='ping')['status']=='healthy'
    assert store.probe_state(READER,'sample-device')['status']=='unknown'
    assert [item['probe_id'] for item in store.probe_states(READER,'sample-device')['items']]==['http','ping']
    assert store.db.execute('SELECT count(*) FROM infra_probes').fetchone()[0]==2


def test_rejected_future_probe_never_becomes_healthy_when_time_catches_up(registry):
    store,clock=registry
    future=NOW+timedelta(minutes=10)
    with pytest.raises(HubError):store.record_probe(AGENT,'sample-device','healthy',
        observed_at=future.isoformat(),valid_until=(future+timedelta(minutes=5)).isoformat())
    clock[0]=future
    assert store.probe_state(READER,'sample-device')['status']=='unknown'
    assert store.db.execute('SELECT count(*) FROM infra_probes').fetchone()[0]==0
    # Even invalid persisted future-at-admission history remains unknown later.
    bad=dict(probe_id='default',source_label='fixture',status='healthy',observed_at=future.isoformat(),
             valid_until=(future+timedelta(minutes=5)).isoformat(),recorded_at=NOW.isoformat())
    store.db.execute('INSERT INTO infra_probes(device_id,principal,data) VALUES(?,?,?)',
                     ('sample-device','{}',json.dumps(bad)));store.db.commit()
    assert store.probe_state(READER,'sample-device')['status']=='unknown'


@pytest.mark.parametrize('kind',['temp','attached'])
def test_dedicated_connection_rejects_temp_shadow_and_attached_before_schema_mutation(kind):
    db=sqlite3.connect(':memory:')
    if kind=='temp':db.execute('CREATE TEMP TABLE infra_probes(sequence INTEGER,data TEXT)')
    else:db.execute("ATTACH DATABASE ':memory:' AS foreign_db")
    db.commit()
    with pytest.raises(HubError,match='dedicated_infrastructure_database_required'):
        InfrastructureRegistry(db,capabilities={OWNER:CAPABILITIES})
    assert list(db.execute("SELECT name FROM main.sqlite_master WHERE type='table'"))==[]
    if kind=='temp':assert list(db.execute('SELECT * FROM temp.infra_probes'))==[]
    db.close()


def test_multiline_maintenance_only_not_device_names_or_other_controls(registry):
    store,_=registry
    opened=store.append_maintenance(AGENT,'sample-device','First line\nSecond line','multiline')
    assert opened['reason']=='First line\nSecond line'
    assert store.resolve_maintenance(OWNER,opened['id'],expected_revision=1,
        resolution='Checked\nResolved')['resolution']=='Checked\nResolved'
    item=device();item['name']='First\nSecond'
    with pytest.raises(HubError):store.upsert_device(OWNER,item,expected_revision=1)
    with pytest.raises(HubError):store.append_maintenance(AGENT,'sample-device','Bad\tcontrol','tab')


def test_probe_identity_limit_and_scoped_ephemeral_retention(registry):
    store,clock=registry
    for number in range(MAX_PROBE_IDENTITIES):
        store.record_probe(AGENT,'sample-device','healthy',probe_id='probe-'+str(number),
            observed_at=clock[0].isoformat(),valid_until=(clock[0]+timedelta(days=1)).isoformat())
    with pytest.raises(HubError,match='probe_identity_limit'):
        store.record_probe(AGENT,'sample-device','healthy',probe_id='overflow',
            observed_at=clock[0].isoformat(),valid_until=(clock[0]+timedelta(days=1)).isoformat())
    for number in range(MAX_PROBES_PER_DEVICE-MAX_PROBE_IDENTITIES):
        clock[0]+=timedelta(seconds=1)
        store.record_probe(AGENT,'sample-device','healthy',probe_id='probe-0',
            observed_at=clock[0].isoformat(),valid_until=(clock[0]+timedelta(days=1)).isoformat())
    clock[0]+=timedelta(seconds=1)
    store.record_probe(AGENT,'sample-device','unhealthy',probe_id='probe-0',
        observed_at=clock[0].isoformat(),valid_until=(clock[0]+timedelta(days=1)).isoformat())
    assert store.db.execute('SELECT count(*) FROM infra_probes').fetchone()[0]==MAX_PROBE_SAMPLES+MAX_PROBE_IDENTITIES-1
    assert store.probe_state(READER,'sample-device',probe_id='probe-0')['status']=='unhealthy'
    assert store.probe_state(READER,'sample-device',probe_id='probe-1')['status']=='healthy'


def test_maintenance_and_idempotency_history_bound_preserves_original_requests(registry):
    store,_=registry
    first=None
    for number in range(MAX_MAINTENANCE_PER_DEVICE):
        item=store.append_maintenance(AGENT,'sample-device','fixture',str(number))
        first=first or item
        store.resolve_maintenance(OWNER,item['id'],expected_revision=1,resolution='done')
    with pytest.raises(HubError,match='maintenance_history_limit'):
        store.append_maintenance(AGENT,'sample-device','new','overflow')
    assert store.append_maintenance(AGENT,'sample-device','fixture','0')==first
    assert store.db.execute('SELECT count(*) FROM infra_requests').fetchone()[0]==MAX_MAINTENANCE_PER_DEVICE


def test_event_limit_reserves_resolution_of_open_maintenance(registry):
    store,_=registry
    opened=store.append_maintenance(AGENT,'sample-device','fixture','open')
    item=device();revision=1
    while store.db.execute('SELECT count(*) FROM infra_events').fetchone()[0]<MAX_EVENTS_PER_DEVICE-1:
        updated=store.upsert_device(OWNER,item,expected_revision=revision);revision=updated['revision']
    with pytest.raises(HubError,match='infrastructure_event_limit'):
        store.upsert_device(OWNER,item,expected_revision=revision)
    assert store.get_device(READER,'sample-device')['revision']==revision
    resolved=store.resolve_maintenance(OWNER,opened['id'],expected_revision=1,resolution='done')
    assert resolved['state']=='resolved'
    assert store.db.execute('SELECT count(*) FROM infra_events').fetchone()[0]==MAX_EVENTS_PER_DEVICE
    with pytest.raises(HubError,match='infrastructure_event_limit'):
        store.append_maintenance(AGENT,'sample-device','new','blocked')
    assert store.db.execute('SELECT count(*) FROM infra_maintenance').fetchone()[0]==1


def test_continuous_probe_more_than256_samples_rolls_without_losing_current_or_evidence(registry):
    store,clock=registry
    maintenance=store.append_maintenance(AGENT,'sample-device','Preserve maintenance evidence','evidence')
    events=store.db.execute('SELECT count(*) FROM infra_events').fetchone()[0]
    for number in range(300):
        clock[0]=NOW+timedelta(seconds=number)
        store.record_probe(AGENT,'sample-device','unhealthy' if number==299 else 'healthy',probe_id='http',
            observed_at=clock[0].isoformat(),valid_until=(clock[0]+timedelta(minutes=5)).isoformat())
        assert store.db.execute('SELECT count(*) FROM infra_probes').fetchone()[0]<=MAX_PROBE_SAMPLES
    current=store.probe_state(READER,'sample-device',probe_id='http')
    assert current['status']=='unhealthy' and current['observed_at']==clock[0].isoformat()
    with pytest.raises(HubError,match='probe_observation_out_of_order'):
        store.record_probe(AGENT,'sample-device','healthy',probe_id='http',
            observed_at=NOW.isoformat(),valid_until=(clock[0]+timedelta(minutes=5)).isoformat())
    assert store.probe_state(READER,'sample-device',probe_id='http')==current
    assert store.maintenance_history(READER,'sample-device')['items']==[maintenance]
    assert store.db.execute('SELECT count(*) FROM infra_events').fetchone()[0]==events
    assert store.append_maintenance(AGENT,'sample-device','Preserve maintenance evidence','evidence')==maintenance
