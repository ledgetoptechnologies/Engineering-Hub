"""Fixed FD-driven disposable local Docker acceptance coordinator.

Explicitly synthetic build/operator assertions; not staging enrollment or
host isolation. The trusted launcher must prepare the exact private paths and
volume, and retains cleanup authority if this process reports an uncertainty.
"""
import asyncio
import base64
import ctypes
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time
import uuid
try:
    import resource
except ImportError:
    resource=None

_ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(_ROOT/'src'),str(_ROOT)]
from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.pilot_ownership import PilotOwner
from hub_container_executor.baseline_receipt import ProtectedBaselineProvider
from hub_container_executor.baseline_snapshot import FrozenBaselineSnapshot
from scripts.hub_local_acceptance_components import LocalSource, LocalSuccessor
from scripts.hub_local_baseline_bootstrap import _final_manifest
from scripts.hub_local_cleanup_receipt import write_cleanup_receipt
from scripts.hub_local_native_coordinator import LocalNativeCoordinator
from scripts.hub_local_test_oci import verify_fixture_image, discover_cached_image
from scripts.hub_local_fixture_metadata import decode_fixture as _fixture
from scripts.hub_local_test_engine_broker import ArchiveBudget
from scripts.hub_local_test_volume import OwnedVolume, TMPFS_OPTIONS

SCHEMA='engineering-hub.local-acceptance-request.v1'
_MAX_REQUEST=8*1024*1024


def _pairs(pairs):
    output={}
    for key,value in pairs:
        if key in output: raise ValueError('duplicate request field')
        output[key]=value
    return output


def _directory(value, *, empty=False):
    if type(value) is not str: raise ValueError('invalid local directory')
    path=Path(value)
    if not path.is_absolute() or str(path)!=value or '..' in path.parts or path.resolve(strict=True)!=path:
        raise ValueError('invalid local directory')
    info=os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=10001 or stat.S_IMODE(info.st_mode)!=0o700:
        raise ValueError('local directory unavailable')
    if empty:
        with os.scandir(path) as entries:
            if next(entries,None) is not None: raise ValueError('local directory is not empty')
    return path


def _read_request(fd):
    if fd != 3: raise ValueError('fixed request descriptor required')
    info=os.fstat(fd)
    if not (stat.S_ISFIFO(info.st_mode) or stat.S_ISSOCK(info.st_mode)):
        raise ValueError('private request stream required')
    data=bytearray(); deadline=time.monotonic()+10
    selector=selectors.DefaultSelector()
    try:
        os.set_blocking(fd,False); selector.register(fd,selectors.EVENT_READ)
        while len(data)<=_MAX_REQUEST:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not selector.select(remaining): raise TimeoutError('request deadline exceeded')
            block=os.read(fd,min(65536,_MAX_REQUEST+1-len(data)))
            if not block: break
            data.extend(block)
    finally:
        selector.close()
    if not 0<len(data)<=_MAX_REQUEST: raise ValueError('invalid request size')
    request=json.loads(data,object_pairs_hook=_pairs)
    canonical=json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
    if canonical!=bytes(data): raise ValueError('canonical request required')
    return request


def _snapshot_diagnostic(output):
    try:
        diagnostic=json.loads(output,object_pairs_hook=_pairs)
        allowed={'baseline_inventory_changed','private_snapshot_file_required',
                 'foreign_baseline_entry','baseline_snapshot_failed','unavailable'}
        if (type(diagnostic) is dict and set(diagnostic)=={'error','phase','code'} and
                diagnostic['error']=='local_baseline_unavailable' and diagnostic['phase']=='snapshot' and
                type(diagnostic['code']) is str and diagnostic['code'] in allowed):
            return diagnostic['code']
    except (ValueError,TypeError,UnicodeError):
        pass
    return None


def _phase_diagnostic(output):
    try:
        value=json.loads(output,object_pairs_hook=_pairs)
        phases={'initial_start','initial_probe','freeze','restart','restart_wait','observer_setup','observe'}
        categories={'names_changed','content_changed','identity_changed','match','unavailable'}
        if (type(value) is dict and set(value)=={'error','phase','inventory'} and
                value['error']=='local_baseline_unavailable' and type(value['phase']) is str and
                value['phase'] in phases and type(value['inventory']) is str and value['inventory'] in categories):
            return value['phase'],value['inventory']
    except (ValueError,TypeError,UnicodeError):
        pass
    return None


