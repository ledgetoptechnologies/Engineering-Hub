import hashlib,hmac,json,os,pytest
from scripts.hub_local_test_launcher import DOMAIN,SCHEMA,_canonical,remove_after_coordinator_exit
from scripts.hub_local_test_volume import OwnedVolume,VolumeError,VolumeOutcomeUnknown
from engineering_hub.container_pilot_manifest import PilotManifest
from scripts.hub_local_baseline_bootstrap import _final_manifest
from tests.test_hub_container_pilot_manifest import make_manifest_data
TASK='db04aa59-54f0-4269-bcba-539c0151e13e'; NAME='hub-local-test-'+TASK
DAEMON='daemon-1234567890'; ROOT='/var/lib/docker'; MOUNT=ROOT+'/volumes/'+NAME+'/_data'
CID='c'*64; IMG='sha256:'+'a'*64; INITIAL=PilotManifest.from_bytes(json.dumps(make_manifest_data()).encode()); BACKUP='sha256:'+'f'*64; MAN=_final_manifest(INITIAL,BACKUP).digest(); NONCE='d'*64; KEY=b'k'*32
V=OwnedVolume(NAME,TASK,DAEMON,MOUNT)

class Docker:
 def __init__(self): self.calls=[]; self.present=False; self.lost=False
 def __call__(self,a):
  self.calls.append(a); op=a[3:]
  if op[0]=='info': return ROOT if 'DockerRootDir' in op[-1] else DAEMON
  if op[:2]==['volume','inspect']: return json.dumps([{'Name':NAME,'Driver':'local','Scope':'local','Mountpoint':MOUNT,'Options':{},'Labels':{'engineering-hub.local-test-owner':TASK}}])
  if op[0]=='ps': return CID if self.present else ''
  if op[:2]==['volume','ls']: return ''
  if op[:2]==['volume','rm']:
   if self.lost: raise TimeoutError
   return NAME
  raise AssertionError(op)

@pytest.fixture
def receipt(tmp_path):
 if os.geteuid()!=0: pytest.skip('synthetic owner mapping needs root')
 p=tmp_path/'receipt'; p.mkdir(mode=0o700); os.chown(p,10001,10001)
 payload=dict(schema=SCHEMA,task_id=TASK,daemon_id=DAEMON,docker_root=ROOT,volume_name=NAME,volume_mount=MOUNT,coordinator_id=CID,coordinator_image_id=IMG,volume_storage='local-v1',volume_options={},initial_manifest_digest=INITIAL.digest(),baseline_backup_digest=BACKUP,manifest_digest=MAN,nonce=NONCE,containers=[])
 envelope={'payload':payload,'signature':hmac.new(KEY,DOMAIN+_canonical(payload),hashlib.sha256).hexdigest()}
 f=p/'cleanup-receipt.json'; f.write_bytes(_canonical(envelope)); f.chmod(0o600); os.chown(f,10001,10001)
 fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY); st=os.fstat(fd)
 yield fd,(st.st_dev,st.st_ino),f
 os.close(fd)

def call(receipt,tmp_path,docker,**kwargs):
 fd,identity,_=receipt
 return remove_after_coordinator_exit(volume=V,coordinator_id=CID,coordinator_image_id=IMG,initial_manifest=INITIAL,nonce=NONCE,key=KEY,receipt_directory_fd=fd,receipt_directory_identity=identity,journal_dir=str(tmp_path),run=docker,**kwargs)

def test_authenticated_absent_then_one_removal(receipt,tmp_path):
 d=Docker(); call(receipt,tmp_path,d)
 assert (tmp_path/(TASK+'.postexit-delete')).exists()
 with pytest.raises(VolumeError): call(receipt,tmp_path,d)
 assert sum(a[3:5]==['volume','rm'] for a in d.calls)==1

def test_live_coordinator_prevents_delete(receipt,tmp_path):
 d=Docker(); d.present=True
 with pytest.raises(VolumeError): call(receipt,tmp_path,d)
 assert not (tmp_path/(TASK+'.postexit-delete')).exists()

def test_tampered_receipt_denied(receipt,tmp_path):
 fd,_,f=receipt; os.chown(f,0,0); f.write_bytes(b'{}'); os.chown(f,10001,10001)
 d=Docker()
 with pytest.raises(VolumeError): call(receipt,tmp_path,d)
 assert not any(a[3:5]==['volume','rm'] for a in d.calls)

def test_lost_delete_is_held_and_not_retried(receipt,tmp_path):
 d=Docker(); d.lost=True
 with pytest.raises(VolumeOutcomeUnknown): call(receipt,tmp_path,d)
 with pytest.raises(VolumeError): call(receipt,tmp_path,d)
 assert sum(a[3:5]==['volume','rm'] for a in d.calls)==1

def test_fifo_receipt_denied_without_blocking(receipt,tmp_path):
 fd,_,f=receipt; f.unlink(); os.mkfifo(f,0o600); os.chown(f,10001,10001)
 with pytest.raises(VolumeError): call(receipt,tmp_path,Docker())

