import json
import math
import time
import types
import os
import sys
import pytest
from scripts import hub_local_baseline_bootstrap as b
from tests.test_hub_container_pilot_manifest import make_manifest_data
from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor.daemon_backend import DockerEngineBackend
from hub_container_executor.operation_journal import SQLiteOperationJournal


def args(monkeypatch):
    monkeypatch.setattr(b.os,'getuid',lambda:10001); monkeypatch.setattr(b.os,'getgid',lambda:10001)
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
    (source/'unexpected').write_text('preserve')
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
