import json
import uuid
import threading
import pytest
from scripts.hub_local_windows_storage import WindowsTask,provision_storage,OPTIONS,LABEL,KEEPER,Held


def fixture_task(fail=None,wrong_label=False):
    t=object.__new__(WindowsTask); t.task_id=str(uuid.uuid4()); t.daemon_id='daemon-123456789'; t.docker_root='/var/lib/docker'; t._storage_started=False; t._storage_lock=threading.Lock(); t._storage=None; t._operator_lock=threading.Lock(); t._operator_attempted=False; t._operator_binding=None; t._seed_lock=threading.RLock(); t._seed_started=False; t._seed_failed=False; t._cleanup_lock=threading.Lock(); t._cleanup_started=False
    events=[]; calls=[]; volumes={}; image='sha256:'+'a'*64; cid='b'*64
    t.record=lambda event,**v:events.append((event,v))
    t.inspect_daemon=lambda:None
    def call(*args):
        calls.append(args)
        if fail and args[:len(fail)]==fail: raise RuntimeError('unknown')
        if args[:2]==('image','inspect'): return json.dumps([{'Id':image,'Config':{'Volumes':None,'Labels':{'codex.owner':'inherited-image-label','desktop.docker.io/mounts/0/Source':'C:\\old','desktop.docker.io/mounts/0/SourceKind':'hostFile','desktop.docker.io/mounts/0/Target':'/old','desktop.docker.io/mounts/1/Source':'C:\\old-input','desktop.docker.io/mounts/1/SourceKind':'hostFile','desktop.docker.io/mounts/1/Target':'/old-input','desktop.docker.io/ports.scheme':'v2'}}}])
        if args[:2]==('volume','ls'): return ''
        if args[:2]==('volume','create'):
            name=args[-1]; options=OPTIONS if len(volumes)==1 else {}
            volumes[name]={'Name':name,'Driver':'local','Scope':'local','Mountpoint':t.docker_root+'/volumes/'+name+'/_data','Labels':{LABEL:t.task_id},'Options':options}
            return name
        if args[:2]==('volume','inspect'): return json.dumps([volumes[args[-1]]])
        if args[0]=='ps': return ''
        if args[0]=='create': return cid
        if args[:2]==('container','inspect') and '--format' not in args:
            name='hub-operator-scratch-keeper-'+t.task_id
            host={'NetworkMode':'none','ReadonlyRootfs':True,'CapDrop':['ALL'],'CapAdd':None,'Privileged':False,'SecurityOpt':['no-new-privileges'],'CgroupnsMode':'private','NanoCpus':100000000,'Memory':67108864,'MemorySwap':67108864,'PidsLimit':16,'LogConfig':{'Type':'local','Config':{'max-size':'1m','max-file':'2'}}}
            cfg={'Image':image,'Labels':{LABEL:'wrong' if wrong_label else t.task_id,'codex.owner':'inherited-image-label'},'User':'10001:10001','Entrypoint':['/usr/local/bin/python'],'Cmd':['-I','-S','-B','-c',KEEPER]}
            scratch=list(volumes)[1]; mount={'Name':scratch,'Source':volumes[scratch]['Mountpoint'],'Destination':'/fixture','Type':'volume','RW':True}
            return json.dumps([{'Id':cid,'Name':'/'+name,'Config':cfg,'HostConfig':host,'Mounts':[mount]}])
        if args[0]=='start': return cid
        if args[:2]==('container','inspect'): return json.dumps({'Status':'running','Running':True,'Pid':42,'Paused':False,'Restarting':False,'Dead':False})
        raise AssertionError(args)
    t.call=call
    return t,image,events,calls


