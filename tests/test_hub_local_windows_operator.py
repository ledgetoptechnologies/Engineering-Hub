"""Offline fixed operator sequencing; no Docker daemon is contacted."""
import json
import sys
import types
import uuid
import threading

import pytest

from scripts.hub_local_windows_operator import run_operator, OPERATOR_IMAGE, BOOT, LABEL
from scripts.hub_local_windows_storage import WindowsTask, OperatorStorage


def rig(monkeypatch, *, bad_profile=False, bad_image=False):
    task=object.__new__(WindowsTask); task.task_id=str(uuid.uuid4()); task.daemon_id='daemon-123456789'
    ids=[str(uuid.uuid4()) for _ in range(4)]
    storage=OperatorStorage('source','/v/source','scratch','/v/scratch','journal','/v/journal','a'*64,OPERATOR_IMAGE,*ids[:3])
    request={'task_id':ids[3],'daemon_id':task.daemon_id,'source_root':'/v/source/checkout',
             'private_dir':'/v/scratch','journal_dir':'/v/journal','initial_manifest':'fixture','socket_group':0,'source_digest':'sha256:'+'d'*64}
    resources=types.SimpleNamespace(memory_bytes=64*1024**2,cpu_millicores=100,pids_limit=16)
    manifest=types.SimpleNamespace(runtime=types.SimpleNamespace(resources=resources),canonical_bytes=lambda:b'fixture')
    fake=types.ModuleType('engineering_hub.container_pilot_manifest')
    fake.PilotManifest=types.SimpleNamespace(from_bytes=lambda _:manifest)
    monkeypatch.setitem(sys.modules,'engineering_hub.container_pilot_manifest',fake)
    events=[]; calls=[]; cid='b'*64; coordinator='c'*64
    task.claim_verified_operator=lambda *a:events.append('claim')
    task.recheck_verified_operator=lambda *a:events.append('recheck')
    task.inspect_daemon=lambda:events.append('daemon')
    task.record=lambda e,**kw:events.append(e)
    requested=[{'Type':'volume','Source':name,'Target':target,'VolumeOptions':{'NoCopy':True}}
               for name,target in [('source','/v/source'),('scratch','/v/scratch'),('journal','/v/journal')]]
    mounts=[{'Type':'volume','Name':name,'Driver':'local','Source':target,'Destination':target,'RW':rw}
            for name,target,rw in [('source','/v/source',False),('scratch','/v/scratch',True),('journal','/v/journal',True)]]
    mounts += [{'Type':'bind','Source':src,'Destination':dst,'RW':rw,'Propagation':'rprivate'}
               for src,dst,rw in [('/v/source/docker','/usr/bin/docker',False),('/var/run/docker.sock','/var/run/docker.sock',True),('/v/source/checkout','/opt/hub',False)]]
    host={'NetworkMode':'none','ReadonlyRootfs':True,'CapDrop':['ALL'],'CapAdd':None,'Privileged':False,
          'SecurityOpt':['no-new-privileges'],'CgroupnsMode':'private','GroupAdd':['0'],'NanoCpus':250000000,
          'Memory':536870912,'MemorySwap':536870912,'PidsLimit':128,'LogConfig':{'Type':'local','Config':{'max-size':'1m','max-file':'2'}},
          'Tmpfs':{'/tmp':'rw,noexec,nosuid,nodev,size=64m'},'Devices':[],'DeviceRequests':None,'PortBindings':{},
          'PublishAllPorts':False,'PidMode':'','IpcMode':'private','RestartPolicy':{'Name':'no'},'Mounts':requested}
    if bad_profile: host['Privileged']=True
    cfg={'Image':OPERATOR_IMAGE,'Labels':{LABEL:task.task_id},'User':'10001:10001',
         'Entrypoint':['/opt/validation-venv/bin/python'],'Cmd':['-I','-B','-c',BOOT]}
    def call(*args):
        calls.append(args)
        if args[:2]==('image','inspect'): return 'null' if '--format' in args and 'Volumes' in args[-1] else OPERATOR_IMAGE
        if args[:2]==('container','ls'): return ''
        if args[:2]==('volume','ls'): return ''
        if args[:2]==('container','rm'): return cid
        if args[:2]==('container','create'): return cid
        if args[:2]==('container','inspect'):
            state={'Status':'created','Running':False,'Pid':0} if 'operator-start-returned' not in events else {'Status':'exited','Running':False,'Pid':0,'ExitCode':0,'Paused':False,'Restarting':False,'Dead':False}
            return json.dumps([{'Id':cid,'Image':('sha256:'+'f'*64 if bad_image else OPERATOR_IMAGE),'Name':'/hub-local-operator-'+task.task_id,'Config':cfg,'HostConfig':host,'Mounts':mounts,'State':state}])
        raise AssertionError(args)
    task.call=call
    task.call_input=lambda args,data,timeout:(events.append('start') or json.dumps({'status':'completed','coordinator_id':coordinator}))
    return task,storage,request,events,calls,cid,coordinator


