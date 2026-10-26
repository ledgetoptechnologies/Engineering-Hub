"""Optional composition uses only temporary files; no live grants or DBs."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from engineering_hub.infrastructure_module import compose_infrastructure
from engineering_hub.repository import HubError


POSIX=pytest.mark.skipif(os.name!='posix',reason='POSIX nofollow/FLOCK/private ownership primitives')


def config(enabled=True):
    return {'schema_version':1,'enabled':enabled,'principals':[
        {'kind':'human','identity':'human:1','capabilities':['read','device_write','maintenance_write']},
        {'kind':'agent','identity':'hermes','capabilities':['read','maintenance_write','probe_write']}]}


def write(path,value):
    path.write_text(json.dumps(value));path.chmod(0o600)
    return path


def test_missing_default_and_disabled_never_create_state(tmp_path):
    state=tmp_path/'missing-state'
    for path in (None,tmp_path/'absent.json',write(tmp_path/'disabled.json',config(False))):
        module=compose_infrastructure(path,state_root=state)
        assert module.state=='disabled' and module.service is None
        module.close();module.close()
        assert not state.exists()


@pytest.mark.parametrize('value',[
    {'schema_version':True,'enabled':False,'principals':[]},
    {'schema_version':2,'enabled':False,'principals':[]},
    {'schema_version':1,'enabled':'false','principals':[]},
    {'schema_version':1,'enabled':False,'principals':[],'db_path':'private-path'},
    {'schema_version':1,'enabled':True,'principals':[{'kind':'future','identity':'new','capabilities':['read']}]},
    {'schema_version':1,'enabled':True,'principals':[{'kind':'human','identity':'hermes','capabilities':['read']}]},
    {'schema_version':1,'enabled':True,'principals':[{'kind':'agent','identity':'human:1','capabilities':['read']}]},
    {'schema_version':1,'enabled':True,'principals':[{'kind':'agent','identity':'hermes','capabilities':['device_write','read']}]},
    {'schema_version':1,'enabled':True,'principals':[{'kind':'agent','identity':'hermes','capabilities':['probe_write']}]},
    {'schema_version':1,'enabled':True,'principals':[{'kind':'human','identity':'human:1','capabilities':['read','read']}]},
    {'schema_version':1,'enabled':True,'principals':[{'kind':'agent','identity':'future-agent','capabilities':['read']}]},
    {'schema_version':1,'enabled':True,'principals':[{'kind':'human','identity':'human:1','capabilities':['read'],'actor':'spoofed'}]},
    {'schema_version':1,'enabled':True,'principals':[{'kind':'human','identity':'human:1','capabilities':['read']}]*2},
    {'schema_version':1,'enabled':False,'principals':[{}]*129},
])
def test_invalid_optional_settings_closed_before_state_creation(tmp_path,value):
    settings=write(tmp_path/'settings.json',value);state=tmp_path/'state'
    module=compose_infrastructure(settings,state_root=state)
    assert module.state=='invalid' and module.service is None and not state.exists()
    assert not hasattr(module,'error')


@pytest.mark.parametrize('raw',[b'{"schema_version":1,"schema_version":1,"enabled":false,"principals":[]}',
                               b'x'*16385,b'{"enabled":NaN}',b'not json'])
def test_duplicate_malformed_oversized_settings_are_inert(tmp_path,raw):
    settings=tmp_path/'settings.json';settings.write_bytes(raw);settings.chmod(0o600)
    state=tmp_path/'state'
    module=compose_infrastructure(settings,state_root=state)
    assert module.state=='invalid' and module.service is None and not state.exists()


@pytest.mark.parametrize('source',[
    {'address':'example.test','port':30104,'mappings':[]},
    {'address':'192.0.2.80','port':True,'mappings':[]},
    {'address':'192.0.2.80','port':30104,'mappings':[], 'url':'http://untrusted.test'},
    {'address':'192.0.2.80','port':30104,'mappings':[{'job':'Fixture','instance':'host.test:9100',
        'device_id':'fixture','probe_id':'node'}]},
    {'address':'192.0.2.80','port':30104,'mappings':[{'job':'Fixture','instance':'192.0.2.1:9100',
        'device_id':'fixture','probe_id':'node','unexpected':True}]},
    {'address':'192.0.2.80','port':30104,'mappings':[{'job':'Fixture','instance':'192.0.2.1:9100',
        'device_id':'fixture','probe_id':'node'}]*2},
])
def test_invalid_collector_config_never_creates_inventory(tmp_path,source):
    value=config();value['prometheus']=source
    module=compose_infrastructure(write(tmp_path/'settings.json',value),state_root=tmp_path)
    assert module.state=='invalid' and module.service is None and module.collector is None
    assert not (tmp_path/'infrastructure').exists()


@POSIX
def test_enabled_private_owned_storage_service_callback_and_durable_reopen(tmp_path):
    settings=write(tmp_path/'settings.json',config());changes=[]
    module=compose_infrastructure(settings,state_root=tmp_path,on_change=changes.append)
    assert module.state=='ready' and module.service is not None
    directory=tmp_path/'infrastructure';database=directory/'inventory.sqlite3'
    assert stat.S_IMODE(directory.stat().st_mode)==0o700
    assert stat.S_IMODE(database.stat().st_mode)==0o600 and database.stat().st_nlink==1
    service=module.service
    service.upsert_device('human:1',dict(id='fixture',name='Fixture',addresses=[],role='Fixture'),expected_revision=0)
    assert changes==[{'kind':'infrastructure.changed','device_id':'fixture'}]
    assert service.list_devices('hermes')['capabilities']=={'device_write':False,'maintenance_write':True}
    with pytest.raises(HubError):service.list_devices('codex')
    module.close();module.close()
    reopened=compose_infrastructure(settings,state_root=tmp_path)
    try:
        assert reopened.state=='ready'
        assert reopened.service.get_device('hermes','fixture')['device']['revision']==1
    finally:reopened.close()


@POSIX
def test_lifetime_flock_denies_second_owner_and_releases_on_close(tmp_path):
    settings=write(tmp_path/'settings.json',config())
    first=compose_infrastructure(settings,state_root=tmp_path)
    assert first.state=='ready'
    database=tmp_path/'infrastructure/inventory.sqlite3';before=database.read_bytes()
    second=compose_infrastructure(settings,state_root=tmp_path)
    assert second.state=='invalid' and second.service is None and database.read_bytes()==before
    second.close();first.close()
    third=compose_infrastructure(settings,state_root=tmp_path)
    assert third.state=='ready';third.close()


@POSIX
@pytest.mark.parametrize('unsafe',['config_symlink','config_hardlink','config_writable','parent_symlink'])
def test_config_integrity_refuses_links_or_writes_before_state_creation(tmp_path,unsafe):
    actual=write(tmp_path/'actual.json',config());settings=actual
    if unsafe=='config_symlink':settings=tmp_path/'settings.json';settings.symlink_to(actual)
    elif unsafe=='config_hardlink':settings=tmp_path/'settings.json';os.link(actual,settings)
    elif unsafe=='config_writable':actual.chmod(0o622)
    else:
        parent=tmp_path/'linked';parent.symlink_to(tmp_path,target_is_directory=True);settings=parent/'actual.json'
    state=tmp_path/'state'
    module=compose_infrastructure(settings,state_root=state)
    assert module.state=='invalid' and module.service is None and not state.exists()


@POSIX
@pytest.mark.parametrize('unsafe',['directory_symlink','directory_mode','db_symlink','db_hardlink','db_mode','journal_sidecar'])
def test_existing_storage_integrity_refuses_without_adopting(tmp_path,unsafe):
    settings=write(tmp_path/'settings.json',config())
    original=compose_infrastructure(settings,state_root=tmp_path);assert original.state=='ready';original.close()
    root=tmp_path/'infrastructure';database=root/'inventory.sqlite3'
    if unsafe=='directory_symlink':root.rename(tmp_path/'preserved');root.symlink_to(tmp_path/'preserved',target_is_directory=True)
    elif unsafe=='directory_mode':root.chmod(0o755)
    elif unsafe=='db_symlink':database.rename(root/'preserved.sqlite3');database.symlink_to(root/'preserved.sqlite3')
    elif unsafe=='db_hardlink':os.link(database,root/'preserved.sqlite3')
    elif unsafe=='db_mode':database.chmod(0o644)
    else:(root/'inventory.sqlite3-journal').write_bytes(b'unknown original transaction')
    before={path.name:path.read_bytes() for path in root.iterdir() if path.is_file()}
    module=compose_infrastructure(settings,state_root=tmp_path)
    assert module.state=='invalid' and module.service is None
    assert {path.name:path.read_bytes() for path in root.iterdir() if path.is_file()}==before


@POSIX
def test_foreign_or_modified_owned_schema_is_readonly_refused(tmp_path):
    settings=write(tmp_path/'settings.json',config());directory=tmp_path/'infrastructure';directory.mkdir(mode=0o700)
    path=directory/'inventory.sqlite3'
    db=sqlite3.connect(path);db.execute('CREATE TABLE hub_private_ledger(id TEXT)');db.commit();db.close();path.chmod(0o600)
    before=hashlib.sha256(path.read_bytes()).hexdigest();names=set(directory.iterdir())
    module=compose_infrastructure(settings,state_root=tmp_path)
    assert module.state=='invalid' and module.service is None
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before and set(directory.iterdir())==names
    check=sqlite3.connect(path)
    try:assert check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[('hub_private_ledger',)]
    finally:check.close()


@POSIX
def test_explicit_enabled_no_grants_never_assigns_first_user(tmp_path):
    value=config();value['principals']=[];settings=write(tmp_path/'settings.json',value)
    module=compose_infrastructure(settings,state_root=tmp_path)
    try:
        assert module.state=='ready'
        for actor in ('human:1','hermes','codex'):
            with pytest.raises(HubError):module.service.list_devices(actor)
    finally:module.close()


@POSIX
def test_sibling_module_directory_does_not_invalidate_shared_state_root(tmp_path):
    settings=write(tmp_path/'settings.json',config())
    module=compose_infrastructure(settings,state_root=tmp_path)
    try:
        assert module.state=='ready'
        service=module.service
        before=tmp_path.stat()
        (tmp_path/'another-module').mkdir(mode=0o700)
        # On ext4, a legitimate sibling directory changes the parent's link count.
        # It does not replace the named state root or weaken its owner/mode checks.
        after=tmp_path.stat()
        assert (after.st_dev,after.st_ino)==(before.st_dev,before.st_ino)
        assert after.st_nlink>=before.st_nlink
        service.upsert_device('human:1',dict(id='fixture',name='Fixture',addresses=[],role='Fixture'),expected_revision=0)
        assert service.list_devices('human:1')['items'][0]['id']=='fixture'
        assert module.state=='ready'
    finally:module.close()


@POSIX
def test_live_directory_rename_replacement_denies_before_sqlite_write_and_latches(tmp_path):
    settings=write(tmp_path/'settings.json',config())
    module=compose_infrastructure(settings,state_root=tmp_path);assert module.state=='ready'
    service=module.service
    service.upsert_device('human:1',dict(id='original',name='Original',addresses=[],role='Fixture'),expected_revision=0)
    # Inspect actual VFS behavior rather than assuming proc-FD paths are kept.
    paths=service.registry.db.execute('PRAGMA database_list').fetchall()
    main = [row for row in paths if row[1] == 'main']
    assert len(main) == 1 and Path(main[0][2]).name == 'inventory.sqlite3'
    # Reading sqlite_temp_master can initialize an empty built-in temp schema.
    assert all(row[1] in {'main', 'temp'} for row in paths)
    original=tmp_path/'infrastructure';preserved=tmp_path/'preserved-original'
    original.rename(preserved);original.mkdir(mode=0o700)
    before={path.name:path.read_bytes() for path in preserved.iterdir() if path.is_file()}
    with pytest.raises(HubError,match='infrastructure_unavailable'):
        service.upsert_device('human:1',dict(id='replacement',name='Denied',addresses=[],role='Fixture'),expected_revision=0)
    assert module.state=='invalid' and module.service is None
    assert list(original.iterdir())==[]
    assert {path.name:path.read_bytes() for path in preserved.iterdir() if path.is_file()}==before
    # Restoring names does not adopt/recover the prior service automatically.
    original.rename(tmp_path/'unadopted-replacement');preserved.rename(original)
    with pytest.raises(HubError,match='infrastructure_unavailable'):service.list_devices('human:1')
    module.close();module.close()
    assert list((tmp_path/'unadopted-replacement').iterdir())==[]


@POSIX
@pytest.mark.parametrize('drift',['state_root_mode','directory_mode','database_mode','database_hardlink','database_replaced'])
def test_live_named_metadata_drift_blocks_reads_and_writes_without_adoption(tmp_path,drift):
    settings=write(tmp_path/'settings.json',config())
    module=compose_infrastructure(settings,state_root=tmp_path);assert module.state=='ready'
    service=module.service;database=tmp_path/'infrastructure/inventory.sqlite3'
    if drift=='state_root_mode':tmp_path.chmod(0o755)
    elif drift=='directory_mode':database.parent.chmod(0o755)
    elif drift=='database_mode':database.chmod(0o644)
    elif drift=='database_hardlink':os.link(database,database.parent/'preserved-hardlink')
    else:
        database.rename(database.parent/'preserved.sqlite3')
        database.write_bytes(b'foreign replacement');database.chmod(0o600)
    snapshot={path.name:path.read_bytes() for path in database.parent.iterdir() if path.is_file()}
    with pytest.raises(HubError,match='infrastructure_unavailable'):service.list_devices('human:1')
    assert module.service is None and module.state=='invalid'
    with pytest.raises(HubError,match='infrastructure_unavailable'):
        service.upsert_device('human:1',dict(id='denied',name='Denied',addresses=[],role='Fixture'),expected_revision=0)
    module.close()
    assert {path.name:path.read_bytes() for path in database.parent.iterdir() if path.is_file()}==snapshot


def test_trusted_guard_read_postcheck_and_sticky_failure(tmp_path):
    from engineering_hub.infrastructure import InfrastructureRegistry,InfrastructurePrincipal,CAPABILITIES
    live=[True]
    registry=InfrastructureRegistry(sqlite3.connect(':memory:'),capabilities={
        InfrastructurePrincipal('human','human:1'):CAPABILITIES},admission_guard=lambda:live[0])
    principal=InfrastructurePrincipal('human','human:1')
    registry.upsert_device(principal,dict(id='fixture',name='Fixture',addresses=[],role='Fixture'),expected_revision=0)
    original=registry._device
    def drifted(db,identity):
        result=original(db,identity);live[0]=False;return result
    registry._device=drifted
    try:
        with pytest.raises(HubError,match='infrastructure_unavailable'):registry.get_device(principal,'fixture')
        live[0]=True
        with pytest.raises(HubError,match='infrastructure_unavailable'):registry.list_devices(principal)
    finally:registry.close()


def test_trusted_guard_write_postcheck_rolls_back_before_commit():
    from engineering_hub.infrastructure import InfrastructureRegistry,InfrastructurePrincipal,CAPABILITIES
    live=[True];principal=InfrastructurePrincipal('human','human:1')
    registry=InfrastructureRegistry(sqlite3.connect(':memory:'),capabilities={principal:CAPABILITIES},
                                    admission_guard=lambda:live[0])
    original=registry._event
    def drifted(*args):original(*args);live[0]=False
    registry._event=drifted
    try:
        with pytest.raises(HubError,match='infrastructure_unavailable'):
            registry.upsert_device(principal,dict(id='denied',name='Denied',addresses=[],role='Fixture'),expected_revision=0)
        assert registry.db.execute('SELECT count(*) FROM infra_devices').fetchone()[0]==0
        assert registry.db.execute('SELECT count(*) FROM infra_events').fetchone()[0]==0
    finally:registry.close()
