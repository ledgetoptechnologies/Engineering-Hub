"""Prepare a bounded, pinned source snapshot for the disposable local operator.

The caller must exclusively control the archive and parent directory and independently bind
the supplied uncompressed Git archive digest to its intended Git commit. This does not prove that
Docker Desktop can bind the resulting path or provide scratch cleanup authority.
"""
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import time

from scripts.hub_local_test_launcher import source_tree_digest


def prepare_source(archive, output, *, archive_sha256, deadline_monotonic):
    """Create one immutable-mode source tree; failure leaves it for inspection."""
    if (type(archive) is not str or type(output) is not str or not os.path.isabs(archive)
        or not os.path.isabs(output) or os.path.realpath(archive)!=archive
        or os.path.realpath(os.path.dirname(output))!=os.path.dirname(output)
        or not re.fullmatch(r'sha256:[a-f0-9]{64}',archive_sha256)
        or type(deadline_monotonic) not in (int,float) or not math.isfinite(deadline_monotonic)):
        raise ValueError('Invalid source enrollment')
    def check():
        if time.monotonic()>=deadline_monotonic: raise TimeoutError('Source preparation expired')
    check()
    fd=os.open(archive,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>512*1024*1024:
            raise ValueError('Invalid source archive')
        digest=hashlib.sha256()
        with os.fdopen(os.dup(fd),'rb') as stream:
            while data:=stream.read(1024*1024):
                check(); digest.update(data)
        if 'sha256:'+digest.hexdigest()!=archive_sha256: raise ValueError('Source archive digest mismatch')
        os.mkdir(output,0o700)
        root=Path(output); seen=set(); total=0; dirs={root}
        os.lseek(fd,0,os.SEEK_SET)
        with os.fdopen(os.dup(fd),'rb') as stream, tarfile.open(fileobj=stream,mode='r:') as tar:
            for member in tar:
                check(); name=member.name; path=PurePosixPath(name)
                if (not name or name.startswith('/') or str(path)!=name.rstrip('/')
                    or any(part in ('.','..','.git') for part in path.parts)
                    or name.rstrip('/') in seen or len(seen)>=10000
                    or not (member.isfile() or member.isdir())):
                    raise ValueError('Invalid source archive member')
                seen.add(name.rstrip('/')); target=root.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(mode=0o700,parents=True,exist_ok=True)
                    dirs.update([target,*[p for p in target.parents if p!=root.parent and root in p.parents]])
                else:
                    total+=member.size
                    if member.size<0 or total>512*1024*1024: raise ValueError('Source archive too large')
                    target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                    dirs.update([target.parent,*[p for p in target.parent.parents if p!=root.parent and root in p.parents]])
                    inp=tar.extractfile(member)
                    if inp is None: raise ValueError('Missing source bytes')
                    out=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    try:
                        remaining=member.size
                        while remaining:
                            check(); data=inp.read(min(1024*1024,remaining))
                            if not data: raise ValueError('Truncated source member')
                            view=memoryview(data)
                            while view:
                                written=os.write(out,view)
                                if written<=0: raise OSError('Short source write')
                                view=view[written:]
                            remaining-=len(data)
                        os.fchmod(out,0o555 if member.mode & 0o111 else 0o444); os.fsync(out)
                    finally: os.close(out); inp.close()
        after=os.fstat(fd)
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise ValueError('Source archive changed')
        for directory in sorted(dirs,key=lambda p:len(p.parts),reverse=True): os.chmod(directory,0o555)
        check()
        return str(root),source_tree_digest(str(root),deadline_monotonic=deadline_monotonic)
    finally: os.close(fd)
