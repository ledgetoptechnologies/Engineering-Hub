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


@POSIX
def test_explicit_app_defaults_need_no_file_and_preserve_operator_disable(tmp_path):
    path = tmp_path / 'infrastructure.json'
    module = compose_infrastructure(path, state_root=tmp_path, default_owner='human:1')
    assert module.state == 'ready' and not path.exists()
    assert module.prometheus_origin.url == 'http://192.168.50.80:30104'
    assert module.service.list_devices('human:1')['items'] == []
    module.close()
    write(path, config(False))
    disabled = compose_infrastructure(path, state_root=tmp_path, default_owner='human:1')
    assert disabled.state == 'disabled' and disabled.service is None

    write(path, config(True))
    explicit = compose_infrastructure(path, state_root=tmp_path, default_owner='human:1')
    assert explicit.state == 'ready' and explicit.prometheus_origin is None
    explicit.close()


@POSIX
def test_prometheus_origin_without_mapping_list_is_valid(tmp_path):
    value = config()
    value['prometheus'] = {'address': '192.0.2.80', 'port': 9090}
    path = write(tmp_path / 'infrastructure.json', value)
    module = compose_infrastructure(path, state_root=tmp_path)
    assert module.state == 'ready' and module.prometheus_origin.url == 'http://192.0.2.80:9090'
    assert module.collector is None
    module.close()


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


def test_startup_diagnostics_are_bounded_and_do_not_expose_configuration(tmp_path, caplog):
    secret = 'private-token-in-path-and-content'
    path = tmp_path / secret
    path.write_text(secret)
    module = compose_infrastructure(path, state_root=tmp_path)
    assert module.state == 'invalid' and module.service is None
    assert 'config_unreadable_or_invalid' in caplog.text
    assert secret not in caplog.text and str(tmp_path) not in caplog.text
    caplog.clear()
    module = compose_infrastructure(tmp_path / 'absent', state_root=tmp_path)
    assert module.state == 'disabled'
    assert 'config_missing_no_default_owner' in caplog.text
    caplog.clear()
    module = compose_infrastructure(write(tmp_path / 'disabled', config(False)), state_root=tmp_path)
    assert module.state == 'disabled'
    assert 'explicitly_disabled' in caplog.text


@POSIX
def test_startup_diagnostics_identify_private_directory_failure(tmp_path, caplog):
    settings = write(tmp_path / 'settings.json', config())
    root = tmp_path / 'infrastructure'
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    module = compose_infrastructure(settings, state_root=tmp_path)
    assert module.state == 'invalid' and module.service is None
    assert 'state_directory_mode_failed' in caplog.text
    assert str(tmp_path) not in caplog.text


@POSIX
def test_directory_chmod_failure_is_safe(tmp_path, caplog, monkeypatch):
    settings = write(tmp_path / 'settings.json', config())
    def fail(*args, **kwargs):
        raise OSError('private failure detail')
    monkeypatch.setattr(os, 'chmod', fail)
    module = compose_infrastructure(settings, state_root=tmp_path)
    assert module.state == 'invalid' and module.service is None
    assert 'state_directory_chmod_failed' in caplog.text
    assert 'private failure detail' not in caplog.text and str(tmp_path) not in caplog.text


@POSIX
def test_directory_owner_failure_is_safe(tmp_path, caplog, monkeypatch):
    settings = write(tmp_path / 'settings.json', config())
    root = tmp_path / 'infrastructure'
    root.mkdir(mode=0o700)
    original = os.fstat
    def altered(fd):
        info = original(fd)
        if info.st_ino == root.stat().st_ino:
            values = list(info)
            values[4] = info.st_uid + 1
            return os.stat_result(values)
        return info
    monkeypatch.setattr(os, 'fstat', altered)
    module = compose_infrastructure(settings, state_root=tmp_path)
    assert module.state == 'invalid' and module.service is None
    assert 'state_directory_owner_failed' in caplog.text
    assert str(tmp_path) not in caplog.text


@POSIX
def test_directory_identity_failure_is_safe(tmp_path, caplog, monkeypatch):
    import engineering_hub.infrastructure_module as module_impl
    settings = write(tmp_path / 'settings.json', config())
    root = tmp_path / 'infrastructure'
    root.mkdir(mode=0o700)
    original = module_impl._identity
    calls = 0
    def altered(info):
        nonlocal calls
        calls += 1
        identity = original(info)
        return (identity[0], identity[1] + 1) if calls == 3 else identity
    monkeypatch.setattr(module_impl, '_identity', altered)
    module = compose_infrastructure(settings, state_root=tmp_path)
    assert module.state == 'invalid' and module.service is None
    assert 'state_directory_identity_failed' in caplog.text
    assert str(tmp_path) not in caplog.text


