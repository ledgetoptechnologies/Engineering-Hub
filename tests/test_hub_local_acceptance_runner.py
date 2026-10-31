"""Fixed runner FD admission tests; never contacts a Docker daemon."""
import os
import json
from pathlib import Path
import subprocess
import sys
import threading
import ctypes
import time

import pytest

if sys.platform != 'linux':
    pytest.skip('local acceptance runner requires Linux', allow_module_level=True)

from scripts import hub_local_acceptance_runner as runner

_BOOT='import os,sys; fd=int(sys.argv[1]); os.dup2(fd,3); os.close(fd) if fd!=3 else None; os.set_inheritable(3,True); os.execv(sys.executable,[sys.executable,"-I","-B",sys.argv[2],"--request-fd","3"])'


@pytest.mark.skipif(sys.platform != 'linux',reason='fixed Linux FD entrypoint')
@pytest.mark.parametrize('payload',[b'{}',b'{"z":1, "a":2}',b'{"x":1,"x":2}'],
                         ids=['wrong-schema','noncanonical','duplicate'])
def test_fixed_fd_rejects_invalid_request_without_effects(tmp_path,payload):
    entry=Path(__file__).resolve().parents[1]/'scripts/hub_local_acceptance_runner.py'
    read_fd,write_fd=os.pipe()
    try:
        os.write(write_fd,payload); os.close(write_fd); write_fd=None
        child=subprocess.run([sys.executable,'-I','-B','-c',_BOOT,str(read_fd),str(entry)],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=tmp_path,
            env={'LANG':'C.UTF-8','PATH':'/usr/bin:/bin'},pass_fds=(read_fd,),timeout=10)
    finally:
        os.close(read_fd)
        if write_fd is not None: os.close(write_fd)
    assert child.returncode==2 and child.stderr==b''
    assert child.stdout==b'{"error":"local_acceptance_held"}\n'
    assert list(tmp_path.iterdir())==[]


@pytest.mark.skipif(sys.platform != 'linux',reason='Linux tmpfs check')
def test_bounded_volume_rejects_non_tmpfs(tmp_path,monkeypatch):
    class Libc:
        def fstatfs(self,_fd,pointer):
            ctypes.cast(pointer,ctypes.POINTER(ctypes.c_long))[0]=0
            return 0
    monkeypatch.setattr(runner.ctypes,'CDLL',lambda *_a,**_k:Libc())
    with pytest.raises(ValueError,match='bounded tmpfs unavailable'):
        runner._bounded_volume(tmp_path)


@pytest.mark.skipif(sys.platform != 'linux',reason='Linux tmpfs check')
@pytest.mark.parametrize('blocks,available,inodes,free',[(131073,131073,4096,4096),(100,1,4096,4096),
                                                           (10000,10000,4097,100),(10000,10000,4096,63)])
def test_bounded_volume_rejects_oversized_or_full_filesystem(tmp_path,monkeypatch,blocks,available,inodes,free):
    from types import SimpleNamespace
    class Libc:
        def fstatfs(self,_fd,pointer):
            ctypes.cast(pointer,ctypes.POINTER(ctypes.c_long))[0]=0x01021994
            return 0
    monkeypatch.setattr(runner.ctypes,'CDLL',lambda *_a,**_k:Libc())
    monkeypatch.setattr(runner.os,'fstatvfs',lambda _fd:SimpleNamespace(f_frsize=4096,f_blocks=blocks,
        f_bavail=available,f_files=inodes,f_ffree=free))
    with pytest.raises(ValueError,match='bounded tmpfs unavailable'):
        runner._bounded_volume(tmp_path)


@pytest.mark.skipif(sys.platform != 'linux',reason='Linux tmpfs check')
def test_bounded_volume_accepts_fixed_data_and_inode_limits(tmp_path,monkeypatch):
    from types import SimpleNamespace
    class Libc:
        def fstatfs(self,_fd,pointer):
            ctypes.cast(pointer,ctypes.POINTER(ctypes.c_long))[0]=0x01021994
            return 0
    monkeypatch.setattr(runner.ctypes,'CDLL',lambda *_a,**_k:Libc())
    monkeypatch.setattr(runner.os,'fstatvfs',lambda _fd:SimpleNamespace(f_frsize=4096,f_blocks=131072,
        f_bavail=120000,f_files=4096,f_ffree=4000))
    runner._bounded_volume(tmp_path)


@pytest.mark.skipif(sys.platform != 'linux',reason='Linux file-size limit')
def test_main_preserves_stricter_per_file_limit_before_run(monkeypatch,capsys):
    calls=[]
    monkeypatch.setattr(runner.sys,'argv',['runner','--request-fd','3'])
    monkeypatch.setattr(runner,'_read_request',lambda fd:{})
    monkeypatch.setattr(runner.os,'close',lambda fd:None)
    monkeypatch.setattr(runner.resource,'getrlimit',lambda _resource:(4*1024*1024,32*1024*1024))
    monkeypatch.setattr(runner.resource,'setrlimit',lambda which,value:calls.append((which,value)))
    async def run(_request):
        assert calls==[(runner.resource.RLIMIT_FSIZE,(4*1024*1024,4*1024*1024))]
        return {'state':'test'}
    monkeypatch.setattr(runner,'run',run)
    assert runner.main()==0 and json.loads(capsys.readouterr().out)=={'state':'test'}


def test_selector_construction_failure_reaps_bootstrap_child(monkeypatch):
    started=[]; original=subprocess.Popen
    def start(*args,**kwargs):
        process=original(*args,**kwargs); started.append(process); return process
    monkeypatch.setattr(runner.subprocess,'Popen',start)
    monkeypatch.setattr(runner.selectors,'DefaultSelector',lambda:(_ for _ in ()).throw(OSError('fixture')))
    with pytest.raises(OSError): runner._child({})
    assert len(started)==1 and started[0].poll() is not None
    assert all(stream.closed for stream in (started[0].stdin,started[0].stdout,started[0].stderr))


@pytest.mark.skipif(sys.platform != 'linux',reason='fixed Linux FD entrypoint')
@pytest.mark.parametrize('case',['oversized','open-writer'])
def test_fixed_fd_bounds_and_sanitizes_child(tmp_path,case):
    entry=Path(__file__).resolve().parents[1]/'scripts/hub_local_acceptance_runner.py'
    read_fd,write_fd=os.pipe(); process=None; writer=None
    try:
        process=subprocess.Popen([sys.executable,'-I','-B','-c',_BOOT,str(read_fd),str(entry)],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=tmp_path,
            env={'LANG':'C.UTF-8','PATH':'/usr/bin:/bin'},pass_fds=(read_fd,))
        os.close(read_fd); read_fd=None
        if case=='oversized':
            def send():
                try:
                    for _ in range(1025): os.write(write_fd,b'x'*8192)
                except OSError:
                    pass
                finally:
                    os.close(write_fd)
            writer=threading.Thread(target=send); writer.start()
        else:
            os.write(write_fd,b'{"cleanup_key":"sensitive-marker"}')
        started=time.monotonic()
        output,error=process.communicate(timeout=20)
        if case=='open-writer': assert time.monotonic()-started >= 8
        assert process.returncode==2 and error==b''
        assert output==b'{"error":"local_acceptance_held"}\n' and b'sensitive-marker' not in output
    finally:
        if process is not None and process.poll() is None:
            process.kill(); process.wait(timeout=5)
        if read_fd is not None: os.close(read_fd)
        if writer is None: os.close(write_fd)
        else:
            writer.join(timeout=5)
            assert not writer.is_alive()
    assert list(tmp_path.iterdir())==[]
