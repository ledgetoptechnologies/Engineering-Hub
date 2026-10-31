"""Synthetic OAuth over real local sockets; no GitHub or host authority."""
import asyncio
import hashlib
import json
import os
import socket
import struct

import httpx
import pytest

from engineering_hub.github_oauth_broker import GitHubOAuthBroker, GitHubOAuthUnixClient, OAuthBrokerError, serve_github_oauth_broker

pytestmark=pytest.mark.skipif(not hasattr(__import__('socket'),'SO_PEERCRED'),reason='Linux UDS peer credentials required')


@pytest.mark.asyncio
async def test_real_uds_broker_returns_only_identity_with_fixed_provider_urls(tmp_path,monkeypatch):
    calls=[]
    keylog=tmp_path/'keylog';monkeypatch.setenv('SSLKEYLOGFILE',str(keylog))
    original=httpx.AsyncClient
    def fixed_client(*args,**kwargs):
        assert kwargs['verify'].keylog_filename is None
        return original(*args,**kwargs)
    monkeypatch.setattr(httpx,'AsyncClient',fixed_client)
    async def provider(request):
        calls.append((request.method,str(request.url),request.headers.get('authorization')))
        if len(calls)==1:
            assert b'client_secret=private-synthetic' in await request.aread()
            return httpx.Response(200,json={'access_token':'secret-bearer'})
        assert request.headers['authorization']=='Bearer secret-bearer'
        return httpx.Response(200,json={'id':123})
    broker=GitHubOAuthBroker(client_id='fixture',client_secret='private-synthetic',
        secret_sha256='sha256:'+hashlib.sha256(b'private-synthetic').hexdigest(),origin='https://pilot.example.test',
        peer_uid=os.geteuid(),peer_gid=os.getegid(),transport=httpx.MockTransport(provider))
    path=str(tmp_path/'broker.sock');server=await asyncio.start_unix_server(broker.handle,path=path)
    try:
        client=GitHubOAuthUnixClient(socket_path=path,issuer_uid=os.geteuid(),issuer_gid=os.getegid())
        assert await client.exchange('one-time-code','v'*43)==123
        assert calls==[('POST','https://github.com/login/oauth/access_token',None),('GET','https://api.github.com/user','Bearer secret-bearer')]
        assert not keylog.exists()
    finally:
        server.close();await server.wait_closed();broker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('response',[
    httpx.Response(302,headers={'Location':'https://evil.invalid/'}),
    httpx.Response(200,content=b'{"access_token":"a","access_token":"b"}'),
    httpx.Response(200,content=b'x'*65537),
    httpx.Response(200,headers={'Content-Encoding':'gzip'},stream=httpx.ByteStream(b'not-decoded')),
])
async def test_bad_provider_response_fails_closed(tmp_path,response):
    async def provider(_):return response
    broker=GitHubOAuthBroker(client_id='fixture',client_secret='secret',secret_sha256='sha256:'+hashlib.sha256(b'secret').hexdigest(),
        origin='https://pilot.example.test',peer_uid=os.geteuid(),peer_gid=os.getegid(),transport=httpx.MockTransport(provider))
    path=str(tmp_path/'broker.sock');server=await asyncio.start_unix_server(broker.handle,path=path)
    try:
        client=GitHubOAuthUnixClient(socket_path=path,issuer_uid=os.geteuid(),issuer_gid=os.getegid())
        with pytest.raises(OAuthBrokerError): await client.exchange('code','v'*43)
    finally:
        server.close();await server.wait_closed();broker.close()


@pytest.mark.asyncio
async def test_wrong_broker_peer_denies_before_code(tmp_path):
    received=[];done=asyncio.Event()
    async def handler(reader,writer):
        try: received.append(await reader.read(4096))
        finally: writer.close();await writer.wait_closed();done.set()
    path=str(tmp_path/'broker.sock');server=await asyncio.start_unix_server(handler,path=path)
    try:
        client=GitHubOAuthUnixClient(socket_path=path,issuer_uid=os.geteuid()+1,issuer_gid=os.getegid())
        with pytest.raises(OAuthBrokerError): await client.exchange('code','v'*43)
        await asyncio.wait_for(done.wait(),2)
        assert received==[b'']
    finally:server.close();await server.wait_closed()


def test_broker_requires_independent_secret_pin():
    with pytest.raises(OAuthBrokerError):
        GitHubOAuthBroker(client_id='fixture',client_secret='secret',secret_sha256='sha256:'+'0'*64,
                          origin='https://pilot.example.test',peer_uid=0,peer_gid=0)


