"""Windows parent for task-owned local test storage; uncertainty is retained.

Trusted exclusive operator and Docker Desktop daemon required. The fsynced
journal is process-crash evidence; Windows directory-entry power-loss durability
is not established. No cleanup or source-population authority is implied.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import threading
import uuid
import hashlib

DOCKER=r'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
ENDPOINT='npipe:////./pipe/dockerDesktopLinuxEngine'
LABEL='engineering-hub.local-test-owner'
OPTIONS={'type':'tmpfs','device':'tmpfs','o':'size=536870912,mode=0700,nr_inodes=4096'}
KEEPER='import time;time.sleep(1000000000)'


class Held(RuntimeError): pass


def image_labels_match(actual,image_labels,task_id,binds):
    """Check pinned image labels while allowing observed Desktop metadata stripping."""
    if type(actual) is not dict or type(image_labels) is not dict or LABEL in image_labels: return False
    expected={**image_labels,LABEL:task_id}; prefix='desktop.docker.io/mounts/'; dynamic=set()
    for key in set(actual)|set(expected):
        if key in actual and key in expected and actual[key]==expected[key]: continue
        match=re.fullmatch(r'desktop\.docker\.io/mounts/([0-9]{1,2})/(Source|SourceKind|Target)',key)
        if key not in actual and key in expected and (match or key=='desktop.docker.io/ports.scheme'): continue
        if not match or key not in actual: return False
        dynamic.add(match.group(1))
    for index in dynamic:
        values={field:actual.get(prefix+index+'/'+field) for field in ('Source','SourceKind','Target')}
        if values['SourceKind']!='hostFile' or (values['Source'],values['Target']) not in binds: return False
    return True


class WindowsTask:
    """One task, one exclusively created fsynced journal and fixed transport."""
    def __init__(self, *, task_id, daemon_id, docker_root, directory):
        if (type(task_id) is not str or str(uuid.UUID(task_id))!=task_id
            or type(daemon_id) is not str or not re.fullmatch(r'[A-Za-z0-9:._-]{12,128}',daemon_id)
            or type(docker_root) is not str or not re.fullmatch(r'/[A-Za-z0-9_./-]+',docker_root)
            or docker_root.startswith('//') or str(PurePosixPath(docker_root))!=docker_root
            or '..' in docker_root.split('/') or docker_root.endswith('/')
            or type(directory) is not str or not os.path.isabs(directory)):
            raise ValueError('Invalid Windows task enrollment')
        self.task_id=task_id; self.daemon_id=daemon_id; self.docker_root=docker_root
        self.directory=Path(directory); self.directory.mkdir(exist_ok=False)
        self.journal_path=self.directory/'journal.jsonl'
        self._journal=self.journal_path.open('xb',buffering=0)
        self._storage_started=False; self._storage_lock=threading.Lock(); self._storage=None
        self._operator_lock=threading.Lock(); self._operator_attempted=False; self._operator_binding=None
        self._seed_lock=threading.RLock(); self._seed_started=False; self._seed_failed=False
        self._cleanup_lock=threading.Lock(); self._cleanup_started=False
        try:
            config=self.directory/'docker-config'; config.mkdir()
            self._env={'SystemRoot':os.environ['SystemRoot'],'DOCKER_CONFIG':str(config)}
            self.record('task-created',daemon_id=daemon_id,docker_root=docker_root)
        except BaseException:
            self._journal.close()
            raise

    def record(self,event,**values):
        raw=(json.dumps({'task':self.task_id,'event':event,**values},sort_keys=True,separators=(',',':'))+'\n').encode()
        if len(raw)>16384 or self._journal.write(raw)!=len(raw): raise Held('Journal write uncertain')
        os.fsync(self._journal.fileno())

    def close(self): self._journal.close()

    def call(self,*args):
        return self._invoke(args,None,25)

    def call_input(self,args,data,timeout):
        if (type(args) not in (tuple,list) or any(type(x) is not str for x in args)
            or type(data) is not bytes or len(data)>8*1024*1024
            or type(timeout) not in (int,float) or not 0<timeout<=600):
            raise ValueError('Invalid bounded Docker request')
        return self._invoke(args,data,timeout)

    def _invoke(self,args,data,timeout):
        p=subprocess.Popen([DOCKER,'--host',ENDPOINT,*args],stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=self._env) if data is None else subprocess.Popen(
                           [DOCKER,'--host',ENDPOINT,*args],stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=self._env)
        buffers=[bytearray(),bytearray()]; overflow=threading.Event(); errors=[]
        def drain(stream,buf):
            try:
                while True:
                    chunk=stream.read(4096)
                    if not chunk: break
                    if len(buf)+len(chunk)>65536: overflow.set(); p.kill(); break
                    buf.extend(chunk)
            except (OSError,ValueError) as error: errors.append(error)
        threads=[threading.Thread(target=drain,args=(p.stdout,buffers[0])),threading.Thread(target=drain,args=(p.stderr,buffers[1]))]
        def write_input():
            try:
                p.stdin.write(data)
            except (OSError,ValueError) as error: errors.append(error)
            finally:
                try: p.stdin.close()
                except (OSError,ValueError) as error: errors.append(error)
        if data is not None: threads.append(threading.Thread(target=write_input))
        failed=False; result=None
        try:
            for t in threads: t.start()
            p.wait(timeout=timeout)
            for t in threads: t.join(timeout=5)
            if p.returncode or errors or overflow.is_set() or any(t.is_alive() for t in threads): raise Held('Docker result unavailable')
            result=bytes(buffers[0]).decode('utf8').strip()
        except Exception:
            failed=True
        finally:
            try:
                if p.poll() is None: p.kill()
            except OSError: failed=True
            try: p.wait(timeout=5)
            except Exception: failed=True
            for stream in (p.stdin,p.stdout,p.stderr):
                if stream is not None:
                    try: stream.close()
                    except (OSError,ValueError): failed=True
            for t in threads:
                if t.ident is not None:
                    t.join(timeout=5)
                    if t.is_alive(): failed=True
        if failed or errors: raise Held('Docker result unavailable') from None
        return result

    def inspect_daemon(self):
        if self.call('info','--format','{{.ID}}|{{.DockerRootDir}}')!=self.daemon_id+'|'+self.docker_root:
            raise Held('Daemon identity changed')

    def claim_verified_operator(self,storage,request,image_id):
        from scripts.hub_local_windows_initialization import remeasure
        if (self._storage is not storage or type(request) is not dict or type(image_id) is not str
            or not re.fullmatch(r'sha256:[a-f0-9]{64}',image_id)
            or request.get('source_root')!=storage.source_mount+'/checkout'
            or request.get('private_dir')!=storage.scratch_mount or request.get('journal_dir')!=storage.journal_mount
            or request.get('daemon_id')!=self.daemon_id or type(request.get('socket_group')) is not int or request.get('socket_group')!=0): raise Held('Operator enrollment unavailable')
        raw=json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
        if len(raw)>8*1024*1024: raise Held('Operator enrollment unavailable')
        digest=hashlib.sha256(raw).hexdigest()
        with self._operator_lock:
            if self._operator_attempted: raise Held('Operator already attempted')
            self._operator_attempted=True
        try:
            self.record('operator-admission-intent',request_sha256=digest,image_id=image_id)
            measured=remeasure(self,storage)
            if measured!=request.get('source_digest'): raise ValueError('Source changed')
            self.record('operator-admitted',request_sha256=digest,image_id=image_id,source_digest=measured)
            self._operator_binding=(storage,digest,image_id)
        except Exception: raise Held('Operator admission unknown; preserve resources') from None

    def recheck_verified_operator(self,storage,request,image_id):
        from scripts.hub_local_windows_initialization import remeasure
        try:
            raw=json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
            digest=hashlib.sha256(raw).hexdigest()
            if self._operator_binding!=(storage,digest,image_id) or self._storage is not storage: raise ValueError
            if remeasure(self,storage)!=request.get('source_digest'): raise ValueError
        except Exception: raise Held('Operator recheck unavailable; preserve resources') from None


@dataclass(frozen=True)
class OperatorStorage:
    source_name: str
    source_mount: str
    scratch_name: str
    scratch_mount: str
    journal_name: str
    journal_mount: str
    scratch_keeper_id: str
    scratch_keeper_image: str
    source_task_id: str
    scratch_task_id: str
    journal_task_id: str


def provision_storage(task, *, helper_image_id, source_task_id, scratch_task_id, journal_task_id):
    """Create source and scratch, and start one scratch keeper; never cleanup."""
    if (type(task) is not WindowsTask or type(helper_image_id) is not str
        or not re.fullmatch(r'sha256:[a-f0-9]{64}',helper_image_id)
        or any(type(x) is not str or str(uuid.UUID(x))!=x for x in (source_task_id,scratch_task_id,journal_task_id))
        or len({task.task_id,source_task_id,scratch_task_id,journal_task_id})!=4): raise ValueError('Invalid storage enrollment')
    names=['hub-local-test-'+source_task_id,'hub-local-test-'+scratch_task_id,'hub-local-test-'+journal_task_id]
    ids=[]
    with task._storage_lock:
        if task._storage_started: raise Held('Operator storage already admitted; do not retry')
        task._storage_started=True
    try:
        task.record('storage-enrollment',source=names[0],scratch=names[1],journal=names[2],helper_image_id=helper_image_id)
        task.inspect_daemon()
        image=json.loads(task.call('image','inspect',helper_image_id))
        if len(image)!=1 or image[0]['Id']!=helper_image_id or image[0]['Config'].get('Volumes') not in (None,{}): raise ValueError
        image_labels=image[0]['Config'].get('Labels') or {}
        if type(image_labels) is not dict or LABEL in image_labels: raise ValueError
        existing=task.call('volume','ls','--format','{{.Name}}').splitlines()
        if any(name in existing for name in names): raise ValueError
        for i,name in enumerate(names):
            task.inspect_daemon(); task.record('volume-create-intent',name=name)
            opts=sum((['--opt',k+'='+v] for k,v in OPTIONS.items()),[]) if i==1 else []
            if task.call('volume','create','--driver','local',*opts,'--label',LABEL+'='+task.task_id,name)!=name: raise ValueError
            task.record('volume-created',name=name)
            rows=json.loads(task.call('volume','inspect',name)); v=rows[0]
            if (len(rows)!=1 or v['Name']!=name or v['Driver']!='local' or v['Scope']!='local'
                or v['Mountpoint']!=task.docker_root+'/volumes/'+name+'/_data'
                or v['Labels']!={LABEL:task.task_id} or (v.get('Options') or {})!=(OPTIONS if i==1 else {})): raise ValueError
        name='hub-operator-scratch-keeper-'+task.task_id
        if name in task.call('ps','--all','--format','{{.Names}}').splitlines(): raise ValueError
        task.inspect_daemon(); task.record('keeper-create-intent',name=name)
        cid=task.call('create','--pull=never','--name',name,'--label',LABEL+'='+task.task_id,
            '--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--cgroupns','private',
            '--user','10001:10001','--cpus','0.1','--memory','64m','--memory-swap','64m','--pids-limit','16',
            '--log-driver','local','--log-opt','max-size=1m','--log-opt','max-file=2',
            '--mount','type=volume,src='+names[1]+',dst=/fixture,volume-nocopy',
            '--entrypoint','/usr/local/bin/python',helper_image_id,'-I','-S','-B','-c',KEEPER)
        if not re.fullmatch(r'[a-f0-9]{64}',cid): raise ValueError
        ids.append(cid); task.record('keeper-created',id=cid)
        c=json.loads(task.call('container','inspect',cid))[0]; h=c['HostConfig']; cfg=c['Config']; mounts=c['Mounts']
        if (c['Id']!=cid or c['Name']!='/'+name or cfg['Image']!=helper_image_id or not image_labels_match(cfg.get('Labels'),image_labels,task.task_id,set())
            or cfg['User']!='10001:10001' or cfg['Entrypoint']!=['/usr/local/bin/python'] or cfg['Cmd']!=['-I','-S','-B','-c',KEEPER]
            or h['NetworkMode']!='none' or h['ReadonlyRootfs'] is not True or h['CapDrop']!=['ALL']
            or h.get('CapAdd') not in (None,[]) or h['Privileged'] is not False or h['SecurityOpt']!=['no-new-privileges']
            or h.get('CgroupnsMode')!='private'
            or h['NanoCpus']!=100000000 or h['Memory']!=67108864 or h['MemorySwap']!=67108864 or h['PidsLimit']!=16
            or h['LogConfig']!={'Type':'local','Config':{'max-size':'1m','max-file':'2'}} or len(mounts)!=1
            or mounts[0].get('Name')!=names[1] or mounts[0].get('Source')!=task.docker_root+'/volumes/'+names[1]+'/_data'
            or mounts[0].get('Destination')!='/fixture' or mounts[0].get('Type')!='volume' or mounts[0].get('RW') is not True): raise ValueError
        task.inspect_daemon(); task.record('keeper-start-intent',id=cid)
        if task.call('start',cid)!=cid: raise ValueError
        task.record('keeper-started',id=cid)
        state=json.loads(task.call('container','inspect','--format','{{json .State}}',cid))
        if (state.get('Status')!='running' or state.get('Running') is not True or type(state.get('Pid')) is not int or state['Pid']<=0
            or any(state.get(k) is not False for k in ('Paused','Restarting','Dead'))): raise ValueError
        result=OperatorStorage(names[0],task.docker_root+'/volumes/'+names[0]+'/_data',names[1],
                               task.docker_root+'/volumes/'+names[1]+'/_data',names[2],
                               task.docker_root+'/volumes/'+names[2]+'/_data',cid,helper_image_id,source_task_id,scratch_task_id,journal_task_id)
        task._storage=result
        return result
    except Exception:
        try: task.record('storage-held',names=names,ids=ids)
        except Exception: pass
        raise Held('Operator storage uncertain; preserve original journal and resources') from None
