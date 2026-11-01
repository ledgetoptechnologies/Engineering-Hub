"""Fixed FD3 launcher through actual lifecycle, synthetic Docker/coordinator only."""
import base64,hashlib,json,os,subprocess,sys,time,uuid
from pathlib import Path
import pytest
from scripts.hub_local_test_oci import prepare_fixture_image
from scripts.hub_local_test_launcher import source_tree_digest
from tests.test_prepare_local_oci_layout import fixture,archive
from tests.test_hub_container_pilot_manifest import make_manifest_data
from engineering_hub.container_pilot_manifest import PilotManifest

CHILD=r'''
import json,os,stat,sys,types,hmac,hashlib
from pathlib import Path
source=Path(sys.argv[1]).resolve(); sys.path[:0]=[str(source),str(source/'src')]
from scripts import hub_local_test_launcher as l
import engineering_hub
assert Path(l.__file__).resolve().is_relative_to(source) and Path(engineering_hub.__file__).resolve().is_relative_to(source)
from scripts.hub_local_test_volume import TMPFS_OPTIONS
from scripts.hub_local_volume_initializer import INIT_SCRIPT
from scripts.hub_local_baseline_bootstrap import _final_manifest
original_stat=l.os.stat; original_real=l.os.path.realpath
l.os.stat=lambda p,*a,**k: types.SimpleNamespace(st_mode=stat.S_IFSOCK|0o600,st_uid=10001,st_gid=10001,st_dev=1,st_ino=2) if str(p)=='/var/run/docker.sock' else original_stat(p,*a,**k)
l.os.path.realpath=lambda p,*a,**k:'/run/docker.sock' if str(p)=='/var/run/docker.sock' else original_real(p,*a,**k)
orig=l.run_local_acceptance
class Engine:
 def __init__(self): self.states={}; self.volume_exists=False; self.owner='db04aa59-54f0-4269-bcba-539c0151e13e'; self.name='hub-local-test-'+self.owner; self.root='/fake-docker'; self.mount=self.root+'/volumes/'+self.name+'/_data'; self.daemon='daemon-1234567890'; self.image='sha256:'+'a'*64
 def __call__(self,a):
  op=a[3:]
  if op[0]=='info': return self.root if 'DockerRootDir' in op[-1] else self.daemon
  if op[:2]==['volume','ls']: return self.name if self.volume_exists else ''
  if op[:2]==['volume','create']:
   assert not self.volume_exists; self.volume_exists=True; return self.name
  if op[:2]==['volume','inspect']: return json.dumps([dict(Name=self.name,Driver='local',Scope='local',Mountpoint=self.mount,Options=dict(TMPFS_OPTIONS),Labels={l.LABEL:self.owner})])
  if op[:2]==['volume','rm']:
   assert self.volume_exists and not self.states; self.volume_exists=False; return self.name
  if op[:2]==['image','inspect']:
   if 'Volumes' in op[-2]: return 'null'
   if '{{.Id}}' in op[-2] and '|' not in op[-2]: return self.image
   return self.configs[op[-1]]+'|linux|amd64|'
  if op[0]=='ps': return '\n'.join(self.states.keys() if '--quiet' in op else (item[1] for item in self.states.values()))
  if op[0]=='create':
   name=a[a.index('--name')+1]; role='keeper' if 'keeper' in name else ('init' if 'init' in name else 'coordinator'); ident={'keeper':'a','init':'b','coordinator':'c'}[role]*64; self.states[ident]=['created',name,role]; return ident
  if op[0]=='inspect':
   ident=op[-1]; state,name,role=self.states[ident]; fmt=op[2]
   if fmt=='{{json .State}}': return json.dumps(dict(Status=state,Running=state=='running',Pid=123 if state=='running' else 0,Paused=False,Restarting=False,Dead=False,ExitCode=0))
   if '.Config.User' in fmt: return json.dumps('0:0' if role=='init' else '10001:10001')
   if '.Config.Entrypoint' in fmt: return json.dumps(['/opt/validation-venv/bin/python'] if role=='coordinator' else ['/usr/local/bin/python'])
   if '.Config.Cmd' in fmt: return json.dumps(['-I','-B','-c',{'keeper':l._KEEPER_SCRIPT,'init':INIT_SCRIPT,'coordinator':l._COORD_BOOT}[role]])
   if '.Config.Volumes' in fmt: return 'null'
   if 'HostConfig' in fmt:
    host=dict(NetworkMode='none',ReadonlyRootfs=True,Privileged=False,CapDrop=['ALL'],CapAdd=['CHOWN'] if role=='init' else None,SecurityOpt=['no-new-privileges'],Memory={'keeper':67108864,'init':134217728,'coordinator':1073741824}[role],MemorySwap={'keeper':67108864,'init':134217728,'coordinator':1073741824}[role],NanoCpus={'keeper':100000000,'init':250000000,'coordinator':1000000000}[role],PidsLimit={'keeper':16,'init':32,'coordinator':256}[role],Binds=None,Devices=None,DeviceRequests=None,VolumesFrom=None,AutoRemove=False,PublishAllPorts=False,PortBindings=None,PidMode='',IpcMode='private',RestartPolicy={'Name':'no'},LogConfig={'Type':'local','Config':{'max-size':'1m','max-file':'2'}},Mounts=[dict(Type='volume',Source=self.name,Target=self.mount if role=='coordinator' else '/fixture',VolumeOptions={'NoCopy':True})])
    if role=='coordinator': host['Tmpfs']={'/tmp':'rw,noexec,nosuid,nodev,size=64m'}
    return json.dumps(host)
   if 'Mounts' in fmt:
    mount=[dict(Type='volume',Driver='local',Name=self.name,Source=self.mount,Destination=self.mount if role=='coordinator' else '/fixture',RW=True)]
    if role=='coordinator':
     for source,dest,rw in [(self.source,'/opt/hub',False),(self.private,self.private,True),('/run/docker.sock','/var/run/docker.sock',True)]: mount.append(dict(Type='bind',Propagation='rprivate',Source=source,Destination=dest,RW=rw))
    return json.dumps(mount)
   return '|'.join([ident,'/'+name,self.image,self.owner,state]+([] if role=='keeper' else ['0']))
  if op[0]=='start':
   ident=op[-1]; self.states[ident][0]='exited' if '--attach' in op else 'running'; return '' if '--attach' in op else ident
  if op[0]=='stop': self.states[op[-1]][0]='exited'; return op[-1]
  if op[0]=='rm':
   ident=op[-1]; assert self.states[ident][0]=='exited'; del self.states[ident]; return ident
  raise AssertionError(op)
 def start(self,args,data,deadline):
  request=json.loads(data); self.states[args[-1]][0]='exited'; initial=__import__('engineering_hub.container_pilot_manifest',fromlist=['PilotManifest']).PilotManifest.from_bytes(request['manifest'].encode()); backup='sha256:'+'f'*64
  payload=dict(schema=l.SCHEMA,task_id=self.owner,daemon_id=self.daemon,docker_root=self.root,volume_name=self.name,volume_mount=self.mount,coordinator_id=args[-1],coordinator_image_id=self.image,volume_storage='tmpfs-512m-v1',volume_options=dict(TMPFS_OPTIONS),initial_manifest_digest=initial.digest(),baseline_backup_digest=backup,manifest_digest=_final_manifest(initial,backup).digest(),nonce=request['cleanup_nonce'],containers=[])
  sig=hmac.new(bytes.fromhex(request['cleanup_key']),l.DOMAIN+l._canonical(payload),hashlib.sha256).hexdigest(); p=os.path.join(self.private,'cleanup-receipt.json'); fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); os.write(fd,l._canonical(dict(payload=payload,signature=sig))); os.fsync(fd); os.fchown(fd,10001,10001); os.close(fd)
e=Engine()
def wrapped(**kw):
 e.source=kw['source_root']; e.private=kw['private_dir']; e.configs={kw['candidate']['manifest_digest']:kw['candidate']['config_digest'],kw['rollback']['manifest_digest']:kw['rollback']['config_digest']}; kw['run']=e; kw['run_input']=e.start
 return orig(**kw)
l.run_local_acceptance=wrapped
os.dup2(0,3); os.set_inheritable(3,True)
result=l.main(['--request-fd','3'])
if result==0: assert not e.states and not e.volume_exists
raise SystemExit(result)
'''


