"""Fixed Windows-owned operator for the local fixture; no staging use.

Storage remains owned by its provisioner. Any unknown Docker result retains
the operator and all storage; this function never adopts or retries effects.
"""
import json
import re
import uuid
from dataclasses import dataclass
import hashlib

from scripts.hub_local_windows_storage import WindowsTask, OperatorStorage

ID_RE = re.compile(r'[0-9a-f]{64}\Z')
LABEL = 'engineering-hub.local-test-owner'
CLI_DIGEST = '6f7a95d27f98fc8c5f6b220898988bf8f61dd9b040936797917cbdc72c6cabe6'
OPERATOR_IMAGE = 'sha256:16bc3929102c174fc598783588f0268528ff237b60466b8ba3fac4ee088ebdc3'
SOCKET_PROXY = '/run/host-services/docker.proxy.sock'
BOOT_PREFIX = ("import os,sys,hashlib,stat; "
        "assert os.getuid()==10001 and os.geteuid()==10001 and os.getgid()==10001 and os.getegid()==10001; "
        "assert 0 in os.getgroups() and set(os.getgroups())<={0,10001}; "
        "s=os.stat('/var/run/docker.sock'); assert stat.S_ISSOCK(s.st_mode) and s.st_gid==0 and s.st_mode&0o060==0o060 and not s.st_mode&0o007; "
        "assert hashlib.sha256(open('/usr/bin/docker','rb').read()).hexdigest()=='"+CLI_DIGEST+"'; ")
BOOT_SUFFIX = (
        "os.dup2(0,3);os.set_inheritable(3,True); "
        "sys.path[:0]=['/opt/hub','/opt/hub/src']; "
        "from scripts.hub_local_test_launcher import main;raise SystemExit(main(['--request-fd','3']))")

def operator_boot(daemon_id):
    if type(daemon_id) is not str or not re.fullmatch(r'[A-Za-z0-9:._-]{12,128}',daemon_id):
        raise ValueError('Invalid daemon identity')
    return (BOOT_PREFIX+"import socket,http.client,json,signal;signal.alarm(5); "
        "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(5);s.connect('/var/run/docker.sock'); "
        "s.sendall(b'GET /info HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n'); "
        "r=http.client.HTTPResponse(s);r.begin();b=r.read(1048577); "
        "assert r.status==200 and len(b)<=1048576 and json.loads(b).get('ID')=="+repr(daemon_id)+"; "
        "r.close();s.close();signal.alarm(0); "+BOOT_SUFFIX)

@dataclass(frozen=True)
class OperatorCompletion:
    """Same-process evidence; the owning task also retains this exact object."""
    task: WindowsTask
    storage: OperatorStorage
    inner_task_id: str
    operator_id: str
    coordinator_id: str
    image_id: str
    request_sha256: str

