import json
import math
import time
import types
import os
import sys
import pytest
if sys.platform != 'linux':
    pytest.skip('local baseline bootstrap requires Linux', allow_module_level=True)
from scripts import hub_local_baseline_bootstrap as b
from tests.test_hub_container_pilot_manifest import make_manifest_data
from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor.daemon_backend import DockerEngineBackend
from hub_container_executor.operation_journal import SQLiteOperationJournal


def args(monkeypatch):
    monkeypatch.setattr(b.os,'getuid',lambda:10001); monkeypatch.setattr(b.os,'getgid',lambda:10001)
    monkeypatch.setattr(SQLiteOperationJournal,'inspect',lambda self, operation_id:None)
    backend=object.__new__(DockerEngineBackend); backend.operation_journal=object.__new__(SQLiteOperationJournal)
    backend.binding=types.SimpleNamespace(generation_root='/owned/generations')
    return dict(owner_database='/owned/hub.sqlite3',backend=backend,
        manifest=PilotManifest.from_bytes(json.dumps(make_manifest_data()).encode()),record_store=None,
        metadata_store=None,verifier=None,secret_resolver=None,owner_id='9e6223e4-5e11-4ea4-931e-3f56b51733f0',
        generation_root='/owned/generations',source_generation_id='baseline-one',frozen_leaf='frozen.sqlite3',
        create_id='c694ef57-160f-4a45-83d1-1ad0f811103a',start_id='8577d0b7-5d96-45b6-b2d8-a6845f099447',
        observe_id='057b7dcc-26d1-4f16-81d8-c5e640e8ad94',maximum_bytes=1048576,
        deadline_monotonic=time.monotonic()+30)

@pytest.mark.parametrize('value',[float('nan'),float('inf'),0])
def test_invalid_deadline_denied_before_owner(monkeypatch,value):
    a=args(monkeypatch); a['deadline_monotonic']=value
    monkeypatch.setattr(b,'PilotOwner',lambda *x,**y:pytest.fail('owner opened'))
    with pytest.raises(ValueError): b.bootstrap_baseline(**a)

def test_different_backend_generation_root_denied_before_owner(monkeypatch):
    a=args(monkeypatch); a['generation_root']='/other/generations'
    monkeypatch.setattr(b,'PilotOwner',lambda *x,**y:pytest.fail('owner opened'))
    with pytest.raises(ValueError): b.bootstrap_baseline(**a)

def test_duplicate_operation_denied_before_owner(monkeypatch):
    a=args(monkeypatch); a['start_id']=a['create_id']
    monkeypatch.setattr(b,'PilotOwner',lambda *x,**y:pytest.fail('owner opened'))
    with pytest.raises(ValueError): b.bootstrap_baseline(**a)


@pytest.mark.parametrize('used', ['create_id', 'start_id', 'observe_id',
                                  'preflight_probe_id', 'stop_id', 'restart_id'])
def test_used_operation_denied_before_snapshot_or_docker(monkeypatch, used):
    a=args(monkeypatch)
    a.update(quiescent=True, preflight_probe_id='6f665c4c-98d7-4715-b909-645411bc30f7',
             stop_id='d80c95ff-ec54-4bf9-85bf-69e15f589010',
             restart_id='24a60394-13b0-4d4c-a61f-152629e7c1a5')
    visited=[]; closed=[]
    class Owner:
        def __init__(self,*args,**kwargs): pass
        def assert_active(self,*args): pass
        def close(self): closed.append(True)
    monkeypatch.setattr(b,'PilotOwner',Owner)
    def inspect(self, operation_id):
        visited.append(operation_id)
        return object() if operation_id == a[used] else None
    monkeypatch.setattr(SQLiteOperationJournal,'inspect',inspect)
    monkeypatch.setattr(b,'_preflight_snapshot',lambda *a,**k:pytest.fail('snapshot preflight reached'))
    monkeypatch.setattr(DockerEngineBackend,'create_container',lambda *a,**k:pytest.fail('Docker create reached'))
    with pytest.raises(RuntimeError, match='identity already used'):
        b.bootstrap_baseline(**a)
    assert a[used] in visited and closed == [True]

def test_final_manifest_preserves_aliases_and_updates_only_backup():
    manifest=PilotManifest.from_bytes(json.dumps(make_manifest_data()).encode())
    final=b._final_manifest(manifest,'sha256:'+'f'*64)
    before=json.loads(manifest.canonical_bytes()); after=json.loads(final.canonical_bytes())
    assert after['state_policy']['baseline_backup_digest']=='sha256:'+'f'*64
    after['state_policy']['baseline_backup_digest']=before['state_policy']['baseline_backup_digest']
    assert after==before

