"""Post-exit cleanup for a trusted, task-owned local Docker fixture only.

The coordinator and daemon/operator are trusted and exclusive. The HMAC binds
one child receipt to its parent; it is not a host-wide custody certificate.
"""
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path, PurePosixPath
_ROOT=Path(__file__).resolve().parent.parent
sys.path[:0]=[str(_ROOT/'src'),str(_ROOT)]
from hub_container_executor.source_tree import source_tree_digest
from scripts.hub_local_test_volume import OwnedVolume, VolumeError, VolumeOutcomeUnknown, _call, _reserve_intent, _check_path, _close_handles, _options_match, _install_attested_ancestor_fds, TMPFS_OPTIONS, LABEL

DOMAIN=b'engineering-hub.local-cleanup-receipt.v1\0'
SCHEMA='engineering-hub.local-cleanup-receipt.v1'
PREFIX=['/usr/bin/docker','--host','unix:///var/run/docker.sock']
FIELDS={'schema','task_id','daemon_id','docker_root','volume_name','volume_mount','coordinator_id','coordinator_image_id','volume_storage','volume_options','initial_manifest_digest','baseline_backup_digest','manifest_digest','nonce','containers'}

def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')

def read_cleanup_receipt(*, directory_fd, directory_identity, key, expected, initial_manifest):
    """Read and authenticate the fixed receipt from the retained directory FD."""
    try:
        if (type(key) is not bytes or len(key)!=32 or type(expected) is not dict
            or set(expected)!=FIELDS-{'schema','containers','baseline_backup_digest','manifest_digest'}): raise ValueError
        directory=os.fstat(directory_fd)
        if (not stat.S_ISDIR(directory.st_mode) or (directory.st_dev,directory.st_ino)!=directory_identity
            or directory.st_uid!=10001 or stat.S_IMODE(directory.st_mode)!=0o700): raise ValueError
        fd=os.open('cleanup-receipt.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory_fd)
        try:
            before=os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid!=10001 or before.st_nlink!=1
                or stat.S_IMODE(before.st_mode)!=0o600 or not 1<=before.st_size<=16384): raise ValueError
            raw=os.read(fd,16385); after=os.fstat(fd)
            named=os.stat('cleanup-receipt.json',dir_fd=directory_fd,follow_symlinks=False)
            if len(raw)!=before.st_size or (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns,before.st_uid,before.st_mode,before.st_nlink)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_uid,after.st_mode,after.st_nlink) or (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns,before.st_uid,before.st_mode,before.st_nlink)!=(named.st_dev,named.st_ino,named.st_size,named.st_mtime_ns,named.st_ctime_ns,named.st_uid,named.st_mode,named.st_nlink): raise ValueError
        finally: os.close(fd)
        def pairs(items):
            result={}
            for k,v in items:
                if k in result: raise ValueError
                result[k]=v
            return result
        envelope=json.loads(raw,object_pairs_hook=pairs)
        if type(envelope) is not dict or set(envelope)!={'payload','signature'} or _canonical(envelope)!=raw: raise ValueError
        payload=envelope['payload']; signature=envelope['signature']
        if (type(payload) is not dict or set(payload)!=FIELDS or payload['schema']!=SCHEMA
            or any(type(payload[k]) is not type(expected[k]) or payload[k]!=expected[k] for k in expected)
            or type(signature) is not str or not re.fullmatch('[a-f0-9]{64}',signature)
            or not hmac.compare_digest(signature,hmac.new(key,DOMAIN+_canonical(payload),hashlib.sha256).hexdigest())): raise ValueError
        from engineering_hub.container_pilot_manifest import PilotManifest
        from scripts.hub_local_baseline_bootstrap import _final_manifest
        if (type(initial_manifest) is not PilotManifest
            or payload['initial_manifest_digest'] != initial_manifest.digest()
            or type(payload['baseline_backup_digest']) is not str
            or not re.fullmatch('sha256:[a-f0-9]{64}',payload['baseline_backup_digest'])
            or payload['manifest_digest'] != _final_manifest(initial_manifest,payload['baseline_backup_digest']).digest()):
            raise ValueError
        rows=payload['containers']
        if (type(rows) is not list or len(rows)>3 or rows!=sorted(rows)
            or any(type(r) is not list or len(r)!=2 or type(r[0]) is not str
                   or not re.fullmatch('[a-f0-9]{64}',r[0]) or type(r[1]) is not str
                   or not re.fullmatch(r'hub-pilot-[a-f0-9]{24}-[a-f0-9]{8}-[a-f0-9]{3}-(?:baseline|candidate|rollback)',r[1]) for r in rows)
            or len({r[0] for r in rows})!=len(rows)): raise ValueError
        return tuple((x[0],x[1]) for x in rows)
    except Exception:
        raise VolumeError('Cleanup receipt unavailable or invalid') from None


