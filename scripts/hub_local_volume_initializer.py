"""One-shot fixed local test volume initializer; never staging or production.

Trusted local fixture operator and exclusive daemon access are prerequisites.
Uncertain effects are permanently held for inspection; this module never retries.
"""
import json
import re
import uuid
from scripts.hub_local_test_volume import OwnedVolume, VolumeError, VolumeOutcomeUnknown, _call, _reserve_intent, _check_path, _close_handles, _options_match, TMPFS_OPTIONS

LABEL='engineering-hub.local-test-owner'
PREFIX=['/usr/bin/docker','--host','unix:///var/run/docker.sock']
INIT_SCRIPT = '''import os,stat
root=os.open('/fixture',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
 if os.listdir(root): raise RuntimeError('nonempty')
 for name in ('generations','reservations'):
  os.mkdir(name,0o700,dir_fd=root)
  fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
  try:
   if name=='generations':
    os.mkdir('baseline-g1',0o700,dir_fd=fd)
    child=os.open('baseline-g1',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
    try: os.fchmod(child,0o700); os.fchown(child,10001,10001); os.fsync(child)
    finally: os.close(child)
   os.fchmod(fd,0o700); os.fchown(fd,10001,10001); os.fsync(fd)
  finally: os.close(fd)
 os.fchmod(root,0o700); os.fchown(root,10001,10001); os.fsync(root)
finally: os.close(root)
'''