def test_fixed_fd3_full_synthetic_lifecycle(tmp_path):
 if os.geteuid()!=0: pytest.skip('synthetic UID requires root')
 source=tmp_path/'source'; source.mkdir(); (source/'fixture.py').write_text('synthetic')
 private=tmp_path/'private'; private.mkdir(mode=0o700); os.chown(private,10001,10001)
 journal=tmp_path/'journal'; journal.mkdir(mode=0o700)
 rows,digest=fixture(); data=make_manifest_data(); data['runtime']['resources']['cpu_millicores']=300; data['runtime']['resources']['pids_limit']=80
 for p in data['probes']['rollback']: p['startup_grace_ms']=0
 fixtures={}
 for role in ('candidate','rollback'):
  role_rows,role_digest=rows,digest
  if role=='rollback':
   old_manifest=json.loads(next(raw for name,raw in rows if name=='blobs/sha256/'+digest[7:]))
   old_config=old_manifest['config']['digest']; config=json.loads(next(raw for name,raw in rows if name=='blobs/sha256/'+old_config[7:])); config['fixture_variant']='rollback'
   config_raw=json.dumps(config,separators=(',',':')).encode(); config_id='sha256:'+hashlib.sha256(config_raw).hexdigest(); old_manifest['config'].update(digest=config_id,size=len(config_raw))
   manifest_raw=json.dumps(old_manifest,separators=(',',':')).encode(); role_digest='sha256:'+hashlib.sha256(manifest_raw).hexdigest()
   role_rows=[(name,raw) for name,raw in rows if name not in ('index.json','blobs/sha256/'+digest[7:],'blobs/sha256/'+old_config[7:])]
   role_rows += [('blobs/sha256/'+config_id[7:],config_raw),('blobs/sha256/'+role_digest[7:],manifest_raw),('index.json',json.dumps({'schemaVersion':2,'manifests':[{'mediaType':old_manifest['mediaType'],'digest':role_digest,'size':len(manifest_raw)}]},separators=(',',':')).encode())]
  item=prepare_fixture_image(archive(role_rows),tmp_path/('oci-'+role),role_digest,source_commit=data[role]['source_commit'],context_digest=data[role]['provenance']['build_context_digest'],recipe_digest=data[role]['provenance']['build_recipe_digest'],observations={'synthetic':True},preparation_id=str(uuid.uuid4()))
  data[role]['image_config_id']=item.config_digest; data[role]['provenance']['build_inputs_digest']=item.inputs_digest; data[role]['provenance']['attestation_digest']=item.record_digest
  fixtures[role]={'record':base64.b64encode(item.record).decode(),'inputs':base64.b64encode(item.inputs).decode(),'manifest_digest':item.manifest_digest,'config_digest':item.config_digest,'blobs':{k:base64.b64encode(v).decode() for k,v in item.blobs.items()}}
 manifest=PilotManifest.from_bytes(json.dumps(data).encode()); owner='db04aa59-54f0-4269-bcba-539c0151e13e'
 request=dict(schema='engineering-hub.local-launch-request.v1',task_id=owner,daemon_id='daemon-1234567890',helper_image_id='sha256:'+'a'*64,coordinator_image_id='sha256:'+'a'*64,initial_manifest=manifest.canonical_bytes().decode(),candidate=fixtures['candidate'],rollback=fixtures['rollback'],source_root=str(source),source_digest=source_tree_digest(str(source),deadline_monotonic=time.monotonic()+30),private_dir=str(private),journal_dir=str(journal),startup_wait_ms=0,timeout_seconds=90)
 raw=json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
 result=subprocess.run([sys.executable,'-I','-B','-c',CHILD,str(Path(__file__).resolve().parents[1])],input=raw,capture_output=True,timeout=30,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
 assert result.returncode==0, result.stderr.decode()
 assert json.loads(result.stdout)['status']=='completed'
 assert (journal/(owner+'.postexit-delete')).is_file()