@pytest.mark.parametrize('origin',['https://pilot.example.test\n','https://pilot.example.test\\evil','https://pilot.example.test:99999','https://PILOT.example.test'])
def test_broker_rejects_noncanonical_origin(origin):
    with pytest.raises(OAuthBrokerError):
        GitHubOAuthBroker(client_id='fixture',client_secret='secret',secret_sha256='sha256:'+hashlib.sha256(b'secret').hexdigest(),
                          origin=origin,peer_uid=0,peer_gid=0)


@pytest.mark.asyncio
async def test_replayed_nonce_denied_without_second_github_exchange(tmp_path):
    calls=[]
    async def provider(request):
        calls.append(request.method)
        return httpx.Response(200,json={'access_token':'token'} if request.method=='POST' else {'id':123})
    broker=GitHubOAuthBroker(client_id='fixture',client_secret='secret',secret_sha256='sha256:'+hashlib.sha256(b'secret').hexdigest(),
        origin='https://pilot.example.test',peer_uid=os.geteuid(),peer_gid=os.getegid(),transport=httpx.MockTransport(provider))
    path=str(tmp_path/'broker.sock');server=await asyncio.start_unix_server(broker.handle,path=path)
    async def send():
        reader,writer=await asyncio.open_unix_connection(path)
        raw=json.dumps({'protocol':'engineering-hub.github-oauth-request.v1','nonce':'a'*64,'code':'code','verifier':'v'*43},sort_keys=True,separators=(',',':')).encode()
        writer.write(struct.pack('!I',len(raw))+raw);writer.write_eof();await writer.drain()
        answer=await reader.read();writer.close();await writer.wait_closed();return answer
    try:
        assert await send()
        assert await send()==b''
        assert calls==['POST','GET']
    finally:server.close();await server.wait_closed();broker.close()


@pytest.mark.asyncio
async def test_owned_listener_drains_exchange_before_release(tmp_path):
    entered=asyncio.Event();release=asyncio.Event()
    async def provider(request):
        if request.method=='POST':
            entered.set();await release.wait();return httpx.Response(200,json={'access_token':'test-token'})
        return httpx.Response(200,json={'id':123})
    broker=GitHubOAuthBroker(client_id='fixture',client_secret='secret',secret_sha256='sha256:'+hashlib.sha256(b'secret').hexdigest(),
        origin='https://pilot.example.test',peer_uid=os.geteuid(),peer_gid=os.getegid(),transport=httpx.MockTransport(provider))
    path=str(tmp_path/'broker.sock');listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    listener.bind(path);listener.listen(8);listener.setblocking(False)
    stop=asyncio.Event();serving=asyncio.create_task(serve_github_oauth_broker(listener,broker,stop)); exchange=None
    try:
        client=GitHubOAuthUnixClient(socket_path=path,issuer_uid=os.geteuid(),issuer_gid=os.getegid())
        exchange=asyncio.create_task(client.exchange('code','v'*43))
        await asyncio.wait_for(entered.wait(),2);stop.set();await asyncio.sleep(0)
        assert not serving.done()
        release.set();assert await asyncio.wait_for(exchange,2)==123
        await asyncio.wait_for(serving,2)
        assert broker._closed and listener.fileno()==-1
    finally:
        release.set();stop.set()
        if exchange is not None:await asyncio.gather(exchange,return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(serving,return_exceptions=True),2)


@pytest.mark.asyncio
async def test_shutdown_error_still_drains_and_clears_secret(tmp_path,monkeypatch):
    entered=asyncio.Event();release=asyncio.Event();original=asyncio.start_unix_server
    async def provider(request):
        if request.method=='POST': entered.set();await release.wait();return httpx.Response(200,json={'access_token':'token'})
        return httpx.Response(200,json={'id':123})
    broker=GitHubOAuthBroker(client_id='fixture',client_secret='secret',secret_sha256='sha256:'+hashlib.sha256(b'secret').hexdigest(),
        origin='https://pilot.example.test',peer_uid=os.geteuid(),peer_gid=os.getegid(),transport=httpx.MockTransport(provider))
    async def failing_server(*args,**kwargs):
        actual=await original(*args,**kwargs)
        class Wrapper:
            def close(self):raise OSError('synthetic shutdown failure')
            async def wait_closed(self):await actual.wait_closed()
        return Wrapper()
    monkeypatch.setattr(asyncio,'start_unix_server',failing_server)
    path=str(tmp_path/'broker.sock');listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);listener.bind(path);listener.listen(8);listener.setblocking(False)
    stop=asyncio.Event();serving=asyncio.create_task(serve_github_oauth_broker(listener,broker,stop)); exchange=None
    try:
        client=GitHubOAuthUnixClient(socket_path=path,issuer_uid=os.geteuid(),issuer_gid=os.getegid())
        exchange=asyncio.create_task(client.exchange('code','v'*43));await asyncio.wait_for(entered.wait(),2)
        stop.set();await asyncio.sleep(0);assert not serving.done()
        release.set();assert await asyncio.wait_for(exchange,2)==123
        with pytest.raises(OSError,match='shutdown failure'):await asyncio.wait_for(serving,2)
        assert broker._closed and broker._secret=='' and listener.fileno()==-1
    finally:
        release.set();stop.set()
        if exchange is not None:await asyncio.gather(exchange,return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(serving,return_exceptions=True),2)


