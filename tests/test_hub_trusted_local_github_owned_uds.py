"""Owned GitHub split service over real UDS with synthetic signed authority."""
import asyncio
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import os
import socket
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

pytest.importorskip('fcntl')

from engineering_hub.auth import GitHubIdentityProvider
from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.trusted_queued_session import SCHEMA_V3, parse_enrollment
from hub_container_executor import installed_coordinator as installed
from hub_container_executor import trusted_local_session as local
from hub_container_executor.asgi import create_app as executor_app
from hub_container_executor.launch_transport import UnixCurrentObservationTransport
from tests.test_hub_container_executor_installed_coordinator import github_configuration, canonical
from tests.test_hub_container_executor_launch_supervisor import synthetic_issuer, short_root, root_journal, current_pins, current_ready
from tests.test_hub_current_observation_end_to_end import owned_supervisor_wait, request, NoCommands
from tests.test_hub_trusted_queued_session import v2_document, encode
from tests.test_hub_unified_pilot_startup import profile_document


@pytest.fixture
def owned_resources():
    with ExitStack() as stack:
        yield stack


@pytest.mark.asyncio
async def test_github_owned_split_coordinator_accepts_signed_session_over_uds(synthetic_issuer, monkeypatch, owned_resources):
    import engineering_hub.pilot_ownership as ownership
    synthetic_filesystem=ownership._local_filesystem
    monkeypatch.setattr(ownership,'_local_filesystem',lambda fd, **_:synthetic_filesystem(fd))
    authority, issuer_path, checks = synthetic_issuer
    data=github_configuration(); data['manifest']=profile_document()['manifest']
    manifest=PilotManifest.from_bytes(canonical(data['manifest']))
    authority.release=replace(authority.release,implementation_commit=manifest.driver.implementation_commit,
                              platform=manifest.runner_binding.platform)
    authority._daemon_pin=replace(authority._daemon_pin,daemon_id=manifest.runner_binding.daemon_identity)
    current_ready(authority)
    _, launch=current_pins(authority)
    from engineering_hub.executor_launch_profile import canonical as profile_canonical
    from tests.test_executor_launch_profile import profile as launch_profile, policy
    raw_profile=profile_canonical(launch_profile(daemon_policy=policy(engine_id=launch.engine_id)))
    authority.release=replace(authority.release,launch_profile_digest='sha256:'+hashlib.sha256(raw_profile).hexdigest())
    release, launch=current_pins(authority)
    evidence=v2_document(); evidence.update(schema=SCHEMA_V3,operator_actor='human:123',profile_json=raw_profile.decode())
    evidence['release']={name:getattr(release,name) for name in release.__dataclass_fields__ if name not in {'public_key','platform'}}
    evidence['release'].update(public_key_hex=release.public_key.hex(),platform=release.platform.model_dump(mode='json'))
    evidence['launch']={name:getattr(launch,name) for name in launch.__dataclass_fields__}
    evidence['report']['executor']=dict(evidence['launch'],image_config_id=release.image_config_id)
    evidence['report']['expires_at_ms']=launch.approval_expires_at_ms
    token='synthetic-local-executor-token'
    tokens={'codex':'synthetic-codex','hermes':'synthetic-hermes','claude':'synthetic-claude','executor':token,'github':'synthetic-github'}
    for role,value in tokens.items(): data['secret_bindings'][role]['digest']='sha256:'+hashlib.sha256(value.encode()).hexdigest()
    root=issuer_path.parent
    executor_dir=root/'executor'; executor_dir.mkdir(mode=0o700)
    (root/'browser').mkdir(mode=0o700)
    (root/'controller').mkdir(mode=0o700)
    executor_path=executor_dir/'executor.sock'
    data.update(executor_socket_path=str(executor_path),database_path=str(root/'controller'/'hub.sqlite3'),
                coordinator_socket_path=str(root/'browser'/'hub.sock'),secret_root=str(root/'secrets'))
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0)); data['agent_port']=probe.getsockname()[1]
    config=installed.parse_configuration(canonical(data))
    enrollment=parse_enrollment(encode(evidence))
    class Secrets:
        def __init__(self, root, *, bindings): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def resolve_test_secret(self, slot, version): return tokens[slot.removeprefix('test-')]
    monkeypatch.setattr(installed,'ImmutableTestSecretResolver',Secrets)
    # The test owns this process; it is not an installed UID10001/PID1 service.
    monkeypatch.setattr(installed,'_process',lambda:None)
    async def exchange(self, code, verifier):
        assert code=='synthetic-code' and verifier
        return 123
    monkeypatch.setattr(GitHubIdentityProvider,'exchange',exchange)
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    owned_resources.callback(listener.close)
    listener.bind(str(executor_path));os.chmod(executor_path,0o600);listener.listen(4);listener.settimeout(.2)
    relay=UnixCurrentObservationTransport(socket_path=str(issuer_path),expected_issuer_uid=os.geteuid(),expected_issuer_gid=os.getegid())
    adapter=NoCommands(); executor=executor_app(adapter,bearer_token=token,current_observation_transport=relay)
    stop_thread=threading.Event(); errors=[]; observations=[]
    def worker():
        try:
            while not stop_thread.is_set():
                try: peer,_=listener.accept()
                except socket.timeout: continue
                with peer:
                    peer.settimeout(3); raw=b''
                    while b'\r\n\r\n' not in raw:
                        chunk=peer.recv(4096)
                        if not chunk: raise ValueError('short request')
                        raw+=chunk
                    header,body=raw.split(b'\r\n\r\n',1)
                    length=int(next(line.split(b':',1)[1] for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:')))
                    while len(body)<length:
                        chunk=peer.recv(length-len(body))
                        if not chunk: raise ValueError('short body')
                        body+=chunk
                    with asyncio.Runner() as runner: start,end=runner.run(request(executor,header,body))
                    observations.append(start['status'])
                    peer.sendall(b'HTTP/1.1 '+str(start['status']).encode()+b' OK\r\n'+b'\r\n'.join(k+b': '+v for k,v in start['headers'])+b'\r\n\r\n'+end['body'])
        except BaseException as error: errors.append(error)
    thread=threading.Thread(target=worker); stop=asyncio.Event(); ready=asyncio.get_running_loop().create_future()
    original=installed.serve_owned_queued_pilot
    async def serve(*args,**kwargs):
        return await original(*args,**kwargs,stop_event=stop,on_ready=lambda *a:ready.set_result(a),install_signals=False)
    monkeypatch.setattr(installed,'serve_owned_queued_pilot',serve)
    session=local.TrustedLocalQueuedSession(config=config,enrollment=enrollment,credential=token.encode())
    owned_resources.callback(session.close)
    owned_resources.enter_context(owned_supervisor_wait(authority))
    serving=None
    try:
        thread.start()
        serving=asyncio.create_task(installed.run_installed_coordinator(config,reconciliation_factory=session.reconciliation_factory,
                                               session_acceptance_factory=session.session_acceptance_factory))
        done,_=await asyncio.wait({ready,serving},timeout=10,return_when=asyncio.FIRST_COMPLETED)
        if serving in done: await serving
        assert ready in done
        transport=httpx.AsyncHTTPTransport(uds=str(config.coordinator_socket))
        async with httpx.AsyncClient(transport=transport,base_url='http://pilot.example.test',follow_redirects=False,timeout=8) as browser:
            route='/api/hub/v1/staging/session-reconciliation'
            assert (await browser.get(route)).status_code==401
            login=await browser.get('/api/hub/v1/auth/login');assert login.status_code==302
            state=parse_qs(urlsplit(login.headers['location']).query)['state'][0]
            oauth_cookie=browser.cookies.get('hub_session_oauth'); assert oauth_cookie
            callback=await browser.get('/api/hub/v1/auth/callback',params={'state':state,'code':'synthetic-code'},
                                       headers={'Cookie':'hub_session_oauth='+oauth_cookie})
            assert callback.status_code==303
            cookie=browser.cookies.get('hub_session');assert cookie
            browser.headers['Cookie']='hub_session='+cookie
            me=(await browser.get('/api/hub/v1/auth/me')).json();assert me['authenticated'] and me['actor']=='human:123'
            snapshot=await browser.get(route);assert snapshot.status_code==200
            decision={'request_id':snapshot.json()['request_id'],'expected_revision':snapshot.json()['revision'],'action':'accept'}
            assert (await browser.post(route,json=decision)).status_code==403
            accepted=await browser.post(route,json=decision,headers={'Origin':'https://pilot.example.test','X-Hub-CSRF':me['csrf_token']})
            assert accepted.status_code==200
        assert observations and all(status==200 for status in observations) and checks and not errors and not adapter.touched
    finally:
        stop.set()
        try:
            if serving is not None:
                import sys
                active_error=sys.exc_info()[1]
                outcome=await asyncio.wait_for(asyncio.gather(serving,return_exceptions=True),10)
                if active_error is None and isinstance(outcome[0],BaseException): raise outcome[0]
        finally:
            stop_thread.set()
            if thread.ident is not None: thread.join(4); assert not thread.is_alive()