def test_ordered_storage_effects_and_held_keeper():
    task,image,events,calls=fixture_task(); a,b,c=str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4())
    result=provision_storage(task,helper_image_id=image,source_task_id=a,scratch_task_id=b,journal_task_id=c)
    assert result.source_name=='hub-local-test-'+a and result.scratch_name=='hub-local-test-'+b
    assert result.scratch_keeper_id=='b'*64
    assert result.journal_name=='hub-local-test-'+c
    assert [e for e,_ in events]==['storage-enrollment','volume-create-intent','volume-created','volume-create-intent','volume-created','volume-create-intent','volume-created','keeper-create-intent','keeper-created','keeper-start-intent','keeper-started']
    assert not any(c[0] in ('rm','stop') for c in calls)
    before=len(calls)
    with pytest.raises(Held): provision_storage(task,helper_image_id=image,source_task_id=str(uuid.uuid4()),scratch_task_id=str(uuid.uuid4()),journal_task_id=str(uuid.uuid4()))
    assert len(calls)==before


def test_uncertain_create_is_held_without_cleanup():
    task,image,events,calls=fixture_task(('create',)); a,b,c=str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4())
    with pytest.raises(Held): provision_storage(task,helper_image_id=image,source_task_id=a,scratch_task_id=b,journal_task_id=c)
    assert events[-1][0]=='storage-held'
    assert not any(c[0] in ('rm','stop') for c in calls)


def test_alias_rejected_before_effect():
    task,image,events,calls=fixture_task(); a=str(uuid.uuid4())
    with pytest.raises(ValueError): provision_storage(task,helper_image_id=image,source_task_id=a,scratch_task_id=a,journal_task_id=str(uuid.uuid4()))
    assert not calls and not events

def test_wrong_keeper_owner_denied_before_start():
    task,image,events,calls=fixture_task(wrong_label=True); a,b,c=[str(uuid.uuid4()) for _ in range(3)]
    with pytest.raises(Held): provision_storage(task,helper_image_id=image,source_task_id=a,scratch_task_id=b,journal_task_id=c)
    assert not any(call[0]=='start' for call in calls)


def test_bounded_private_input_transport(monkeypatch):
    import subprocess,sys
    import scripts.hub_local_windows_storage as module
    task=object.__new__(WindowsTask); task._env={'PATH':'/usr/bin:/bin'}
    original=subprocess.Popen
    def spawn(args,**kwargs):
        return original([sys.executable,'-c','import sys; print(len(sys.stdin.buffer.read()))'],**kwargs)
    monkeypatch.setattr(module.subprocess,'Popen',spawn)
    assert task.call_input(['start','--attach','id'],b'secret',5)=='6'
    with pytest.raises(ValueError): task.call_input([],b'x',float('nan'))
    with pytest.raises(ValueError): task.call_input([],b'x'*(8*1024*1024+1),5)


def test_input_timeout_kills_only_owned_cli_and_returns_held(monkeypatch):
    import subprocess,sys,time
    import scripts.hub_local_windows_storage as module
    task=object.__new__(WindowsTask); task._env={'PATH':'/usr/bin:/bin'}
    original=subprocess.Popen; children=[]
    def spawn(args,**kwargs):
        p=original([sys.executable,'-c','import time; time.sleep(30)'],**kwargs); children.append(p); return p
    monkeypatch.setattr(module.subprocess,'Popen',spawn)
    started=time.monotonic()
    with pytest.raises(Held): task.call_input(['start','id'],b'x'*1024*1024,.2)
    assert time.monotonic()-started<6 and children[0].poll() is not None


