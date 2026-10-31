"""One-shot fixed helper admission for the local Windows fixture only.

The parent and daemon are trusted and exclusively controlled. Unknown outcomes
are held. The journal is not a power-loss durability or host-custody proof.
"""
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from scripts.hub_local_windows_storage import WindowsTask,OperatorStorage,Held,LABEL,OPTIONS,KEEPER,image_labels_match

CLI='sha256:6f7a95d27f98fc8c5f6b220898988bf8f61dd9b040936797917cbdc72c6cabe6'
SEED='sha256:e6ea621f0c175f9678509272e266f055d22b457ba81f75c2d935fc56931bd7a7'
SHA=re.compile(r'sha256:[a-f0-9]{64}\Z')
BOOT=("import os,sys,stat,hashlib;f=os.open('/seed/program',os.O_RDONLY|os.O_NOFOLLOW);"
      "s=os.fstat(f);assert stat.S_ISREG(s.st_mode) and s.st_size<=131072;"
      "b=b''\nwhile len(b)<s.st_size:\n c=os.read(f,min(65536,s.st_size-len(b)));assert c;b+=c\n"
      "os.close(f);assert 'sha256:'+hashlib.sha256(b).hexdigest()==sys.argv[1];"
      "sys.argv=['/seed/program']+sys.argv[2:];exec(compile(b,'/seed/program','exec'),{'__name__':'__main__'})")


def _helper_labels(actual,image_labels,task_id,binds):
    """Allow only Desktop mount metadata tied to an inspected fixed bind."""
    return image_labels_match(actual,image_labels,task_id,binds)


def _stable_file_observation(named,before,after,final):
    # Windows named stat and fstat can report different ctime semantics.
    cross=lambda s:(s.st_dev,s.st_ino,s.st_mode,s.st_nlink,s.st_size,s.st_mtime_ns,getattr(s,'st_file_attributes',0))
    stable=lambda s:(cross(s),s.st_ctime_ns)
    return stable(before)==stable(after) and stable(named)==stable(final) and cross(named)==cross(before) and cross(final)==cross(after)


def _file(path,expected,maximum):
    if (type(path) is not str or not os.path.isabs(path) or ',' in path
        or any(ord(c)<32 or ord(c)==127 for c in path) or not SHA.fullmatch(expected)):
        raise ValueError('Invalid fixture input')
    named=os.stat(path,follow_symlinks=False)
    if not stat.S_ISREG(named.st_mode) or getattr(named,'st_file_attributes',0)&0x400 or named.st_nlink!=1 or named.st_size>maximum:
        raise ValueError('Invalid fixture input')
    flags=os.O_RDONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)|getattr(os,'O_NOINHERIT',0)
    with os.fdopen(os.open(path,flags),'rb') as f:
        before=os.fstat(f.fileno()); h=hashlib.sha256(); size=0
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>maximum or getattr(before,'st_file_attributes',0)&0x400: raise ValueError('Invalid fixture input')
        while b:=f.read(65536):
            size+=len(b)
            if size>before.st_size or size>maximum: raise ValueError('Input changed')
            h.update(b)
        after=os.fstat(f.fileno())
    final=os.stat(path,follow_symlinks=False)
    if size!=before.st_size or not _stable_file_observation(named,before,after,final) or 'sha256:'+h.hexdigest()!=expected: raise ValueError('Input changed')
    return (after.st_dev,after.st_ino,after.st_mode,after.st_nlink,after.st_size,after.st_mtime_ns,after.st_ctime_ns,getattr(after,'st_file_attributes',0))