def _child(payload):
    """Owned child, bounded input/output and deadline; uncertainty is held."""
    raw=json.dumps(payload,separators=(',',':'),allow_nan=False).encode()
    if len(raw)>4*1024*1024: raise ValueError('child request exceeds bound')
    process=subprocess.Popen([sys.executable,'-I','-B',str(_ROOT/'scripts/hub_local_baseline_child.py')],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        env={'LANG':'C.UTF-8','PATH':'/usr/bin:/bin'},close_fds=True)
    selector=None; output=bytearray(); errors=bytearray(); offset=0
    deadline=time.monotonic()+130
    try:
        selector=selectors.DefaultSelector()
        for stream,event in ((process.stdin,selectors.EVENT_WRITE),(process.stdout,selectors.EVENT_READ),
                             (process.stderr,selectors.EVENT_READ)):
            os.set_blocking(stream.fileno(),False); selector.register(stream,event)
        while selector.get_map():
            remaining=deadline-time.monotonic()
            if remaining<=0: raise TimeoutError('child outcome unknown')
            for entry,_ in selector.select(remaining):
                stream=entry.fileobj
                if stream is process.stdin:
                    if offset==len(raw): selector.unregister(stream); stream.close(); continue
                    offset+=os.write(stream.fileno(),raw[offset:offset+65536])
                else:
                    block=os.read(stream.fileno(),65536)
                    if not block: selector.unregister(stream); stream.close(); continue
                    target=output if stream is process.stdout else errors
                    target.extend(block)
                    if len(target)>256*1024: raise ValueError('child output exceeds bound')
        if process.wait(timeout=max(.001,deadline-time.monotonic()))!=0 or errors:
            # Only a closed set of synthetic fixture codes can cross this
            # boundary; child output may otherwise contain private data.
            code=_snapshot_diagnostic(output)
            if code is not None:
                print('local baseline snapshot: '+code,file=sys.stderr)
            phase=_phase_diagnostic(output)
            if phase is not None:
                print('local baseline phase: '+phase[0]+' inventory: '+phase[1],file=sys.stderr)
            raise ValueError('child outcome unavailable')
        return json.loads(output,object_pairs_hook=_pairs)
    finally:
        failed=sys.exc_info()[0] is not None; cleanup_failed=False
        if selector is not None:
            try: selector.close()
            except Exception: cleanup_failed=True
        try:
            if process.poll() is None: process.kill()
        except Exception: cleanup_failed=True
        try: process.wait(timeout=5)
        except Exception: cleanup_failed=True
        for stream in (process.stdin,process.stdout,process.stderr):
            try:
                if stream is not None and not stream.closed: stream.close()
            except Exception: cleanup_failed=True
        if cleanup_failed and not failed: raise RuntimeError('child cleanup unavailable') from None


class _CombinedStore:
    def __init__(self, values): self._values=values
    def get(self, key): return self._values[key]


class _CombinedTrust:
    def __init__(self, candidate, rollback): self._verifiers={'candidate':candidate,'rollback':rollback}
    def verify_build_record(self, raw, record, manifest, role):
        return self._verifiers[role].verify_build_record(raw,record,manifest,role)


def _socket_identity():
    sock=os.lstat('/var/run/docker.sock')
    if not stat.S_ISSOCK(sock.st_mode) or not os.access('/var/run/docker.sock',os.R_OK|os.W_OK):
        raise ValueError('local daemon socket inaccessible')
    return sock.st_dev,sock.st_ino


def _bounded_volume(path):
    """Require a mounted tmpfs with the fixed total size, not a claimed quota."""
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        before=os.fstat(fd); named=os.stat(path,follow_symlinks=False)
        if (before.st_dev,before.st_ino)!=(named.st_dev,named.st_ino): raise ValueError('volume path changed')
        buffer=ctypes.create_string_buffer(256)
        libc=ctypes.CDLL(None,use_errno=True)
        if libc.fstatfs(fd,ctypes.byref(buffer))!=0 or ctypes.c_long.from_buffer(buffer).value!=0x01021994:
            raise ValueError('bounded tmpfs unavailable')
        info=os.fstatvfs(fd); size=info.f_frsize*info.f_blocks
        if (not 0<size<=512*1024*1024 or info.f_frsize*info.f_bavail<8*1024*1024
                or not 0<info.f_files<=4096 or info.f_ffree<64):
            raise ValueError('bounded tmpfs unavailable')
        named=os.stat(path,follow_symlinks=False)
        if (before.st_dev,before.st_ino)!=(named.st_dev,named.st_ino): raise ValueError('volume path changed')
    finally:
        os.close(fd)


