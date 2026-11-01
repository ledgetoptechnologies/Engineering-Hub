import json,uuid
import pytest
from scripts import hub_local_windows_cleanup as module
from scripts.hub_local_windows_operator import OperatorCompletion
from scripts.hub_local_windows_storage import Held,LABEL,OPTIONS,provision_storage
from tests.test_hub_local_windows_storage import fixture_task


def setup(monkeypatch,inner_present=False,lose_stop=False):
    task,image,events,_=fixture_task(); a,b,c=[str(uuid.uuid4()) for _ in range(3)]
    storage=provision_storage(task,helper_image_id=image,source_task_id=a,scratch_task_id=b,journal_task_id=c)
    inner=str(uuid.uuid4()); op='d'*64; coord='e'*64; digest='f'*64
    completion=OperatorCompletion(task,storage,inner,op,coord,image,digest)
    task._operator_completion=completion; task._operator_binding=(storage,digest,image)
    events.clear(); calls=[]; state=['running']; alive=[True]; volumes={storage.source_name:{},storage.scratch_name:OPTIONS,storage.journal_name:{}}
    monkeypatch.setattr(module,'_storage',lambda *a:None)
    def call(*args):
        calls.append(args)
        if args[:2]==('container','ls'):
            if '--filter' in args:
                f=args[args.index('--filter')+1]
                if f=='label='+LABEL+'='+inner: return 'f'*64 if inner_present else ''
                if f=='label='+LABEL+'='+task.task_id: return storage.scratch_keeper_id if alive[0] else ''
                if f=='volume='+storage.scratch_name and alive[0]: return storage.scratch_keeper_id
                return ''
            return storage.scratch_keeper_id if alive[0] else ''
        if args[:2]==('volume','ls'):
            if '--filter' in args: return '\n'.join(volumes) if args[-1]=='label='+LABEL+'='+task.task_id else ''
            return '\n'.join(volumes)
        if args[:2]==('volume','inspect'):
            name=args[-1]; return json.dumps([{'Name':name,'Mountpoint':task.docker_root+'/volumes/'+name+'/_data','Labels':{LABEL:task.task_id},'Driver':'local','Scope':'local','Options':volumes[name]}])
        if args[:2]==('container','inspect'):
            return json.dumps([{'Id':storage.scratch_keeper_id,'Name':'/keeper','Config':{},'HostConfig':{},'Mounts':[],
                'State':{'Status':state[0],'Running':state[0]=='running','Pid':12 if state[0]=='running' else 0,'Paused':False,'Restarting':False,'Dead':False}}])
        if args[:2]==('container','stop'):
            state[0]='exited'
            if lose_stop: raise OSError('lost reply')
            return storage.scratch_keeper_id
        if args[:2]==('container','rm'): alive[0]=False; return storage.scratch_keeper_id
        if args[:2]==('volume','rm'): del volumes[args[-1]]; return args[-1]
        raise AssertionError(args)
    task.call=call
    return task,storage,completion,calls,events,volumes


def test_cleanup_owned_scopes_and_retain_journal(monkeypatch):
    task,storage,completion,calls,events,volumes=setup(monkeypatch)
    result=module.cleanup(task,storage,completion)
    assert result=={'status':'cleaned_journal_retained','journal_volume':storage.journal_name}
    assert set(volumes)=={storage.journal_name} and task._operator_completion is None
    assert [e for e,_ in events if e.endswith('intent')]==['scratch-keeper-stop-intent','scratch-keeper-remove-intent','storage-volume-remove-intent','storage-volume-remove-intent']


def test_cleanup_denies_present_nested_scope_before_effect(monkeypatch):
    task,storage,completion,calls,events,volumes=setup(monkeypatch,inner_present=True)
    with pytest.raises(Held): module.cleanup(task,storage,completion)
    assert not any(args[:2] in (('container','stop'),('container','rm'),('volume','rm')) for args in calls)
    assert len(volumes)==3
    count=len(calls)
    with pytest.raises(Held): module.cleanup(task,storage,completion)
    assert len(calls)==count


def test_cleanup_rejects_forged_completion_before_effect(monkeypatch):
    task,storage,completion,calls,events,_=setup(monkeypatch)
    forged=OperatorCompletion(task,storage,completion.inner_task_id,completion.operator_id,completion.coordinator_id,completion.image_id,completion.request_sha256)
    with pytest.raises(Held): module.cleanup(task,storage,forged)
    assert calls==[]

def test_lost_stop_reply_holds_and_never_retries(monkeypatch):
    task,storage,completion,calls,events,volumes=setup(monkeypatch,lose_stop=True)
    with pytest.raises(Held): module.cleanup(task,storage,completion)
    count=len(calls)
    with pytest.raises(Held): module.cleanup(task,storage,completion)
    assert len(calls)==count and len(volumes)==3
    assert not any(args[:2] in (('container','rm'),('volume','rm')) for args in calls)
    import scripts.hub_local_windows_initialization as init
    task._seed_binding=(storage,object(),'sha256:'+'1'*64)
    monkeypatch.setattr(init,'_helper',lambda *args:pytest.fail('remeasure started'))
    with pytest.raises(Held): init.remeasure(task,storage)