def _storage(task,storage):
    if type(task) is not WindowsTask or type(storage) is not OperatorStorage or task._storage is not storage:
        raise ValueError('Storage not owned by task')
    task.inspect_daemon()
    for name,mount,opts in ((storage.source_name,storage.source_mount,{}),(storage.scratch_name,storage.scratch_mount,OPTIONS),(storage.journal_name,storage.journal_mount,{})):
        rows=json.loads(task.call('volume','inspect',name))
        if type(rows) is not list or len(rows)!=1: raise ValueError('Volume unavailable')
        v=rows[0]
        if (v.get('Name')!=name or v.get('Mountpoint')!=mount or mount!=task.docker_root+'/volumes/'+name+'/_data'
            or v.get('Driver')!='local' or v.get('Scope')!='local' or (v.get('Options') or {})!=opts or v.get('Labels')!={LABEL:task.task_id}): raise ValueError('Volume changed')
    c=json.loads(task.call('container','inspect',storage.scratch_keeper_id))[0]; s=c['State']; cfg=c['Config']; h=c['HostConfig']; mounts=c['Mounts']
    image=json.loads(task.call('image','inspect',storage.scratch_keeper_image))
    if len(image)!=1 or image[0].get('Id')!=storage.scratch_keeper_image: raise ValueError('Keeper image changed')
    labels=image[0].get('Config',{}).get('Labels') or {}
    if type(labels) is not dict or LABEL in labels: raise ValueError('Keeper image labels invalid')
    if (c.get('Id')!=storage.scratch_keeper_id or c.get('Name')!='/hub-operator-scratch-keeper-'+task.task_id
        or cfg.get('Image')!=storage.scratch_keeper_image or not image_labels_match(cfg.get('Labels'),labels,task.task_id,set())
        or cfg.get('User')!='10001:10001' or cfg.get('Entrypoint')!=['/usr/local/bin/python'] or cfg.get('Cmd')!=['-I','-S','-B','-c',KEEPER]
        or s.get('Status')!='running' or s.get('Running') is not True or type(s.get('Pid')) is not int or s['Pid']<=0
        or any(s.get(k) is not False for k in ('Paused','Restarting','Dead'))
        or h.get('NetworkMode')!='none' or h.get('ReadonlyRootfs') is not True or h.get('Privileged') is not False or h.get('CapDrop')!=['ALL']
        or h.get('CapAdd') not in (None,[]) or h.get('SecurityOpt')!=['no-new-privileges'] or h.get('CgroupnsMode')!='private'
        or h.get('NanoCpus')!=100000000 or h.get('Memory')!=67108864 or h.get('MemorySwap')!=67108864 or h.get('PidsLimit')!=16
        or h.get('LogConfig')!={'Type':'local','Config':{'max-size':'1m','max-file':'2'}}
        or h.get('GroupAdd') not in (None,[]) or h.get('Devices') not in (None,[]) or h.get('DeviceRequests') not in (None,[])
        or h.get('VolumesFrom') not in (None,[]) or h.get('PortBindings') not in (None,{}) or h.get('Binds') not in (None,[])
        or h.get('PidMode') not in ('','private') or h.get('IpcMode') not in ('','private') or h.get('RestartPolicy',{}).get('Name') not in ('','no')
        or len(mounts)!=1 or mounts[0].get('Type')!='volume' or mounts[0].get('Name')!=storage.scratch_name
        or mounts[0].get('Source')!=storage.scratch_mount or mounts[0].get('Destination')!='/fixture' or mounts[0].get('RW') is not True): raise ValueError('Keeper changed')