def test_invalid_snapshot_parameters_deny_before_owner(monkeypatch):
    for overrides in ({'maximum_bytes':0},{'frozen_leaf':'../bad'}, {'frozen_leaf':'baseline-one'}):
        a=args(monkeypatch); a.update(overrides)
        monkeypatch.setattr(b,'PilotOwner',lambda *x,**y:pytest.fail('owner opened'))
        with pytest.raises(ValueError): b.bootstrap_baseline(**a)


def test_empty_initialization_requires_quiescent_mode_before_owner(monkeypatch):
    a=args(monkeypatch); a['initialize_empty']=True
    monkeypatch.setattr(b,'PilotOwner',lambda *x,**y:pytest.fail('owner opened'))
    with pytest.raises(ValueError): b.bootstrap_baseline(**a)


@pytest.mark.skipif(sys.platform != 'linux' or os.getuid() != 10001 or os.getgid() != 10001,
                    reason='isolated Linux app UID/GID 10001 required')
def test_empty_generation_preflight_denies_existing_entries(tmp_path):
    root=tmp_path/'generations'; root.mkdir(mode=0o700)
    source=root/'baseline-one'; source.mkdir(mode=0o700)
    b._preflight_snapshot(str(root),'baseline-one','frozen.sqlite3',1048576,time.monotonic()+10,
                          initialize_empty=True)
    database=source/'hub.sqlite3'
    assert database.read_bytes()==b''
    assert database.stat().st_mode & 0o777 == 0o600
    assert database.stat().st_nlink==1
    (source/'unexpected').write_text('preserve')
    with pytest.raises(ValueError,match='must be empty'):
        b._preflight_snapshot(str(root),'baseline-one','frozen.sqlite3',1048576,time.monotonic()+10,
                              initialize_empty=True)


@pytest.mark.parametrize('link', [False, True])
def test_empty_preflight_preserves_existing_database_or_symlink(tmp_path, monkeypatch, link):
    root=tmp_path/'generations'; root.mkdir()
    source=root/'baseline-one'; source.mkdir()
    target=tmp_path/'original'; target.write_bytes(b'preserve')
    database=source/'hub.sqlite3'
    if link:
        database.symlink_to(target)
    else:
        database.write_bytes(b'preserve')
    def directory(path, **_):
        fd=os.open(path,os.O_RDONLY | os.O_DIRECTORY)
        info=os.fstat(fd)
        return fd,(info.st_dev,info.st_ino)
    monkeypatch.setattr(b,'_directory',directory)
    monkeypatch.setattr(b,'_guard',lambda *args:None)
    with pytest.raises(ValueError,match='must be empty'):
        b._preflight_snapshot(str(root),'baseline-one','frozen.sqlite3',1048576,time.monotonic()+10,
                              initialize_empty=True)
    assert target.read_bytes()==b'preserve'
    assert database.is_symlink() if link else database.read_bytes()==b'preserve'


def test_empty_preflight_creates_exclusive_private_database(tmp_path, monkeypatch):
    root=tmp_path/'generations'; root.mkdir()
    source=root/'baseline-one'; source.mkdir()
    real_fstat=os.fstat
    def directory(path, **_):
        fd=os.open(path,os.O_RDONLY | os.O_DIRECTORY)
        info=real_fstat(fd)
        return fd,(info.st_dev,info.st_ino)
    def enrolled_owner(fd):
        info=real_fstat(fd)
        if not __import__('stat').S_ISREG(info.st_mode):
            return info
        fields=list(info); fields[4]=fields[5]=10001
        return os.stat_result(fields)
    monkeypatch.setattr(b,'_directory',directory)
    monkeypatch.setattr(b,'_guard',lambda *args:None)
    monkeypatch.setattr(b.os,'fstat',enrolled_owner)
    b._preflight_snapshot(str(root),'baseline-one','frozen.sqlite3',1048576,time.monotonic()+10,
                          initialize_empty=True)
    database=source/'hub.sqlite3'
    assert database.read_bytes()==b''
    assert database.stat().st_mode & 0o777 == 0o600
    assert database.stat().st_nlink==1
    with pytest.raises(ValueError,match='must be empty'):
        b._preflight_snapshot(str(root),'baseline-one','frozen.sqlite3',1048576,time.monotonic()+10,
                              initialize_empty=True)