def test_operator_checks_profile_and_rechecks_before_start(monkeypatch):
    task,storage,request,events,calls,cid,coordinator=rig(monkeypatch)
    result=run_operator(task,storage,image_id=OPERATOR_IMAGE,request=request,timeout_seconds=60)
    assert result.operator_id==cid and result.coordinator_id==coordinator and task._operator_completion is result
    assert events.index('claim')<events.index('operator-create-intent')<events.index('recheck')<events.index('start')
    assert sum(args[:2]==('container','rm') for args in calls)==1


def test_operator_rejects_effective_profile_before_start(monkeypatch):
    task,storage,request,events,calls,_,_=rig(monkeypatch,bad_profile=True)
    with pytest.raises(ValueError,match='profile mismatch'): run_operator(task,storage,image_id=OPERATOR_IMAGE,request=request,timeout_seconds=60)
    assert 'operator-created' in events and 'start' not in events and 'recheck' not in events


def test_failed_storage_claim_does_not_contact_daemon(monkeypatch):
    task,storage,request,events,calls,_,_=rig(monkeypatch)
    def denied(*args): raise PermissionError('source unavailable')
    task.claim_verified_operator=denied
    with pytest.raises(PermissionError): run_operator(task,storage,image_id=OPERATOR_IMAGE,request=request,timeout_seconds=60)
    assert not calls and not events


def test_operator_rejects_wrong_effective_image_before_start(monkeypatch):
    task,storage,request,events,_,_,_=rig(monkeypatch,bad_image=True)
    with pytest.raises(ValueError,match='profile mismatch'): run_operator(task,storage,image_id=OPERATOR_IMAGE,request=request,timeout_seconds=60)
    assert 'operator-created' in events and 'start' not in events


def test_actual_claim_and_recheck_are_used_by_operator(monkeypatch):
    import scripts.hub_local_windows_initialization as init
    task,storage,request,events,calls,_,_=rig(monkeypatch)
    task._storage=storage; task._operator_lock=threading.Lock(); task._operator_attempted=False; task._operator_binding=None
    task.claim_verified_operator=WindowsTask.claim_verified_operator.__get__(task)
    task.recheck_verified_operator=WindowsTask.recheck_verified_operator.__get__(task)
    measurements=[]
    monkeypatch.setattr(init,'remeasure',lambda t,s:(measurements.append((t,s)) or request['source_digest']))
    run_operator(task,storage,image_id=OPERATOR_IMAGE,request=request,timeout_seconds=60)
    assert len(measurements)==2 and all(t is task and s is storage for t,s in measurements)
    assert 'operator-admission-intent' in events and 'operator-admitted' in events
    assert task._operator_attempted and 'start' in events


def test_inner_resource_remains_prevents_operator_removal(monkeypatch):
    task,storage,request,events,calls,_,_=rig(monkeypatch)
    original=task.call
    def call(*args):
        if args[:2]==('volume','ls'): return 'unexpected-volume'
        return original(*args)
    task.call=call
    with pytest.raises(ValueError,match='inner volumes remain'): run_operator(task,storage,image_id=OPERATOR_IMAGE,request=request,timeout_seconds=60)
    assert not any(a[:2]==('container','rm') for a in calls)
    assert not hasattr(task,'_operator_completion')


def test_unknown_operator_remove_cannot_certify_completion(monkeypatch):
    task,storage,request,events,_,_,_=rig(monkeypatch)
    original=task.call
    def call(*args):
        if args[:2]==('container','rm'): raise OSError('unknown')
        return original(*args)
    task.call=call
    with pytest.raises(OSError): run_operator(task,storage,image_id=OPERATOR_IMAGE,request=request,timeout_seconds=60)
    assert 'operator-remove-intent' in events and 'operator-absent' not in events
    assert not hasattr(task,'_operator_completion')