def test_foreign_volume_name_denied_before_any_docker(receipt,tmp_path):
 fd,identity,_=receipt; d=Docker(); bad=OwnedVolume('unrelated',TASK,DAEMON,MOUNT)
 with pytest.raises(VolumeError): remove_after_coordinator_exit(volume=bad,coordinator_id=CID,coordinator_image_id=IMG,initial_manifest=INITIAL,nonce=NONCE,key=KEY,receipt_directory_fd=fd,receipt_directory_identity=identity,journal_dir=str(tmp_path),run=d)
 assert d.calls==[]

def test_changed_receipt_metadata_during_read_denied(receipt,tmp_path,monkeypatch):
 import scripts.hub_local_test_launcher as launcher
 fd,_,f=receipt; original=launcher.os.stat; changed=False
 def drift(path,*a,**kw):
  nonlocal changed
  if path=='cleanup-receipt.json' and not changed:
   changed=True; f.chmod(0o644)
  return original(path,*a,**kw)
 monkeypatch.setattr(launcher.os,'stat',drift)
 d=Docker()
 with pytest.raises(VolumeError): call(receipt,tmp_path,d)
 assert not any(a[3:5]==['volume','rm'] for a in d.calls)

def test_actual_writer_to_consumer_compatibility(tmp_path):
 import subprocess,sys
 if os.geteuid()!=0: pytest.skip('synthetic UID10001 required')
 for ancestor in (tmp_path,tmp_path.parent,tmp_path.parent.parent): ancestor.chmod(0o711)
 directory=tmp_path/'writer'; directory.mkdir(mode=0o700); os.chown(directory,10001,10001)
 script='''
import os,sys,types,threading
sys.path[:0]=['/src','/src/src']
from scripts.hub_local_cleanup_receipt import write_cleanup_receipt
from scripts.hub_local_native_coordinator import LocalNativeCoordinator
from scripts.hub_local_acceptance_components import LocalSource, LocalSuccessor
from scripts.hub_local_test_engine_broker import CleanedBrokerSnapshot
from scripts.hub_local_test_volume import OwnedVolume
from engineering_hub.container_pilot_manifest import PilotManifest
from scripts.hub_local_baseline_bootstrap import _final_manifest
from tests.test_hub_container_pilot_manifest import make_manifest_data
import json
T='db04aa59-54f0-4269-bcba-539c0151e13e'; N='hub-local-test-'+T; D='daemon-1234567890'; R='/var/lib/docker'; M=R+'/volumes/'+N+'/_data'; initial=PilotManifest.from_bytes(json.dumps(make_manifest_data()).encode()); backup='sha256:'+'f'*64; final=_final_manifest(initial,backup); manifest=final.digest()
v=OwnedVolume(N,T,D,M); ident='e'*64; name='hub-pilot-'+'a'*24+'-'+'b'*8+'-'+'c'*3+'-rollback'
cleaned=CleanedBrokerSnapshot(T,D,N,M,'f'*64,((ident,name),))
c=object.__new__(LocalNativeCoordinator); c._pid=os.getpid(); c._closed=True
source=object.__new__(LocalSource); source._pid=os.getpid(); source._disposed=True; source.thread=threading.Thread(); source.broker=object(); source.volume=v; source.manifest=initial
successor=object.__new__(LocalSuccessor); successor._pid=os.getpid(); successor._closed=True; successor.thread=threading.Thread(); successor._cleaned_snapshot=cleaned; successor._adapter=object(); successor.source_broker=source.broker; successor.volume=v; successor.manifest=final
c.client=types.SimpleNamespace(adapter=successor._adapter)
write_cleanup_receipt(directory=sys.argv[1],key=b'k'*32,nonce='d'*64,coordinator_id='c'*64,coordinator_image_id='sha256:'+'a'*64,initial_manifest_digest=initial.digest(),baseline_backup_digest=backup,manifest_digest=manifest,coordinator=c,source=source,successor=successor,cleaned=cleaned)
'''
 subprocess.run([sys.executable,'-I','-B','-c',script,str(directory)],user=10001,group=10001,extra_groups=(),check=True,timeout=10,capture_output=True)
 tmp_path.chmod(0o700)
 fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY); st=os.fstat(fd)
 try:
  d=Docker(); remove_after_coordinator_exit(volume=V,coordinator_id=CID,coordinator_image_id=IMG,initial_manifest=INITIAL,nonce=NONCE,key=KEY,receipt_directory_fd=fd,receipt_directory_identity=(st.st_dev,st.st_ino),journal_dir=str(tmp_path),run=d)
  assert sum(a[3:5]==['volume','rm'] for a in d.calls)==1
 finally: os.close(fd)

def test_signed_wrong_final_manifest_denied(receipt,tmp_path):
 fd,_,f=receipt; obj=json.loads(f.read_bytes()); obj['payload']['manifest_digest']='sha256:'+'0'*64
 obj['signature']=hmac.new(KEY,DOMAIN+_canonical(obj['payload']),hashlib.sha256).hexdigest()
 os.chown(f,0,0); f.write_bytes(_canonical(obj)); os.chown(f,10001,10001)
 d=Docker()
 with pytest.raises(VolumeError): call(receipt,tmp_path,d)
 assert not any(a[3:5]==['volume','rm'] for a in d.calls)