def test_order_reaches_real_final_parse(monkeypatch):
    events=[]; a=args(monkeypatch); manifest=a['manifest']
    class Backend:
        def __init__(self): self.operation_journal=object.__new__(SQLiteOperationJournal); self.binding=types.SimpleNamespace(generation_root='/owned/generations')
        def inspect_image(self,*x): events.append('inspect'); return 'defaults'
        def create_container(self,*x): events.append('create'); return 'c'*64
        def start_container(self,*x): events.append('start')
    class Owner:
        def __init__(self,*x,**y): events.append('owner')
        def assert_active(self,*x): events.append('check')
        def close(self): events.append('close')
    class Resolver:
        def resolve_test_secret(self,*x): return 'synthetic'
    class Observer:
        def __init__(self, final,**kwargs):
            events.append('observer'); assert final.state_policy.baseline_backup_digest=='sha256:'+'f'*64
        def observe(self,*x): events.append('observe'); return 'draft'
    monkeypatch.setattr(b,'DockerEngineBackend',Backend); a['backend']=Backend(); a['secret_resolver']=Resolver()
    monkeypatch.setattr(b,'PilotOwner',Owner); monkeypatch.setattr(b,'_preflight_snapshot',lambda *x,**k:events.append('preflight'))
    image=types.SimpleNamespace(runnable_reference='sha256:'+'e'*64)
    monkeypatch.setattr(b,'verify_image_record',lambda *x:image)
    monkeypatch.setattr(b.HostAdapter,'_defaults_from_record',lambda x:'defaults')
    monkeypatch.setattr(b,'derive_runtime_profile',lambda *x:'profile')
    monkeypatch.setattr(b,'freeze_baseline_snapshot',lambda **x:(events.append('freeze') or types.SimpleNamespace(sha256='sha256:'+'f'*64)))
    monkeypatch.setattr(b,'BaselineObserver',Observer)
    result=b.bootstrap_baseline(**a)
    assert result.manifest.state_policy.baseline_backup_digest=='sha256:'+'f'*64
    assert [x for x in events if x in ('owner','preflight','inspect','create','start','freeze','observer','observe','close')]==['owner','preflight','inspect','create','start','freeze','observer','observe','close']


def test_unhealthy_preflight_never_stops_or_freezes(monkeypatch):
    from hub_container_executor.backend import ProbeObservation
    from hub_container_executor.runtime_profile import probe_set_digest
    a=args(monkeypatch); manifest=a['manifest']; events=[]
    class Backend:
        def __init__(self):
            self.operation_journal=object.__new__(SQLiteOperationJournal)
            self.binding=types.SimpleNamespace(generation_root='/owned/generations')
        def inspect_image(self,*x): return 'defaults'
        def create_container(self,*x): return 'c'*64
        def start_container(self,*x): pass
        def run_compiled_probes(self,*x):
            return ProbeObservation('c'*64,'sha256:'+'d'*64,probe_set_digest(manifest.probes.rollback),'unhealthy',True)
        def stop_container(self,*x): pytest.fail('unhealthy baseline must not be stopped for snapshot')
    class Owner:
        def __init__(self,*x,**y): pass
        def assert_active(self,*x): pass
        def close(self): events.append('closed')
    class Resolver:
        def resolve_test_secret(self,*x): return 'synthetic'
    monkeypatch.setattr(b,'DockerEngineBackend',Backend); a['backend']=Backend(); a['secret_resolver']=Resolver()
    monkeypatch.setattr(b,'PilotOwner',Owner); monkeypatch.setattr(b,'_preflight_snapshot',lambda *x,**k:None)
    monkeypatch.setattr(b,'verify_image_record',lambda *x:types.SimpleNamespace(
        runnable_reference='sha256:'+'e'*64,image_config_id='sha256:'+'d'*64))
    monkeypatch.setattr(b.HostAdapter,'_defaults_from_record',lambda x:'defaults')
    monkeypatch.setattr(b,'derive_runtime_profile',lambda *x:'profile')
    monkeypatch.setattr(b,'freeze_baseline_snapshot',lambda **x:pytest.fail('must not snapshot'))
    a.update(quiescent=True,preflight_probe_id='6f665c4c-98d7-4715-b909-645411bc30f7',
             stop_id='d80c95ff-ec54-4bf9-85bf-69e15f589010',restart_id='24a60394-13b0-4d4c-a61f-152629e7c1a5')
    with pytest.raises(RuntimeError,match='health is unverified'): b.bootstrap_baseline(**a)
    assert events == ['closed']


@pytest.mark.parametrize('wait_ms',[0,1000])
def test_restart_wait_checks_owner_and_fixed_budget(monkeypatch,wait_ms):
    now=[10.0];events=[]
    monkeypatch.setattr(b.time,'monotonic',lambda:now[0])
    def sleep(seconds):events.append(('sleep',seconds));now[0]+=seconds
    monkeypatch.setattr(b.time,'sleep',sleep)
    b._wait_after_restart(lambda:events.append('check'),20.0,wait_ms,[types.SimpleNamespace(timeout_ms=1000)])
    assert events==(['check',('sleep',1.0),'check'] if wait_ms else ['check','check'])


