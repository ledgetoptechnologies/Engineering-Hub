import asyncio
import json
import os
import socket
import pytest

pytest.importorskip('fcntl')
from hub_container_executor import github_broker_service as service


def enrollment(tmp_path):
    return {'schema':'engineering-hub.github-broker-service.v1','service_uid':12345,'service_gid':12346,
            'peer_uid':12347,'peer_gid':12346,'socket_path':str(tmp_path/'oauth.sock'),
            'socket_uid':12345,'socket_gid':12346,'socket_mode':0o660,'listener_dev':1,'listener_ino':2,
            'path_dev':3,'path_ino':4,'listener_uid':0,'listener_gid':0,'client_id':'fixture',
            'origin':'https://example.test','secret_root':str(tmp_path/'secrets'),'secret_slot':'test-github',
            'secret_version':1,'secret_digest':'sha256:'+'a'*64}


def fd_for(tmp_path,data):
    path=tmp_path/'enrollment';path.write_bytes(json.dumps(data,sort_keys=True,separators=(',',':')).encode());path.chmod(0o644)
    return os.open(path,os.O_RDONLY)


@pytest.mark.skipif(os.getuid()!=0,reason='requires root-owned synthetic enrollment')
def test_enrollment_is_closed_read_only_and_root_owned(tmp_path):
    data=enrollment(tmp_path);fd=fd_for(tmp_path,data)
    try:
        assert service.read_enrollment(fd)==data
        with pytest.raises(service.BrokerServiceError): service.read_enrollment(1)
    finally: os.close(fd)
    data['peer_uid']=True;fd=fd_for(tmp_path,data)
    try:
        with pytest.raises(service.BrokerServiceError): service.read_enrollment(fd)
    finally: os.close(fd)


@pytest.mark.asyncio
@pytest.mark.skipif(os.getuid()!=0,reason='requires root-owned synthetic enrollment')
async def test_invalid_listener_is_closed_before_secret_resolver(tmp_path,monkeypatch):
    fd=fd_for(tmp_path,enrollment(tmp_path));listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    monkeypatch.setattr(service,'ImmutableTestSecretResolver',lambda *a,**k: pytest.fail('secret read'))
    try:
        with pytest.raises(service.BrokerServiceError):
            await service.serve_enrolled_broker(enrollment_fd=fd,listener=listener,stop_event=asyncio.Event())
        assert listener.fileno()==-1
    finally: os.close(fd);listener.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.getuid()!=0,reason='requires root-owned synthetic enrollment')
async def test_wrong_expected_enrollment_denies_before_secret(tmp_path,monkeypatch):
    fd=fd_for(tmp_path,enrollment(tmp_path));listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    monkeypatch.setattr(service,'ImmutableTestSecretResolver',lambda *a,**k: pytest.fail('secret read'))
    try:
        with pytest.raises(service.BrokerServiceError):
            await service.serve_enrolled_broker(enrollment_fd=fd,listener=listener,stop_event=asyncio.Event(),
                                                expected_enrollment_digest='sha256:'+'0'*64)
        assert listener.fileno()==-1
    finally: os.close(fd);listener.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.getuid()!=0,reason='requires root-owned synthetic enrollment')
@pytest.mark.parametrize('replace_path',[False,True])
async def test_enrolled_service_owns_listener_until_stop(tmp_path,monkeypatch,replace_path):
    import hashlib
    data=enrollment(tmp_path)
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    listener.bind(data['socket_path']);os.chown(data['socket_path'],12345,12346);os.chmod(data['socket_path'],0o660)
    listener.listen();listener.setblocking(False)
    opened=os.fstat(listener.fileno());named=os.lstat(data['socket_path'])
    data.update(listener_dev=opened.st_dev,listener_ino=opened.st_ino,path_dev=named.st_dev,path_ino=named.st_ino,
                secret_digest='sha256:'+hashlib.sha256(b'synthetic').hexdigest())
    fd=fd_for(tmp_path,data)
    replacement=None
    if replace_path:
        os.unlink(data['socket_path']);replacement=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        replacement.bind(data['socket_path']);os.chown(data['socket_path'],12345,12346);os.chmod(data['socket_path'],0o660)
    class Resolver:
        def __init__(self,*args,**kwargs):
            if replace_path: pytest.fail('secret read after socket replacement')
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def resolve_test_secret(self,*args): return 'synthetic'
    monkeypatch.setattr(service,'ImmutableTestSecretResolver',Resolver)
    monkeypatch.setattr(service.os,'getuid',lambda:12345);monkeypatch.setattr(service.os,'geteuid',lambda:12345)
    monkeypatch.setattr(service.os,'getgid',lambda:12346);monkeypatch.setattr(service.os,'getegid',lambda:12346)
    monkeypatch.setattr(service.os,'getgroups',lambda:[])
    stop=asyncio.Event();task=asyncio.create_task(service.serve_enrolled_broker(enrollment_fd=fd,listener=listener,stop_event=stop))
    try:
        if replace_path:
            with pytest.raises(service.BrokerServiceError): await task
            assert listener.fileno()==-1
            return
        await asyncio.sleep(0)
        assert not task.done()
        stop.set();await asyncio.wait_for(task,2)
        assert listener.fileno()==-1
    finally:
        stop.set()
        if not task.done(): await task
        os.close(fd);listener.close()
        if replacement is not None: replacement.close()