def remove_after_coordinator_exit(*, volume, coordinator_id, coordinator_image_id, initial_manifest,
                                  nonce, key, receipt_directory_fd, receipt_directory_identity,
                                  journal_dir, run=_call):
    """Make one volume removal after an independently verified fixture exit.

    The trusted launcher must first verify exact coordinator image/profile/label,
    clean exit and remove that exact container. This function checks absence only;
    receipt authentication is not independent proof of that prior verification.
    """
    if (type(volume) is not OwnedVolume or type(volume.owner) is not str
        or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',volume.owner)
        or volume.name != 'hub-local-test-'+volume.owner
        or not re.fullmatch(r'[A-Za-z0-9:._-]{12,128}',volume.daemon_id)
        or not re.fullmatch('[a-f0-9]{64}',coordinator_id)
        or not re.fullmatch('sha256:[a-f0-9]{64}',coordinator_image_id)
        or not re.fullmatch('[a-f0-9]{64}',nonce)):
        raise VolumeError('Invalid cleanup enrollment')
    def metadata():
        if run(PREFIX+['info','--format','{{.ID}}']).strip()!=volume.daemon_id: raise ValueError
        root=run(PREFIX+['info','--format','{{.DockerRootDir}}']).strip()
        if (not root.startswith('/') or root.startswith('//') or str(PurePosixPath(root))!=root
            or '..' in PurePosixPath(root).parts or any(ord(c)<32 or ord(c)==127 for c in root)
            or volume.mountpoint!=root+'/volumes/'+volume.name+'/_data'): raise ValueError
        rows=json.loads(run(PREFIX+['volume','inspect',volume.name]))
        if type(rows) is not list or len(rows)!=1 or type(rows[0]) is not dict: raise ValueError
        v=rows[0]
        if (v.get('Name')!=volume.name or v.get('Driver')!='local' or v.get('Scope')!='local'
            or v.get('Mountpoint')!=volume.mountpoint or not _options_match(volume,v.get('Options'))
            or v.get('Labels')!={LABEL:volume.owner}): raise ValueError
        return root
    def absent(ids):
        rows=run(PREFIX+['ps','--all','--quiet','--no-trunc']).splitlines()
        if any(not re.fullmatch('[a-f0-9]{64}',x) for x in rows) or any(x in rows for x in ids): raise ValueError
    try:
        root=metadata()
        expected={'task_id':volume.owner,'daemon_id':volume.daemon_id,'docker_root':root,
                  'volume_name':volume.name,'volume_mount':volume.mountpoint,'coordinator_id':coordinator_id,
                  'coordinator_image_id':coordinator_image_id,'volume_storage': 'tmpfs-512m-v1' if volume.options==TMPFS_OPTIONS else 'local-v1',
                  'volume_options':dict(volume.options),'initial_manifest_digest':initial_manifest.digest(),'nonce':nonce}
        rows=read_cleanup_receipt(directory_fd=receipt_directory_fd,directory_identity=receipt_directory_identity,key=key,expected=expected,initial_manifest=initial_manifest)
        absent([coordinator_id]+[i for i,_ in rows])
    except Exception: raise VolumeError('Cannot establish post-exit cleanup') from None
    handles=_reserve_intent(journal_dir,volume.owner,volume.daemon_id,volume.name,'.postexit-delete','postexit-delete')
    try:
        _check_path(handles)
        if metadata()!=root: raise ValueError
        if read_cleanup_receipt(directory_fd=receipt_directory_fd,directory_identity=receipt_directory_identity,key=key,expected=expected,initial_manifest=initial_manifest)!=rows: raise ValueError
        absent([coordinator_id]+[i for i,_ in rows]); _check_path(handles)
        if run(PREFIX+['volume','rm',volume.name]).strip()!=volume.name: raise ValueError
        if run(PREFIX+['info','--format','{{.ID}}']).strip()!=volume.daemon_id: raise ValueError
        if volume.name in run(PREFIX+['volume','ls','--format','{{.Name}}']).splitlines(): raise ValueError
    except Exception: raise VolumeOutcomeUnknown('Volume deletion unknown; do not retry') from None
    finally:
        try: _close_handles(handles)
        except OSError: raise VolumeOutcomeUnknown('Volume deletion unknown; journal cleanup failed') from None


# Tmpfs volumes lose their contents when their last container mount disappears.
# This keeper is a bounded, owned local-test process; no restart or adoption.
from dataclasses import dataclass
from scripts.hub_local_test_volume import TMPFS_OPTIONS

@dataclass(frozen=True)
class TmpfsKeeper:
    container_id: str
    image_id: str
    volume: OwnedVolume

_KEEPER_SCRIPT='import time\nwhile True: time.sleep(3600)'

def _keeper_volume(volume, run):
    if (type(volume) is not OwnedVolume or volume.options != TMPFS_OPTIONS
        or type(volume.owner) is not str or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',volume.owner)
        or volume.name!='hub-local-test-'+volume.owner): raise ValueError
    if run(PREFIX+['info','--format','{{.ID}}']).strip()!=volume.daemon_id: raise ValueError
    root=run(PREFIX+['info','--format','{{.DockerRootDir}}']).strip()
    if (not root.startswith('/') or root.startswith('//') or str(PurePosixPath(root))!=root
        or '..' in PurePosixPath(root).parts or any(ord(c)<32 or ord(c)==127 for c in root)
        or volume.mountpoint!=root+'/volumes/'+volume.name+'/_data'): raise ValueError
    rows=json.loads(run(PREFIX+['volume','inspect',volume.name]))
    if type(rows) is not list or len(rows)!=1 or type(rows[0]) is not dict: raise ValueError
    v=rows[0]
    if (v.get('Name')!=volume.name or v.get('Driver')!='local' or v.get('Scope')!='local'
        or v.get('Mountpoint')!=volume.mountpoint or not _options_match(volume,v.get('Options'))
        or v.get('Labels')!={LABEL:volume.owner}): raise ValueError