def run_operator(task, storage, *, image_id, request, timeout_seconds, socket_source):
    """Run fixed inner launcher once; on any exception retain all resources.

    Source content and Linux journal volume must already be independently
    provisioned and verified by the same live WindowsTask. No cleanup
    certificate or host custody assertion is returned by this function.
    """
    if type(task) is not WindowsTask or type(storage) is not OperatorStorage:
        raise ValueError('invalid owned context')
    if image_id != OPERATOR_IMAGE or socket_source!=SOCKET_PROXY or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
        raise ValueError('invalid operator enrollment')
    boot=operator_boot(task.daemon_id)
    if type(request) is not dict or request.get('source_root') != storage.source_mount+'/checkout' or request.get('private_dir') != storage.scratch_mount or request.get('journal_dir') != storage.journal_mount:
        raise ValueError('operator paths mismatch')
    inner=request.get('task_id')
    if (request.get('daemon_id') != task.daemon_id or type(inner) is not str or str(uuid.UUID(inner))!=inner
        or inner in {task.task_id,storage.source_task_id,storage.scratch_task_id,storage.journal_task_id}):
        raise ValueError('operator task mismatch')
    from engineering_hub.container_pilot_manifest import PilotManifest
    manifest=PilotManifest.from_bytes(request['initial_manifest'].encode('ascii'))
    if manifest.canonical_bytes()!=request['initial_manifest'].encode('ascii'): raise ValueError('manifest mismatch')
    r=manifest.runtime.resources
    if (3*r.memory_bytes+1073741824+67108864+134217728+536870912+67108864+536870912+536870912+67108864>4*1024**3
        or 3*r.cpu_millicores+1000+100+250+250>2500 or 3*r.pids_limit+256+16+32+128>600):
        raise ValueError('aggregate resource envelope exceeded')
    # The provisioner must independently verify initialized source/CLI and
    # live storage before it grants its one-shot operation. Until that API is
    # implemented this fails before any Docker request or effect.
    task.claim_verified_operator(storage, request, image_id)
    payload=json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')
    if len(payload)>8*1024*1024: raise ValueError('operator request too large')
    name='hub-local-operator-'+task.task_id
    task.inspect_daemon()
    if task.call('image','inspect',image_id,'--format','{{.Id}}')!=image_id:
        raise ValueError('operator image unavailable')
    if json.loads(task.call('image','inspect',image_id,'--format','{{json (index .Config "Volumes")}}')) not in (None,{}):
        raise ValueError('operator image has volumes')
    if name in task.call('container','ls','--all','--format','{{.Names}}').splitlines(): raise ValueError('operator name exists')
    task.record('operator-create-intent',name=name,image=image_id)
    cid=task.call('container','create','--pull=never','--interactive','--name',name,'--label',LABEL+'='+task.task_id,
        '--user','10001:10001','--group-add','0','--network','none','--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges','--cgroupns','private','--cpus','0.25','--memory','512m',
        '--memory-swap','512m','--pids-limit','128','--log-driver','local','--log-opt','max-size=1m','--log-opt','max-file=2',
        '--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=64m',
        '--mount','type=volume,src='+storage.source_name+',dst='+storage.source_mount+',readonly,volume-nocopy',
        '--mount','type=volume,src='+storage.scratch_name+',dst='+storage.scratch_mount+',volume-nocopy',
        '--mount','type=volume,src='+storage.journal_name+',dst='+storage.journal_mount+',volume-nocopy',
        '--mount','type=bind,src='+storage.source_mount+'/docker,dst=/usr/bin/docker,readonly,bind-propagation=rprivate',
        '--mount','type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock,bind-propagation=rprivate',
        '--mount','type=bind,src='+storage.source_mount+'/checkout,dst=/opt/hub,readonly,bind-propagation=rprivate',
        '--entrypoint','/opt/validation-venv/bin/python',image_id,'-I','-B','-c',boot).strip()
    if not ID_RE.fullmatch(cid): raise ValueError('operator create unknown')
    task.record('operator-created',id=cid)
    c=json.loads(task.call('container','inspect',cid))[0]; h=c['HostConfig']; cfg=c['Config']
    if (c['Id']!=cid or c.get('Image')!=image_id or c['Name'].lstrip('/')!=name or cfg['Image']!=image_id or cfg['Labels'].get(LABEL)!=task.task_id
        or c['State'].get('Status')!='created' or c['State'].get('Running') is not False or c['State'].get('Pid')!=0
        or cfg['User']!='10001:10001' or cfg['Entrypoint']!=['/opt/validation-venv/bin/python'] or cfg['Cmd']!=['-I','-B','-c',boot]
        or h['NetworkMode']!='none' or h['ReadonlyRootfs'] is not True or h['CapDrop']!=['ALL'] or h.get('CapAdd') not in (None,[]) or h.get('Privileged') is not False
        or h.get('SecurityOpt')!=['no-new-privileges'] or h.get('CgroupnsMode')!='private' or h.get('GroupAdd')!=['0']
        or h['NanoCpus']!=250000000 or h['Memory']!=536870912 or h['MemorySwap']!=536870912 or h['PidsLimit']!=128
        or h['LogConfig']!={'Type':'local','Config':{'max-size':'1m','max-file':'2'}}
        or h.get('Tmpfs')!={'/tmp':'rw,noexec,nosuid,nodev,size=64m'} or h.get('Devices') not in (None,[])
        or h.get('DeviceRequests') not in (None,[]) or h.get('PortBindings') not in (None,{})
        or h.get('PublishAllPorts') is not False or h.get('PidMode') not in ('','private') or h.get('IpcMode') not in ('','private')
        or h.get('RestartPolicy',{}).get('Name') not in ('','no')):
        raise ValueError('operator profile mismatch; retain resources')
    # Verify all six effective mounts before invoking any inner effect.
    actual={(m.get('Type'),m.get('Source'),m.get('Destination'),m.get('RW')) for m in c['Mounts']}
    expected={('volume',storage.source_mount,storage.source_mount,False),('volume',storage.scratch_mount,storage.scratch_mount,True),
        ('volume',storage.journal_mount,storage.journal_mount,True),('bind',storage.source_mount+'/docker','/usr/bin/docker',False),
        ('bind',socket_source,'/var/run/docker.sock',True),('bind',storage.source_mount+'/checkout','/opt/hub',False)}
    if actual!=expected or len(c['Mounts'])!=len(expected): raise ValueError('operator mounts mismatch; retain resources')
    for name,target in ((storage.source_name,storage.source_mount),(storage.scratch_name,storage.scratch_mount),(storage.journal_name,storage.journal_mount)):
        if not any(m.get('Type')=='volume' and m.get('Name')==name and m.get('Driver')=='local' and m.get('Destination')==target for m in c['Mounts']):
            raise ValueError('operator volume identity mismatch; retain resources')
    if any(m.get('Propagation')!='rprivate' for m in c['Mounts'] if m.get('Type')=='bind'):
        raise ValueError('operator bind propagation mismatch; retain resources')
    requested=h.get('Mounts')
    if type(requested) is not list or any(not any(m.get('Type')=='volume' and m.get('Source')==name
        and m.get('Target')==target and m.get('VolumeOptions',{}).get('NoCopy') is True for m in requested)
        for name,target in ((storage.source_name,storage.source_mount),(storage.scratch_name,storage.scratch_mount),(storage.journal_name,storage.journal_mount))):
        raise ValueError('operator volume admission mismatch; retain resources')
    for source,target,readonly in ((storage.source_mount+'/docker','/usr/bin/docker',True),
        (socket_source,'/var/run/docker.sock',False),(storage.source_mount+'/checkout','/opt/hub',True)):
        found=[m for m in requested if m.get('Type')=='bind' and m.get('Source')==source and m.get('Target')==target
               and m.get('ReadOnly',False) is readonly and m.get('BindOptions',{}).get('Propagation')=='rprivate']
        if len(found)!=1: raise ValueError('operator bind admission mismatch; retain resources')
    task.recheck_verified_operator(storage, request, image_id)
    task.inspect_daemon(); task.record('operator-start-intent',id=cid)
    output=task.call_input(['container','start','--attach','--interactive',cid],payload,timeout_seconds)
    task.record('operator-start-returned',id=cid)
    result=json.loads(output)
    if type(result) is not dict or set(result)!={'status','coordinator_id'} or result['status']!='completed' or not ID_RE.fullmatch(result['coordinator_id']):
        raise ValueError('operator outcome unknown; retain resources')
    before=c
    c=json.loads(task.call('container','inspect',cid))[0]; state=c['State']
    if c['Id']!=cid or c.get('Image')!=image_id or state.get('Running') is not False or state.get('Pid')!=0 or state.get('ExitCode')!=0 or any(state.get(k) is not False for k in ('Paused','Restarting','Dead')):
        raise ValueError('operator exit unknown; retain resources')
    if c['Name']!=before['Name'] or c['Config']!=before['Config'] or c['HostConfig']!=before['HostConfig'] or c['Mounts']!=before['Mounts']:
        raise ValueError('operator identity changed; retain resources')
    task.inspect_daemon()
    if task.call('container','ls','--all','--quiet','--no-trunc','--filter','label='+LABEL+'='+inner):
        raise ValueError('inner containers remain; retain resources')
    if task.call('volume','ls','--quiet','--filter','label='+LABEL+'='+inner):
        raise ValueError('inner volumes remain; retain resources')
    task.record('operator-completed',id=cid,coordinator_id=result['coordinator_id'])
    task.inspect_daemon(); task.record('operator-remove-intent',id=cid)
    task.call('container','rm',cid); task.record('operator-removed',id=cid)
    task.inspect_daemon()
    if cid in task.call('container','ls','--all','--quiet','--no-trunc').splitlines():
        raise ValueError('operator removal unknown; retain resources')
    completion=OperatorCompletion(task,storage,inner,cid,result['coordinator_id'],image_id,hashlib.sha256(payload).hexdigest())
    task.record('operator-absent',id=cid,inner_task_id=inner)
    task._operator_completion=completion
    return completion
