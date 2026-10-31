"""Seed task-owned volumes from exclusively held, independently pinned inputs.

Caller must exclude concurrent input/storage writers and bind the archive hash to
the intended Git commit. The deadline is checked between filesystem operations;
it cannot cancel a blocked filesystem call. Failure leaves partial data held.
"""
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import time
import sys
import ctypes


def tree_digest(root, *, deadline_monotonic):
    """Compute the launcher's exact digest without executing checkout code."""
    if type(deadline_monotonic) not in (int,float) or not math.isfinite(deadline_monotonic): raise ValueError('Invalid deadline')
    path=Path(root)
    if not path.is_absolute() or str(path)!=os.path.realpath(path) or (path/'.git').exists(): raise ValueError('Invalid tree')
    def check():
        if time.monotonic()>=deadline_monotonic: raise TimeoutError('Tree deadline')
    s=path.lstat()
    if not stat.S_ISDIR(s.st_mode): raise ValueError('Invalid tree')
    entries=[('root',stat.S_IMODE(s.st_mode))]; total=0
    def error(_): raise ValueError('Tree unavailable')
    for base,dirs,files in os.walk(path,followlinks=False,onerror=error):
        dirs.sort(); files.sort()
        for name in dirs:
            item=Path(base)/name; info=item.lstat()
            if not stat.S_ISDIR(info.st_mode) or len(entries)>=20000: raise ValueError('Invalid tree')
            entries.append(('dir',item.relative_to(path).as_posix(),stat.S_IMODE(info.st_mode))); check()
        for name in files:
            item=Path(base)/name; rel=item.relative_to(path).as_posix()
            if any(ord(c)<32 or ord(c)==127 for c in rel): raise ValueError('Invalid tree path')
            fd=os.open(item,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                a=os.fstat(fd)
                if not stat.S_ISREG(a.st_mode) or a.st_nlink!=1 or a.st_size>64*1024*1024: raise ValueError('Invalid file')
                total+=a.st_size
                if total>2*1024**3 or len(entries)>=20000: raise ValueError('Tree bound')
                h=hashlib.sha256(); size=0
                while True:
                    check(); block=os.read(fd,65536)
                    if not block: break
                    h.update(block); size+=len(block)
                    if size>a.st_size: raise ValueError('File grew')
                b=os.fstat(fd); named=item.lstat()
                if size!=a.st_size or (a.st_dev,a.st_ino,a.st_size,a.st_mtime_ns,a.st_ctime_ns,a.st_mode)!=(b.st_dev,b.st_ino,b.st_size,b.st_mtime_ns,b.st_ctime_ns,b.st_mode) or (a.st_dev,a.st_ino)!=(named.st_dev,named.st_ino): raise ValueError('File changed')
                entries.append(('file',rel,size,stat.S_IMODE(a.st_mode),h.hexdigest())); check()
            finally: os.close(fd)
    return 'sha256:'+hashlib.sha256(json.dumps(entries,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')).hexdigest()


def seed(archive, client, source, scratch, journal, *, archive_sha256, client_sha256, deadline_monotonic):
    if (any(type(x) is not str or not os.path.isabs(x) or os.path.realpath(x)!=x for x in (archive,client,source,scratch,journal))
        or any(type(x) is not str or not re.fullmatch(r'sha256:[a-f0-9]{64}',x) for x in (archive_sha256,client_sha256))
        or type(deadline_monotonic) not in (int,float) or not math.isfinite(deadline_monotonic)):
        raise ValueError('Invalid seed enrollment')
    roots=[Path(source),Path(scratch),Path(journal)]
    if any(a==b or a in b.parents or b in a.parents for i,a in enumerate(roots) for b in roots[i+1:]):
        raise ValueError('Overlapping roots')
    def check():
        if time.monotonic()>=deadline_monotonic: raise TimeoutError('Seed deadline')
    def identity(st): return (st.st_dev,st.st_ino,st.st_mode,st.st_nlink,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
    handles=[]
    try:
        check()
        for path in (archive,client):
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC); handles.append(fd)
            s=os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1 or s.st_size>512*1024*1024: raise ValueError('Invalid input')
        inputs=[identity(os.fstat(fd)) for fd in handles]
        rootfds=[]; rootids=[]
        for path in roots:
            fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC); handles.append(fd); rootfds.append(fd)
            s=os.fstat(fd); rootids.append((s.st_dev,s.st_ino))
            if (s.st_uid,s.st_gid)!=(0,0) or os.listdir(fd): raise ValueError('Storage not empty or not root-owned')
        if len(set(rootids))!=3: raise ValueError('Aliased roots')
        def hash_fd(fd,expected):
            os.lseek(fd,0,os.SEEK_SET); h=hashlib.sha256(); count=0
            while True:
                check(); b=os.read(fd,65536)
                if not b: break
                count+=len(b)
                if count>expected[4]: raise ValueError('Input grew')
                h.update(b)
            if count!=expected[4] or identity(os.fstat(fd))!=expected: raise ValueError('Input changed')
            return 'sha256:'+h.hexdigest()
        if hash_fd(handles[0],inputs[0])!=archive_sha256 or hash_fd(handles[1],inputs[1])!=client_sha256: raise ValueError('Input digest')
        for path,fd,rid in zip(roots,rootfds,rootids):
            s=os.stat(path,follow_symlinks=False)
            if (s.st_dev,s.st_ino)!=rid or os.listdir(fd): raise ValueError('Storage changed')
        os.mkdir('checkout',0o700,dir_fd=rootfds[0]); checkout=roots[0]/'checkout'; dirs={checkout}; seen=set(); total=0
        os.lseek(handles[0],0,os.SEEK_SET)
        with os.fdopen(os.dup(handles[0]),'rb') as f, tarfile.open(fileobj=f,mode='r:') as tar:
            for member in tar:
                check(); name=member.name; p=PurePosixPath(name)
                if (not name or name.startswith('/') or str(p)!=name.rstrip('/') or any(ord(c)<32 or ord(c)==127 for c in name) or any(x in ('.','..','.git') for x in p.parts)
                    or name.rstrip('/') in seen or len(seen)>=10000 or not (member.isdir() or member.isfile())): raise ValueError('Invalid member')
                seen.add(name.rstrip('/')); target=checkout.joinpath(*p.parts)
                if member.isdir(): target.mkdir(mode=0o700,parents=True,exist_ok=True); parent=target
                else:
                    total+=member.size
                    if member.size<0 or member.size>64*1024*1024 or total>512*1024*1024: raise ValueError('Archive bound')
                    target.parent.mkdir(mode=0o700,parents=True,exist_ok=True); parent=target.parent
                    inp=tar.extractfile(member); fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    try:
                        left=member.size
                        while left:
                            check(); block=inp.read(min(left,65536))
                            if not block: raise ValueError('Truncated input')
                            view=memoryview(block)
                            while view:
                                check(); n=os.write(fd,view)
                                if n<=0: raise OSError('Write failed')
                                view=view[n:]
                            left-=len(block)
                        os.fchmod(fd,0o555 if member.mode&0o111 else 0o444); os.fsync(fd)
                    finally: os.close(fd); inp.close()
                dirs.update([parent,*[q for q in parent.parents if checkout in q.parents]])
        os.lseek(handles[1],0,os.SEEK_SET)
        out=os.open('docker',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=rootfds[0])
        try:
            remaining=inputs[1][4]
            while remaining:
                check(); b=os.read(handles[1],min(remaining,65536))
                if not b: raise ValueError('Client truncated')
                view=memoryview(b)
                while view:
                    check(); n=os.write(out,view)
                    if n<=0: raise OSError('Write failed')
                    view=view[n:]
                remaining-=len(b)
            os.fchmod(out,0o555); os.fsync(out)
        finally: os.close(out)
        if hash_fd(handles[0],inputs[0])!=archive_sha256 or hash_fd(handles[1],inputs[1])!=client_sha256: raise ValueError('Input changed')
        for path,expected in zip((archive,client),inputs):
            if identity(os.stat(path,follow_symlinks=False))!=expected: raise ValueError('Input name changed')
        copied=os.open('docker',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=rootfds[0])
        try:
            if hash_fd(copied,identity(os.fstat(copied)))!=client_sha256: raise ValueError('Client copy changed')
        finally: os.close(copied)
        for path,fd,rid in zip(roots,rootfds,rootids):
            s=os.stat(path,follow_symlinks=False)
            if (s.st_dev,s.st_ino)!=rid: raise ValueError('Storage changed')
        for p in sorted(dirs,key=lambda x:len(x.parts),reverse=True): check(); os.chmod(p,0o555)
        check(); os.fchmod(rootfds[0],0o755)
        for fd in rootfds[1:]:
            check(); os.fchmod(fd,0o700)
            check(); os.fchown(fd,10001,10001)
            check(); os.fsync(fd)
        check(); os.fsync(rootfds[0])
        for path,rid in zip(roots,rootids):
            s=os.stat(path,follow_symlinks=False)
            if (s.st_dev,s.st_ino)!=rid: raise ValueError('Storage changed')
        check()
        # No code from the archive executes here. A separate post-init check
        # must remeasure this digest before operator admission.
        return {'archive_sha256':archive_sha256,'client_sha256':client_sha256,
                'source_digest':tree_digest(str(checkout),deadline_monotonic=deadline_monotonic)}
    finally:
        close_failed=False
        for fd in reversed(handles):
            try: os.close(fd)
            except OSError: close_failed=True
        if close_failed: raise OSError('Seed cleanup uncertain')


def verify(source,scratch,journal,*,client_sha256,deadline_monotonic):
    """Read-only remeasurement; caller still authenticates helper and mounts."""
    roots=[Path(p) for p in (source,scratch,journal)]
    if (type(deadline_monotonic) not in (int,float) or not math.isfinite(deadline_monotonic)
        or type(client_sha256) is not str or not re.fullmatch(r'sha256:[a-f0-9]{64}',client_sha256)
        or any(not p.is_absolute() or str(p)!=os.path.realpath(p) for p in roots)
        or any(a==b or a in b.parents or b in a.parents for i,a in enumerate(roots) for b in roots[i+1:])): raise ValueError('Invalid verify enrollment')
    ids=[]
    for i,p in enumerate(roots):
        s=p.lstat(); ids.append((s.st_dev,s.st_ino))
        if not stat.S_ISDIR(s.st_mode) or ((s.st_uid,s.st_gid,stat.S_IMODE(s.st_mode))!=((0,0,0o755) if i==0 else (10001,10001,0o700))): raise ValueError('Invalid root')
    if len(set(ids))!=3 or set(os.listdir(roots[0]))!={'checkout','docker'} or os.listdir(roots[1]) or os.listdir(roots[2]): raise ValueError('Invalid root content')
    fd=os.open(roots[1],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        buf=ctypes.create_string_buffer(256)
        if ctypes.CDLL(None,use_errno=True).fstatfs(fd,ctypes.byref(buf))!=0 or ctypes.c_long.from_buffer(buf).value!=0x01021994: raise ValueError('Scratch is not tmpfs')
        v=os.fstatvfs(fd)
        if not 0<v.f_frsize*v.f_blocks<=512*1024*1024 or not 0<v.f_files<=4096 or v.f_ffree<64: raise ValueError('Scratch bound')
    finally: os.close(fd)
    fd=os.open(roots[0]/'docker',os.O_RDONLY|os.O_NOFOLLOW)
    try:
        a=os.fstat(fd)
        if not stat.S_ISREG(a.st_mode) or a.st_nlink!=1 or stat.S_IMODE(a.st_mode)!=0o555 or a.st_size>128*1024*1024: raise ValueError('Invalid client')
        h=hashlib.sha256(); size=0
        while True:
            if time.monotonic()>=deadline_monotonic: raise TimeoutError('Verify deadline')
            b=os.read(fd,65536)
            if not b: break
            h.update(b); size+=len(b)
            if size>a.st_size: raise ValueError('Client grew')
        z=os.fstat(fd); named=os.stat(roots[0]/'docker',follow_symlinks=False)
        if size!=a.st_size or (a.st_dev,a.st_ino,a.st_size,a.st_mtime_ns,a.st_ctime_ns)!=(z.st_dev,z.st_ino,z.st_size,z.st_mtime_ns,z.st_ctime_ns) or (a.st_dev,a.st_ino)!=(named.st_dev,named.st_ino) or 'sha256:'+h.hexdigest()!=client_sha256: raise ValueError('Client changed')
    finally: os.close(fd)
    digest=tree_digest(str(roots[0]/'checkout'),deadline_monotonic=deadline_monotonic)
    for i,(p,rid) in enumerate(zip(roots,ids)):
        s=p.lstat()
        if (s.st_dev,s.st_ino)!=rid or (s.st_uid,s.st_gid,stat.S_IMODE(s.st_mode))!=((0,0,0o755) if i==0 else (10001,10001,0o700)): raise ValueError('Root changed')
    if time.monotonic()>=deadline_monotonic: raise TimeoutError('Verify deadline')
    return {'client_sha256':client_sha256,'source_digest':digest}


if __name__=='__main__':
    # Fixed initializer mount destinations; inputs are fixture pins, not secrets.
    if len(sys.argv)!=5 or sys.argv[1] not in ('seed','verify') or not sys.argv[4].isdecimal() or not 1<=int(sys.argv[4])<=120: raise SystemExit(1)
    if sys.argv[1]=='seed':
        result=seed('/seed/archive','/seed/client','/seed/source','/seed/scratch','/seed/journal',archive_sha256=sys.argv[2],client_sha256=sys.argv[3],deadline_monotonic=time.monotonic()+int(sys.argv[4]))
    else:
        result=verify('/seed/source','/seed/scratch','/seed/journal',client_sha256=sys.argv[3],deadline_monotonic=time.monotonic()+int(sys.argv[4]))
    print(json.dumps(result,sort_keys=True,separators=(',',':')))