def test_seed_and_operator_one_shot_identity_binding(monkeypatch):
    import scripts.hub_local_windows_initialization as init
    task,image,events,calls=fixture_task(); a,b,c=[str(uuid.uuid4()) for _ in range(3)]
    storage=provision_storage(task,helper_image_id=image,source_task_id=a,scratch_task_id=b,journal_task_id=c)
    source='sha256:'+'c'*64
    monkeypatch.setattr(init,'_helper',lambda task,storage,binding,mode: {'source_digest':source,'client_sha256':init.CLI,**({'archive_sha256':binding[3]} if mode=='seed' else {})})
    assert init.initialize(task,storage,script='script',script_sha256=init.SEED,archive='archive',archive_sha256='sha256:'+'b'*64,client='client',helper_image_id=image,daemon_paths=('/program','/archive','/client'))==source
    request={'source_root':storage.source_mount+'/checkout','source_digest':source,'private_dir':storage.scratch_mount,'journal_dir':storage.journal_mount,'daemon_id':task.daemon_id,'socket_group':0}
    task.claim_verified_operator(storage,request,image)
    task.recheck_verified_operator(storage,request,image)
    with pytest.raises(Held): task.claim_verified_operator(storage,request,image)
    with pytest.raises(Held): task.recheck_verified_operator(storage,{**request,'source_digest':'sha256:'+'d'*64},image)

def test_seed_bootstrap_is_valid_python():
    import hashlib
    from pathlib import Path
    from scripts.hub_local_windows_initialization import BOOT,SEED
    compile(BOOT,'<fixed-bootstrap>','exec')
    assert 'sha256:'+hashlib.sha256((Path(__file__).resolve().parents[1]/'scripts/hub_local_operator_seed.py').read_bytes()).hexdigest()==SEED

def test_seed_pin_is_exact_lf_archive_member():
    import hashlib,io,tarfile
    from pathlib import Path
    from scripts.hub_local_windows_initialization import SEED
    raw=(Path(__file__).resolve().parents[1]/'scripts/hub_local_operator_seed.py').read_bytes()
    lf=raw.replace(b'\r\n',b'\n')
    archive=io.BytesIO()
    with tarfile.open(fileobj=archive,mode='w') as tar:
        item=tarfile.TarInfo('scripts/hub_local_operator_seed.py'); item.size=len(lf); tar.addfile(item,io.BytesIO(lf))
    archive.seek(0)
    with tarfile.open(fileobj=archive,mode='r:') as tar: member=tar.extractfile('scripts/hub_local_operator_seed.py').read()
    assert b'\r' not in member and 'sha256:'+hashlib.sha256(member).hexdigest()==SEED
    assert 'sha256:'+hashlib.sha256(member.replace(b'\n',b'\r\n')).hexdigest()!=SEED

def test_desktop_metadata_must_match_fixed_bind_and_inherited_labels():
    from scripts.hub_local_windows_initialization import _helper_labels
    image={'codex.owner':'root','desktop.docker.io/mounts/0/Source':'C:\\old','desktop.docker.io/mounts/0/SourceKind':'hostFile','desktop.docker.io/mounts/0/Target':'/old'}
    current={**image,LABEL:'task','desktop.docker.io/mounts/0/Source':'C:\\new','desktop.docker.io/mounts/0/Target':'/seed/program'}
    assert _helper_labels(current,image,'task',{('C:\\new','/seed/program')})
    assert not _helper_labels({**current,'codex.owner':'wrong'},image,'task',{('C:\\new','/seed/program')})
    assert not _helper_labels(current,image,'task',{('C:\\other','/seed/program')})
    assert not _helper_labels({**current,'desktop.docker.io/arbitrary':'x'},image,'task',{('C:\\new','/seed/program')})

def test_desktop_strips_only_known_inherited_metadata():
    from scripts.hub_local_windows_storage import image_labels_match
    image={'codex.owner':'root','desktop.docker.io/mounts/0/Source':'C:\\old',
        'desktop.docker.io/mounts/0/SourceKind':'hostFile','desktop.docker.io/mounts/0/Target':'/old',
        'desktop.docker.io/ports.scheme':'v2'}
    actual={'codex.owner':'root',LABEL:'task'}
    assert image_labels_match(actual,image,'task',set())
    assert not image_labels_match({LABEL:'task'},image,'task',set())
    assert not image_labels_match({**actual,LABEL:'other'},image,'task',set())
    assert not image_labels_match({**actual,'desktop.docker.io/ports.scheme':'v3'},image,'task',set())
    assert not image_labels_match({**actual,'desktop.docker.io/mounts/0/Source':'C:\\new'},image,'task',set())

