"""Disposable local-fixture snapshot using bounded in-root tmpfs scratch.

Requires an independently quiesced source and a trusted single-task UID10001
operator. Failure leaves the exclusive scratch tree and any publication held.
"""
import ctypes, hashlib, math, os, sqlite3, stat, sys, time
from contextlib import closing
from pathlib import Path
from uuid import uuid4
from hub_container_executor.baseline_snapshot import (_directory, _guard, _root_entries,
    _operator_process_gate, _inventory, _file, _hash, _deadline, FrozenBaselineSnapshot, _NAME, _RESERVED)
from hub_container_executor.state import copy_sqlite_generation


def _scratch_quota(fd):
    buf=ctypes.create_string_buffer(256); libc=ctypes.CDLL(None,use_errno=True)
    if libc.fstatfs(fd,ctypes.byref(buf))!=0:
        raise ValueError('bounded scratch filesystem required')
    kind=ctypes.c_long.from_buffer(buf).value
    v=os.fstatvfs(fd)
    if (v.f_flag & os.ST_RDONLY or not 0<v.f_frsize*v.f_blocks<=512*1024*1024
            or kind!=0x01021994 or not 0<v.f_files<=4096):
        raise ValueError('bounded scratch filesystem required')


def _work_entries(fd, generation, leaf, identity, frozen=None, output=None):
    expected={generation,leaf} | ({frozen} if frozen is not None else set())
    if set(os.listdir(fd))!=expected:
        raise ValueError('snapshot root changed')
    info=os.stat(leaf,dir_fd=fd,follow_symlinks=False)
    if (not stat.S_ISDIR(info.st_mode) or (info.st_dev,info.st_ino)!=identity or
            (info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode))!=(10001,10001,0o700)):
        raise ValueError('scratch identity changed')
    if frozen is not None and _file(os.stat(frozen,dir_fd=fd,follow_symlinks=False))[:2]!=output:
        raise ValueError('snapshot publication changed')