@POSIX
def test_existing_directory_is_allowed_but_chmod_file_exists_fails_closed(tmp_path, caplog, monkeypatch):
    settings = write(tmp_path / 'settings.json', config())
    existing_state = tmp_path / 'existing'
    existing_state.mkdir()
    (existing_state / 'infrastructure').mkdir(mode=0o700)
    existing = compose_infrastructure(settings, state_root=existing_state)
    assert existing.state == 'ready'
    existing.close()

    new_state = tmp_path / 'new'
    new_state.mkdir()
    def fail_chmod(*args, **kwargs):
        raise FileExistsError('private failure detail')
    monkeypatch.setattr(os, 'chmod', fail_chmod)
    failed = compose_infrastructure(settings, state_root=new_state)
    assert failed.state == 'invalid' and failed.service is None
    assert 'state_directory_chmod_failed' in caplog.text
    assert 'private failure detail' not in caplog.text and str(tmp_path) not in caplog.text



def _leftover(tmp_path, mode=0o600, content=b''):
    """A first start that created the file and stopped before the schema."""
    directory = tmp_path / 'infrastructure'; directory.mkdir(mode=0o700); directory.chmod(0o700)
    database = directory / 'inventory.sqlite3'; database.write_bytes(content); database.chmod(mode)
    return directory, database


def _snapshot(directory):
    return {p.name: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode), p.stat().st_ino) for p in directory.iterdir()}


def _preserved(directory):
    return sorted(p for p in directory.iterdir() if p.name.startswith('inventory.sqlite3.empty-'))


@POSIX
@pytest.mark.parametrize('mode', [0o600, 0o700])
def test_empty_leftover_is_kept_aside_and_a_fresh_inventory_starts(tmp_path, caplog, mode):
    caplog.set_level('INFO')
    path = tmp_path / 'infrastructure.json'
    directory, database = _leftover(tmp_path, mode)
    inode = database.stat().st_ino
    module = compose_infrastructure(path, state_root=tmp_path, default_owner='human:1')
    try:
        assert module.state == 'ready' and not path.exists()
        module.service.upsert_device('human:1', dict(id='fixture', name='Fixture', addresses=[], role='Fixture'),
                                     expected_revision=0)
    finally:
        module.close()
    assert 'empty_database_preserved' in caplog.text and str(tmp_path) not in caplog.text
    [kept] = _preserved(directory)
    # The old inode is kept untouched; the live inventory is a new file.
    assert kept.stat().st_ino == inode and kept.read_bytes() == b'' and stat.S_IMODE(kept.stat().st_mode) == mode
    assert database.stat().st_ino != inode
    assert {p.name for p in directory.iterdir()} == {'inventory.sqlite3', kept.name}
    caplog.clear()
    reopened = compose_infrastructure(path, state_root=tmp_path, default_owner='human:1')
    try:
        assert reopened.state == 'ready'
        assert reopened.service.get_device('human:1', 'fixture')['device']['revision'] == 1
    finally:
        reopened.close()
    assert 'empty_database_preserved' not in caplog.text and len(_preserved(directory)) == 1


@POSIX
def test_populated_owner_only_database_with_execute_bit_reopens(tmp_path):
    settings = write(tmp_path / 'settings.json', config())
    first = compose_infrastructure(settings, state_root=tmp_path); assert first.state == 'ready'
    first.service.upsert_device('human:1', dict(id='fixture', name='Fixture', addresses=[], role='Fixture'),
                                expected_revision=0)
    first.close()
    database = tmp_path / 'infrastructure/inventory.sqlite3'; database.chmod(0o700)
    module = compose_infrastructure(settings, state_root=tmp_path)
    try:
        assert module.state == 'ready'
        assert module.service.get_device('hermes', 'fixture')['device']['revision'] == 1
        assert stat.S_IMODE(database.stat().st_mode) == 0o700
    finally:
        module.close()


@POSIX
@pytest.mark.parametrize('unsafe', ['group_read', 'other_read', 'group_exec', 'owner_read_only', 'hardlink',
                                    'wal_sidecar', 'shm_sidecar', 'journal_sidecar', 'empty_sidecar',
                                    'already_preserved'])