@pytest.mark.asyncio
async def test_client_rejects_success_after_blocking_close_deadline(tmp_path,monkeypatch):
    import time
    async def provider(request):return httpx.Response(200,json={'access_token':'token'} if request.method=='POST' else {'id':123})
    broker=GitHubOAuthBroker(client_id='fixture',client_secret='secret',secret_sha256='sha256:'+hashlib.sha256(b'secret').hexdigest(),
        origin='https://pilot.example.test',peer_uid=os.geteuid(),peer_gid=os.getegid(),transport=httpx.MockTransport(provider))
    path=str(tmp_path/'broker.sock');server=await asyncio.start_unix_server(broker.handle,path=path)
    original=asyncio.open_unix_connection
    async def slow_open(*args,**kwargs):
        reader,writer=await original(*args,**kwargs)
        class Wrapper:
            def __getattr__(self,name):return getattr(writer,name)
            async def wait_closed(self):time.sleep(1.05);await writer.wait_closed()
        return reader,Wrapper()
    monkeypatch.setattr(asyncio,'open_unix_connection',slow_open)
    try:
        client=GitHubOAuthUnixClient(socket_path=path,issuer_uid=os.geteuid(),issuer_gid=os.getegid(),timeout_seconds=1)
        with pytest.raises(OAuthBrokerError):await client.exchange('code','v'*43)
    finally:server.close();await server.wait_closed();broker.close()


@pytest.mark.asyncio
async def test_auth_router_uses_real_uds_broker_without_exposing_token(tmp_path):
    from fastapi import FastAPI
    from urllib.parse import parse_qs,urlsplit
    from engineering_hub.auth import AuthConfig,SessionStore,build_auth_router
    calls=[]
    async def provider(request):
        calls.append((request.method,str(request.url)))
        return httpx.Response(200,json={'access_token':'test-only-token'} if request.method=='POST' else {'id':123})
    broker=GitHubOAuthBroker(client_id='fixture',client_secret='secret',secret_sha256='sha256:'+hashlib.sha256(b'secret').hexdigest(),
        origin='https://pilot.example.test',peer_uid=os.geteuid(),peer_gid=os.getegid(),transport=httpx.MockTransport(provider))
    path=str(tmp_path/'broker.sock');server=await asyncio.start_unix_server(broker.handle,path=path)
    try:
        client=GitHubOAuthUnixClient(socket_path=path,issuer_uid=os.geteuid(),issuer_gid=os.getegid())
        auth=AuthConfig('https://pilot.example.test','fixture','secret',123)
        app=FastAPI();app.include_router(build_auth_router(auth,SessionStore(tmp_path/'hub.db'),client))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=auth.origin,follow_redirects=False) as browser:
            login=await browser.get('/api/hub/v1/auth/login');assert login.status_code==302
            state=parse_qs(urlsplit(login.headers['location']).query)['state'][0]
            wrong=await browser.get('/api/hub/v1/auth/callback',params={'state':'wrong','code':'code'})
            assert wrong.status_code==400 and calls==[]
            callback=await browser.get('/api/hub/v1/auth/callback',params={'state':state,'code':'code'})
            assert callback.status_code==303 and 'test-only-token' not in callback.text
            me=(await browser.get('/api/hub/v1/auth/me')).json();assert me['actor']=='human:123' and me['authenticated']
            assert (await browser.get('/api/hub/v1/auth/callback',params={'state':state,'code':'code'})).status_code==400
            assert len(calls)==2
    finally:server.close();await server.wait_closed();broker.close()