def test_input_file_size_limit(tmp_path):
    import hashlib
    from scripts.hub_local_windows_initialization import _file
    path=tmp_path/'input'; path.write_bytes(b'12345'); digest='sha256:'+hashlib.sha256(b'12345').hexdigest()
    assert _file(str(path),digest,5)
    with pytest.raises(ValueError): _file(str(path),digest,4)

def test_windows_named_and_handle_ctime_domains_remain_individually_stable():
    from types import SimpleNamespace
    from scripts.hub_local_windows_initialization import _stable_file_observation
    common=dict(st_dev=1,st_ino=2,st_mode=0o100644,st_nlink=1,st_size=5,st_mtime_ns=10,st_file_attributes=32)
    named=SimpleNamespace(**common,st_ctime_ns=11); handle=SimpleNamespace(**common,st_ctime_ns=12)
    assert _stable_file_observation(named,handle,handle,named)
    assert not _stable_file_observation(named,handle,SimpleNamespace(**common,st_ctime_ns=13),named)
    assert not _stable_file_observation(named,handle,handle,SimpleNamespace(**common,st_ctime_ns=13))
    assert not _stable_file_observation(named,handle,SimpleNamespace(**{**common,'st_ino':3},st_ctime_ns=12),named)

def test_unknown_verification_never_retries(monkeypatch):
    import scripts.hub_local_windows_initialization as init
    task,image,events,calls=fixture_task(); a,b,c=[str(uuid.uuid4()) for _ in range(3)]
    storage=provision_storage(task,helper_image_id=image,source_task_id=a,scratch_task_id=b,journal_task_id=c)
    attempts=[]
    def helper(task,storage,binding,mode):
        attempts.append(mode)
        if mode=='verify': raise OSError('unknown')
        return {'source_digest':'sha256:'+'c'*64}
    monkeypatch.setattr(init,'_helper',helper)
    kwargs=dict(script='script',script_sha256=init.SEED,archive='archive',archive_sha256='sha256:'+'b'*64,client='client',helper_image_id=image,daemon_paths=('/program','/archive','/client'))
    with pytest.raises(Held): init.initialize(task,storage,**kwargs)
    with pytest.raises(Held): init.initialize(task,storage,**kwargs)
    with pytest.raises(Held): init.remeasure(task,storage)
    assert attempts==['seed','verify']

def test_wrong_seed_pin_denied_before_helper_or_journal(monkeypatch):
    import scripts.hub_local_windows_initialization as init
    task,image,events,calls=fixture_task(); a,b,c=[str(uuid.uuid4()) for _ in range(3)]
    storage=provision_storage(task,helper_image_id=image,source_task_id=a,scratch_task_id=b,journal_task_id=c)
    before=(len(events),len(calls)); monkeypatch.setattr(init,'_helper',lambda *a:pytest.fail('helper called'))
    with pytest.raises(ValueError): init.initialize(task,storage,script='script',script_sha256='sha256:'+'0'*64,archive='archive',archive_sha256='sha256:'+'b'*64,client='client',helper_image_id=image,daemon_paths=('/program','/archive','/client'))
    assert (len(events),len(calls))==before and not task._seed_started

