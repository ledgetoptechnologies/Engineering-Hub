"""Owned subprocess test: synthetic transport, no external network or secrets."""
import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

import pytest
pytest.importorskip('fcntl')
from hub_container_executor import github_broker_owner as owner
from tests.test_hub_github_broker_service import enrollment, fd_for


def test_stop_write_failure_still_waits_for_owned_child(monkeypatch):
    from hub_container_executor.github_broker_service import BrokerServiceError
    seen=[]
    class Child:
        def wait(self,timeout): seen.append(timeout);return 0
    process=owner.OwnedBrokerProcess();process._process=Child()
    read_fd,write_fd=os.pipe();process._stop=write_fd
    monkeypatch.setattr(owner.os,'write',lambda *_: (_ for _ in ()).throw(OSError()))
    try:
        with pytest.raises(BrokerServiceError): process.stop(timeout_seconds=2)
        assert seen==[2]
    finally: os.close(read_fd)


def test_second_pipe_failure_closes_first_pair(monkeypatch):
    original=os.pipe2; created=[]
    def pipe(flags):
        if created: raise OSError('synthetic')
        pair=original(flags);created.extend(pair);return pair
    monkeypatch.setattr(owner,'_runtime',lambda _: {'source_root':'/unused','python_path':'/unused'})
    monkeypatch.setattr(owner,'read_enrollment',lambda _: {})
    monkeypatch.setattr(owner.os,'pipe2',pipe)
    process=owner.OwnedBrokerProcess()
    class Listener:
        def close(self): pass
    with pytest.raises(OSError): process.start(runtime_policy='/unused',enrollment_fd=3,listener=Listener())
    for fd in created:
        with pytest.raises(OSError): os.fstat(fd)


def test_pipe_cleanup_attempts_every_close_after_error(monkeypatch):
    from hub_container_executor.github_broker_service import BrokerServiceError
    original_pipe=os.pipe2;original_close=os.close;created=[];fail=[True]
    def pipe(flags):
        pair=original_pipe(flags);created.extend(pair);return pair
    def close(fd):
        original_close(fd)
        if fd in created and fail[0]:
            fail[0]=False;raise OSError('synthetic close failure')
    monkeypatch.setattr(owner,'_runtime',lambda _: {'source_root':'/unused','python_path':'/unused'})
    monkeypatch.setattr(owner,'read_enrollment',lambda _: {'service_uid':1,'service_gid':1})
    monkeypatch.setattr(owner.os,'pipe2',pipe);monkeypatch.setattr(owner.os,'close',close)
    class Listener:
        def fileno(self): return 9
        def close(self): pass
    monkeypatch.setattr(owner.subprocess,'Popen',lambda *a,**k: (_ for _ in ()).throw(ValueError()))
    with pytest.raises(BrokerServiceError):
        owner.OwnedBrokerProcess().start(runtime_policy='/unused',enrollment_fd=3,listener=Listener())
    for fd in created:
        with pytest.raises(OSError): os.fstat(fd)


@pytest.mark.skipif(os.getuid()!=0,reason='requires private root-owned synthetic ancestor')
def test_pinned_path_detects_replaced_ancestor():
    from hub_container_executor.github_broker_service import BrokerServiceError
    root=Path(tempfile.mkdtemp(prefix='eh-broker-pin-',dir='/root'))
    pin=None
    try:
        (root/'parent').mkdir();(root/'parent'/'leaf').mkdir()
        pin=owner._PinnedPath(root/'parent'/'leaf',directory=True)
        (root/'parent').rename(root/'previous');(root/'parent').mkdir();(root/'parent'/'leaf').mkdir()
        with pytest.raises(BrokerServiceError): pin.check()
    finally:
        if pin is not None: pin.close()
        shutil.rmtree(root)


@pytest.mark.skipif(os.getuid()!=0,reason='requires root to create owned synthetic nonroot children')
def test_owned_nonroot_broker_process_exchanges_and_drains(monkeypatch):
    root=Path(tempfile.mkdtemp(prefix='eh-broker-process-'));root.chmod(0o755)
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); fd=None
    process=owner.OwnedBrokerProcess()
    try:
        data=enrollment(root);path=data['socket_path']
        listener.bind(path);os.chown(path,12345,12346);os.chmod(path,0o660);listener.listen()
        opened=os.fstat(listener.fileno());named=os.lstat(path)
        data.update(listener_dev=opened.st_dev,listener_ino=opened.st_ino,path_dev=named.st_dev,path_ino=named.st_ino,
                    secret_digest='sha256:'+hashlib.sha256(b'synthetic').hexdigest())
        fd=fd_for(root,data)
        checkout=str(Path(__file__).resolve().parent.parent)
        monkeypatch.setattr(owner,'_runtime',lambda _: {'source_root':checkout,'python_path':sys.executable})
        # Child uses the real service/UDS protocol with only its test secret and
        # fixed GitHub HTTP transport substituted. No network is available.
        monkeypatch.setattr(owner,'_BOOT',"""import sys
root=sys.argv[1];sys.path[:0]=[root,root+'/src']
import httpx
import hub_container_executor.github_broker_service as s
from engineering_hub.github_oauth_broker import GitHubOAuthBroker
class R:
 def __init__(self,*a,**k): pass
 def __enter__(self): return self
 def __exit__(self,*a): pass
 def resolve_test_secret(self,*a): return 'synthetic'
s.ImmutableTestSecretResolver=R
async def response(request):
 return httpx.Response(200,json={'access_token':'synthetic'} if request.method=='POST' else {'id':123})
s.GitHubOAuthBroker=lambda **kw: GitHubOAuthBroker(**kw,transport=httpx.MockTransport(response))
from hub_container_executor.github_broker_process import main
raise SystemExit(main(sys.argv[2:]))
""")
        process.start(runtime_policy='/unused-test-policy',enrollment_fd=fd,listener=listener,timeout_seconds=10)
        client="""import asyncio,sys
root=sys.argv[1];sys.path[:0]=[root,root+'/src']
from engineering_hub.github_oauth_broker import GitHubOAuthUnixClient
c=GitHubOAuthUnixClient(socket_path=sys.argv[2],issuer_uid=12345,issuer_gid=12346)
print(asyncio.run(c.exchange('synthetic-code','a'*43)))
"""
        result=subprocess.run([sys.executable,'-I','-B','-c',client,checkout,path],user=12347,group=12346,
                              extra_groups=(),env={'LANG':'C.UTF-8'},capture_output=True,timeout=10)
        assert result.returncode==0, result.stderr
        assert result.stdout==b'123\n'
        process.stop(timeout_seconds=10)
        assert process._process.poll()==0
    finally:
        if process._process is not None and process._process.poll() is None:
            process.stop(timeout_seconds=10)
        if fd is not None: os.close(fd)
        listener.close();shutil.rmtree(root)