def test_signed_wrong_initial_manifest_denied(receipt,tmp_path):
 fd,_,f=receipt; obj=json.loads(f.read_bytes()); obj['payload']['initial_manifest_digest']='sha256:'+'0'*64
 obj['signature']=hmac.new(KEY,DOMAIN+_canonical(obj['payload']),hashlib.sha256).hexdigest()
 os.chown(f,0,0); f.write_bytes(_canonical(obj)); os.chown(f,10001,10001)
 d=Docker()
 with pytest.raises(VolumeError): call(receipt,tmp_path,d)
 assert not any(a[3:5]==['volume','rm'] for a in d.calls)

class KeeperDocker(Docker):
 def __init__(self): super().__init__(); self.state='created'
 def __call__(self,a):
  from scripts.hub_local_test_volume import TMPFS_OPTIONS
  from scripts.hub_local_test_launcher import _KEEPER_SCRIPT
  self.calls.append(a); op=a[3:]
  if op[0]=='info': return ROOT if 'DockerRootDir' in op[-1] else DAEMON
  if op[:2]==['volume','inspect']: return json.dumps([{'Name':NAME,'Driver':'local','Scope':'local','Mountpoint':MOUNT,'Options':dict(TMPFS_OPTIONS),'Labels':{'engineering-hub.local-test-owner':TASK}}])
  if op[:2]==['image','inspect']:
   if 'Volumes' in op[-2]:
    assert op[-2]=='{{json (index .Config "Volumes")}}'; return 'null'
   return IMG
  if op[0]=='ps': return ''
  if op[0]=='create': return 'a'*64
  if op[0]=='inspect':
   if '.Config.User' in op[2]: return json.dumps('10001:10001')
   if '.Config.Entrypoint' in op[2]: return json.dumps(['/usr/local/bin/python'])
   if '.Config.Cmd' in op[2]: return json.dumps(['-I','-B','-c',_KEEPER_SCRIPT])
   if '.Config.Volumes' in op[2]: return 'null'
   if '.State}}' in op[2]: return json.dumps({'Status':self.state,'Running':self.state=='running','Pid':123 if self.state=='running' else 0,'Paused':False,'Restarting':False,'Dead':False})
   if 'HostConfig' in op[2]: return json.dumps({'PidMode':'','IpcMode':'private','NetworkMode':'none','ReadonlyRootfs':True,'Privileged':False,'CapDrop':['ALL'],'CapAdd':None,'SecurityOpt':['no-new-privileges'],'LogConfig':{'Type':'local','Config':{'max-size':'1m','max-file':'2'}},'Memory':67108864,'MemorySwap':67108864,'NanoCpus':100000000,'PidsLimit':16,'Binds':None,'Devices':None,'DeviceRequests':None,'VolumesFrom':None,'AutoRemove':False,'PublishAllPorts':False,'PortBindings':None,'RestartPolicy':{'Name':'no'},'Mounts':[{'Type':'volume','Source':NAME,'Target':'/fixture','VolumeOptions':{'NoCopy':True}}]})
   if 'Mounts' in op[2]: return json.dumps([{'Type':'volume','Name':NAME,'Source':MOUNT,'Driver':'local','Destination':'/fixture','RW':True}])
   return '|'.join(['a'*64,'/hub-local-keeper-'+TASK,IMG,TASK,self.state])
  if op[0]=='start': self.state='running'; return 'a'*64
  if op[0]=='stop': self.state='exited'; return 'a'*64
  if op[0]=='rm': return 'a'*64
  raise AssertionError(op)

def test_tmpfs_keeper_create_start_and_recheck(tmp_path):
 from scripts.hub_local_test_volume import TMPFS_OPTIONS
 from scripts.hub_local_test_launcher import start_tmpfs_keeper,check_tmpfs_keeper
 volume=OwnedVolume(NAME,TASK,DAEMON,MOUNT,TMPFS_OPTIONS); d=KeeperDocker()
 keeper=start_tmpfs_keeper(volume=volume,helper_image_id=IMG,journal_dir=str(tmp_path),run=d)
 check_tmpfs_keeper(keeper,volume=volume,run=d)
 assert (tmp_path/(TASK+'.keeper-create')).exists() and (tmp_path/(TASK+'.keeper-start')).exists()
 assert not any(a[3]=='stop' for a in d.calls)

def test_tmpfs_initializer_denies_without_keeper(tmp_path):
 from scripts.hub_local_test_volume import TMPFS_OPTIONS
 from scripts.hub_local_volume_initializer import initialize_owned_volume
 volume=OwnedVolume(NAME,TASK,DAEMON,MOUNT,TMPFS_OPTIONS); d=KeeperDocker()
 with pytest.raises(VolumeError): initialize_owned_volume(volume=volume,helper_image_id=IMG,journal_dir=str(tmp_path),run=d)
 assert not any(a[3]=='create' for a in d.calls)