def initialize_owned_volume(*, volume, helper_image_id, journal_dir, keeper=None, run=_call):
    """Create/start/remove exactly one labeled initializer; uncertain means held."""
    if (type(volume) is not OwnedVolume or not re.fullmatch(r'sha256:[0-9a-f]{64}',helper_image_id)
            or volume.name != 'hub-local-test-' + volume.owner
            or type(volume.owner) is not str or str(uuid.UUID(volume.owner)) != volume.owner):
        raise VolumeError('Explicit test volume and immutable helper image required')
    def check_keeper():
        if volume.options == TMPFS_OPTIONS:
            from scripts.hub_local_test_launcher import check_tmpfs_keeper
            check_tmpfs_keeper(keeper,volume=volume,run=run)
        elif keeper is not None:
            raise ValueError('Unexpected keeper')
    name='hub-local-init-'+volume.owner
    def verify_daemon_volume():
        if run(PREFIX+['info','--format','{{.ID}}']).strip()!=volume.daemon_id: raise ValueError
        root=run(PREFIX+['info','--format','{{.DockerRootDir}}']).strip()
        if volume.mountpoint != root+'/volumes/'+volume.name+'/_data': raise ValueError
        rows=json.loads(run(PREFIX+['volume','inspect',volume.name]))
        if type(rows) is not list or len(rows)!=1: raise ValueError
        v=rows[0]
        if (v.get('Name')!=volume.name or v.get('Driver')!='local' or v.get('Scope')!='local'
            or v.get('Mountpoint')!=volume.mountpoint or not _options_match(volume,v.get('Options'))
            or v.get('Labels')!={LABEL:volume.owner}): raise ValueError
    try:
        verify_daemon_volume(); check_keeper()
        if run(PREFIX+['image','inspect','--format','{{.Id}}',helper_image_id]).strip()!=helper_image_id:
            raise ValueError
        image_volumes=json.loads(run(PREFIX+['image','inspect','--format','{{json .Config.Volumes}}',helper_image_id]))
        if image_volumes not in (None,{}): raise ValueError
        existing=run(PREFIX+['ps','--all','--format','{{.Names}}']).splitlines()
        if name in existing: raise ValueError
    except Exception: raise VolumeError('Owned test volume or helper unavailable') from None
    handles=_reserve_intent(journal_dir,volume.owner,volume.daemon_id,volume.name,'.init-create','init-create')
    try:
        _check_path(handles); verify_daemon_volume(); check_keeper(); _check_path(handles)
        ident=run(PREFIX+['create','--pull=never','--name',name,'--label',LABEL+'='+volume.owner,
            '--network','none','--read-only','--cpus','0.25','--memory','128m','--memory-swap','128m',
            '--pids-limit','32','--log-driver','local','--log-opt','max-size=1m','--log-opt','max-file=2','--user','0:0','--cap-drop','ALL','--cap-add','CHOWN',
            '--security-opt','no-new-privileges','--mount','type=volume,src='+volume.name+',dst=/fixture,volume-nocopy',
            '--entrypoint','/usr/local/bin/python',helper_image_id,'-I','-B','-c',INIT_SCRIPT]).strip()
        if not re.fullmatch('[0-9a-f]{64}',ident): raise ValueError
    except Exception: raise VolumeOutcomeUnknown('Initializer create outcome unknown; preserve') from None
    finally:
        try: _close_handles(handles)
        except OSError: raise VolumeOutcomeUnknown('Initializer create cleanup unknown; preserve') from None
    def inspect(expected):
        value=run(PREFIX+['inspect','--format','{{.Id}}|{{.Name}}|{{.Config.Image}}|{{index .Config.Labels "'+LABEL+'"}}|{{.State.Status}}|{{.State.ExitCode}}',ident]).strip().split('|')
        if value != [ident,'/'+name,helper_image_id,volume.owner,*expected]: raise ValueError
        for expression, expected in (('User','0:0'),('Entrypoint',['/usr/local/bin/python']),
                                     ('Cmd',['-I','-B','-c',INIT_SCRIPT]),('Volumes',None)):
            if json.loads(run(PREFIX+['inspect','--format','{{json .Config.'+expression+'}}',ident])) != expected:
                raise ValueError
        profile=json.loads(run(PREFIX+['inspect','--format','{{json .HostConfig}}',ident]))
        if (type(profile) is not dict or profile.get('NetworkMode')!='none'
            or profile.get('ReadonlyRootfs') is not True or profile.get('Privileged') is not False
            or profile.get('CapDrop') != ['ALL'] or profile.get('CapAdd') != ['CHOWN']
            or profile.get('LogConfig') != {'Type':'local','Config':{'max-size':'1m','max-file':'2'}}
            or profile.get('Memory') != 134217728 or profile.get('MemorySwap') != 134217728
            or profile.get('NanoCpus') != 250000000 or profile.get('PidsLimit') != 32
            or profile.get('Binds') not in (None,[]) or profile.get('Devices') not in (None,[])
            or profile.get('DeviceRequests') not in (None,[]) or profile.get('VolumesFrom') not in (None,[])
            or profile.get('IpcMode') not in ('private','') or profile.get('PidMode') not in ('private','')
            or profile.get('AutoRemove') is not False or profile.get('PublishAllPorts') is not False
            or profile.get('PortBindings') not in (None,{}) or profile.get('RestartPolicy',{}).get('Name') not in ('','no')
            or profile.get('SecurityOpt') != ['no-new-privileges']):
            raise ValueError
        requested=profile.get('Mounts')
        if (type(requested) is not list or len(requested)!=1
            or requested[0].get('Type')!='volume' or requested[0].get('Source')!=volume.name
            or requested[0].get('Target')!='/fixture' or requested[0].get('ReadOnly') is True
            or requested[0].get('VolumeOptions',{}).get('NoCopy') is not True): raise ValueError
        mounts=json.loads(run(PREFIX+['inspect','--format','{{json .Mounts}}',ident]))
        if (type(mounts) is not list or len(mounts)!=1 or mounts[0].get('Type')!='volume'
            or mounts[0].get('Name')!=volume.name or mounts[0].get('Source')!=volume.mountpoint
            or mounts[0].get('Driver')!='local' or mounts[0].get('Destination')!='/fixture'
            or mounts[0].get('RW') is not True): raise ValueError
    try: verify_daemon_volume(); inspect(['created','0'])
    except Exception: raise VolumeOutcomeUnknown('Initializer identity unverified; preserve') from None
    handles=_reserve_intent(journal_dir,volume.owner,volume.daemon_id,volume.name,'.init-start','init-start')
    try:
        _check_path(handles); verify_daemon_volume(); check_keeper(); inspect(['created','0']); _check_path(handles)
        run(PREFIX+['start','--attach',ident])
        verify_daemon_volume(); check_keeper(); inspect(['exited','0'])
    except Exception: raise VolumeOutcomeUnknown('Initializer start outcome unknown; preserve') from None
    finally:
        try: _close_handles(handles)
        except OSError: raise VolumeOutcomeUnknown('Initializer start cleanup unknown; preserve') from None
    handles=_reserve_intent(journal_dir,volume.owner,volume.daemon_id,volume.name,'.init-remove','init-remove')
    try:
        _check_path(handles); verify_daemon_volume(); check_keeper(); inspect(['exited','0']); _check_path(handles)
        if run(PREFIX+['rm',ident]).strip()!=ident: raise ValueError
        verify_daemon_volume()
        remaining=run(PREFIX+['ps','--all','--quiet','--no-trunc']).splitlines()
        if any(not re.fullmatch('[0-9a-f]{64}', item) for item in remaining) or ident in remaining: raise ValueError
    except Exception: raise VolumeOutcomeUnknown('Initializer removal outcome unknown; preserve') from None
    finally:
        try: _close_handles(handles)
        except OSError: raise VolumeOutcomeUnknown('Initializer removal cleanup unknown; preserve') from None
    return ident
