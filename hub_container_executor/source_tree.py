"""Bounded digest of an independently protected source tree.

A trusted operator must prevent concurrent writers; this is not provenance.
"""
import hashlib
import json
import os
import stat

def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode("ascii")

def source_tree_digest(root, *, deadline_monotonic):
    """Hash an isolated extracted source tree, not a release-provenance check.

    A trusted local operator must exclude concurrent writers through execution.
    This rejects symlinks, hardlinks, .git, special files and unbounded trees.
    """
    from pathlib import Path
    import hashlib,time,math
    if type(deadline_monotonic) not in (int,float) or not math.isfinite(deadline_monotonic): raise ValueError('Source deadline required')
    def check():
        if time.monotonic()>=deadline_monotonic: raise TimeoutError('Source deadline expired')
    check()
    path=Path(root)
    if not path.is_absolute() or str(path)!=root or path.resolve(strict=True)!=path or (path/'.git').exists():
        raise ValueError('Isolated source tree required')
    root_info=path.lstat()
    if not stat.S_ISDIR(root_info.st_mode): raise ValueError('Source root unavailable')
    entries=[('root',stat.S_IMODE(root_info.st_mode))]; total=0
    def walk_error(error):
        raise ValueError('Source directory unavailable') from None
    for base,dirs,files in os.walk(path,followlinks=False,onerror=walk_error):
        dirs.sort(); files.sort()
        for name in dirs:
            item=Path(base)/name; info=item.lstat()
            if not stat.S_ISDIR(info.st_mode) or len(entries)>=20000: raise ValueError('Source directory unavailable')
            entries.append(('dir',item.relative_to(path).as_posix(),stat.S_IMODE(info.st_mode))); check()
        for name in files:
            item=Path(base)/name; relative=item.relative_to(path).as_posix()
            if any(ord(c)<32 or ord(c)==127 for c in relative): raise ValueError('Source path invalid')
            try: fd=os.open(item,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            except OSError: raise ValueError('Source file unavailable') from None
            try:
                before=os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>64*1024*1024: raise ValueError
                total+=before.st_size
                if total>2*1024**3 or len(entries)>=20000: raise ValueError
                h=hashlib.sha256(); size=0
                while True:
                    check(); block=os.read(fd,65536)
                    if not block: break
                    h.update(block); size+=len(block)
                    if size>before.st_size: raise ValueError
                after=os.fstat(fd); named=item.lstat()
                if size!=before.st_size or (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns,before.st_mode)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_mode) or (before.st_dev,before.st_ino)!=(named.st_dev,named.st_ino): raise ValueError
                entries.append(('file',relative,size,stat.S_IMODE(before.st_mode),h.hexdigest())); check()
            finally: os.close(fd)
    return 'sha256:'+hashlib.sha256(_canonical(entries)).hexdigest()