def test_keeper_stop_requires_authenticated_postexit_receipt(receipt,tmp_path):
 from scripts.hub_local_test_volume import TMPFS_OPTIONS
 from scripts.hub_local_test_launcher import TmpfsKeeper,stop_tmpfs_keeper
 fd,identity,f=receipt; obj=json.loads(f.read_bytes())
 obj['payload']['volume_storage']='tmpfs-512m-v1'; obj['payload']['volume_options']=dict(TMPFS_OPTIONS)
 obj['signature']=hmac.new(KEY,DOMAIN+_canonical(obj['payload']),hashlib.sha256).hexdigest()
 os.chown(f,0,0); f.write_bytes(_canonical(obj)); os.chown(f,10001,10001)
 d=KeeperDocker(); d.state='running'; volume=OwnedVolume(NAME,TASK,DAEMON,MOUNT,TMPFS_OPTIONS)
 keeper=TmpfsKeeper('a'*64,IMG,volume)
 stop_tmpfs_keeper(keeper=keeper,initial_manifest=INITIAL,coordinator_id=CID,coordinator_image_id=IMG,nonce=NONCE,key=KEY,receipt_directory_fd=fd,receipt_directory_identity=identity,journal_dir=str(tmp_path),run=d)
 assert (tmp_path/(TASK+'.keeper-stop')).exists() and (tmp_path/(TASK+'.keeper-remove')).exists()
 assert len([a for a in d.calls if a[3]=='stop'])==1 and len([a for a in d.calls if a[3]=='rm'])==1

def test_keeper_stop_denies_wrong_receipt_before_effect(receipt,tmp_path):
 from scripts.hub_local_test_volume import TMPFS_OPTIONS
 from scripts.hub_local_test_launcher import TmpfsKeeper,stop_tmpfs_keeper
 fd,identity,_=receipt; d=KeeperDocker(); d.state='running'; keeper=TmpfsKeeper('a'*64,IMG,OwnedVolume(NAME,TASK,DAEMON,MOUNT,TMPFS_OPTIONS))
 with pytest.raises(VolumeError): stop_tmpfs_keeper(keeper=keeper,initial_manifest=INITIAL,coordinator_id=CID,coordinator_image_id=IMG,nonce=NONCE,key=KEY,receipt_directory_fd=fd,receipt_directory_identity=identity,journal_dir=str(tmp_path),run=d)
 assert not any(a[3] in ('stop','rm') for a in d.calls)

def test_keeper_start_loss_is_held_without_cleanup_or_retry(tmp_path):
 from scripts.hub_local_test_volume import TMPFS_OPTIONS
 from scripts.hub_local_test_launcher import start_tmpfs_keeper
 volume=OwnedVolume(NAME,TASK,DAEMON,MOUNT,TMPFS_OPTIONS); d=KeeperDocker(); attempts=[]
 def lost(a):
  if a[3]=='start': attempts.append(1); raise TimeoutError
  return d(a)
 with pytest.raises(VolumeOutcomeUnknown): start_tmpfs_keeper(volume=volume,helper_image_id=IMG,journal_dir=str(tmp_path),run=lost)
 with pytest.raises(VolumeError): start_tmpfs_keeper(volume=volume,helper_image_id=IMG,journal_dir=str(tmp_path),run=lost)
 assert len(attempts)==1 and (tmp_path/(TASK+'.keeper-start')).exists()
 assert not any(a[3] in ('stop','rm') for a in d.calls)

def test_keeper_exited_with_nonzero_pid_is_rejected():
 from scripts.hub_local_test_volume import TMPFS_OPTIONS
 from scripts.hub_local_test_launcher import TmpfsKeeper,check_tmpfs_keeper
 d=KeeperDocker(); d.state='exited'; keeper=TmpfsKeeper('a'*64,IMG,OwnedVolume(NAME,TASK,DAEMON,MOUNT,TMPFS_OPTIONS))
 def bad(a):
  result=d(a)
  if a[3]=='inspect' and '.State}}' in a[5]:
   v=json.loads(result); v['Pid']=42; return json.dumps(v)
  return result
 with pytest.raises(ValueError): check_tmpfs_keeper(keeper,volume=keeper.volume,run=bad,state='exited')

def test_private_input_transport_bounds_and_timeout():
 import sys,time
 from scripts.hub_local_test_launcher import _call_with_input
 _call_with_input([sys.executable,'-c','import sys; assert sys.stdin.buffer.read()==b"secret"'],b'secret',time.monotonic()+5)
 with pytest.raises(TimeoutError):
  _call_with_input([sys.executable,'-c','import time; time.sleep(5)'],b'',time.monotonic()+0.1)

