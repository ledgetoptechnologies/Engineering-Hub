"""Full launcher FD3 to real coordinator using exclusively synthetic Engine."""
import base64,hashlib,json,os,shutil,subprocess,sys,time,uuid
from pathlib import Path
import pytest
from scripts.hub_local_test_oci import prepare_fixture_image
from scripts.hub_local_test_launcher import source_tree_digest
from tests.test_prepare_local_oci_layout import fixture,archive
from tests.test_hub_container_pilot_manifest import make_manifest_data
from engineering_hub.container_pilot_manifest import PilotManifest
from tests.test_hub_local_launcher_synthetic_e2e import CHILD

ACTUAL_START=r''' def start(self,args,data,deadline):
  import subprocess
  request=json.loads(data)
  boot='import os,sys;os.dup2(0,3);os.set_inheritable(3,True);os.execv(sys.executable,[sys.executable,"-I","-B",sys.argv[1],"--request-fd","3"])'
  result=subprocess.run([sys.executable,'-I','-B','-c',boot,os.path.join(self.source,'scripts/hub_local_acceptance_runner.py')],input=data,capture_output=True,timeout=120,user=10001,group=10001,extra_groups=(),env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
  if result.returncode!=0:
   print('synthetic coordinator exit',result.returncode,'stderr',result.stderr.decode()[-4000:],file=sys.stderr)
   raise RuntimeError('synthetic coordinator failed')
  assert json.loads(result.stdout)['state']=='rolled_back'
  self.states[args[-1]][0]='exited'
'''

SETUP=r'''e=Engine()
from scripts.hub_local_test_volume import OwnedVolume,TMPFS_OPTIONS
from scripts.hub_local_test_oci import verify_fixture_image
from scripts.hub_local_acceptance_runner import _fixture
from tests.local_synthetic_engine import SyntheticEngine
from tests.test_hub_container_executor_daemon_backend import oci_archive
from hub_container_executor.adapter import HostAdapter
from hub_container_executor.runtime_profile import derive_runtime_profile
from engineering_hub.container_pilot_manifest import PilotManifest
import threading
synthetic=None
def wrapped(**kw):
 global synthetic
 e.source=kw['source_root']; e.private=kw['private_dir']; e.root=sys.argv[2]; e.daemon='fixture-daemon'; e.mount=e.root+'/volumes/'+e.name+'/_data'
 e.configs={kw['candidate']['manifest_digest']:kw['candidate']['config_digest'],kw['rollback']['manifest_digest']:kw['rollback']['config_digest']}
 manifest=kw['initial_manifest']; fixtures={role:_fixture(kw[role]) for role in ('candidate','rollback')}
 images={role:verify_fixture_image(manifest,role,fixtures[role])[0] for role in fixtures}
 archives={role:oci_archive(image,dict(fixtures[role].blobs)) for role,image in images.items()}
 volume=OwnedVolume(e.name,e.owner,e.daemon,e.mount,TMPFS_OPTIONS)
 synthetic=SyntheticEngine(socket_path='/var/run/docker.sock',fake_root=e.root,volume=volume,owner=e.owner,images=images,archives=archives,manifest=manifest,secrets={'CODEX_TOKEN':'synthetic-codex','HERMES_TOKEN':'synthetic-hermes','CLAUDE_TOKEN':'synthetic-claude'},baseline_db=os.path.join(e.mount,'generations/baseline-g1/hub.sqlite3'),dynamic_names=True,baseline_uid_gid=(10001,10001))
 prefix=manifest.runtime.resource_name_prefix; name=prefix+'-00000000-000-rollback'; synthetic.names['baseline']=name; synthetic.by_name[name]='baseline'
 synthetic.profiles['baseline']=derive_runtime_profile(manifest,HostAdapter._defaults_from_record(images['rollback']),'baseline-g1',synthetic.secrets)
 synthetic.start()
 try:
  os.chown('/var/run/docker.sock',10001,10001); os.chmod('/var/run/docker.sock',0o600)
  marker=sys.argv[3]; st=os.lstat('/var/run/docker.sock'); fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
  try: os.write(fd,json.dumps([st.st_dev,st.st_ino]).encode()); os.fsync(fd)
  finally: os.close(fd)
  kw['run']=e; kw['run_input']=e.start
  result=orig(**kw)
  assert not synthetic.containers
  return result
 finally: synthetic.close()
l.run_local_acceptance=wrapped
'''