def _helper(task,storage,binding,mode):
    script,script_sha,archive,archive_sha,client,helper_image,daemon_paths=binding
    identities=[_file(script,script_sha,131072),_file(archive,archive_sha,512*1024*1024),_file(client,CLI,128*1024*1024)]
    if len({(x[0],x[1]) for x in identities})!=3: raise ValueError('Aliased fixture inputs')
    _storage(task,storage)
    if (task.call('image','inspect','--format','{{.Id}}',helper_image)!=helper_image
        or json.loads(task.call('image','inspect','--format','{{json (index .Config "Volumes")}}',helper_image)) not in (None,{})): raise ValueError('Helper image unavailable')
    labels=json.loads(task.call('image','inspect','--format','{{json (index .Config "Labels")}}',helper_image)) or {}
    if type(labels) is not dict or LABEL in labels: raise ValueError('Helper image labels invalid')
    name='hub-local-seed-'+task.task_id+'-'+uuid.uuid4().hex[:12]
    cmd=['-I','-B','-c',BOOT,script_sha,mode,archive_sha,CLI,'120']
    rw=mode=='seed'; user='0:0' if rw else '10001:10001'; caps=['--cap-add','CHOWN'] if rw else []
    mounts=[('volume',storage.source_name,'/seed/source',rw),('volume',storage.scratch_name,'/seed/scratch',rw),('volume',storage.journal_name,'/seed/journal',rw),('bind',script,'/seed/program',False),('bind',archive,'/seed/archive',False),('bind',client,'/seed/client',False)]
    args=[]
    for kind,src,dst,writable in mounts:
        args+=['--mount','type='+kind+',src='+src+',dst='+dst+(',readonly' if not writable else '')+(',volume-nocopy' if kind=='volume' else '')]
    task.inspect_daemon(); task.record('seed-helper-create-intent',name=name,mode=mode)
    cid=task.call('container','create','--pull=never','--name',name,'--label',LABEL+'='+task.task_id,'--user',user,
        '--network','none','--read-only','--cap-drop','ALL',*caps,'--security-opt','no-new-privileges','--cgroupns','private',
        '--cpus','0.25','--memory','256m','--memory-swap','256m','--pids-limit','32','--log-driver','local',
        '--log-opt','max-size=1m','--log-opt','max-file=2',*args,'--entrypoint','/opt/validation-venv/bin/python',helper_image,*cmd)
    if not re.fullmatch(r'[a-f0-9]{64}',cid): raise Held('Helper create unknown')
    task.record('seed-helper-created',id=cid,mode=mode)
    def inspect(expected):
        rows=json.loads(task.call('container','inspect',cid))
        if len(rows)!=1: raise ValueError('Helper unavailable')
        c=rows[0]; cfg=c['Config']; h=c['HostConfig']; s=c['State']; actual=c['Mounts']
        if (c['Id']!=cid or c['Name']!='/'+name or cfg['Image']!=helper_image or not _helper_labels(cfg.get('Labels'),labels,task.task_id,set(zip(daemon_paths,('/seed/program','/seed/archive','/seed/client'))))
            or cfg['User']!=user or cfg['Entrypoint']!=['/opt/validation-venv/bin/python'] or cfg['Cmd']!=cmd
            or h['NetworkMode']!='none' or h['ReadonlyRootfs'] is not True or h['Privileged'] is not False or h['CapDrop']!=['ALL']
            or h.get('CapAdd')!=(['CHOWN'] if rw else None) or h['SecurityOpt']!=['no-new-privileges'] or h.get('CgroupnsMode')!='private'
            or h['NanoCpus']!=250000000 or h['Memory']!=268435456 or h['MemorySwap']!=268435456 or h['PidsLimit']!=32
            or h['LogConfig']!={'Type':'local','Config':{'max-size':'1m','max-file':'2'}}
            or h.get('Devices') not in (None,[]) or h.get('DeviceRequests') not in (None,[]) or h.get('VolumesFrom') not in (None,[])
            or h.get('PortBindings') not in (None,{}) or h.get('AutoRemove') is not False or h.get('RestartPolicy',{}).get('Name') not in ('','no')
            or h.get('GroupAdd') not in (None,[]) or h.get('PidMode') not in ('','private') or h.get('IpcMode') not in ('','private') or h.get('Tmpfs') not in (None,{}) or h.get('Binds') not in (None,[])
            or s.get('Status')!=expected or s.get('Running') is not False or type(s.get('Pid')) is not int or s.get('Pid')!=0 or any(s.get(k) is not False for k in ('Paused','Restarting','Dead'))
            or len(actual)!=len(mounts)): raise ValueError('Helper profile mismatch')
        requested=h.get('Mounts')
        if type(requested) is not list or len(requested)!=len(mounts): raise ValueError('Helper requested mounts mismatch')
        for kind,src,dst,writable in mounts:
            found=[x for x in actual if x.get('Destination')==dst and x.get('Type')==kind and x.get('RW') is writable]
            if len(found)!=1 or (kind=='volume' and (found[0].get('Name')!=src or found[0].get('Source')!=task.docker_root+'/volumes/'+src+'/_data')): raise ValueError('Helper mounts mismatch')
            if kind=='bind' and found[0].get('Source')!=daemon_paths[('/seed/program','/seed/archive','/seed/client').index(dst)]: raise ValueError('Helper bind source mismatch')
            spec=[x for x in requested if x.get('Type')==kind and x.get('Target')==dst and x.get('ReadOnly') is (not writable)]
            if len(spec)!=1 or (kind=='volume' and (spec[0].get('Source')!=src or spec[0].get('VolumeOptions',{}).get('NoCopy') is not True)):
                raise ValueError('Helper requested mounts mismatch')
            if kind=='bind' and spec[0].get('Source')!=daemon_paths[('/seed/program','/seed/archive','/seed/client').index(dst)]: raise ValueError('Helper requested bind mismatch')
        if expected=='exited' and (type(s.get('ExitCode')) is not int or s.get('ExitCode')!=0): raise ValueError('Helper failed')
    inspect('created'); _storage(task,storage)
    task.record('seed-helper-start-intent',id=cid,mode=mode)
    raw=task.call_input(['container','start','--attach','--interactive',cid],b'',150)
    task.record('seed-helper-start-returned',id=cid,mode=mode); inspect('exited')
    result=json.loads(raw)
    if type(result) is not dict or result.get('client_sha256')!=CLI or not SHA.fullmatch(result.get('source_digest','')): raise ValueError('Helper output invalid')
    if mode=='seed' and (set(result)!={'archive_sha256','client_sha256','source_digest'} or result['archive_sha256']!=archive_sha): raise ValueError('Seed output invalid')
    if mode=='verify' and set(result)!={'client_sha256','source_digest'}: raise ValueError('Verify output invalid')
    if identities!=[_file(script,script_sha,131072),_file(archive,archive_sha,512*1024*1024),_file(client,CLI,128*1024*1024)]: raise ValueError('Input changed')
    _storage(task,storage); inspect('exited'); task.record('seed-helper-remove-intent',id=cid,mode=mode)
    if task.call('container','rm',cid)!=cid: raise ValueError('Helper removal unknown')
    if cid in task.call('container','ls','--all','--quiet','--no-trunc').splitlines(): raise ValueError('Helper remains')
    task.record('seed-helper-removed',id=cid,mode=mode)
    return result