@pytest.mark.parametrize('socket_group',[None,0])
def test_coordinator_launcher_orders_effects_and_keeps_key_off_argv(tmp_path,monkeypatch,socket_group):
 import time,types,stat as stmod
 import scripts.hub_local_test_launcher as l
 import scripts.hub_local_volume_initializer as init
 import scripts.hub_local_acceptance_runner as ar
 import scripts.hub_local_test_oci as oci
 monkeypatch.setattr(l,'_check_resource_envelope',lambda manifest:None)
 monkeypatch.setattr(ar,'_bounded_volume',lambda value:None)
 monkeypatch.setattr(ar,'_fixture',lambda value:value)
 monkeypatch.setattr(oci,'verify_fixture_image',lambda *a:None)
 from scripts.hub_local_test_volume import TMPFS_OPTIONS
 if os.geteuid()!=0: pytest.skip('synthetic UID ownership requires root')
 private=tmp_path/'private'; private.mkdir(mode=0o700); os.chown(private,10001,10001)
 vj=tmp_path/'volume-journal'; vj.mkdir(mode=0o700); os.chown(vj,10001,10001)
 journal=tmp_path/'journal'; journal.mkdir(mode=0o700)
 v=OwnedVolume(NAME,TASK,DAEMON,MOUNT,TMPFS_OPTIONS); keeper=l.TmpfsKeeper('a'*64,IMG,v)
 calls=[]; state=['created']; cid='f'*64; command=[]
 monkeypatch.setattr(l,'source_tree_digest',lambda root,**kwargs:'sha256:'+'a'*64)
 monkeypatch.setattr(l,'check_tmpfs_keeper',lambda *a,**k:calls.append('keeper'))
 monkeypatch.setattr(init,'initialize_owned_volume',lambda **kw:calls.append('initialize'))
 monkeypatch.setattr(l,'stop_tmpfs_keeper',lambda **kw:calls.append('stop-keeper'))
 monkeypatch.setattr(l,'remove_after_coordinator_exit',lambda **kw:calls.append('remove-volume'))
 original_stat=l.os.stat; original_realpath=l.os.path.realpath
 def fake_stat(path,*args,**kwargs):
  if str(path)=='/var/run/docker.sock': return types.SimpleNamespace(st_mode=stmod.S_IFSOCK|(0o600 if socket_group is None else 0o660),st_uid=10001 if socket_group is None else 0,st_gid=10001 if socket_group is None else 0,st_dev=1,st_ino=2)
  return original_stat(path,*args,**kwargs)
 monkeypatch.setattr(l.os,'stat',fake_stat)
 monkeypatch.setattr(l.os.path,'realpath',lambda p,*a,**k:'/run/docker.sock' if str(p)=='/var/run/docker.sock' else original_realpath(p,*a,**k))
 def run(a):
  op=a[3:]; calls.append(op[0]);
  if op[:2]==['image','inspect']:
   if 'Volumes' in op[-2]:
    assert op[-2]=='{{json (index .Config "Volumes")}}'; return 'null'
   return IMG
  if op[0]=='ps': return ''
  if op[0]=='create':
   assert '--pull=never' in a and all('cleanup_key' not in x for x in a)
   assert ('--group-add' in a)==(socket_group is not None)
   if socket_group is not None: assert a[a.index('--group-add')+1]=='0'
   command.extend(a[-4:])
   return cid
  if op[0]=='inspect':
   if '.State}}' in op[2]: return json.dumps({'Status':state[0],'Running':False,'Pid':0,'Paused':False,'Restarting':False,'Dead':False,'ExitCode':0})
   if '.Config.User' in op[2]: return json.dumps('10001:10001')
   if '.Config.Entrypoint' in op[2]: return json.dumps(['/opt/validation-venv/bin/python'])
   if '.Config.Cmd' in op[2]: return json.dumps(command)
   if '.Config.Volumes' in op[2]: return 'null'
   if 'HostConfig' in op[2]: return json.dumps({'GroupAdd':None if socket_group is None else ['0'],'NetworkMode':'none','ReadonlyRootfs':True,'Privileged':False,'CapDrop':['ALL'],'CapAdd':None,'SecurityOpt':['no-new-privileges'],'Memory':1073741824,'MemorySwap':1073741824,'NanoCpus':1000000000,'PidsLimit':256,'Tmpfs':{'/tmp':'rw,noexec,nosuid,nodev,size=64m'},'LogConfig':{'Type':'local','Config':{'max-size':'1m','max-file':'2'}},'AutoRemove':False,'PublishAllPorts':False,'Binds':None,'Devices':None,'DeviceRequests':None,'VolumesFrom':None,'PortBindings':None,'PidMode':'','IpcMode':'private','RestartPolicy':{'Name':'no'},'Mounts':[{'Type':'volume','Source':NAME,'Target':MOUNT,'VolumeOptions':{'NoCopy':True}}]})
   if 'Mounts' in op[2]: return json.dumps([{'Type':'volume','Driver':'local','Name':NAME,'Source':MOUNT,'Destination':MOUNT,'RW':True},{'Type':'bind','Propagation':'rprivate','Source':'/src','Destination':'/opt/hub','RW':False},{'Type':'bind','Propagation':'rprivate','Source':str(private),'Destination':str(private),'RW':True},{'Type':'bind','Propagation':'rprivate','Source':'/run/docker.sock','Destination':'/var/run/docker.sock','RW':True}])
   return '|'.join([cid,'/hub-local-coordinator-'+TASK,IMG,TASK,state[0],'0'])
  if op[0]=='rm': return cid
  raise AssertionError(op)
 def start(args,data,deadline):
  request=json.loads(data); assert request['coordinator_id']==cid and len(request['cleanup_key'])==64
  assert request['cleanup_key'] not in ' '.join(args)
  calls.append('start'); state[0]='exited'
 result=l.launch_initialized_fixture(volume=v,keeper=keeper,helper_image_id=IMG,coordinator_image_id=IMG,initial_manifest=INITIAL,candidate={},rollback={},source_root='/src',source_digest='sha256:'+'a'*64,private_dir=str(private),journal_dir=str(journal),startup_wait_ms=0,socket_group=socket_group,deadline_monotonic=time.monotonic()+30,run=run,run_input=start)
 assert result=={'status':'completed','coordinator_id':cid}
 assert calls.index('initialize') < calls.index('create') < calls.index('start') < calls.index('rm') < calls.index('stop-keeper') < calls.index('remove-volume')