def test_fixed_launcher_actual_coordinator_with_synthetic_engine(tmp_path,request):
 if os.environ.get('HUB_LOCAL_LAUNCHER_E2E')!='1': pytest.skip('explicit isolated QA opt-in required')
 if os.geteuid()!=0 or os.getpid()!=1: pytest.skip('dedicated root PID1 QA container required')
 import ctypes
 def require_owned_tmpfs(path):
  fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
  try:
   buf=ctypes.create_string_buffer(256); libc=ctypes.CDLL(None,use_errno=True)
   fs=os.fstatvfs(fd)
   if libc.fstatfs(fd,ctypes.byref(buf))!=0 or ctypes.c_long.from_buffer(buf).value!=0x01021994 or fs.f_frsize*fs.f_blocks>8*1024*1024 or not 0<fs.f_files<=4096:
    pytest.skip('bounded isolated QA tmpfs required')
  finally: os.close(fd)
 for path in ('/usr/bin','/run'): require_owned_tmpfs(path)
 if os.path.realpath('/var/run')!='/run' or os.path.lexists('/var/run/docker.sock') or os.path.lexists('/usr/bin/docker'):
  pytest.skip('fresh isolated QA paths required')
 for parent in (tmp_path,tmp_path.parent,tmp_path.parent.parent): parent.chmod(0o711)
 snapshot=tmp_path/'snapshot'; snapshot.mkdir(mode=0o755)
 root=Path(__file__).resolve().parents[1]
 for item in ('src','hub_container_executor','scripts'):
  shutil.copytree(root/item,snapshot/item,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
  for copied in (snapshot/item).rglob('*'):
   if copied.is_file():
    original=root/copied.relative_to(snapshot)
    assert hashlib.sha256(copied.read_bytes()).digest()==hashlib.sha256(original.read_bytes()).digest()
    copied.chmod(0o444)
   elif copied.is_dir(): copied.chmod(0o555)
  (snapshot/item).chmod(0o555)
 snapshot.chmod(0o555)
 private=tmp_path/'private'; private.mkdir(mode=0o700); os.chown(private,10001,10001)
 journal=tmp_path/'journal'; journal.mkdir(mode=0o700)
 fake_root=tmp_path/'fake-docker'; fake_root.mkdir(mode=0o755)
 rows,digest=fixture(); data=make_manifest_data(); owner='db04aa59-54f0-4269-bcba-539c0151e13e'
 data['runtime']['resource_name_prefix']='hub-pilot-'+uuid.UUID(owner).hex[:24]; data['runtime']['resources']['cpu_millicores']=300; data['runtime']['resources']['pids_limit']=80; data['retention']['max_generations']=4
 for probe in data['probes']['rollback']: probe['startup_grace_ms']=0
 fixtures={}
 for role in ('candidate','rollback'):
  image_manifest=json.loads(next(raw for name,raw in rows if name=='blobs/sha256/'+digest[7:])); old=image_manifest['config']['digest']
  config={'os':'linux','architecture':'amd64','config':{'Entrypoint':['/usr/local/bin/hub'],'Cmd':['serve'],'Env':['PATH=/usr/bin:/bin','DEFAULT_MODE=fixture-'+role]}}
  craw=json.dumps(config,separators=(',',':')).encode(); cid='sha256:'+hashlib.sha256(craw).hexdigest(); image_manifest['config'].update(digest=cid,size=len(craw))
  mraw=json.dumps(image_manifest,separators=(',',':')).encode(); role_digest='sha256:'+hashlib.sha256(mraw).hexdigest()
  role_rows=[(name,raw) for name,raw in rows if name not in ('index.json','blobs/sha256/'+digest[7:],'blobs/sha256/'+old[7:])]+[('blobs/sha256/'+cid[7:],craw),('blobs/sha256/'+role_digest[7:],mraw),('index.json',json.dumps({'schemaVersion':2,'manifests':[{'mediaType':image_manifest['mediaType'],'digest':role_digest,'size':len(mraw)}]},separators=(',',':')).encode())]
  f=prepare_fixture_image(archive(role_rows),tmp_path/('oci-'+role),role_digest,source_commit=data[role]['source_commit'],context_digest=data[role]['provenance']['build_context_digest'],recipe_digest=data[role]['provenance']['build_recipe_digest'],observations={'synthetic':True},preparation_id=str(uuid.uuid4()))
  data[role]['image_config_id']=f.config_digest; data[role]['provenance']['build_inputs_digest']=f.inputs_digest; data[role]['provenance']['attestation_digest']=f.record_digest
  fixtures[role]={'record':base64.b64encode(f.record).decode(),'inputs':base64.b64encode(f.inputs).decode(),'manifest_digest':f.manifest_digest,'config_digest':f.config_digest,'blobs':{k:base64.b64encode(v).decode() for k,v in f.blobs.items()}}
 manifest=PilotManifest.from_bytes(json.dumps(data).encode()); image='sha256:'+'a'*64
 launch_request=dict(schema='engineering-hub.local-launch-request.v1',task_id=owner,daemon_id='fixture-daemon',helper_image_id=image,coordinator_image_id=image,initial_manifest=manifest.canonical_bytes().decode(),candidate=fixtures['candidate'],rollback=fixtures['rollback'],source_root=str(snapshot),source_digest=source_tree_digest(str(snapshot),deadline_monotonic=time.monotonic()+30),private_dir=str(private),journal_dir=str(journal),startup_wait_ms=0,timeout_seconds=180)
 # Private test-only fake CLI is confined to /usr/bin tmpfs in the owned QA container.
 script=Path('/usr/bin/docker'); assert not script.exists(); mapping=tmp_path/'images.json'; mapping.write_text(json.dumps({fixtures[r]['manifest_digest']:[fixtures[r]['config_digest'],'linux','amd64',''] for r in fixtures})); mapping.chmod(0o644)
 script_text='#!'+sys.executable+'\nimport json,sys\na=sys.argv[1:]\nif a[2:3]==["info"]: print("fixture-daemon")\nelif a[2:4]==["image","inspect"]: print("|".join(json.load(open('+repr(str(mapping))+'))[a[-1]]))\nelse: raise SystemExit(2)\n'
 fd=os.open(script,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o755)
 identity=os.fstat(fd)
 def cleanup_cli():
  current=os.lstat(script)
  assert (current.st_dev,current.st_ino)==(identity.st_dev,identity.st_ino)
  os.unlink(script)
 request.addfinalizer(cleanup_cli)
 try:
  raw_script=script_text.encode(); assert os.write(fd,raw_script)==len(raw_script); os.fsync(fd)
 finally: os.close(fd)
 marker=' def start(self,args,data,deadline):'; start=CHILD.index(marker); end=CHILD.index('e=Engine()',start); child=CHILD[:start]+ACTUAL_START+SETUP+CHILD[CHILD.index('os.dup2(0,3)',end):]
 child=child.replace("self.root='/fake-docker'","self.root=sys.argv[2]").replace("self.daemon='daemon-1234567890'","self.daemon='fixture-daemon'")
 child=child.replace("assert not self.volume_exists; self.volume_exists=True; return self.name", "assert not self.volume_exists; self.volume_exists=True; os.makedirs(self.mount,mode=0o700); return self.name")
 child=child.replace("assert self.volume_exists and not self.states; self.volume_exists=False; return self.name", "assert self.volume_exists and not self.states; self.volume_exists=False; __import__('shutil').rmtree(self.mount); return self.name")
 child=child.replace("ident=op[-1]; self.states[ident][0]='exited' if '--attach' in op else 'running'; return '' if '--attach' in op else ident", "ident=op[-1]; role=self.states[ident][2];\n   if role=='init':\n    import subprocess; subprocess.run([sys.executable,'-I','-B','-c',INIT_SCRIPT.replace(\"'/fixture'\",repr(self.mount))],check=True,timeout=10,capture_output=True)\n   self.states[ident][0]='exited' if '--attach' in op else 'running'; return '' if '--attach' in op else ident")
 # Restrict fake socket/CLI to this QA container; no host socket is mounted.
 raw=json.dumps(launch_request,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode(); marker=tmp_path/'socket-identity.json'
 process=subprocess.Popen([sys.executable,'-I','-B','-c',child,str(root),str(fake_root),str(marker)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
 try:
  try: output,error=process.communicate(input=raw,timeout=150)
  except subprocess.TimeoutExpired:
   if process.poll() is None and os.getpgid(process.pid)==process.pid:
    os.killpg(process.pid,__import__('signal').SIGKILL)
   process.communicate(timeout=5)
   raise
  assert process.returncode==0, error.decode()[-2000:]
  assert json.loads(output)['status']=='completed'
 finally:
  # The root QA test is PID1 in its own container; only the process group
  # created by our start_new_session child may be signalled/reaped.
  if process.poll() is None:
   if os.getpgid(process.pid)==process.pid: os.killpg(process.pid,__import__('signal').SIGKILL)
   process.communicate(timeout=5)
  try: os.killpg(process.pid,0)
  except ProcessLookupError: pass
  else:
   os.killpg(process.pid,__import__('signal').SIGKILL)
   deadline=time.monotonic()+5
   while time.monotonic()<deadline:
    try: child,_=os.waitpid(-process.pid,os.WNOHANG)
    except ChildProcessError: break
    if child==0: time.sleep(.01)
   try: os.killpg(process.pid,0)
   except ProcessLookupError: pass
   else: raise AssertionError('owned synthetic process group did not exit')
  if os.path.lexists('/var/run/docker.sock'):
   assert marker.is_file(), 'synthetic socket identity unavailable; retained'
   expected=tuple(json.loads(marker.read_text())); current=os.lstat('/var/run/docker.sock')
   assert (current.st_dev,current.st_ino)==expected, 'synthetic socket changed; retained'
   os.unlink('/var/run/docker.sock')