def check_tmpfs_keeper(keeper, *, volume, run=_call, state='running'):
    """Check exact owned keeper; a live check does not guarantee future liveness."""
    if (type(keeper) is not TmpfsKeeper or keeper.volume!=volume
        or not re.fullmatch('[0-9a-f]{64}',keeper.container_id)
        or not re.fullmatch('sha256:[0-9a-f]{64}',keeper.image_id)): raise ValueError
    _keeper_volume(volume,run)
    identity=run(PREFIX+['inspect','--format','{{.Id}}|{{.Name}}|{{.Config.Image}}|{{index .Config.Labels "'+LABEL+'"}}|{{.State.Status}}',keeper.container_id]).strip().split('|')
    if identity != [keeper.container_id,'/hub-local-keeper-'+volume.owner,keeper.image_id,volume.owner,state]: raise ValueError
    actual=json.loads(run(PREFIX+['inspect','--format','{{json .State}}',keeper.container_id]))
    if (type(actual) is not dict or actual.get('Status')!=state or type(actual.get('Pid')) is not int
        or actual.get('Running') is not (state=='running')
        or (actual['Pid']<=0 if state=='running' else actual['Pid']!=0)
        or any(actual.get(k) is not False for k in ('Paused','Restarting','Dead'))): raise ValueError
    for field, expected in (('User','10001:10001'),('Entrypoint',['/usr/local/bin/python']),
                            ('Cmd',['-I','-B','-c',_KEEPER_SCRIPT]),('Volumes',None)):
        if json.loads(run(PREFIX+['inspect','--format','{{json .Config.'+field+'}}',keeper.container_id]))!=expected: raise ValueError
    config=json.loads(run(PREFIX+['inspect','--format','{{json .HostConfig}}',keeper.container_id]))
    if (type(config) is not dict or config.get('NetworkMode')!='none' or config.get('ReadonlyRootfs') is not True
        or config.get('Privileged') is not False or config.get('CapDrop')!=['ALL'] or config.get('CapAdd') not in (None,[])
        or config.get('SecurityOpt')!=['no-new-privileges']
        or config.get('LogConfig')!={'Type':'local','Config':{'max-size':'1m','max-file':'2'}}
        or config.get('Memory')!=67108864
        or config.get('MemorySwap')!=67108864 or config.get('NanoCpus')!=100000000 or config.get('PidsLimit')!=16
        or config.get('Binds') not in (None,[]) or config.get('Devices') not in (None,[])
        or config.get('DeviceRequests') not in (None,[]) or config.get('VolumesFrom') not in (None,[])
        or config.get('PidMode') not in ('','private') or config.get('IpcMode') not in ('','private')
        or config.get('AutoRemove') is not False or config.get('PublishAllPorts') is not False
        or config.get('PortBindings') not in (None,{}) or config.get('RestartPolicy',{}).get('Name') not in ('','no')): raise ValueError
    requested=config.get('Mounts')
    if (type(requested) is not list or len(requested)!=1 or requested[0].get('Type')!='volume'
        or requested[0].get('Source')!=volume.name or requested[0].get('Target')!='/fixture'
        or requested[0].get('VolumeOptions',{}).get('NoCopy') is not True): raise ValueError
    mounts=json.loads(run(PREFIX+['inspect','--format','{{json .Mounts}}',keeper.container_id]))
    if (type(mounts) is not list or len(mounts)!=1 or mounts[0].get('Type')!='volume'
        or mounts[0].get('Name')!=volume.name or mounts[0].get('Source')!=volume.mountpoint
        or mounts[0].get('Destination')!='/fixture' or mounts[0].get('Driver')!='local'
        or mounts[0].get('RW') is not True): raise ValueError


def start_tmpfs_keeper(*, volume, helper_image_id, journal_dir, run=_call):
    """Start one bounded owned keeper before initializer; uncertain means held."""
    try:
        _keeper_volume(volume,run)
        if (not re.fullmatch('sha256:[0-9a-f]{64}',helper_image_id)
            or run(PREFIX+['image','inspect','--format','{{.Id}}',helper_image_id]).strip()!=helper_image_id
            or json.loads(run(PREFIX+['image','inspect','--format','{{json (index .Config "Volumes")}}',helper_image_id])) not in (None,{})
            or 'hub-local-keeper-'+volume.owner in run(PREFIX+['ps','--all','--format','{{.Names}}']).splitlines()): raise ValueError
    except Exception: raise VolumeError('Keeper enrollment unavailable') from None
    handles=_reserve_intent(journal_dir,volume.owner,volume.daemon_id,volume.name,'.keeper-create','keeper-create')
    try:
        _check_path(handles); _keeper_volume(volume,run); _check_path(handles)
        ident=run(PREFIX+['create','--pull=never','--name','hub-local-keeper-'+volume.owner,
            '--label',LABEL+'='+volume.owner,'--network','none','--read-only','--cpus','0.1',
            '--memory','64m','--memory-swap','64m','--pids-limit','16','--log-driver','local','--log-opt','max-size=1m','--log-opt','max-file=2','--user','10001:10001',
            '--cap-drop','ALL','--security-opt','no-new-privileges',
            '--mount','type=volume,src='+volume.name+',dst=/fixture,volume-nocopy',
            '--entrypoint','/usr/local/bin/python',helper_image_id,'-I','-B','-c',_KEEPER_SCRIPT]).strip()
        if not re.fullmatch('[0-9a-f]{64}',ident): raise ValueError
    except Exception: raise VolumeOutcomeUnknown('Keeper creation unknown; preserve') from None
    finally:
        try: _close_handles(handles)
        except OSError: raise VolumeOutcomeUnknown('Keeper creation cleanup unknown; preserve') from None
    keeper=TmpfsKeeper(ident,helper_image_id,volume)
    try: check_tmpfs_keeper(keeper,volume=volume,run=run,state='created')
    except Exception: raise VolumeOutcomeUnknown('Keeper identity unverified; preserve') from None
    handles=_reserve_intent(journal_dir,volume.owner,volume.daemon_id,volume.name,'.keeper-start','keeper-start')
    try:
        _check_path(handles); check_tmpfs_keeper(keeper,volume=volume,run=run,state='created'); _check_path(handles)
        if run(PREFIX+['start',ident]).strip()!=ident: raise ValueError
        check_tmpfs_keeper(keeper,volume=volume,run=run)
    except Exception: raise VolumeOutcomeUnknown('Keeper start unknown; preserve') from None
    finally:
        try: _close_handles(handles)
        except OSError: raise VolumeOutcomeUnknown('Keeper start cleanup unknown; preserve') from None
    return keeper