def test_coordinator_socket_permission_denied_before_effect(tmp_path,monkeypatch):
 import time,types,stat as stmod
 import scripts.hub_local_test_launcher as l
 import scripts.hub_local_acceptance_runner as ar
 import scripts.hub_local_test_oci as oci
 from scripts.hub_local_test_volume import TMPFS_OPTIONS
 if os.geteuid()!=0: pytest.skip('synthetic UID ownership requires root')
 monkeypatch.setattr(l,'_check_resource_envelope',lambda manifest:None)
 monkeypatch.setattr(ar,'_bounded_volume',lambda value:None)
 monkeypatch.setattr(ar,'_bounded_volume',lambda value:None)
 monkeypatch.setattr(ar,'_fixture',lambda value:value); monkeypatch.setattr(oci,'verify_fixture_image',lambda *a:None)
 monkeypatch.setattr(l,'source_tree_digest',lambda root,**kwargs:'sha256:'+'a'*64)
 private=tmp_path/'private'; private.mkdir(mode=0o700); os.chown(private,10001,10001)
 vj=tmp_path/'vj'; vj.mkdir(mode=0o700); os.chown(vj,10001,10001)
 journal=tmp_path/'journal'; journal.mkdir(mode=0o700)
 volume=OwnedVolume(NAME,TASK,DAEMON,MOUNT,TMPFS_OPTIONS); keeper=l.TmpfsKeeper('a'*64,IMG,volume)
 original=l.os.stat
 def fake(path,*a,**kw):
  if str(path)=='/var/run/docker.sock': return types.SimpleNamespace(st_mode=stmod.S_IFSOCK|0o660,st_uid=0,st_gid=999)
  return original(path,*a,**kw)
 monkeypatch.setattr(l.os,'stat',fake); calls=[]
 with pytest.raises(VolumeError,match='inaccessible'):
  l.launch_initialized_fixture(volume=volume,keeper=keeper,helper_image_id=IMG,coordinator_image_id=IMG,initial_manifest=INITIAL,candidate={},rollback={},source_root='/src',source_digest='sha256:'+'a'*64,private_dir=str(private),journal_dir=str(journal),startup_wait_ms=0,deadline_monotonic=time.monotonic()+30,run=lambda a:calls.append(a))
 assert calls==[]

def test_socket_group_is_explicit_exact_and_pins_identity(monkeypatch):
 import types,stat as stmod
 import scripts.hub_local_test_launcher as l
 state=types.SimpleNamespace(st_mode=stmod.S_IFSOCK|0o660,st_uid=0,st_gid=0,st_dev=7,st_ino=8)
 monkeypatch.setattr(l.os.path,'realpath',lambda path:'/run/docker.sock')
 monkeypatch.setattr(l.os,'stat',lambda path:state)
 with pytest.raises(VolumeError): l._socket_pin(None)
 with pytest.raises(VolumeError): l._socket_pin(1)
 with pytest.raises(VolumeError): l._socket_pin(False)
 pin=l._socket_pin(0)
 assert pin==('/run/docker.sock',7,8,0,0,0o660)
 state.st_ino=9
 assert l._socket_pin(0)!=pin
 state.st_mode=stmod.S_IFSOCK|0o666
 with pytest.raises(VolumeError): l._socket_pin(0)

@pytest.mark.parametrize('groups,expected',[([0],0),([0,10001],0),([],1),([0,7],1)])
def test_coordinator_bootstrap_rejects_missing_or_extra_groups(groups,expected):
 import subprocess,sys
 from scripts.hub_local_test_launcher import _coordinator_bootstrap
 prefix=('import os;os.getuid=os.geteuid=os.getgid=os.getegid=lambda:10001;'
         'os.getgroups=lambda:'+repr(groups)+';os.dup2=lambda *a:None;'
         'os.set_inheritable=lambda *a:None;os.execv=lambda *a:None;')
 result=subprocess.run([sys.executable,'-I','-B','-c',prefix+_coordinator_bootstrap(0)],capture_output=True,timeout=5)
 assert result.returncode==expected


def test_aggregate_resource_envelope_fails_large_and_accepts_small():
 from scripts.hub_local_test_launcher import _check_resource_envelope
 with pytest.raises(VolumeError): _check_resource_envelope(INITIAL)
 data=make_manifest_data(); data['runtime']['resources']['cpu_millicores']=300; data['runtime']['resources']['pids_limit']=80
 small=PilotManifest.from_bytes(json.dumps(data).encode())
 _check_resource_envelope(small)