def validate_daemon_paths(daemon_paths):
    def valid_daemon_path(p):
        if type(p) is not str or any(ord(c)<32 or ord(c)==127 for c in p): return False
        posix=PurePosixPath(p); windows=PureWindowsPath(p)
        return ((p.startswith('/') and not p.startswith('//') and str(posix)==p and '..' not in posix.parts)
            or (windows.is_absolute() and not p.startswith('\\\\') and str(windows)==p and '..' not in windows.parts))
    if (type(daemon_paths) is not tuple or len(daemon_paths)!=3 or not all(valid_daemon_path(p) for p in daemon_paths)
        or len({p.casefold() if PureWindowsPath(p).is_absolute() else p for p in daemon_paths})!=3):
        raise ValueError('Exact daemon input paths required')


def initialize(task,storage,*,script,script_sha256,archive,archive_sha256,client,helper_image_id,daemon_paths):
    validate_daemon_paths(daemon_paths)
    if task._storage is not storage: raise Held('Seed unavailable')
    if not SHA.fullmatch(helper_image_id) or script_sha256!=SEED: raise ValueError('Invalid fixed helper')
    binding=(script,script_sha256,archive,archive_sha256,client,helper_image_id,daemon_paths)
    with task._seed_lock:
        if task._seed_started: raise Held('Seed already attempted')
        task._seed_started=True; task._seed_failed=True
        return _initialize_locked(task,storage,binding,script_sha256,archive_sha256,helper_image_id)

def _initialize_locked(task,storage,binding,script_sha256,archive_sha256,helper_image_id):
    try:
        task.record('seed-enrollment',script_sha256=script_sha256,archive_sha256=archive_sha256,client_sha256=CLI,helper_image_id=helper_image_id)
        seeded=_helper(task,storage,binding,'seed'); observed=_helper(task,storage,binding,'verify')
        if seeded['source_digest']!=observed['source_digest']: raise ValueError('Tree changed')
        task.record('seed-verified',source_digest=observed['source_digest'])
        task._seed_binding=(storage,binding,observed['source_digest'])
        task._seed_failed=False
        return observed['source_digest']
    except Exception:
        raise Held('Seed outcome unknown; preserve helper and storage') from None


def remeasure(task,storage):
    with task._seed_lock:
        evidence=getattr(task,'_seed_binding',None)
        if task._cleanup_started or task._seed_failed or evidence is None or evidence[0] is not storage: raise Held('Verified seed unavailable')
        task._seed_failed=True
        try:
            result=_helper(task,storage,evidence[1],'verify')
            if result['source_digest']!=evidence[2]: raise ValueError('Tree changed')
            task._seed_failed=False
            return evidence[2]
        except Exception:
            raise Held('Seed remeasurement unknown; preserve resources') from None