def stop_tmpfs_keeper(*, keeper, initial_manifest, coordinator_id, coordinator_image_id,
                      nonce, key, receipt_directory_fd, receipt_directory_identity, journal_dir, run=_call):
    """Stop/remove the keeper only after authenticated coordinator exit evidence.

    The trusted launcher must independently verify exit-zero and remove the
    exact coordinator first. Every uncertainty retains all remaining resources.
    """
    try:
        if type(keeper) is not TmpfsKeeper: raise ValueError
        volume=keeper.volume
        check_tmpfs_keeper(keeper,volume=volume,run=run)
        root=run(PREFIX+['info','--format','{{.DockerRootDir}}']).strip()
        expected={'task_id':volume.owner,'daemon_id':volume.daemon_id,'docker_root':root,
            'volume_name':volume.name,'volume_mount':volume.mountpoint,'coordinator_id':coordinator_id,
            'coordinator_image_id':coordinator_image_id,'volume_storage':'tmpfs-512m-v1',
            'volume_options':dict(volume.options),'initial_manifest_digest':initial_manifest.digest(),'nonce':nonce}
        def authorize():
            rows=read_cleanup_receipt(directory_fd=receipt_directory_fd,directory_identity=receipt_directory_identity,
                                      key=key,expected=expected,initial_manifest=initial_manifest)
            ids=run(PREFIX+['ps','--all','--quiet','--no-trunc']).splitlines()
            if (any(not re.fullmatch('[a-f0-9]{64}',x) for x in ids)
                or coordinator_id in ids or any(ident in ids for ident,_ in rows)): raise ValueError
            return rows
        original=authorize()
    except Exception: raise VolumeError('Keeper stop authorization unavailable') from None
    handles=_reserve_intent(journal_dir,volume.owner,volume.daemon_id,volume.name,'.keeper-stop','keeper-stop')
    try:
        _check_path(handles); check_tmpfs_keeper(keeper,volume=volume,run=run)
        if authorize()!=original: raise ValueError
        _check_path(handles)
        if run(PREFIX+['stop','--time','5',keeper.container_id]).strip()!=keeper.container_id: raise ValueError
        check_tmpfs_keeper(keeper,volume=volume,run=run,state='exited')
    except Exception: raise VolumeOutcomeUnknown('Keeper stop unknown; preserve') from None
    finally:
        try: _close_handles(handles)
        except OSError: raise VolumeOutcomeUnknown('Keeper stop cleanup unknown; preserve') from None
    handles=_reserve_intent(journal_dir,volume.owner,volume.daemon_id,volume.name,'.keeper-remove','keeper-remove')
    try:
        _check_path(handles); check_tmpfs_keeper(keeper,volume=volume,run=run,state='exited')
        if authorize()!=original: raise ValueError
        _check_path(handles)
        if run(PREFIX+['rm',keeper.container_id]).strip()!=keeper.container_id: raise ValueError
        ids=run(PREFIX+['ps','--all','--quiet','--no-trunc']).splitlines()
        if any(not re.fullmatch('[0-9a-f]{64}',x) for x in ids) or keeper.container_id in ids: raise ValueError
    except Exception: raise VolumeOutcomeUnknown('Keeper removal unknown; preserve') from None
    finally:
        try: _close_handles(handles)
        except OSError: raise VolumeOutcomeUnknown('Keeper removal cleanup unknown; preserve') from None

def _call_with_input(args, data, deadline):
    """Fixed launcher transport; kill only owned CLI on timeout, never container."""
    import selectors,subprocess,time,sys
    if type(data) is not bytes or len(data)>8*1024*1024: raise ValueError('request bound')
    p=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent'})
    selector=None; buffers={p.stdout:bytearray(),p.stderr:bytearray()}; offset=0
    try:
        selector=selectors.DefaultSelector()
        for stream in (p.stdin,p.stdout,p.stderr): os.set_blocking(stream.fileno(),False)
        selector.register(p.stdin,selectors.EVENT_WRITE)
        selector.register(p.stdout,selectors.EVENT_READ); selector.register(p.stderr,selectors.EVENT_READ)
        while selector.get_map():
            remaining=deadline-time.monotonic()
            if remaining<=0: raise TimeoutError
            for entry,_ in selector.select(remaining):
                if entry.fileobj is p.stdin:
                    if offset==len(data):
                        selector.unregister(p.stdin); p.stdin.close(); continue
                    n=os.write(p.stdin.fileno(),data[offset:offset+65536]); offset+=n
                else:
                    chunk=os.read(entry.fd,4096)
                    if not chunk: selector.unregister(entry.fileobj)
                    else:
                        target=buffers[entry.fileobj]; target.extend(chunk)
                        if len(target)>65536: raise ValueError('CLI output exceeds bound')
        remaining=deadline-time.monotonic()
        if remaining<=0 or p.wait(timeout=remaining)!=0: raise ValueError('CLI failed')
    finally:
        try:
            if selector is not None: selector.close()
        finally:
            try:
                if p.poll() is None: p.kill()
                p.wait()
            finally:
                failed = sys.exc_info()[0] is not None
                close_error = False
                for stream in (p.stdin,p.stdout,p.stderr):
                    try:
                        if not stream.closed: stream.close()
                    except OSError:
                        close_error = True
                if close_error and not failed:
                    raise OSError('CLI stream cleanup failed') from None

_COORD_BOOT = "import os;os.dup2(0,3);os.set_inheritable(3,True);os.execv('/opt/validation-venv/bin/python',['/opt/validation-venv/bin/python','-I','-B','/opt/hub/scripts/hub_local_acceptance_runner.py','--request-fd','3'])"

def _coordinator_bootstrap(group):
    if group is None: return _COORD_BOOT
    return ('import os;assert os.getuid()==10001 and os.geteuid()==10001 and '
        'os.getgid()==10001 and os.getegid()==10001 and '
        +str(group)+' in os.getgroups() and set(os.getgroups()) <= {10001,'+str(group)+'};'
        +_COORD_BOOT)


def _check_resource_envelope(manifest):
    """Worst-case simultaneous task caps, not a host cgroup reservation."""
    resources=manifest.runtime.resources
    memory=3*resources.memory_bytes + 1073741824 + 67108864 + 134217728 + 536870912 + 67108864 + 536870912
    cpu=3*resources.cpu_millicores + 1000 + 100 + 250
    pids=3*resources.pids_limit + 256 + 16 + 32
    if memory>4*1024**3 or cpu>2500 or pids>600:
        raise VolumeError('Local fixture aggregate resource envelope exceeded')


def _validate_local_paths(source_root, private_dir, journal_dir, volume_mount):
    paths=(source_root,private_dir,journal_dir,volume_mount)
    if any(type(p) is not str or not p.startswith('/') or p.startswith('//') or ',' in p
           or str(PurePosixPath(p))!=p or '..' in PurePosixPath(p).parts for p in paths):
        raise VolumeError('Invalid local fixture paths')
    for a in paths:
        for b in paths:
            if a!=b and PurePosixPath(a) in PurePosixPath(b).parents:
                raise VolumeError('Local fixture paths overlap')
    if len(set(paths))!=len(paths): raise VolumeError('Local fixture paths overlap')
    for p in (source_root,private_dir,journal_dir):
        if os.path.realpath(p)!=p: raise VolumeError('Local fixture path unavailable')