def test_source_digest_covers_bytes_and_rejects_symlink(tmp_path):
 import time
 from scripts.hub_local_test_launcher import source_tree_digest
 root=tmp_path/'tree'; root.mkdir(); (root/'module.py').write_bytes(b'first')
 before=source_tree_digest(str(root),deadline_monotonic=time.monotonic()+5)
 (root/'module.py').write_bytes(b'second')
 assert source_tree_digest(str(root),deadline_monotonic=time.monotonic()+5)!=before
 (root/'link').symlink_to(root/'module.py')
 with pytest.raises(ValueError): source_tree_digest(str(root),deadline_monotonic=time.monotonic()+5)

def test_outer_runner_denies_ungranted_socket_before_volume_create(tmp_path,monkeypatch):
 import time,types,stat as stmod
 import scripts.hub_local_test_launcher as l
 import scripts.hub_local_acceptance_runner as ar
 import scripts.hub_local_test_oci as oci
 import scripts.hub_local_test_volume as volumes
 if os.geteuid()!=0: pytest.skip('synthetic UID ownership requires root')
 monkeypatch.setattr(l,'_check_resource_envelope',lambda manifest:None)
 monkeypatch.setattr(l,'source_tree_digest',lambda *a,**k:'sha256:'+'a'*64)
 monkeypatch.setattr(ar,'_fixture',lambda v:v); monkeypatch.setattr(ar,'_bounded_volume',lambda p:None)
 monkeypatch.setattr(oci,'verify_fixture_image',lambda *a:(object(),))
 private=tmp_path/'private'; private.mkdir(mode=0o700); os.chown(private,10001,10001)
 vj=tmp_path/'vj'; vj.mkdir(mode=0o700); os.chown(vj,10001,10001)
 original=l.os.stat
 def fake(path,*a,**kw):
  if str(path)=='/var/run/docker.sock': return types.SimpleNamespace(st_mode=stmod.S_IFSOCK|0o660,st_uid=0,st_gid=999)
  return original(path,*a,**kw)
 monkeypatch.setattr(l.os,'stat',fake); called=[]
 monkeypatch.setattr(volumes,'create_volume',lambda **kw:called.append(kw))
 with pytest.raises(VolumeError):
  l.run_local_acceptance(task_id=TASK,daemon_id=DAEMON,helper_image_id=IMG,coordinator_image_id=IMG,
   initial_manifest=INITIAL,candidate={},rollback={},source_root='/src',source_digest='sha256:'+'a'*64,
   private_dir=str(private),journal_dir=str(tmp_path),startup_wait_ms=0,
   deadline_monotonic=time.monotonic()+30,run=lambda x:'')
 assert called==[]

def test_source_digest_changes_with_mode(tmp_path):
 import time
 from scripts.hub_local_test_launcher import source_tree_digest
 root=tmp_path/'tree'; root.mkdir(); f=root/'module.py'; f.write_bytes(b'same'); f.chmod(0o600)
 before=source_tree_digest(str(root),deadline_monotonic=time.monotonic()+5)
 f.chmod(0o700)
 assert source_tree_digest(str(root),deadline_monotonic=time.monotonic()+5)!=before

def test_path_overlap_denied_before_effect(tmp_path):
 from scripts.hub_local_test_launcher import _validate_local_paths
 source=tmp_path/'source'; private=tmp_path/'private'; journal=tmp_path/'journal'
 for p in (source,private,journal): p.mkdir()
 with pytest.raises(VolumeError): _validate_local_paths(str(source),str(source),str(journal),'/some/volume')

def test_outer_missing_helper_image_denies_before_volume_create(tmp_path,monkeypatch):
 import time,types,stat as stmod
 import scripts.hub_local_test_launcher as l
 import scripts.hub_local_acceptance_runner as ar
 import scripts.hub_local_test_oci as oci
 import scripts.hub_local_test_volume as volumes
 if os.geteuid()!=0: pytest.skip('synthetic UID ownership requires root')
 monkeypatch.setattr(l,'_check_resource_envelope',lambda manifest:None)
 monkeypatch.setattr(l,'source_tree_digest',lambda *a,**k:'sha256:'+'a'*64)
 monkeypatch.setattr(ar,'_fixture',lambda v:v); monkeypatch.setattr(ar,'_bounded_volume',lambda p:None)
 monkeypatch.setattr(oci,'verify_fixture_image',lambda *a:(object(),))
 private=tmp_path/'private'; private.mkdir(mode=0o700); os.chown(private,10001,10001)
 vj=tmp_path/'vj'; vj.mkdir(mode=0o700); os.chown(vj,10001,10001)
 journal=tmp_path/'journal'; journal.mkdir(mode=0o700)
 original=l.os.stat
 def fake(path,*a,**kw):
  if str(path)=='/var/run/docker.sock': return types.SimpleNamespace(st_mode=stmod.S_IFSOCK|0o600,st_uid=10001,st_gid=10001,st_dev=1,st_ino=2)
  return original(path,*a,**kw)
 monkeypatch.setattr(l.os,'stat',fake); created=[]
 monkeypatch.setattr(volumes,'create_volume',lambda **kw:created.append(kw))
 def run(a):
  if a[3]=='info': return ROOT if 'DockerRootDir' in a[-1] else DAEMON
  if a[3:5]==['image','inspect']: raise FileNotFoundError
  raise AssertionError(a)
 with pytest.raises(VolumeError):
  l.run_local_acceptance(task_id=TASK,daemon_id=DAEMON,helper_image_id=IMG,coordinator_image_id=IMG,
   initial_manifest=INITIAL,candidate={},rollback={},source_root='/src',source_digest='sha256:'+'a'*64,
   private_dir=str(private),journal_dir=str(journal),startup_wait_ms=0,
   deadline_monotonic=time.monotonic()+30,run=run)
 assert created==[]

