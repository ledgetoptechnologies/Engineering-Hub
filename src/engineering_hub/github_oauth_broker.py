"""Fixed GitHub OAuth exchange over a trusted, exclusive local UDS channel.

The caller owns listener/path custody and must independently enroll peer IDs.
SO_PEERCRED is not executable identity or protection against same-UID relays.
No access token or broker secret is returned over the local channel.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import socket
import ssl
import struct
import time

import httpx
import certifi

_FRAME=struct.Struct('!I')
_PEER=struct.Struct('=iII')
_NONCE=re.compile(r'[0-9a-f]{64}\Z')
_VERIFIER=re.compile(r'[A-Za-z0-9._~-]{43,128}\Z')
_LIMIT=8192
_TOKEN_URL='https://github.com/login/oauth/access_token'
_USER_URL='https://api.github.com/user'


class OAuthBrokerError(RuntimeError):
    def __init__(self): super().__init__('GitHub identity exchange unavailable')


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')


def _decode(raw):
    def pairs(items):
        value={}
        for key,item in items:
            if key in value: raise ValueError()
            value[key]=item
        return value
    value=json.loads(raw,object_pairs_hook=pairs,parse_float=lambda _:(_ for _ in ()).throw(ValueError()),
                     parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    if _canonical(value)!=raw: raise ValueError()
    return value


def _code(value):
    return type(value) is str and 1<=len(value)<=1024 and all(33<=ord(x)<=126 for x in value)


async def _read_frame(reader):
    size=_FRAME.unpack(await reader.readexactly(_FRAME.size))[0]
    if not 0<size<=_LIMIT: raise ValueError()
    return _decode(await reader.readexactly(size))


async def _send_frame(writer,value):
    raw=_canonical(value)
    if not 0<len(raw)<=_LIMIT: raise ValueError()
    writer.write(_FRAME.pack(len(raw))+raw)
    await writer.drain()


class GitHubOAuthBroker:
    """Serve one request per connection with explicit trusted-local peer pins.

    Use an already-owned listener with at most one accepted operation per
    handler and a bounded service admission limit. Nonce memory is bounded and
    process-local; it is not restart-safe replay protection. OAuth state and
    one-use session admission remain the coordinator's responsibility.
    """
    def __init__(self, *, client_id, client_secret, secret_sha256, origin,
                 peer_uid, peer_gid, timeout_seconds=10, transport=None):
        try:
            from engineering_hub.preview_protocol import _origin
            if (type(client_id) is not str or not 1<=len(client_id)<=256 or any(not 33<=ord(c)<=126 for c in client_id)
                or type(client_secret) is not str or not 1<=len(client_secret)<=1024 or any(not 33<=ord(c)<=126 for c in client_secret)
                or type(secret_sha256) is not str or secret_sha256!='sha256:'+hashlib.sha256(client_secret.encode('ascii')).hexdigest()
                or _origin(origin)!=origin
                or any(type(x) is not int or not 0<=x<2**32-1 for x in (peer_uid,peer_gid))
                or type(timeout_seconds) is not int or not 1<=timeout_seconds<=30
                or (transport is not None and type(transport) is not httpx.MockTransport)): raise ValueError()
            self._client_id=client_id;self._secret=client_secret;self._origin=origin
            self._uid=peer_uid;self._gid=peer_gid;self._timeout=timeout_seconds;self._transport=transport
            self._used={};self._closed=False;self._active=0;self._pid=os.getpid()
        except Exception: raise OAuthBrokerError() from None

    async def handle(self,reader,writer):
        admitted=False
        try:
            async with asyncio.timeout(self._timeout):
                if self._closed or os.getpid()!=self._pid or self._active>=16: raise ValueError()
                self._active+=1;admitted=True
                sock=writer.get_extra_info('socket')
                pid,uid,gid=_PEER.unpack(sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,_PEER.size))
                if pid<0 or uid!=self._uid or gid!=self._gid: raise ValueError()
                value=await _read_frame(reader)
                if await reader.read(1)!=b'': raise ValueError()
                now=time.monotonic()
                self._used={key:expiry for key,expiry in self._used.items() if expiry>now}
                if (type(value) is not dict or set(value)!={'protocol','nonce','code','verifier'}
                    or value['protocol']!='engineering-hub.github-oauth-request.v1' or type(value['nonce']) is not str
                    or not _NONCE.fullmatch(value['nonce']) or not _code(value['code'])
                    or type(value['verifier']) is not str or not _VERIFIER.fullmatch(value['verifier'])
                    or value['nonce'] in self._used or len(self._used)>=1024): raise ValueError()
                self._used[value['nonce']]=now+300  # Bounded, process-local replay window.
                context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.load_verify_locations(cafile=certifi.where())
                async with httpx.AsyncClient(timeout=self._timeout,trust_env=False,follow_redirects=False,verify=context,transport=self._transport) as client:
                    async with client.stream('POST',_TOKEN_URL,headers={'Accept':'application/json','Accept-Encoding':'identity'},data={
                        'client_id':self._client_id,'client_secret':self._secret,'code':value['code'],
                        'code_verifier':value['verifier'],'redirect_uri':self._origin+'/api/hub/v1/auth/callback'}) as response:
                        response.raise_for_status();body=await _bounded_body(response)
                    result=_provider_json(body);token=result.get('access_token') if type(result) is dict and 'error' not in result else None
                    if type(token) is not str or not 1<=len(token)<=4096 or not token.isascii() or any(ord(c)<33 or ord(c)>126 for c in token): raise ValueError()
                    async with client.stream('GET',_USER_URL,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','Accept-Encoding':'identity'}) as response:
                        response.raise_for_status();body=await _bounded_body(response)
                    del token
                    result=_provider_json(body); github_id=result.get('id') if type(result) is dict else None
                    if type(github_id) is not int or not 0<github_id<2**63: raise ValueError()
                await _send_frame(writer,{'protocol':'engineering-hub.github-oauth-response.v1','nonce':value['nonce'],'github_id':github_id})
        except Exception:
            # Never echo provider output, bearer tokens, code, or exceptions.
            pass
        finally:
            if admitted:self._active-=1
            writer.close()
            try: await asyncio.wait_for(writer.wait_closed(),1)
            except Exception: pass

    def close(self):
        self._closed=True;self._secret='';self._used.clear()


async def serve_github_oauth_broker(listener, broker, stop_event):
    """Own one enrolled listener, stop admission and drain accepted exchanges.

    Listener ownership transfers here. The caller must establish and retain
    protected-path custody and separately start the independently configured
    broker; this function does not enroll either process or socket identity.
    """
    from engineering_hub.owned_tasks import finish_owned
    if (type(listener) is not socket.socket or listener.family!=socket.AF_UNIX
        or listener.getsockopt(socket.SOL_SOCKET,socket.SO_TYPE)!=socket.SOCK_STREAM
        or listener.getsockopt(socket.SOL_SOCKET,socket.SO_ACCEPTCONN)!=1
        or type(broker) is not GitHubOAuthBroker or broker._pid!=os.getpid()
        or type(stop_event) is not asyncio.Event): raise OAuthBrokerError()
    active=set()
    stopping=False
    def accepted(reader,writer):
        if stopping or len(active)>=16:
            writer.close();return
        task=asyncio.create_task(broker.handle(reader,writer));active.add(task);task.add_done_callback(active.discard)
    async def operation():
        nonlocal stopping
        server=None
        try:
            server=await asyncio.start_unix_server(accepted,sock=listener)
            await stop_event.wait()
        finally:
            stopping=True
            try:
                try:
                    if server is not None:
                        server.close();await server.wait_closed()
                finally: listener.close()
            finally:
                try:
                    if active: await asyncio.gather(*tuple(active),return_exceptions=True)
                finally: broker.close()
    task=asyncio.create_task(operation())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        stop_event.set()
        try: await finish_owned(task)
        finally: raise


async def _bounded_body(response):
    if response.headers.get('content-encoding','identity').strip().lower()!='identity': raise ValueError()
    raw=bytearray()
    # Identity encoding is required above, so this iterator cannot inflate a
    # compressed provider body; the chunk bound also covers cached test bodies.
    async for part in response.aiter_bytes(chunk_size=4096):
        if len(part)>65536-len(raw): raise ValueError()
        raw.extend(part)
    return bytes(raw)


def _provider_json(raw):
    def unique(items):
        result={}
        for key,value in items:
            if key in result: raise ValueError()
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))


class GitHubOAuthUnixClient:
    """One request per UDS connection, pinned peer and caller-owned path custody."""
    def __init__(self, *, socket_path, issuer_uid, issuer_gid, timeout_seconds=10):
        if (type(socket_path) is not str or not socket_path.startswith('/') or socket_path.startswith('//')
            or os.path.normpath(socket_path)!=socket_path or len(os.fsencode(socket_path))>=108
            or any(type(x) is not int or not 0<=x<2**32-1 for x in (issuer_uid,issuer_gid))
            or type(timeout_seconds) is not int or not 1<=timeout_seconds<=30): raise OAuthBrokerError()
        self._path=socket_path;self._uid=issuer_uid;self._gid=issuer_gid;self._timeout=timeout_seconds;self._pid=os.getpid()

    async def exchange(self,code,verifier):
        writer=None
        try:
            deadline=asyncio.get_running_loop().time()+self._timeout
            async with asyncio.timeout_at(deadline):
                if os.getpid()!=self._pid or not _code(code) or type(verifier) is not str or not _VERIFIER.fullmatch(verifier):raise ValueError()
                before=os.lstat(self._path)
                import stat
                if not stat.S_ISSOCK(before.st_mode):raise ValueError()
                reader,writer=await asyncio.open_unix_connection(self._path)
                sock=writer.get_extra_info('socket');pid,uid,gid=_PEER.unpack(sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,_PEER.size))
                after=os.lstat(self._path)
                if pid<0 or uid!=self._uid or gid!=self._gid or (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):raise ValueError()
                nonce=secrets.token_hex(32)
                await _send_frame(writer,{'protocol':'engineering-hub.github-oauth-request.v1','nonce':nonce,'code':code,'verifier':verifier})
                writer.write_eof();await writer.drain()
                response=await _read_frame(reader)
                if (type(response) is not dict or set(response)!={'protocol','nonce','github_id'}
                    or response['protocol']!='engineering-hub.github-oauth-response.v1' or response['nonce']!=nonce
                    or type(response['github_id']) is not int or not 0<response['github_id']<2**63):raise ValueError()
                if await reader.read(1)!=b'':raise ValueError()
                final=os.lstat(self._path)
                if (before.st_dev,before.st_ino)!=(final.st_dev,final.st_ino):raise ValueError()
                result=response['github_id']
                writer.close();await writer.wait_closed();writer=None
                if asyncio.get_running_loop().time()>=deadline:raise ValueError()
                return result
        except Exception: raise OAuthBrokerError() from None
        finally:
            if writer is not None:
                writer.close()
                try: await asyncio.wait_for(writer.wait_closed(),1)
                except Exception: pass
