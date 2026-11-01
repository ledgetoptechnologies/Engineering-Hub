import json
import pytest
from scripts.hub_local_test_volume import OwnedVolume, VolumeError, VolumeOutcomeUnknown
from scripts.hub_local_volume_initializer import initialize_owned_volume, LABEL, INIT_SCRIPT
OWNER='db04aa59-54f0-4269-bcba-539c0151e13e'
NAME='hub-local-test-'+OWNER
ID='b'*64
IMAGE='sha256:'+'c'*64
ROOT='/var/lib/docker'
V=OwnedVolume(NAME,OWNER,'daemon-1234567890',ROOT+'/volumes/'+NAME+'/_data')

class Docker:
 def __init__(self): self.calls=[]; self.fail=None; self.state='created'
 def __call__(self,a):
  self.calls.append(a); op=a[3:]
  if op[0]=='info': return ROOT if 'DockerRootDir' in op[-1] else V.daemon_id
  if op[:2]==['volume','inspect']: return json.dumps([{'Name':NAME,'Driver':'local','Scope':'local','Mountpoint':V.mountpoint,'Options':{},'Labels':{LABEL:OWNER}}])
  if op[:2]==['image','inspect']:
   if 'Volumes' in op[-2]:
    assert op[-2]=='{{json (index .Config "Volumes")}}'; return 'null'
   return IMAGE
  if op[0]=='ps': return ''
  if op[0]=='create':
   if self.fail=='create': raise TimeoutError
   return ID
  if op[0]=='inspect':
   if '.Config.User' in op[2]: return json.dumps('0:0')
   if '.Config.Entrypoint' in op[2]: return json.dumps(['/usr/local/bin/python'])
   if '.Config.Cmd' in op[2]: return json.dumps(['-I','-B','-c',INIT_SCRIPT])
   if '.Config.Volumes' in op[2]: return 'null'
   if 'HostConfig' in op[2]: return json.dumps({'NetworkMode':'none','ReadonlyRootfs':True,'Privileged':False,'CapDrop':['ALL'],'CapAdd':['CHOWN'],'LogConfig':{'Type':'local','Config':{'max-size':'1m','max-file':'2'}},'Memory':134217728,'MemorySwap':134217728,'NanoCpus':250000000,'PidsLimit':32,'Binds':None,'Devices':None,'DeviceRequests':None,'VolumesFrom':None,'IpcMode':'private','PidMode':'','AutoRemove':False,'PublishAllPorts':False,'PortBindings':None,'RestartPolicy':{'Name':'no'},'SecurityOpt':['no-new-privileges'],'Mounts':[{'Type':'volume','Source':NAME,'Target':'/fixture','VolumeOptions':{'NoCopy':True}}]})
   if 'Mounts' in op[2]: return json.dumps([{'Type':'volume','Name':NAME,'Source':V.mountpoint,'Driver':'local','Destination':'/fixture','RW':True}])
   return '|'.join([ID,'/hub-local-init-'+OWNER,IMAGE,OWNER,self.state,'0'])
  if op[0]=='start':
   if self.fail=='start': raise TimeoutError
   self.state='exited'; return ''
  if op[0]=='rm': return ID
  raise AssertionError(op)

def test_exact_one_shot_profile_and_intents(tmp_path):
 d=Docker(); assert initialize_owned_volume(volume=V,helper_image_id=IMAGE,journal_dir=str(tmp_path),run=d)==ID
 create=next(a for a in d.calls if 'create' in a)
 assert 'none' in create and '--read-only' in create and 'type=volume,src='+NAME+',dst=/fixture,volume-nocopy' in create
 assert all((tmp_path/(OWNER+s)).exists() for s in ('.init-create','.init-start','.init-remove'))

def test_image_declared_volumes_denied_before_create(tmp_path):
 d=Docker()
 def declared(a):
  if a[3:5]==['image','inspect'] and 'Volumes' in a[-2]:
   assert a[-2]=='{{json (index .Config "Volumes")}}'
   return json.dumps({'/unexpected':{}})
  return d(a)
 with pytest.raises(VolumeError): initialize_owned_volume(volume=V,helper_image_id=IMAGE,journal_dir=str(tmp_path),run=declared)
 assert not any('create' in a for a in d.calls)

def test_lost_create_retains_intent_and_never_starts(tmp_path):
 d=Docker(); d.fail='create'
 with pytest.raises(VolumeOutcomeUnknown): initialize_owned_volume(volume=V,helper_image_id=IMAGE,journal_dir=str(tmp_path),run=d)
 assert (tmp_path/(OWNER+'.init-create')).exists()
 assert not any('start' in a or 'rm' in a for a in d.calls)

def test_lost_start_never_removes(tmp_path):
 d=Docker(); d.fail='start'
 with pytest.raises(VolumeOutcomeUnknown): initialize_owned_volume(volume=V,helper_image_id=IMAGE,journal_dir=str(tmp_path),run=d)
 assert (tmp_path/(OWNER+'.init-start')).exists()
 assert not any('rm' in a for a in d.calls)

def test_nonempty_fixture_script_refuses_without_modification(tmp_path):
 import subprocess,sys
 (tmp_path/'keep').write_bytes(b'preserve')
 script=INIT_SCRIPT.replace("'/fixture'",repr(str(tmp_path)))
 result=subprocess.run([sys.executable,'-I','-B','-c',script],capture_output=True,timeout=5)
 assert result.returncode != 0 and (tmp_path/'keep').read_bytes()==b'preserve'
 assert sorted(x.name for x in tmp_path.iterdir())==['keep']

def test_wrong_effective_command_never_starts(tmp_path):
 d=Docker()
 def bad(a):
  if a[3]=='inspect' and '.Config.Cmd' in a[5]: return json.dumps(['unexpected'])
  return d(a)
 with pytest.raises(VolumeOutcomeUnknown): initialize_owned_volume(volume=V,helper_image_id=IMAGE,journal_dir=str(tmp_path),run=bad)
 assert not any('start' in a for a in d.calls)

def test_lost_remove_never_retries(tmp_path):
 d=Docker(); attempts=[]
 def lost(a):
  if a[3]=='rm': attempts.append(1); raise TimeoutError
  return d(a)
 with pytest.raises(VolumeOutcomeUnknown): initialize_owned_volume(volume=V,helper_image_id=IMAGE,journal_dir=str(tmp_path),run=lost)
 with pytest.raises(Exception): initialize_owned_volume(volume=V,helper_image_id=IMAGE,journal_dir=str(tmp_path),run=lost)
 assert len(attempts)==1 and (tmp_path/(OWNER+'.init-remove')).exists()

def test_empty_fixture_script_creates_exact_owned_directories(tmp_path):
 import os,stat,subprocess,sys
 if os.geteuid()!=0: pytest.skip('requires synthetic root-owned fixture')
 script=INIT_SCRIPT.replace("'/fixture'",repr(str(tmp_path)))
 result=subprocess.run([sys.executable,'-I','-B','-c',script],capture_output=True,timeout=5)
 assert result.returncode==0
 for path in (tmp_path,tmp_path/'generations',tmp_path/'generations'/'baseline-g1',tmp_path/'reservations'):
  st=path.stat(); assert (st.st_uid,st.st_gid,stat.S_IMODE(st.st_mode))==(10001,10001,0o700)
