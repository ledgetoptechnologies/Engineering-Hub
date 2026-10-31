"""Single owned broker child; protected runtime and listener are pre-provisioned.

The caller must retain this object until the child has exited. A timeout never
kills an admitted exchange. Trusted root administrators preserve the enrolled
read-only source/runtime, system site/dependencies and socket custody for the
whole lifetime. Isolated Python initializes before cgroup attachment and can
load trusted system site code before the fixed bootstrap reaches its gate; the
cap therefore covers broker work after the gate, not all interpreter startup.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import subprocess
import threading
import time

from .github_broker_service import read_enrollment, BrokerServiceError
from .launch_supervisor import _path, _root_file, _strict


_BOOT = """import os,sys
root=sys.argv[1]
gate=int(sys.argv[2])
if os.read(gate,2)!=b'G': raise SystemExit(2)
os.close(gate)
sys.path[:0]=[root,root+'/src']
import hub_container_executor.github_broker_process as p
import hub_container_executor.github_broker_service as s
import engineering_hub.github_oauth_broker as b
assert p.__file__==root+'/hub_container_executor/github_broker_process.py'
assert s.__file__==root+'/hub_container_executor/github_broker_service.py'
assert b.__file__==root+'/src/engineering_hub/github_oauth_broker.py'
raise SystemExit(p.main(sys.argv[3:]))
"""

# Exact test-pilot envelope; the leaf is independently provisioned by root.
_CGROUP_ROOT=Path('/sys/fs/cgroup')
_LIMITS={'cpu.max':'25000 100000','memory.max':'268435456','memory.swap.max':'0','pids.max':'32'}


class _PinnedPath:
    """Retain and recheck every root-owned, non-writable path component."""
    def __init__(self, path, *, directory):
        self.fds=[];self.names=[]
        try:
            self.fds.append(os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC))
            parts=path.parts[1:]
            for index,name in enumerate(parts):
                parent=os.fstat(self.fds[-1])
                if parent.st_uid!=0 or parent.st_mode & 0o022: raise BrokerServiceError()
                flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK
                if index<len(parts)-1 or directory: flags|=os.O_DIRECTORY
                fd=os.open(name,flags,dir_fd=self.fds[-1]);self.fds.append(fd);self.names.append(name)
            self.check()
        except BaseException:
            self.close();raise

    def check(self):
        for index,name in enumerate(self.names):
            parent=os.fstat(self.fds[index]);opened=os.fstat(self.fds[index+1])
            named=os.stat(name,dir_fd=self.fds[index],follow_symlinks=False)
            if (parent.st_uid!=0 or parent.st_mode & 0o022 or opened.st_uid!=0
                    or opened.st_mode & 0o022 or (opened.st_dev,opened.st_ino)!=(named.st_dev,named.st_ino)):
                raise BrokerServiceError()

    def close(self):
        failed=False
        for fd in reversed(self.fds):
            try: os.close(fd)
            except OSError: failed=True
        self.fds=[]
        if failed: raise BrokerServiceError()


class _BrokerCgroup:
    """Pin one pre-provisioned, exclusive cgroup-v2 leaf without creating it."""
    def __init__(self,path):
        if path==_CGROUP_ROOT or _CGROUP_ROOT not in path.parents: raise BrokerServiceError()
        self._pin=_PinnedPath(path,directory=True)
        self._root_index=len(_CGROUP_ROOT.parts)-1
        try:
            if not _cgroup_v2(self._pin.fds[-1]): raise BrokerServiceError()
            self.check(None)
        except BaseException:
            self._pin.close();raise

    def _control(self,name,*,write=None,directory_fd=None,metadata_only=False):
        flags=(os.O_RDONLY if write is None else os.O_WRONLY)|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK
        fd=os.open(name,flags,dir_fd=self._pin.fds[-1] if directory_fd is None else directory_fd)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022: raise BrokerServiceError()
            if metadata_only:return None
            if write is not None:
                if os.write(fd,write)!=len(write): raise BrokerServiceError()
                return None
            raw=os.read(fd,4097)
            if len(raw)>4096: raise BrokerServiceError()
            return raw.decode('ascii').strip()
        finally: os.close(fd)

    def check(self,pid):
        self._pin.check()
        ancestors=self._pin.fds[self._root_index:]
        device=os.fstat(ancestors[0]).st_dev
        for fd in ancestors:
            if os.fstat(fd).st_dev!=device or not _cgroup_v2(fd): raise BrokerServiceError()
            for name in ('cgroup.procs','cgroup.threads'):
                self._control(name,directory_fd=fd,metadata_only=True)
        with os.scandir(self._pin.fds[-1]) as entries:
            if any(item.is_dir(follow_symlinks=False) for item in entries): raise BrokerServiceError()
        if any(self._control(name)!=value for name,value in _LIMITS.items()): raise BrokerServiceError()
        try: burst=self._control('cpu.max.burst')
        except FileNotFoundError: burst='0'  # Kernels without this control do not support burst.
        if burst!='0': raise BrokerServiceError()
        if self._control('cgroup.subtree_control')!='': raise BrokerServiceError()
        procs=self._control('cgroup.procs').splitlines()
        if procs!=([] if pid is None else [str(pid)]): raise BrokerServiceError()
        events={}
        for line in self._control('cgroup.events').splitlines():
            fields=line.split()
            if len(fields)!=2 or fields[0] in events: raise BrokerServiceError()
            events[fields[0]]=fields[1]
        if events.get('populated')!=('0' if pid is None else '1'): raise BrokerServiceError()

    def attach(self,pid):
        if type(pid) is not int or pid<=0: raise BrokerServiceError()
        self.check(None)
        self._control('cgroup.procs',write=(str(pid)+'\n').encode('ascii'))
        self.check(pid)

    def close(self): self._pin.close()


def _cgroup_v2(fd):
    import ctypes
    fields=(ctypes.c_long*64)();libc=ctypes.CDLL(None,use_errno=True)
    return libc.fstatfs(fd,ctypes.byref(fields))==0 and fields[0]==0x63677270


def _runtime(policy_file):
    """Validate independent root policy and immutable local source snapshot."""
    from scripts.hub_local_test_launcher import source_tree_digest
    raw,_=_root_file(_path(policy_file),16384)
    data=_strict(raw,16384)
    base={'schema','source_root','source_digest','python_path','python_digest'}
    if (type(data) is not dict or not ((data.get('schema')=='engineering-hub.github-broker-runtime.v1' and set(data)==base)
            or (data.get('schema')=='engineering-hub.github-broker-runtime.v2' and set(data)==base|{'cgroup_path'}))
            or any(type(data[k]) is not str or re.fullmatch(r'sha256:[0-9a-f]{64}',data[k]) is None
                   for k in ('source_digest','python_digest'))): raise BrokerServiceError()
    root=_path(data['source_root']); executable=_path(data['python_path'])
    if root.resolve(strict=True)!=root or executable.resolve(strict=True)!=executable: raise BrokerServiceError()
    pins=[];cgroup=None
    try:
        pins.append(_PinnedPath(root,directory=True));pins.append(_PinnedPath(executable,directory=False))
        directory=pins[0].fds[-1];fd=pins[1].fds[-1]
        if not os.fstatvfs(directory).f_flag & os.ST_RDONLY: raise BrokerServiceError()
        if source_tree_digest(str(root),deadline_monotonic=time.monotonic()+30)!=data['source_digest']: raise BrokerServiceError()
        before=os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=0 or before.st_mode & 0o022
                or not 0<before.st_size<=128*1024*1024): raise BrokerServiceError()
        digest=hashlib.sha256();count=0
        while True:
            block=os.read(fd,65536)
            if not block: break
            count+=len(block)
            if count>before.st_size: raise BrokerServiceError()
            digest.update(block)
        after=os.fstat(fd); named=os.stat(executable,follow_symlinks=False)
        if (count!=before.st_size or 'sha256:'+digest.hexdigest()!=data['python_digest']
                or (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
                !=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)
                or (before.st_dev,before.st_ino)!=(named.st_dev,named.st_ino)): raise BrokerServiceError()
        for pin in pins: pin.check()
        if data['schema']=='engineering-hub.github-broker-runtime.v2':
            cgroup=_BrokerCgroup(_path(data['cgroup_path']));data['_cgroup']=cgroup
        data['_pins']=pins
        return data
    except BaseException:
        if cgroup is not None:
            try:cgroup.close()
            except BaseException:pass
        for pin in reversed(pins):
            try: pin.close()
            except BaseException: pass
        raise


class OwnedBrokerProcess:
    """One-shot child handle, kept live on any uncertain start or stop."""
    def __init__(self):
        self._pid=os.getpid(); self._thread=threading.get_ident(); self._process=None; self._stop=None; self._cgroup=None; self._attempted=False; self._stopping=False

    @property
    def pid(self): return None if self._process is None else self._process.pid

    def start_for_coordinator(self, *, profile, config_raw, session_raw, admission_raw,
                              admission_raw_digest, independent_release, runtime_policy,
                              enrollment_fd, listener, timeout_seconds=10, expected_enrollment_digest=None):
        """Bind the broker to a protected v3 coordinator before starting it.

        The caller owns independent release/raw-admission authorization and
        must retain the process through coordinator drain. The issuer IDs in the
        coordinator admission are namespace observations and are not inferred
        from the host socket owner here.
        """
        from .configuration_commitment import supervised_local_coordinator_commitment
        from .coordinator_admission import parse_admission
        try:
            if profile.role!='supervised-local-coordinator-v3': raise ValueError()
            supervised_local_coordinator_commitment(profile,config_raw,session_raw,admission_raw,
                root_raw_digest=admission_raw_digest,independent_release=independent_release)
            admission=parse_admission(admission_raw); broker=admission.oauth_broker
            enrollment=read_enrollment(enrollment_fd)
            encoded=json.dumps(enrollment,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
            if expected_enrollment_digest is not None and 'sha256:'+hashlib.sha256(encoded).hexdigest()!=expected_enrollment_digest:
                raise ValueError()
            mounts=[item for item in profile.mounts if item.role=='local_oauth_socket']
            if len(mounts)!=1 or broker is None: raise ValueError()
            mount=mounts[0]
            def mapped(rows):
                matches=[outside+10001-inside for inside,outside,count in rows if inside<=10001<inside+count]
                if len(matches)!=1: raise ValueError()
                return matches[0]
            if (type(listener) is not socket.socket or enrollment['socket_path']!=mount.source or listener.getsockname()!=mount.source
                    or (enrollment['socket_uid'],enrollment['socket_gid'],enrollment['socket_mode'])!=(mount.uid,mount.gid,mount.mode)
                    or (enrollment['service_uid'],enrollment['service_gid'])!=(mount.uid,mount.gid)
                    or (enrollment['peer_uid'],enrollment['peer_gid'])!=(mapped(profile.daemon_policy.uid_map),mapped(profile.daemon_policy.gid_map))
                    or (enrollment['client_id'],enrollment['origin'],enrollment['secret_digest'])!=(broker.client_id,broker.origin,broker.secret_digest)):
                raise ValueError()
            if (type(listener) is not socket.socket or listener.family!=socket.AF_UNIX
                    or listener.getsockopt(socket.SOL_SOCKET,socket.SO_TYPE)!=socket.SOCK_STREAM
                    or listener.getsockopt(socket.SOL_SOCKET,socket.SO_ACCEPTCONN)!=1): raise ValueError()
            opened=os.fstat(listener.fileno());named=os.lstat(mount.source)
            _,creator_uid,creator_gid=struct.unpack('=iII',listener.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if (not stat.S_ISSOCK(named.st_mode) or (named.st_uid,named.st_gid,stat.S_IMODE(named.st_mode))!=(mount.uid,mount.gid,mount.mode)
                    or (opened.st_dev,opened.st_ino)!=(enrollment['listener_dev'],enrollment['listener_ino'])
                    or (named.st_dev,named.st_ino)!=(enrollment['path_dev'],enrollment['path_ino'])
                    or (creator_uid,creator_gid)!=(enrollment['listener_uid'],enrollment['listener_gid'])): raise ValueError()
        except Exception: raise BrokerServiceError() from None
        self.start(runtime_policy=runtime_policy,enrollment_fd=enrollment_fd,listener=listener,
                   timeout_seconds=timeout_seconds,_expected_enrollment=enrollment,_require_cgroup=True)

    def start(self, *, runtime_policy, enrollment_fd, listener, timeout_seconds=10, _expected_enrollment=None, _require_cgroup=False):
        """Transfer the listener after runtime validation, retaining the child handle."""
        if (os.getpid()!=self._pid or threading.get_ident()!=self._thread or self._attempted or type(timeout_seconds) is not int
                or type(_require_cgroup) is not bool
                or not 1<=timeout_seconds<=30): raise BrokerServiceError()
        self._attempted=True
        runtime=_runtime(runtime_policy)
        stop_read=stop_write=ready_read=ready_write=gate_read=gate_write=None;pin=None
        try:
            cgroup=runtime.get('_cgroup')
            if _require_cgroup and type(cgroup) is not _BrokerCgroup: raise BrokerServiceError()
            enrollment=read_enrollment(enrollment_fd)
            if _expected_enrollment is not None and enrollment!=_expected_enrollment: raise BrokerServiceError()
            enrollment_digest='sha256:'+hashlib.sha256(json.dumps(enrollment,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()
            stop_read,stop_write=os.pipe2(os.O_CLOEXEC)
            ready_read,ready_write=os.pipe2(os.O_CLOEXEC)
            gate_read,gate_write=os.pipe2(os.O_CLOEXEC)
            for source_pin in runtime.get('_pins',()): source_pin.check()
            args=[runtime['python_path'],'-I','-B','-c',_BOOT,runtime['source_root'],str(gate_read),
                  str(enrollment_fd),str(listener.fileno()),str(stop_read),str(ready_write),enrollment_digest]
            deadline=time.monotonic()+timeout_seconds
            self._process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,env={'LANG':'C.UTF-8'},close_fds=True,
                pass_fds=(enrollment_fd,listener.fileno(),stop_read,ready_write,gate_read),
                user=enrollment['service_uid'],group=enrollment['service_gid'],extra_groups=())
            self._stop=stop_write;stop_write=None;self._cgroup=cgroup
            os.close(stop_read);stop_read=None;os.close(ready_write);ready_write=None;os.close(gate_read);gate_read=None
            if cgroup is not None:
                from .launch_supervisor import _Process
                pin=_Process(self._process.pid);cgroup.attach(self._process.pid);pin.check()
            if time.monotonic()>=deadline or os.write(gate_write,b'G')!=1: raise BrokerServiceError()
            os.close(gate_write);gate_write=None
            remaining=deadline-time.monotonic()
            if remaining<=0:raise BrokerServiceError()
            ready,_,_=select.select([ready_read],[],[],remaining)
            if not ready or os.read(ready_read,1)!=b'R' or self._process.poll() is not None:
                raise BrokerServiceError()
            if cgroup is not None:
                cgroup.check(self._process.pid);pin.check()
            if time.monotonic()>=deadline: raise BrokerServiceError()
        finally:
            failed=False
            try: listener.close()
            except BaseException: failed=True
            if pin is not None:
                try: pin.close()
                except BaseException:failed=True
            for fd in (stop_read,stop_write,ready_read,ready_write,gate_read,gate_write):
                if fd is not None:
                    try: os.close(fd)
                    except OSError: failed=True
            if runtime.get('_cgroup') is not None and self._cgroup is None:
                try:runtime['_cgroup'].close()
                except BaseException:failed=True
            for pin in reversed(runtime.get('_pins',())):
                try: pin.close()
                except BaseException: failed=True
            if failed: raise BrokerServiceError()

    def is_live(self):
        if (os.getpid()!=self._pid or threading.get_ident()!=self._thread or self._process is None
                or self._process.poll() is not None):return False
        try:
            if self._cgroup is not None:self._cgroup.check(self._process.pid)
            return True
        except Exception:return False

    def _release_cgroup(self):
        if self._cgroup is not None:
            group=self._cgroup;self._cgroup=None;group.close()

    def _release_after_exit(self):
        failed=False
        if self._stop is not None:
            try:os.close(self._stop)
            except OSError:failed=True
            self._stop=None
        try:self._release_cgroup()
        except BaseException:failed=True
        if failed:raise BrokerServiceError()

    def stop(self, *, timeout_seconds):
        if (os.getpid()!=self._pid or threading.get_ident()!=self._thread or self._process is None or type(timeout_seconds) is not int
                or not 1<=timeout_seconds<=120): raise BrokerServiceError()
        failed=False
        if not self._stopping:
            self._stopping=True
            try:
                if os.write(self._stop,b'S')!=1: failed=True
            except OSError: failed=True
            finally:
                try: os.close(self._stop)
                except OSError: failed=True
                self._stop=None
        try: status=self._process.wait(timeout=timeout_seconds)
        except (subprocess.TimeoutExpired, OSError): raise BrokerServiceError() from None
        self._release_after_exit()
        if failed or status!=0: raise BrokerServiceError()

    def wait(self, *, timeout_seconds):
        """Reap an owned child after a prior interrupted or uncertain stop."""
        if (os.getpid()!=self._pid or threading.get_ident()!=self._thread or self._process is None or type(timeout_seconds) is not int
                or not 1<=timeout_seconds<=120): raise BrokerServiceError()
        try:
            status=self._process.wait(timeout=timeout_seconds)
            self._release_after_exit()
            return status
        except (subprocess.TimeoutExpired,OSError): raise BrokerServiceError() from None