def test_empty_leftover_with_any_other_doubt_stays_closed_and_untouched(tmp_path, caplog, unsafe):
    path = tmp_path / 'infrastructure.json'
    mode = {'group_read': 0o640, 'other_read': 0o604, 'group_exec': 0o610, 'owner_read_only': 0o400}.get(unsafe, 0o600)
    directory, database = _leftover(tmp_path, mode)
    if unsafe == 'hardlink':
        os.link(database, directory / 'preserved.sqlite3')
    elif unsafe == 'empty_sidecar':
        (directory / 'inventory.sqlite3-wal').write_bytes(b'')
    elif unsafe == 'already_preserved':
        for index in range(3):
            (directory / ('inventory.sqlite3.empty-' + str(index))).write_bytes(b'')
    elif unsafe.endswith('_sidecar'):
        (directory / ('inventory.sqlite3-' + unsafe.split('_')[0])).write_bytes(b'unknown original pages')
    before = _snapshot(directory)
    module = compose_infrastructure(path, state_root=tmp_path, default_owner='human:1')
    assert module.state == 'invalid' and module.service is None
    assert _snapshot(directory) == before
    assert 'empty_database_preserved' not in caplog.text and str(tmp_path) not in caplog.text


@POSIX
def test_nonempty_unknown_bytes_are_never_moved_or_opened(tmp_path):
    path = tmp_path / 'infrastructure.json'
    directory, database = _leftover(tmp_path, 0o600, b'\0' * 4096)
    before = _snapshot(directory)
    module = compose_infrastructure(path, state_root=tmp_path, default_owner='human:1')
    assert module.state == 'invalid' and module.service is None
    assert _snapshot(directory) == before


@POSIX
def test_leftover_filled_while_being_moved_is_put_back_unchanged(tmp_path, monkeypatch):
    import engineering_hub.infrastructure_module as module
    path = tmp_path / 'infrastructure.json'
    directory, database = _leftover(tmp_path)
    inode = database.stat().st_ino
    real = os.rename

    def racing(src, dst, **kwargs):
        if src == 'inventory.sqlite3':
            foreign = sqlite3.connect(database)
            foreign.execute('CREATE TABLE someone_else(id TEXT)'); foreign.commit(); foreign.close()
        return real(src, dst, **kwargs)
    monkeypatch.setattr(module.os, 'rename', racing)
    result = compose_infrastructure(path, state_root=tmp_path, default_owner='human:1')
    monkeypatch.undo()
    assert result.state == 'invalid' and result.service is None
    assert database.stat().st_ino == inode and not _preserved(directory)
    check = sqlite3.connect(database)
    try:
        assert check.execute("SELECT name FROM sqlite_master").fetchall() == [('someone_else',)]
    finally:
        check.close()


@POSIX
@pytest.mark.parametrize('late', ['journal', 'wal', 'shm'])
def test_sidecar_appearing_before_open_is_never_replayed(tmp_path, monkeypatch, caplog, late):
    import engineering_hub.infrastructure_module as module
    path = tmp_path / 'infrastructure.json'
    directory, database = _leftover(tmp_path)
    real = module._preserve_empty

    def racing(*args):
        moved = real(*args)
        (directory / ('inventory.sqlite3-' + late)).write_bytes(b'foreign uncommitted pages')
        return moved
    monkeypatch.setattr(module, '_preserve_empty', racing)
    result = compose_infrastructure(path, state_root=tmp_path, default_owner='human:1')
    assert result.state == 'invalid' and result.service is None
    assert 'database_open_failed' in caplog.text
    # SQLite never opened the new file, so the sidecar was neither replayed nor removed.
    assert database.read_bytes() == b''
    assert (directory / ('inventory.sqlite3-' + late)).read_bytes() == b'foreign uncommitted pages'
    assert [p.read_bytes() for p in _preserved(directory)] == [b'']


@POSIX
def test_interruption_after_moving_aside_recovers_on_the_next_start(tmp_path, monkeypatch):
    import engineering_hub.infrastructure_module as module
    path = tmp_path / 'infrastructure.json'
    directory, database = _leftover(tmp_path)

    def interrupted(fd, mode):
        raise OSError('simulated stop after the move')
    monkeypatch.setattr(module.os, 'fchmod', interrupted)
    assert compose_infrastructure(path, state_root=tmp_path, default_owner='human:1').state == 'invalid'
    monkeypatch.undo()
    later = compose_infrastructure(path, state_root=tmp_path, default_owner='human:1')
    try:
        assert later.state == 'ready'
    finally:
        later.close()
    assert [p.read_bytes() for p in _preserved(directory)] == [b'', b'']