def test_input_transport_selector_failure_reaps_and_closes_all(monkeypatch):
 import selectors,subprocess,time
 from scripts.hub_local_test_launcher import _call_with_input
 closed=[]; events=[]
 class Pipe:
  def __init__(self,name): self.name=name; self.closed=False
  def close(self):
   closed.append(self.name); self.closed=True
   if self.name=='stdin': raise OSError('close failed')
 class Process:
  def __init__(self): self.stdin=Pipe('stdin'); self.stdout=Pipe('stdout'); self.stderr=Pipe('stderr')
  def poll(self): return None
  def kill(self): events.append('kill')
  def wait(self,timeout=None): events.append('wait'); return 0
 proc=Process(); monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:proc)
 def fail(): raise ValueError('selector unavailable')
 monkeypatch.setattr(selectors,'DefaultSelector',fail)
 with pytest.raises(ValueError,match='selector unavailable'): _call_with_input(['unused'],b'',time.monotonic()+2)
 assert events==['kill','wait'] and closed==['stdin','stdout','stderr']

def _launch_request():
 return {'schema':'engineering-hub.local-launch-request.v1','task_id':TASK,'daemon_id':DAEMON,
  'helper_image_id':IMG,'coordinator_image_id':IMG,'initial_manifest':INITIAL.canonical_bytes().decode(),
  'candidate':{},'rollback':{},'source_root':'/snapshot','source_digest':'sha256:'+'f'*64,
  'private_dir':'/private','journal_dir':'/journal','startup_wait_ms':0,'timeout_seconds':120}

def test_fixed_fd3_entry_accepts_closed_request(monkeypatch,capsys):
 import scripts.hub_local_test_launcher as launcher
 import scripts.hub_local_acceptance_runner as runner
 req=_launch_request(); monkeypatch.setattr(runner,'_read_request',lambda fd:req)
 closed=[]; monkeypatch.setattr(launcher.os,'close',lambda fd:closed.append(fd))
 seen=[]
 def fake(**kwargs): seen.append(kwargs); return {'status':'completed','coordinator_id':CID}
 monkeypatch.setattr(launcher,'run_local_acceptance',fake)
 assert launcher.main(['--request-fd','3'])==0
 assert json.loads(capsys.readouterr().out)=={'status':'completed','coordinator_id':CID}
 assert seen[0]['initial_manifest'].digest()==INITIAL.digest() and closed==[3]

def test_fixed_fd3_entry_rejects_extra_field_before_effect(monkeypatch,capsys):
 import scripts.hub_local_test_launcher as launcher
 import scripts.hub_local_acceptance_runner as runner
 req=_launch_request(); req['command']='unexpected'; monkeypatch.setattr(runner,'_read_request',lambda fd:req)
 closed=[]; monkeypatch.setattr(launcher.os,'close',lambda fd:closed.append(fd))
 monkeypatch.setattr(launcher,'run_local_acceptance',lambda **kwargs:pytest.fail('unexpected effect'))
 assert launcher.main(['--request-fd','3'])==1
 assert 'preflight' in capsys.readouterr().err and closed==[3]

def test_actual_isolated_fd3_entry_rejects_invalid_request_without_daemon():
 import subprocess,sys
 from pathlib import Path
 path=str(Path(__file__).parents[1]/'scripts'/'hub_local_test_launcher.py')
 result=subprocess.run(['/bin/sh','-c','exec 3<&0; exec "$1" -I -B "$2" --request-fd 3','sh',sys.executable,path],input=b'{}',capture_output=True,timeout=10)
 assert result.returncode==1 and result.stdout==b'' and b'preflight unavailable' in result.stderr

def test_fixed_fd3_entry_closes_descriptor_after_read_error(monkeypatch,capsys):
 import scripts.hub_local_test_launcher as launcher
 import scripts.hub_local_acceptance_runner as runner
 def fail(fd): raise ValueError('private sentinel')
 monkeypatch.setattr(runner,'_read_request',fail); closed=[]
 monkeypatch.setattr(launcher.os,'close',lambda fd:closed.append(fd))
 assert launcher.main(['--request-fd','3'])==1 and closed==[3]
 out=capsys.readouterr(); assert 'private sentinel' not in out.err
