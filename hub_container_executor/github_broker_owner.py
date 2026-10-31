"""Single owned broker child; protected runtime and listener are pre-provisioned.

The caller must retain this object until the child has exited. A timeout never
kills an admitted exchange. Trusted root administrators preserve the enrolled
read-only source/runtime and socket custody for the whole lifetime.
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


_BOOT = """import sys
root=sys.argv[1]
sys.path[:0]=[root,root+'/src']
import hub_container_executor.github_broker_process as p
import hub_container_executor.github_broker_service as s
import engineering_hub.github_oauth_broker as b
assert p.__file__==root+'/hub_container_executor/github_broker_process.py'
assert s.__file__==root+'/hub_container_executor/github_broker_service.py'
assert b.__file__==root+'/src/engineering_hub/github_oauth_broker.py'
raise SystemExit(p.main(sys.argv[2:]))
"""


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


def _runtime(policy_file):
    """Validate independent root policy and immutable local source snapshot."""
    from scripts.hub_local_test_launcher import source_tree_digest
    raw,_=_root_file(_path(policy_file),16384)
    data=_strict(raw,16384)
    if (type(data) is not dict or set(data)!={'schema','source_root','source_digest','python_path','python_digest'}
            or data['schema']!='engineering-hub.github-broker-runtime.v1'
            or any(type(data[k]) is not str or re.fullmatch(r'sha256:[0-9a-f]{64}',data[k]) is None
                   for k in ('source_digest','python_digest'))): raise BrokerServiceError()
    root=_path(data['source_root']); executable=_path(data['python_path'])
    if root.resolve(strict=True)!=root or executable.resolve(strict=True)!=executable: raise BrokerServiceError()
    pins=[]
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
        data['_pins']=pins
        return data
    except BaseException:
        for pin in reversed(pins):
            try: pin.close()
            except BaseException: pass
        raise


class OwnedBrokerProcess:
    """One-shot child handle, kept live on any uncertain start or stop."""
    def __init__(self):
        self._pid=os.getpid(); self._thread=threading.get_ident(); self._process=None; self._stop=None; self._attempted=False; self._stopping=False

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
                   timeout_seconds=timeout_seconds,_expected_enrollment=enrollment)

    def start(self, *, runtime_policy, enrollment_fd, listener, timeout_seconds=10, _expected_enrollment=None):
        """Transfer the listener after runtime validation, retaining the child handle."""
        if (os.getpid()!=self._pid or threading.get_ident()!=self._thread or self._attempted or type(timeout_seconds) is not int
                or not 1<=timeout_seconds<=30): raise BrokerServiceError()
        self._attempted=True
        runtime=_runtime(runtime_policy)
        stop_read=stop_write=ready_read=ready_write=None
        try:
            enrollment=read_enrollment(enrollment_fd)
            if _expected_enrollment is not None and enrollment!=_expected_enrollment: raise BrokerServiceError()
            enrollment_digest='sha256:'+hashlib.sha256(json.dumps(enrollment,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()
            stop_read,stop_write=os.pipe2(os.O_CLOEXEC)
            ready_read,ready_write=os.pipe2(os.O_CLOEXEC)
            for pin in runtime.get('_pins',()): pin.check()
            args=[runtime['python_path'],'-I','-B','-c',_BOOT,runtime['source_root'],
                  str(enrollment_fd),str(listener.fileno()),str(stop_read),str(ready_write),enrollment_digest]
            self._process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,env={'LANG':'C.UTF-8'},close_fds=True,
                pass_fds=(enrollment_fd,listener.fileno(),stop_read,ready_write),
                user=enrollment['service_uid'],group=enrollment['service_gid'],extra_groups=())
            self._stop=stop_write;stop_write=None
            os.close(stop_read);stop_read=None;os.close(ready_write);ready_write=None
            ready,_,_=select.select([ready_read],[],[],timeout_seconds)
            if not ready or os.read(ready_read,1)!=b'R' or self._process.poll() is not None:
                raise BrokerServiceError()
        finally:
            failed=False
            try: listener.close()
            except BaseException: failed=True
            for fd in (stop_read,stop_write,ready_read,ready_write):
                if fd is not None:
                    try: os.close(fd)
                    except OSError: failed=True
            for pin in reversed(runtime.get('_pins',())):
                try: pin.close()
                except BaseException: failed=True
            if failed: raise BrokerServiceError()

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
        if failed or status!=0: raise BrokerServiceError()

    def wait(self, *, timeout_seconds):
        """Reap an owned child after a prior interrupted or uncertain stop."""
        if (os.getpid()!=self._pid or threading.get_ident()!=self._thread or self._process is None or type(timeout_seconds) is not int
                or not 1<=timeout_seconds<=120): raise BrokerServiceError()
        try: return self._process.wait(timeout=timeout_seconds)
        except (subprocess.TimeoutExpired,OSError): raise BrokerServiceError() from None