async def run(request):
    fields={'schema','task_id','daemon_id','volume_mount','private_dir','manifest',
            'candidate','rollback','coordinator_id','coordinator_image_id','cleanup_nonce','cleanup_key','startup_wait_ms',
            'volume_storage'}
    if (os.getuid()!=10001 or os.getgid()!=10001 or type(request) is not dict
            or not (set(request)==fields and request.get('schema')==SCHEMA
                    or set(request)==fields|{'state_alias'} and request.get('schema')=='engineering-hub.local-acceptance-request.v2')
            or type(request['task_id']) is not str
            or str(uuid.UUID(request['task_id']))!=request['task_id']
            or type(request['daemon_id']) is not str or not re.fullmatch('[A-Za-z0-9:._-]{12,128}',request['daemon_id'])
            or type(request['coordinator_id']) is not str or not re.fullmatch('[a-f0-9]{64}',request['coordinator_id'])
            or type(request['coordinator_image_id']) is not str or not re.fullmatch('sha256:[a-f0-9]{64}',request['coordinator_image_id'])
            or type(request['cleanup_nonce']) is not str or not re.fullmatch('[a-f0-9]{64}',request['cleanup_nonce'])
            or type(request['cleanup_key']) is not str or not re.fullmatch('[a-f0-9]{64}',request['cleanup_key'])
            or type(request['startup_wait_ms']) is not int or not 0<=request['startup_wait_ms']<=30000
            or request['volume_storage']!='tmpfs-512m-v1'
            or type(request['manifest']) is not str):
        raise ValueError('local acceptance request unavailable')
    private=_directory(request['private_dir'],empty=True)
    _bounded_volume(private)
    mount=_directory(request['volume_mount'])
    _bounded_volume(mount)
    if private==mount or private.is_relative_to(mount) or mount.is_relative_to(private):
        raise ValueError('local acceptance paths overlap')
    alias=request.get('state_alias')
    if 'state_alias' in request and alias!='/run/eh/state-aliases/'+request['task_id']:
        raise ValueError('local state alias unavailable')
    state_root=mount if alias is None else _directory(alias)
    root=_directory(str(state_root/'generations')); _directory(str(root/'baseline-g1'),empty=True)
    _directory(str(state_root/'reservations'),empty=True)
    with os.scandir(root) as entries:
        if {entry.name for entry in entries}!={'baseline-g1'}: raise ValueError('generation root unavailable')
    manifest=PilotManifest.from_bytes(request['manifest'].encode())
    if manifest.canonical_bytes()!=request['manifest'].encode() or any(p.startup_grace_ms for p in manifest.probes.rollback):
        raise ValueError('local manifest unavailable')
    fixtures={role:_fixture(request[role]) for role in ('candidate','rollback')}
    candidate,_,_,candidate_trust=verify_fixture_image(manifest,'candidate',fixtures['candidate'])
    rollback,_,_,rollback_trust=verify_fixture_image(manifest,'rollback',fixtures['rollback'])
    for image in (candidate,rollback): discover_cached_image(image,daemon_id=request['daemon_id'])
    socket_identity=_socket_identity()
    volume=OwnedVolume('hub-local-test-'+request['task_id'],request['task_id'],request['daemon_id'],str(mount),TMPFS_OPTIONS)
    alias_guard=None
    if alias is not None:
        from scripts.hub_local_state_alias import StateAlias
        alias_guard=StateAlias(alias,volume,uid=10001,gid=10001)
    try:
        owner_path=private/'hub.sqlite3'; owner=PilotOwner(owner_path,allow_ephemeral_test_storage=True); owner.close()
        source=LocalSource(manifest=manifest,candidate=candidate,rollback=rollback,volume=volume,private_dir=private,
                           daemon_socket_identity=socket_identity,archive_budget=ArchiveBudget(),state_alias=alias_guard)
    except BaseException:
        if alias_guard is not None: alias_guard.close()
        raise
    coordinator=successor=provider=None
    try:
        payload={'manifest':manifest.canonical_bytes().decode(),'owner_db':str(owner_path),
            'socket':str(private/'bootstrap.sock'),'generation_root':str(root),'journal':str(private/'bootstrap-operations.sqlite'),
            'owner':request['task_id'],'record':request['rollback']['record'],'blobs':request['rollback']['blobs'],
            'ids':[str(uuid.uuid4()) for _ in range(6)],'startup_wait_ms':request['startup_wait_ms']}
        observed=_child(payload)
        if (type(observed) is not dict or set(observed)!={'manifest','snapshot','receipt','receipt_digest','profile_digest','container_id'}):
            raise ValueError('baseline child result unavailable')
        final=PilotManifest.from_bytes(observed['manifest'].encode())
        saved=observed['snapshot']
        snapshot=FrozenBaselineSnapshot(saved['sha256'],saved['size_bytes'],
            tuple(tuple(x) for x in saved['source_inventory']),tuple(tuple(x) for x in saved['source_identities']))
        if final.canonical_bytes()!=_final_manifest(manifest,snapshot.sha256).canonical_bytes():
            raise ValueError('baseline manifest changed')
        receipt=base64.b64decode(observed['receipt'],validate=True)
        candidate,_,_,candidate_trust=verify_fixture_image(final,'candidate',fixtures['candidate'])
        rollback,_,_,rollback_trust=verify_fixture_image(final,'rollback',fixtures['rollback'])
        records=_CombinedStore({fixture.record_digest:fixture.record for fixture in fixtures.values()})
        metadata=_CombinedStore({k:v for fixture in fixtures.values() for k,v in fixture.blobs.items()})
        verifier=_CombinedTrust(candidate_trust,rollback_trust)
        provider=ProtectedBaselineProvider(_CombinedStore({observed['receipt_digest']:receipt}),
            receipt_digest=observed['receipt_digest'],campaign_id=final.campaign_id,verified_rollback=rollback,
            expected_runtime_profile_digest=observed['profile_digest'])
        source_snapshot=source.seal()
        successor=LocalSuccessor(manifest=final,candidate=candidate,rollback=rollback,records=records,metadata=metadata,
            verifier=verifier,baseline_provider=provider,snapshot=snapshot,source_broker=source.broker,
            source_snapshot=source_snapshot,volume=volume,private_dir=private,daemon_socket_identity=socket_identity,state_alias=alias_guard)
        coordinator=LocalNativeCoordinator(database=owner_path,manifest=final,task_owner=request['task_id'],
                                           prepare_bound=successor.prepare,timeout_seconds=300)
        job,result=await coordinator.run_once()
        if (result!={'id':job['id'],'state':'failed'} or coordinator.repo.list_runs()[0]['status']!='rolled_back'
                or [v[0] for v in coordinator.client.responses]!=['succeeded','failed','succeeded','succeeded']
                or coordinator.client.responses[1][3]!='unhealthy' or coordinator.client.responses[3][3]!='healthy'):
            raise ValueError('local rollback outcome unavailable')
        await coordinator.close()
        cleaned=successor.seal_and_cleanup(coordinator)
        successor.close(); source.close(); provider.close()
        write_cleanup_receipt(directory=private,key=bytes.fromhex(request['cleanup_key']),nonce=request['cleanup_nonce'],
            coordinator_id=request['coordinator_id'],coordinator_image_id=request['coordinator_image_id'],
            initial_manifest_digest=manifest.digest(),baseline_backup_digest=snapshot.sha256,
            manifest_digest=final.digest(),coordinator=coordinator,source=source,successor=successor,cleaned=cleaned)
        return {'state':'rolled_back','cleanup':'receipt_written','manifest_digest':final.digest()}
    finally:
        try:
            if coordinator is not None: await coordinator.close()
        finally:
            try:
                if successor is not None and (coordinator is None or coordinator._closed): successor.close()
            finally:
                try:
                    if provider is not None: provider.close()
                finally:
                    try: source.close()
                    finally:
                        if alias_guard is not None: alias_guard.close()


def main():
    if sys.argv != [sys.argv[0],'--request-fd','3']:
        print('{"error":"local_acceptance_unavailable"}'); return 2
    try:
        try:
            request=_read_request(3)
        finally:
            os.close(3)
        if resource is None: raise ValueError('local resource limits unavailable')
        soft,hard=resource.getrlimit(resource.RLIMIT_FSIZE)
        ceiling=64*1024*1024
        limit=min(ceiling,hard if hard != resource.RLIM_INFINITY else ceiling,
                  soft if soft != resource.RLIM_INFINITY else ceiling)
        if limit<1024*1024: raise ValueError('private file limit unavailable')
        resource.setrlimit(resource.RLIMIT_FSIZE,(limit,limit))
        print(json.dumps(asyncio.run(run(request)),separators=(',',':')))
        return 0
    except Exception:
        print('{"error":"local_acceptance_held"}')
        return 2


if __name__=='__main__': raise SystemExit(main())