def freeze_copy(root, generation, frozen, *, maximum, deadline):
    if (os.getuid()!=10001 or os.getgid()!=10001 or type(root) is not str or not root.startswith('/')
            or str(Path(root))!=root or '..' in Path(root).parts or type(generation) is not str
            or not _NAME.fullmatch(generation) or type(frozen) is not str or not _NAME.fullmatch(frozen)
            or generation==frozen or generation in _RESERVED or frozen in _RESERVED
            or generation.startswith('snapshot-') or frozen.startswith('snapshot-')
            or type(maximum) is not int or not 1<=maximum<=2*1024**3
            or type(deadline) not in (int,float) or not math.isfinite(deadline)):
        raise ValueError('invalid scratch snapshot input')
    _deadline(deadline)
    root = Path(root); source = root / generation
    fds = []
    def own(fd): fds.append(fd); return fd
    try:
        rootfd, rootid = _directory(root, allow_ephemeral_test_storage=True); own(rootfd)
        _scratch_quota(rootfd)
        srcfd, srcid = _directory(source, allow_ephemeral_test_storage=True); own(srcfd)
        if rootid[0]!=srcid[0]: raise ValueError('filesystem mismatch')
        _root_entries(rootfd, generation); _operator_process_gate(srcfd, deadline)
        before = _inventory(srcfd, maximum=maximum, deadline=deadline)
        leaf = 'snapshot-work-' + str(uuid4())
        os.mkdir(leaf, 0o700, dir_fd=rootfd)
        os.fsync(rootfd)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        workfd = own(os.open(leaf, flags, dir_fd=rootfd)); workinfo=os.fstat(workfd)
        if (workinfo.st_uid, workinfo.st_gid, stat.S_IMODE(workinfo.st_mode), workinfo.st_dev)!=(10001,10001,0o700,rootid[0]): raise ValueError('scratch invalid')
        os.mkdir(generation,0o700,dir_fd=workfd)
        copyfd=own(os.open(generation,flags,dir_fd=workfd)); copyinfo=os.fstat(copyfd)
        if (copyinfo.st_uid,copyinfo.st_gid,stat.S_IMODE(copyinfo.st_mode),copyinfo.st_dev)!=(10001,10001,0o700,rootid[0]): raise ValueError('scratch generation invalid')
        identities={}; total=0
        for name, size, sha in before[0]:
            _deadline(deadline)
            sourcefd=own(os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=srcfd))
            destfd=own(os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=copyfd))
            metadata=_file(os.fstat(sourcefd)); deststat=_file(os.fstat(destfd)); identities[name]=deststat[:2]
            digest=hashlib.sha256(); count=0
            while True:
                _deadline(deadline); block=os.read(sourcefd, min(1024*1024,maximum+1-total-count))
                if not block: break
                count+=len(block)
                if total+count>maximum: raise ValueError('copy bound')
                digest.update(block); view=memoryview(block)
                while view:
                    _deadline(deadline); n=os.write(destfd,view)
                    if n<=0: raise ValueError('copy write')
                    view=view[n:]
            os.fsync(destfd)
            if (count, 'sha256:'+digest.hexdigest())!=(size,sha) or _file(os.fstat(sourcefd))!=metadata: raise ValueError('source changed')
            total+=count
            os.close(destfd);fds.remove(destfd);os.close(sourcefd);fds.remove(sourcefd)
        os.fsync(copyfd);os.fsync(workfd)
        workid=(workinfo.st_dev,workinfo.st_ino)
        _guard(root/leaf,workfd,workid); _guard(root/leaf/generation,copyfd,(copyinfo.st_dev,copyinfo.st_ino))
        if _inventory(srcfd,maximum=maximum,deadline=deadline)!=before: raise ValueError('source inventory changed')
        _guard(root,rootfd,rootid);_guard(source,srcfd,srcid);_work_entries(rootfd,generation,leaf,workid);_scratch_quota(rootfd)
        outname='snapshot.sqlite3'; path=root/leaf/generation
        result=copy_sqlite_generation(path/'hub.sqlite3',path/outname,maximum_bytes=maximum,deadline_monotonic=deadline)
        outfd=own(os.open(outname,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=copyfd)); outid=_file(os.fstat(outfd))[:2]
        with closing(sqlite3.connect((path/outname).as_uri()+'?mode=rw',uri=True,timeout=min(1,max(0,deadline-time.monotonic())))) as db:
            db.set_progress_handler(lambda:int(time.monotonic()>=deadline),1000)
            if db.execute('PRAGMA journal_mode=DELETE').fetchone()!=('delete',) or db.execute('PRAGMA integrity_check').fetchone()!=('ok',): raise ValueError('copy integrity')
            if db.execute('PRAGMA page_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0]>maximum: raise ValueError('copy bound')
            db.commit()
        info=_file(os.fstat(outfd)); sha,size=_hash(outfd,maximum,deadline)
        if info[:2]!=outid or _file(os.stat(outname,dir_fd=copyfd,follow_symlinks=False))!=info: raise ValueError('copy changed')
        os.fsync(outfd)
        if _inventory(srcfd,maximum=maximum,deadline=deadline)!=before: raise ValueError('source inventory changed')
        _guard(root,rootfd,rootid);_guard(source,srcfd,srcid);_work_entries(rootfd,generation,leaf,workid);_scratch_quota(rootfd)
        _guard(root/leaf,workfd,workid); _guard(root/leaf/generation,copyfd,(copyinfo.st_dev,copyinfo.st_ino))
        # SQLite can create or replace sidecars on the exclusive scratch copy.
        # Only after every SQLite connection has closed successfully, pin every
        # exact allowed leaf; unfamiliar entries and uncertain cleanup are held.
        allowed={'hub.sqlite3','hub.sqlite3-wal','hub.sqlite3-shm','hub.sqlite3-journal',
                 'snapshot.sqlite3-wal','snapshot.sqlite3-shm','snapshot.sqlite3-journal'}
        current=set(os.listdir(copyfd))
        if outname not in current or 'hub.sqlite3' not in current or current-allowed-{outname}:
            raise ValueError('scratch changed')
        pins=[]; sidecar_bytes=0
        for name in sorted(current-{outname}):
            _deadline(deadline)
            fd=own(os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK,dir_fd=copyfd))
            opened=_file(os.fstat(fd)); named=_file(os.stat(name,dir_fd=copyfd,follow_symlinks=False))
            if opened!=named or opened[0]!=rootid[0]: raise ValueError('scratch changed')
            if name=='hub.sqlite3' and opened[:2]!=identities.get(name): raise ValueError('scratch main changed')
            sidecar_bytes+=opened[2]
            if sidecar_bytes>maximum*2: raise ValueError('scratch byte limit')
            pins.append((name,fd,opened))
        _guard(root/leaf,workfd,workid); _guard(root/leaf/generation,copyfd,(copyinfo.st_dev,copyinfo.st_ino))
        for name,fd,identity in pins:
            _deadline(deadline)
            if _file(os.fstat(fd))!=identity or _file(os.stat(name,dir_fd=copyfd,follow_symlinks=False))!=identity:
                raise ValueError('scratch changed')
            os.unlink(name,dir_fd=copyfd)
        os.fsync(copyfd)
        if set(os.listdir(copyfd))!={outname}: raise ValueError('scratch changed')
        if _inventory(srcfd,maximum=maximum,deadline=deadline)!=before: raise ValueError('source inventory changed')
        _deadline(deadline)
        os.link(outname,frozen,src_dir_fd=copyfd,dst_dir_fd=rootfd,follow_symlinks=False)
        if _file(os.stat(frozen,dir_fd=rootfd,follow_symlinks=False),links=2)[:2]!=outid: raise ValueError('publication changed')
        os.unlink(outname,dir_fd=copyfd);os.fsync(copyfd);os.fsync(rootfd)
        _work_entries(rootfd,generation,leaf,workid,frozen,outid)
        if os.listdir(copyfd): raise ValueError('scratch not empty')
        if (os.stat(generation,dir_fd=workfd,follow_symlinks=False).st_dev,os.stat(generation,dir_fd=workfd,follow_symlinks=False).st_ino)!=(copyinfo.st_dev,copyinfo.st_ino): raise ValueError('scratch generation changed')
        os.rmdir(generation,dir_fd=workfd);os.fsync(workfd)
        if (os.stat(leaf,dir_fd=rootfd,follow_symlinks=False).st_dev,os.stat(leaf,dir_fd=rootfd,follow_symlinks=False).st_ino)!=workid: raise ValueError('scratch root changed')
        os.rmdir(leaf,dir_fd=rootfd);os.fsync(rootfd)
        _guard(root,rootfd,rootid);_guard(source,srcfd,srcid);_scratch_quota(rootfd)
        _root_entries(rootfd,generation,frozen,outid)
        if _inventory(srcfd,maximum=maximum,deadline=deadline)!=before or _hash(outfd,maximum,deadline)!=(sha,size): raise ValueError('final inventory changed')
        return FrozenBaselineSnapshot(sha,size,before[0],before[1])
    finally:
        failed = sys.exc_info()[0] is not None
        close_error = False
        for fd in reversed(fds):
            try: os.close(fd)
            except OSError: close_error = True
        if close_error and not failed: raise RuntimeError('scratch descriptor cleanup unknown')
