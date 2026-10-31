"""Protected, inherited-listener GitHub broker service for an enrolled local owner.

The launcher must bind and protect the socket and retain process ownership. This
module neither creates a socket nor authenticates a process by its account.
The creator and root administrators remain trusted to preserve the enrolled
descriptor and path throughout the service lifetime.
"""
import asyncio
import json
import os
import re
import socket
import stat

from engineering_hub.github_oauth_broker import GitHubOAuthBroker, serve_github_oauth_broker
from engineering_hub.preview_protocol import _origin
from .local_providers import ImmutableTestSecretResolver


class BrokerServiceError(RuntimeError):
    def __init__(self): super().__init__('GitHub broker service unavailable')


def read_enrollment(fd):
    """Read an O_RDONLY descriptor to independently root-owned enrollment."""
    try:
        if type(fd) is not int or fd < 3: raise ValueError()
        import fcntl
        if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY: raise ValueError()
        before=os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o644 or not 0 < before.st_size <= 16384): raise ValueError()
        raw=os.pread(fd,16385,0); after=os.fstat(fd)
        if ((before.st_dev,before.st_ino,before.st_uid,before.st_mode,before.st_nlink,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
                != (after.st_dev,after.st_ino,after.st_uid,after.st_mode,after.st_nlink,after.st_size,after.st_mtime_ns,after.st_ctime_ns)
                or len(raw)!=before.st_size): raise ValueError()
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result: raise ValueError()
                result[key]=value
            return result
        data=json.loads(raw,object_pairs_hook=unique,parse_float=lambda _: (_ for _ in ()).throw(ValueError()),
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        keys={'schema','service_uid','service_gid','peer_uid','peer_gid','socket_path','socket_uid','socket_gid',
              'socket_mode','listener_dev','listener_ino','path_dev','path_ino','listener_uid','listener_gid',
              'client_id','origin','secret_root','secret_slot','secret_version','secret_digest'}
        if (type(data) is not dict or set(data)!=keys or data['schema']!='engineering-hub.github-broker-service.v1'
                or json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()!=raw): raise ValueError()
        for key in ('service_uid','service_gid','peer_uid','peer_gid','socket_uid','socket_gid'):
            if type(data[key]) is not int or not 0 < data[key] < 2**32-1: raise ValueError()
        for key in ('listener_dev','listener_ino','path_dev','path_ino','listener_uid','listener_gid'):
            if type(data[key]) is not int or not 0 <= data[key] < 2**64: raise ValueError()
        if (type(data['socket_mode']) is not int or data['socket_mode'] != 0o660
                or data['socket_uid']!=data['service_uid'] or data['socket_gid']!=data['service_gid']
                or data['peer_uid']==data['service_uid']
                or type(data['secret_version']) is not int or data['secret_version']<1
                or type(data['secret_slot']) is not str or re.fullmatch(r'test-[a-z0-9][a-z0-9-]{0,90}',data['secret_slot']) is None
                or type(data['secret_digest']) is not str or re.fullmatch(r'sha256:[0-9a-f]{64}',data['secret_digest']) is None
                or type(data['client_id']) is not str or not 1<=len(data['client_id'])<=256
                or any(not 33<=ord(c)<=126 for c in data['client_id']) or _origin(data['origin'])!=data['origin']): raise ValueError()
        for key in ('socket_path','secret_root'):
            value=data[key]
            if type(value) is not str or not value.startswith('/') or value.startswith('//') or os.path.normpath(value)!=value: raise ValueError()
        return data
    except Exception: raise BrokerServiceError() from None


async def serve_enrolled_broker(*, enrollment_fd, listener, stop_event, on_ready=None):
    """Take ownership of a prebound listener until all admitted requests drain."""
    broker=None
    try:
        data=read_enrollment(enrollment_fd)
        if (os.getuid()!=data['service_uid'] or os.geteuid()!=data['service_uid']
                or os.getgid()!=data['service_gid'] or os.getegid()!=data['service_gid'] or os.getgroups()
                or type(stop_event) is not asyncio.Event or type(listener) is not socket.socket
                or listener.family!=socket.AF_UNIX or listener.getsockopt(socket.SOL_SOCKET,socket.SO_TYPE)!=socket.SOCK_STREAM
                or listener.getsockopt(socket.SOL_SOCKET,socket.SO_ACCEPTCONN)!=1
                or listener.getsockname()!=data['socket_path']): raise ValueError()
        info=os.lstat(data['socket_path'])
        opened=os.fstat(listener.fileno())
        import struct
        _, creator_uid, creator_gid=struct.unpack('=iII',listener.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid!=data['socket_uid'] or info.st_gid!=data['socket_gid']
                or stat.S_IMODE(info.st_mode)!=data['socket_mode']
                or (opened.st_dev,opened.st_ino)!=(data['listener_dev'],data['listener_ino'])
                or (info.st_dev,info.st_ino)!=(data['path_dev'],data['path_ino'])
                or (creator_uid,creator_gid)!=(data['listener_uid'],data['listener_gid'])): raise ValueError()
        with ImmutableTestSecretResolver(data['secret_root'],bindings=((data['secret_slot'],data['secret_version'],data['secret_digest']),)) as resolver:
            secret=resolver.resolve_test_secret(data['secret_slot'],data['secret_version'])
        again=os.lstat(data['socket_path'])
        current=os.fstat(listener.fileno())
        if ((info.st_dev,info.st_ino,info.st_uid,info.st_gid,info.st_mode)!=(again.st_dev,again.st_ino,again.st_uid,again.st_gid,again.st_mode)
                or (opened.st_dev,opened.st_ino)!=(current.st_dev,current.st_ino)): raise ValueError()
        broker=GitHubOAuthBroker(client_id=data['client_id'],client_secret=secret,secret_sha256=data['secret_digest'],
                                 origin=data['origin'],peer_uid=data['peer_uid'],peer_gid=data['peer_gid'])
        secret=None
        await serve_github_oauth_broker(listener,broker,stop_event,on_ready=on_ready)
    except asyncio.CancelledError: raise
    except Exception: raise BrokerServiceError() from None
    finally:
        if broker is not None: broker.close()
        if type(listener) is socket.socket: listener.close()