def _socket_pin(group):
    if group is not None and (type(group) is not int or not 0 <= group < 2**31):
        raise VolumeError('Invalid explicit socket group')
    source=os.path.realpath('/var/run/docker.sock')
    sock=os.stat('/var/run/docker.sock')
    if (',' in source or not stat.S_ISSOCK(sock.st_mode) or sock.st_mode & 0o007
        or (group is not None and (sock.st_gid!=group or sock.st_mode & 0o060!=0o060))
        or not ((sock.st_uid==10001 and sock.st_mode & 0o600==0o600)
                or (sock.st_gid==10001 and sock.st_mode & 0o060==0o060)
                or (group is not None and sock.st_gid==group and sock.st_mode & 0o060==0o060))):
        raise VolumeError('Existing daemon socket inaccessible to UID10001')
    return (source,sock.st_dev,sock.st_ino,sock.st_uid,sock.st_gid,stat.S_IMODE(sock.st_mode))


def launch_initialized_fixture(*, volume, keeper, helper_image_id, coordinator_image_id,
                               initial_manifest, candidate, rollback, source_root, source_digest,
                               private_dir, journal_dir,
                               startup_wait_ms, deadline_monotonic, socket_group=None, socket_pin=None, run=_call,
                               run_input=_call_with_input):
    """Run fixed local fixture through post-exit cleanup; failure retains resources.

    Trusted exclusive local operator, daemon and source tree are prerequisites.
    Does not establish host custody or authorize staging/production use. Normal
    deadline bounds CLI transport; uncertain accepted work is never force-stopped.
    """
    import math,secrets,time
    from engineering_hub.container_pilot_manifest import PilotManifest
    from scripts.hub_local_volume_initializer import initialize_owned_volume
    from scripts.hub_local_acceptance_runner import _fixture, _bounded_volume
    from scripts.hub_local_test_oci import verify_fixture_image
    if (type(initial_manifest) is not PilotManifest or type(keeper) is not TmpfsKeeper
        or keeper.volume!=volume or volume.options!=TMPFS_OPTIONS
        or type(keeper.container_id) is not str or not re.fullmatch('[a-f0-9]{64}',keeper.container_id)
        or type(volume.owner) is not str or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',volume.owner)
        or volume.name!='hub-local-test-'+volume.owner
        or not re.fullmatch('sha256:[a-f0-9]{64}',coordinator_image_id)
        or type(source_digest) is not str or not re.fullmatch('sha256:[a-f0-9]{64}',source_digest)
        or type(startup_wait_ms) is not int or not 0<=startup_wait_ms<=30000
        or type(deadline_monotonic) not in (int,float) or not math.isfinite(deadline_monotonic)
        or time.monotonic()>=deadline_monotonic or type(candidate) is not dict or type(rollback) is not dict):
        raise VolumeError('Invalid local acceptance enrollment')
    _check_resource_envelope(initial_manifest)
    try:
        if any(probe.startup_grace_ms for probe in initial_manifest.probes.rollback): raise ValueError
        for role,raw in (('candidate',candidate),('rollback',rollback)):
            verify_fixture_image(initial_manifest,role,_fixture(raw))
    except Exception:
        raise VolumeError('Synthetic image enrollment invalid') from None
    _validate_local_paths(source_root,private_dir,journal_dir,volume.mountpoint)
    if source_tree_digest(source_root,deadline_monotonic=deadline_monotonic)!=source_digest: raise VolumeError('Local source identity changed')
    for p in (private_dir,):
        st=os.stat(p,follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=10001 or st.st_gid!=10001 or stat.S_IMODE(st.st_mode)!=0o700:
            raise VolumeError('Private local fixture directory required')
    if os.listdir(private_dir):
        raise VolumeError('Private local fixture directories must be empty')
    # The writable host bind must reside on an independently bounded
    # task-private tmpfs; per-file RLIMIT is not an aggregate quota.
    _bounded_volume(private_dir)
    current_pin=_socket_pin(socket_group)
    if socket_pin is not None and current_pin!=socket_pin: raise VolumeError('Daemon socket identity changed')
    socket_pin=current_pin
    socket_source=socket_pin[0]
    bootstrap=_coordinator_bootstrap(socket_group)
    receipt_fd=os.open(private_dir,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    receipt_stat=os.fstat(receipt_fd); receipt_identity=(receipt_stat.st_dev,receipt_stat.st_ino)
    name='hub-local-coordinator-'+volume.owner; coord_id=None
    key=secrets.token_bytes(32); nonce=secrets.token_hex(32)
    try:
        if time.monotonic()>=deadline_monotonic: raise VolumeError('Local deadline expired; keeper retained')
        check_tmpfs_keeper(keeper,volume=volume,run=run)
        initialize_owned_volume(volume=volume,helper_image_id=helper_image_id,journal_dir=journal_dir,keeper=keeper,run=run)
        check_tmpfs_keeper(keeper,volume=volume,run=run)
        if (run(PREFIX+['image','inspect','--format','{{.Id}}',coordinator_image_id]).strip()!=coordinator_image_id
            or json.loads(run(PREFIX+['image','inspect','--format','{{json (index .Config "Volumes")}}',coordinator_image_id])) not in (None,{})
            or name in run(PREFIX+['ps','--all','--format','{{.Names}}']).splitlines()):
            raise VolumeError('Coordinator image unavailable')
        if time.monotonic()>=deadline_monotonic or source_tree_digest(source_root,deadline_monotonic=deadline_monotonic)!=source_digest: raise VolumeError('Local admission unavailable; keeper retained')
        handles=_reserve_intent(journal_dir,volume.owner,volume.daemon_id,volume.name,'.coordinator-create','coordinator-create')
        try:
            _check_path(handles); check_tmpfs_keeper(keeper,volume=volume,run=run); _check_path(handles)
            if _socket_pin(socket_group)!=socket_pin: raise ValueError
            coord_id=run(PREFIX+['create','--pull=never','--interactive','--name',name,'--label',LABEL+'='+volume.owner,
                '--user','10001:10001','--network','none','--read-only','--cpus','1','--memory','1g',
                '--memory-swap','1g','--pids-limit','256','--log-driver','local','--log-opt','max-size=1m','--log-opt','max-file=2','--cap-drop','ALL','--security-opt','no-new-privileges',
                '--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=64m',
                '--mount','type=volume,src='+volume.name+',dst='+volume.mountpoint+',volume-nocopy',
                '--mount','type=bind,src='+source_root+',dst=/opt/hub,readonly',
                '--mount','type=bind,src='+private_dir+',dst='+private_dir,
                '--mount','type=bind,src='+socket_source+',dst=/var/run/docker.sock',
                *([] if socket_group is None else ['--group-add',str(socket_group)]),
                '--entrypoint','/opt/validation-venv/bin/python',coordinator_image_id,'-I','-B','-c',bootstrap]).strip()
            if not re.fullmatch('[a-f0-9]{64}',coord_id): raise ValueError
        except Exception: raise VolumeOutcomeUnknown('Coordinator create unknown; keeper retained') from None
        finally: _close_handles(handles)
        def inspect(status):
            info=run(PREFIX+['inspect','--format','{{.Id}}|{{.Name}}|{{.Config.Image}}|{{index .Config.Labels "'+LABEL+'"}}|{{.State.Status}}|{{.State.ExitCode}}',coord_id]).strip().split('|')
            if info != [coord_id,'/'+name,coordinator_image_id,volume.owner,status,'0']: raise ValueError
            actual=json.loads(run(PREFIX+['inspect','--format','{{json .State}}',coord_id]))
            if (type(actual) is not dict or actual.get('Status')!=status or actual.get('Running') is not False
                or type(actual.get('Pid')) is not int or actual['Pid']!=0
                or any(actual.get(k) is not False for k in ('Paused','Restarting','Dead'))
                or type(actual.get('ExitCode')) is not int or actual['ExitCode']!=0): raise ValueError
            for field, expected in (('User','10001:10001'),('Entrypoint',['/opt/validation-venv/bin/python']),('Cmd',['-I','-B','-c',bootstrap]),('Volumes',None)):
                if json.loads(run(PREFIX+['inspect','--format','{{json .Config.'+field+'}}',coord_id]))!=expected: raise ValueError
            host=json.loads(run(PREFIX+['inspect','--format','{{json .HostConfig}}',coord_id]))
            if (host.get('NetworkMode')!='none' or host.get('ReadonlyRootfs') is not True or host.get('Privileged') is not False
                or host.get('CapDrop')!=['ALL'] or host.get('CapAdd') not in (None,[]) or host.get('SecurityOpt')!=['no-new-privileges']
                or host.get('Memory')!=1073741824 or host.get('MemorySwap')!=1073741824 or host.get('NanoCpus')!=1000000000
                or host.get('PidsLimit')!=256 or host.get('AutoRemove') is not False or host.get('PublishAllPorts') is not False
                or host.get('Binds') not in (None,[]) or host.get('Devices') not in (None,[]) or host.get('DeviceRequests') not in (None,[])
                or host.get('VolumesFrom') not in (None,[]) or host.get('PortBindings') not in (None,{})
                or host.get('Tmpfs') != {'/tmp':'rw,noexec,nosuid,nodev,size=64m'}
                or host.get('LogConfig') != {'Type':'local','Config':{'max-size':'1m','max-file':'2'}}
                or host.get('PidMode') not in ('','private') or host.get('IpcMode') not in ('','private')
                or host.get('RestartPolicy',{}).get('Name') not in ('','no')): raise ValueError
            if host.get('GroupAdd') != (None if socket_group is None else [str(socket_group)]): raise ValueError
            mounts=json.loads(run(PREFIX+['inspect','--format','{{json .Mounts}}',coord_id]))
            expected={(volume.name,volume.mountpoint,volume.mountpoint,True),('',source_root,'/opt/hub',False),
                      ('',private_dir,private_dir,True),('',socket_source,'/var/run/docker.sock',True)}
            if type(mounts) is not list: raise ValueError
            tmp=[m for m in mounts if m.get('Destination')=='/tmp']
            if len(tmp)>1 or any(m.get('Type')!='tmpfs' or m.get('RW') is not True for m in tmp): raise ValueError
            mounts=[m for m in mounts if m.get('Destination')!='/tmp']
            if len(mounts)!=len(expected): raise ValueError
            for mount in mounts:
                if mount.get('Destination')==volume.mountpoint:
                    if mount.get('Type')!='volume' or mount.get('Driver')!='local': raise ValueError
                elif mount.get('Type')!='bind' or mount.get('Propagation') not in ('rprivate','private'):
                    raise ValueError
            observed={(m.get('Name',''),m.get('Source'),m.get('Destination'),m.get('RW')) for m in mounts}
            if observed!=expected: raise ValueError
            requested=host.get('Mounts')
            if type(requested) is not list or not any(x.get('Type')=='volume' and x.get('Source')==volume.name and x.get('Target')==volume.mountpoint and x.get('VolumeOptions',{}).get('NoCopy') is True for x in requested): raise ValueError
        inspect('created')
        request={'schema':'engineering-hub.local-acceptance-request.v1','task_id':volume.owner,'daemon_id':volume.daemon_id,
            'volume_mount':volume.mountpoint,'private_dir':private_dir,
            'manifest':initial_manifest.canonical_bytes().decode('utf8'),'candidate':candidate,'rollback':rollback,
            'coordinator_id':coord_id,'coordinator_image_id':coordinator_image_id,'cleanup_nonce':nonce,
            'cleanup_key':key.hex(),'startup_wait_ms':startup_wait_ms,'volume_storage':'tmpfs-512m-v1'}
        raw=_canonical(request)
        if len(raw)>8*1024*1024: raise VolumeError('Local acceptance request exceeds limit')
        if time.monotonic()>=deadline_monotonic or source_tree_digest(source_root,deadline_monotonic=deadline_monotonic)!=source_digest: raise VolumeOutcomeUnknown('Local admission unavailable; coordinator and keeper retained')
        handles=_reserve_intent(journal_dir,volume.owner,volume.daemon_id,volume.name,'.coordinator-start','coordinator-start')
        try:
            _check_path(handles); check_tmpfs_keeper(keeper,volume=volume,run=run); inspect('created'); _check_path(handles)
            if _socket_pin(socket_group)!=socket_pin: raise ValueError
            run_input(PREFIX+['start','--attach','--interactive',coord_id],raw,deadline_monotonic)
            check_tmpfs_keeper(keeper,volume=volume,run=run); inspect('exited')
            if source_tree_digest(source_root,deadline_monotonic=deadline_monotonic)!=source_digest: raise ValueError('Local source identity changed')
        except Exception: raise VolumeOutcomeUnknown('Coordinator result unknown; keeper retained') from None
        finally: _close_handles(handles)
        handles=_reserve_intent(journal_dir,volume.owner,volume.daemon_id,volume.name,'.coordinator-remove','coordinator-remove')
        try:
            _check_path(handles); check_tmpfs_keeper(keeper,volume=volume,run=run); inspect('exited'); _check_path(handles)
            if run(PREFIX+['rm',coord_id]).strip()!=coord_id: raise ValueError
            ids=run(PREFIX+['ps','--all','--quiet','--no-trunc']).splitlines()
            if any(not re.fullmatch('[a-f0-9]{64}',x) for x in ids) or coord_id in ids: raise ValueError
        except Exception: raise VolumeOutcomeUnknown('Coordinator removal unknown; keeper retained') from None
        finally: _close_handles(handles)
        stop_tmpfs_keeper(keeper=keeper,initial_manifest=initial_manifest,coordinator_id=coord_id,
            coordinator_image_id=coordinator_image_id,nonce=nonce,key=key,receipt_directory_fd=receipt_fd,
            receipt_directory_identity=receipt_identity,journal_dir=journal_dir,run=run)
        remove_after_coordinator_exit(volume=volume,coordinator_id=coord_id,coordinator_image_id=coordinator_image_id,
            initial_manifest=initial_manifest,nonce=nonce,key=key,receipt_directory_fd=receipt_fd,
            receipt_directory_identity=receipt_identity,journal_dir=journal_dir,run=run)
        return {'status':'completed','coordinator_id':coord_id}
    except Exception:
        safe_coord=coord_id if type(coord_id) is str and re.fullmatch('[a-f0-9]{64}',coord_id) else 'unknown'
        raise VolumeOutcomeUnknown('Local fixture held; volume '+volume.name+'; keeper '+keeper.container_id+
            '; coordinator '+safe_coord+'; preserve resources and inspect original journal') from None
    finally:
        os.close(receipt_fd)


def run_local_acceptance(*, task_id, daemon_id, helper_image_id, coordinator_image_id,
                         initial_manifest, candidate, rollback, source_root, source_digest,
                         private_dir, journal_dir, startup_wait_ms,
                         deadline_monotonic, socket_group=None, run=_call, run_input=_call_with_input):
    """Own a new local tmpfs volume, keeper and fixed coordinator; no retries.

    Source must be an independently checked exact tracked-source snapshot, with
    its full tree digest pinned by the trusted operator. The private writable
    directory must already be on a dedicated bounded tmpfs. Uncertain work is
    retained with exact names for manual inspection; never auto-adopt/replay.
    """
    import math,time,uuid
    from engineering_hub.container_pilot_manifest import PilotManifest
    from scripts.hub_local_acceptance_runner import _fixture,_bounded_volume
    from scripts.hub_local_test_oci import verify_fixture_image, discover_cached_image
    from scripts.hub_local_test_volume import create_volume
    try:
        if (type(task_id) is not str or str(uuid.UUID(task_id))!=task_id
            or type(initial_manifest) is not PilotManifest or type(startup_wait_ms) is not int
            or not 0<=startup_wait_ms<=30000 or type(deadline_monotonic) not in (int,float)
            or not math.isfinite(deadline_monotonic) or time.monotonic()>=deadline_monotonic
            or type(source_digest) is not str or not re.fullmatch('sha256:[a-f0-9]{64}',source_digest)
            or not re.fullmatch('sha256:[a-f0-9]{64}',helper_image_id)
            or not re.fullmatch('sha256:[a-f0-9]{64}',coordinator_image_id)):
            raise ValueError
        _check_resource_envelope(initial_manifest)
        if any(p.startup_grace_ms for p in initial_manifest.probes.rollback): raise ValueError
        images=[]
        for role,raw in (('candidate',candidate),('rollback',rollback)):
            image,*_=verify_fixture_image(initial_manifest,role,_fixture(raw)); images.append(image)
        if source_tree_digest(source_root,deadline_monotonic=deadline_monotonic)!=source_digest: raise ValueError
        for p in (private_dir,):
            if (type(p) is not str or not os.path.isabs(p) or os.path.realpath(p)!=p
                or ',' in p or '..' in PurePosixPath(p).parts): raise ValueError
            st=os.stat(p,follow_symlinks=False)
            if (not stat.S_ISDIR(st.st_mode) or st.st_uid!=10001 or st.st_gid!=10001
                or stat.S_IMODE(st.st_mode)!=0o700 or os.listdir(p)): raise ValueError
            _bounded_volume(p)
        socket_pin=_socket_pin(socket_group)
        if run(PREFIX+['info','--format','{{.ID}}']).strip()!=daemon_id: raise ValueError
        docker_root=run(PREFIX+['info','--format','{{.DockerRootDir}}']).strip()
        if (not docker_root.startswith('/') or docker_root.startswith('//') or str(PurePosixPath(docker_root))!=docker_root
            or '..' in PurePosixPath(docker_root).parts or any(ord(c)<32 or ord(c)==127 for c in docker_root)):
            raise ValueError
        _validate_local_paths(source_root,private_dir,journal_dir,
                              docker_root+'/volumes/hub-local-test-'+task_id+'/_data')
        for image_id in (helper_image_id,coordinator_image_id):
            if (run(PREFIX+['image','inspect','--format','{{.Id}}',image_id]).strip()!=image_id
                or json.loads(run(PREFIX+['image','inspect','--format','{{json (index .Config "Volumes")}}',image_id])) not in (None,{})):
                raise ValueError
        for image in images: discover_cached_image(image,daemon_id=daemon_id,run=run)
        if time.monotonic()>=deadline_monotonic: raise ValueError
    except Exception:
        raise VolumeError('Local acceptance preflight unavailable') from None
    name='hub-local-test-'+task_id; keeper=None
    try:
        volume=create_volume(task_id=task_id,daemon_id=daemon_id,journal_dir=journal_dir,storage='tmpfs-512m-v1',run=run)
        if time.monotonic()>=deadline_monotonic: raise TimeoutError
        keeper=start_tmpfs_keeper(volume=volume,helper_image_id=helper_image_id,journal_dir=journal_dir,run=run)
        if time.monotonic()>=deadline_monotonic: raise TimeoutError
        return launch_initialized_fixture(volume=volume,keeper=keeper,helper_image_id=helper_image_id,
            coordinator_image_id=coordinator_image_id,initial_manifest=initial_manifest,candidate=candidate,
            rollback=rollback,source_root=source_root,source_digest=source_digest,private_dir=private_dir,
            journal_dir=journal_dir,startup_wait_ms=startup_wait_ms,socket_group=socket_group,socket_pin=socket_pin,
            deadline_monotonic=deadline_monotonic,run=run,run_input=run_input)
    except VolumeOutcomeUnknown as error:
        if str(error).startswith('Local fixture held; volume '): raise
        keeper_id=keeper.container_id if type(keeper) is TmpfsKeeper else 'unknown'
        raise VolumeOutcomeUnknown('Local fixture held; volume '+name+'; keeper '+keeper_id+
            '; inspect original journal and preserve resources') from None
    except Exception:
        keeper_id=keeper.container_id if type(keeper) is TmpfsKeeper else 'unknown'
        raise VolumeOutcomeUnknown('Local fixture held; volume '+name+'; keeper '+keeper_id+
            '; inspect original journal and preserve resources') from None


def main(argv=None):
    """Fixed FD3 operator entry; all credentials/receipt keys remain in memory."""
    import argparse,time
    from engineering_hub.container_pilot_manifest import PilotManifest
    from scripts.hub_local_acceptance_runner import _read_request
    parser=argparse.ArgumentParser(description='Disposable local fixture only; unknown effects are held')
    parser.add_argument('--request-fd',type=int,required=True,choices=[3])
    parser.add_argument('--attested-journal-ancestor-fds',default=None,
                        help='Local test only: FDs attested by the trusted initial-namespace root parent')
    args=parser.parse_args(argv)
    pin_fds=()
    try:
        try:
            request=_read_request(args.request_fd)
        finally:
            os.close(args.request_fd)
        keys={'schema','task_id','daemon_id','helper_image_id','coordinator_image_id','initial_manifest',
              'candidate','rollback','source_root','source_digest','private_dir','journal_dir',
              'startup_wait_ms','timeout_seconds'}
        if (type(request) is not dict or not (set(request)==keys and request.get('schema')=='engineering-hub.local-launch-request.v1'
                or set(request)==keys|{'socket_group'} and request.get('schema')=='engineering-hub.local-launch-request.v2')
            or ('socket_group' in request and (type(request['socket_group']) is not int or not 0<=request['socket_group']<2**31))
            or type(request['initial_manifest']) is not str
            or type(request['timeout_seconds']) is not int or not 1<=request['timeout_seconds']<=600):
            raise ValueError
        if args.attested_journal_ancestor_fds is not None:
            values=args.attested_journal_ancestor_fds.split(',')
            if not values or any(not value.isdecimal() for value in values): raise ValueError
            pin_fds=tuple(int(value) for value in values)
            if ','.join(str(value) for value in pin_fds)!=args.attested_journal_ancestor_fds: raise ValueError
            _install_attested_ancestor_fds(pin_fds)
        manifest=PilotManifest.from_bytes(request['initial_manifest'].encode('ascii'))
        if manifest.canonical_bytes()!=request['initial_manifest'].encode('ascii'): raise ValueError
        result=run_local_acceptance(task_id=request['task_id'],daemon_id=request['daemon_id'],
            helper_image_id=request['helper_image_id'],coordinator_image_id=request['coordinator_image_id'],
            initial_manifest=manifest,candidate=request['candidate'],rollback=request['rollback'],
            source_root=request['source_root'],source_digest=request['source_digest'],
            private_dir=request['private_dir'],journal_dir=request['journal_dir'],
            startup_wait_ms=request['startup_wait_ms'],socket_group=request.get('socket_group'),deadline_monotonic=time.monotonic()+request['timeout_seconds'])
        print(_canonical(result).decode('ascii'))
        return 0
    except VolumeOutcomeUnknown as error:
        # Only our locally generated held identifiers are returned; never raw
        # subprocess output, request bytes, keys, or caught exception strings.
        text=str(error)
        if not re.fullmatch(r'Local fixture held; volume hub-local-test-[a-f0-9-]{36}; keeper (?:[a-f0-9]{64}|unknown)(?:; coordinator (?:[a-f0-9]{64}|unknown))?; (?:preserve resources and inspect original journal|inspect original journal and preserve resources)',text):
            text='Local fixture outcome unknown; preserve resources and inspect original journal'
        print(text,file=sys.stderr)
        return 2
    except Exception:
        print('Local fixture preflight unavailable',file=sys.stderr)
        return 1
    finally:
        for fd in pin_fds:
            try: os.close(fd)
            except OSError: pass


if __name__=='__main__':
    # Initializer code imports this module by its package name.  Keep the
    # direct entry point and that import bound to the same exact keeper type.
    existing=sys.modules.get('scripts.hub_local_test_launcher')
    if existing is not None and existing is not sys.modules[__name__]:
        raise RuntimeError('Conflicting local launcher module')
    sys.modules['scripts.hub_local_test_launcher']=sys.modules[__name__]
    raise SystemExit(main())