@pytest.mark.parametrize('case',['insufficient','oversleep','interrupt'])
def test_restart_wait_fails_closed_on_budget_or_interrupt(monkeypatch,case):
    now=[10.0];events=[]
    monkeypatch.setattr(b.time,'monotonic',lambda:now[0])
    def sleep(_):
        events.append('sleep')
        if case=='interrupt':raise InterruptedError('interrupted')
        now[0]=19.5
    monkeypatch.setattr(b.time,'sleep',sleep)
    with pytest.raises((RuntimeError,InterruptedError)):
        b._wait_after_restart(lambda:events.append('check'),11.5 if case=='insufficient' else 20.0,
                              1000,[types.SimpleNamespace(timeout_ms=1000)])
    assert events==(['check'] if case=='insufficient' else ['check','sleep'] if case=='interrupt' else ['check','sleep','check'])


def test_exhausted_budget_after_restart_never_constructs_observer(monkeypatch):
    from hub_container_executor.backend import ProbeObservation
    from hub_container_executor.runtime_profile import probe_set_digest
    a=args(monkeypatch);manifest=a['manifest'];events=[];now=[100.0]
    monkeypatch.setattr(b.time,'monotonic',lambda:now[0])
    class Backend:
        def __init__(self):
            self.operation_journal=object.__new__(SQLiteOperationJournal)
            self.binding=types.SimpleNamespace(generation_root='/owned/generations')
        def inspect_image(self,*x):return 'defaults'
        def create_container(self,*x):return 'c'*64
        def start_container(self,*x):pass
        def run_compiled_probes(self,*x):
            return ProbeObservation('c'*64,'sha256:'+'d'*64,probe_set_digest(manifest.probes.rollback),'healthy',True)
        def stop_container(self,*x):pass
    class Owner:
        def __init__(self,*x,**y):pass
        def assert_active(self,*x):pass
        def close(self):events.append('closed')
    class Resolver:
        def resolve_test_secret(self,*x):return 'synthetic'
    monkeypatch.setattr(b,'DockerEngineBackend',Backend);a['backend']=Backend();a['secret_resolver']=Resolver()
    monkeypatch.setattr(b,'PilotOwner',Owner);monkeypatch.setattr(b,'_preflight_snapshot',lambda *x,**k:None)
    monkeypatch.setattr(b,'verify_image_record',lambda *x:types.SimpleNamespace(runnable_reference='sha256:'+'e'*64,image_config_id='sha256:'+'d'*64))
    monkeypatch.setattr(b.HostAdapter,'_defaults_from_record',lambda x:'defaults')
    monkeypatch.setattr(b,'derive_runtime_profile',lambda *x:'profile')
    monkeypatch.setattr(b,'freeze_baseline_snapshot',lambda **x:types.SimpleNamespace(sha256='sha256:'+'f'*64))
    monkeypatch.setattr(b,'_exited_baseline',lambda *x:None)
    def restart(*_):events.append('restart');now[0]=129.9
    monkeypatch.setattr(b,'_restart_baseline',restart)
    monkeypatch.setattr(b,'BaselineObserver',lambda *x,**y:pytest.fail('observer must not be constructed'))
    a.update(deadline_monotonic=130.0,quiescent=True,preflight_probe_id='6f665c4c-98d7-4715-b909-645411bc30f7',
             stop_id='d80c95ff-ec54-4bf9-85bf-69e15f589010',restart_id='24a60394-13b0-4d4c-a61f-152629e7c1a5')
    with pytest.raises(RuntimeError,match='probe budget unavailable'):b.bootstrap_baseline(**a)
    assert events==['restart','closed']

def test_snapshot_preflight_close_attempts_both_descriptors(monkeypatch):
    seen=[]
    def directory(path,**kwargs):
        return (11,(1,1)) if str(path)=='/owned' else (12,(1,2))
    monkeypatch.setattr(b,'_directory',directory)
    for name in ('_root_entries','_operator_process_gate','_inventory','_guard'):
        monkeypatch.setattr(b,name,lambda *a,**k:None)
    def close(fd):
        seen.append(fd)
        if fd==12: raise OSError('failure')
    monkeypatch.setattr(b.os,'close',close)
    with pytest.raises(RuntimeError,match='cleanup'):
        b._preflight_snapshot('/owned','source','frozen',1024,time.monotonic()+30)
    assert seen==[12,11]