@pytest.mark.parametrize('drift',[False,True])
def test_fixed_helper_lifecycle_and_drift_denial(monkeypatch,drift):
    import scripts.hub_local_windows_initialization as init
    task,image,events,calls=fixture_task(); a,b,c=[str(uuid.uuid4()) for _ in range(3)]
    storage=provision_storage(task,helper_image_id=image,source_task_id=a,scratch_task_id=b,journal_task_id=c)
    monkeypatch.setattr(init,'_file',lambda path,*args:(1,{'C:\\probe\\program':1,'C:\\probe\\archive':2,'C:\\probe\\client':3}[path],3))
    monkeypatch.setattr(init,'_storage',lambda task,storage:None)
    records={}; tree='sha256:'+'c'*64
    def call(*args):
        if args[:2]==('image','inspect'):
            if 'Volumes' in args[-2]: return 'null'
            if 'Labels' in args[-2]: return json.dumps({'codex.owner':'inherited-image-label'})
            return image
        if args[:2]==('container','create'):
            cid=uuid.uuid4().hex*2; name=args[args.index('--name')+1]; user=args[args.index('--user')+1]; i=args.index('--entrypoint')
            cmd=list(args[i+3:]); mode=cmd[-4]; rw=mode=='seed'
            mounts=[]
            for n,x in enumerate(args):
                if x!='--mount': continue
                d=dict(part.split('=',1) if '=' in part else (part,True) for part in args[n+1].split(','))
                m={'Destination':d['dst'],'Type':d['type'],'RW':'readonly' not in d}
                if d['type']=='volume': m.update(Name=d['src'],Source=task.docker_root+'/volumes/'+d['src']+'/_data')
                else: m['Source']='C:\\wrong' if drift and d['dst']=='/seed/program' else d['src']
                mounts.append(m)
            cfg={'Image':image,'Labels':{'codex.owner':'inherited-image-label',LABEL:task.task_id},'User':user,'Entrypoint':['/opt/validation-venv/bin/python'],'Cmd':cmd}
            requested=[]
            for n,x in enumerate(args):
                if x=='--mount':
                    d=dict(part.split('=',1) if '=' in part else (part,True) for part in args[n+1].split(',')); spec={'Type':d['type'],'Target':d['dst'],'Source':d['src'],'ReadOnly':'readonly' in d}
                    if d['type']=='volume': spec['VolumeOptions']={'NoCopy':True}
                    requested.append(spec)
            host={'NetworkMode':'none','ReadonlyRootfs':True,'Privileged':False,'CapDrop':['ALL'],'CapAdd':['CHOWN'] if rw else None,'SecurityOpt':['no-new-privileges'],'CgroupnsMode':'private','NanoCpus':250000000,'Memory':268435456,'MemorySwap':268435456,'PidsLimit':32,'LogConfig':{'Type':'local','Config':{'max-size':'1m','max-file':'2'}},'Devices':None,'DeviceRequests':None,'VolumesFrom':None,'PortBindings':None,'AutoRemove':False,'RestartPolicy':{'Name':'no'},'GroupAdd':None,'PidMode':'','IpcMode':'private','Tmpfs':None,'Binds':None,'Mounts':requested}
            records[cid]={'Id':cid,'Name':'/'+name,'Config':cfg,'HostConfig':host,'State':{'Status':'created','Running':False,'Pid':0,'Paused':False,'Restarting':False,'Dead':False,'ExitCode':0},'Mounts':mounts}
            return cid
        if args[:2]==('container','inspect'): return json.dumps([records[args[-1]]])
        if args[:2]==('container','rm'): del records[args[-1]]; return args[-1]
        if args[:2]==('container','ls'): return '\n'.join(records)
        raise AssertionError(args)
    def call_input(args,data,timeout):
        cid=args[-1]; records[cid]['State']['Status']='exited'; cmd=records[cid]['Config']['Cmd']; mode=cmd[-4]
        return json.dumps({'source_digest':tree,'client_sha256':init.CLI,**({'archive_sha256':cmd[-3]} if mode=='seed' else {})})
    task.call=call; task.call_input=call_input
    binding=('C:\\probe\\program','sha256:'+'1'*64,'C:\\probe\\archive','sha256:'+'2'*64,'C:\\probe\\client',image,('C:\\probe\\program','C:\\probe\\archive','C:\\probe\\client'))
    if drift:
        with pytest.raises(ValueError): init._helper(task,storage,binding,'seed')
        assert not any(event=='seed-helper-start-intent' for event,_ in events)
        return
    assert init._helper(task,storage,binding,'seed')['source_digest']==tree
    assert init._helper(task,storage,binding,'verify')['source_digest']==tree
    assert not records
